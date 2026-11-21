"""Picking list and "Load from project" for Goods Out.

Two jobs that start from what a project (or a part of it) still needs:

* **Picking list** - what to take off the shelves for the project, with the bins to pick from in
  walking order (sorted by the bin's full location code: warehouse, zone, rack, shelf, bin).
* **Load from project** - the same pick lines put straight onto a Goods Out form.

v1.35: stock is no longer reserved by hand. A project's own stock is what was delivered for it
(Goods In with the project) and not yet issued or returned to main stock.

Areas
-----
The work can be limited to a block, a level or a unit of the project ("BLOCK:<id>",
"LEVEL:<id>", "UNIT:<id>"; empty = the whole project). Goods Out is recorded per project, not
per unit, so for an area the quantity is the area's own requirement (unit quantity x quantity
per unit), capped by what the whole project still needs. Nothing is ever picked beyond the
project's figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select

from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    ProjectBlock,
    ProjectLevel,
    ProjectUnit,
    Site,
    StockLevel,
)
from wms.services.base import BaseService
from wms.services.project_demand import DemandScope, demand

#: what a picking list / Goods Out is loaded from
SOURCES = (("RESERVED", "Project stock (delivered for the project)"), ("REMAINING", "Remaining (Required - Net Issued)"))


def _whole(qty: Decimal, allow_decimal: bool) -> Decimal:
    return qty if allow_decimal else qty.to_integral_value(rounding=ROUND_FLOOR)


@dataclass
class PickRow:
    """One stop on the picking list: take ``quantity`` of a material from one bin."""

    bin_id: int
    bin_code: str
    bin_full: str
    product_id: int
    code: str
    name: str
    uom: str
    quantity: Decimal


@dataclass
class PickShort:
    product_id: int
    code: str
    name: str
    uom: str
    wanted: Decimal
    missing: Decimal


@dataclass
class PickList:
    site_label: str
    area_label: str
    source: str
    rows: list[PickRow] = field(default_factory=list)  # in walking order
    short: list[PickShort] = field(default_factory=list)

    @property
    def title(self) -> str:
        return f"Picking list - {self.site_label}" + (f" - {self.area_label}" if self.area_label else "")


class AllocationService(BaseService):
    # -- areas ---------------------------------------------------------------------------------
    def areas(self, site_id: int) -> list[tuple[str, str]]:
        """(label, key) for every block, level and unit of the project, in tree order."""
        self.require(P.SITES_VIEW)
        out: list[tuple[str, str]] = []
        with self.db.read_session() as s:
            blocks = list(s.scalars(select(ProjectBlock).where(ProjectBlock.site_id == site_id)
                                    .order_by(ProjectBlock.sort_order, ProjectBlock.id)))
            levels = list(s.scalars(select(ProjectLevel).where(ProjectLevel.block_id.in_([b.id for b in blocks]))
                                    .order_by(ProjectLevel.sort_order, ProjectLevel.id))) if blocks else []
            units = list(s.scalars(select(ProjectUnit).where(ProjectUnit.site_id == site_id)
                                   .order_by(ProjectUnit.sort_order, ProjectUnit.id)))
        name = lambda o: f"{o.name} {o.code}" if o.code and o.code not in o.name else o.name  # noqa: E731
        unit_name = lambda u: f"{u.unit_code} {u.name}" if u.unit_code and u.unit_code not in u.name else u.name  # noqa: E731
        for u in units:
            if u.level_id is None:
                out.append((f"Unit: {unit_name(u)}", f"UNIT:{u.id}"))
        for b in blocks:
            out.append((f"Block: {name(b)}", f"BLOCK:{b.id}"))
            for lv in (lv for lv in levels if lv.block_id == b.id):
                out.append((f"Level: {name(b)} / {name(lv)}", f"LEVEL:{lv.id}"))
                for u in (u for u in units if u.level_id == lv.id):
                    out.append((f"Unit: {name(b)} / {name(lv)} / {unit_name(u)}", f"UNIT:{u.id}"))
        return out

    def _area(self, s, site_id: int, area: str | None) -> tuple[str, dict[int, Decimal] | None]:
        """(label, {product_id: required in the area}) - (\"\", None) for the whole project."""
        if not area:
            return "", None
        kind, _, raw = area.partition(":")
        try:
            oid = int(raw)
        except ValueError:
            raise ValidationError("Unknown part of the project.") from None
        if kind == "BLOCK":
            obj, scope = s.get(ProjectBlock, oid), DemandScope(block_ids=[oid])
            ok = obj is not None and obj.site_id == site_id
            label = f"Block {obj.name}" if obj else ""
        elif kind == "LEVEL":
            obj = s.get(ProjectLevel, oid)
            block = s.get(ProjectBlock, obj.block_id) if obj else None
            scope, ok = DemandScope(level_ids=[oid]), block is not None and block.site_id == site_id
            label = f"{block.name} / {obj.name}" if ok else ""
        elif kind == "UNIT":
            obj = s.get(ProjectUnit, oid)
            scope, ok = DemandScope(unit_ids=[oid]), obj is not None and obj.site_id == site_id
            label = f"Unit {obj.unit_code or ''} {obj.name}".replace("  ", " ") if ok else ""
        else:
            ok, label, scope = False, "", None
        if not ok:
            raise ValidationError("That block / level / unit is not part of this project.")
        return label, {d.product_id: d.required for d in demand(s, scope)}

    @staticmethod
    def _site(s, site_id: int) -> Site:
        site = s.get(Site, site_id) if site_id else None
        if site is None:
            raise NotFoundError("Project not found.")
        return site

    def _figures(self, site_id: int, product_ids: list[int]):
        """Material rows with the project figures (Remaining, Reserved, Available...)."""
        from wms.services.materials import MaterialsService
        return {r.id: r for r in MaterialsService(self.ctx).rows_for(product_ids, site_id)}

    # -- Picking list / Load from project --------------------------------------------------------
    def pick_list(self, site_id: int, area: str | None = None, source: str = "RESERVED") -> PickList:
        """What to pick for the project and from which bins, in walking order.

        ``RESERVED``: what is still reserved for the project. ``REMAINING``: Required - Net Issued,
        from stock not reserved for other projects. Quantities a bin cannot cover are listed as short."""
        self.require(P.SITES_VIEW)
        if source not in dict(SOURCES):
            raise ValidationError("Choose what to pick: reserved or remaining.")
        with self.db.read_session() as s:
            site = self._site(s, site_id)
            area_label, area_need = self._area(s, site_id, area)
            site_label = site.display
            from wms.services.materials import project_product_ids
            ids = list(s.scalars(project_product_ids([site_id])))
        if area_need is not None:
            ids = [i for i in ids if area_need.get(i, ZERO) > ZERO]
        figures = self._figures(site_id, ids)
        want: dict[int, tuple[Decimal, Decimal]] = {}  # pid -> (wanted, can be picked)
        for r in figures.values():
            if source == "RESERVED":
                wanted = r.proj_reserved
                if r.remaining is not None:  # material already issued or used in production is not picked twice
                    wanted = min(wanted, max(r.remaining, ZERO))
                if area_need is not None:
                    wanted = min(wanted, area_need.get(r.id, ZERO))
                pickable = wanted
            else:
                wanted = r.remaining or ZERO
                if area_need is not None:
                    wanted = min(wanted, area_need.get(r.id, ZERO))
                # stock reserved for this project is ours to pick; stock reserved for others is not
                free = max(r.usable - (r.reserved - r.proj_reserved), ZERO)
                pickable = min(wanted, free)
            if wanted > ZERO:
                want[r.id] = (wanted, pickable)
        out = PickList(site_label, area_label, source)
        with self.db.read_session() as s:
            for pid, (wanted, pickable) in want.items():
                p = figures[pid].product
                left = pickable
                levels = list(s.execute(
                    select(StockLevel.quantity, Bin).join(Bin, Bin.id == StockLevel.bin_id)
                    .where(StockLevel.product_id == pid, StockLevel.quantity > ZERO,
                           Bin.is_available.is_(True), Bin.is_active.is_(True))).unique())
                # default bin first, then the fullest bins - the fewest stops (as Goods Out picks)
                levels.sort(key=lambda lv, d=p.default_bin_id: (lv[1].id != d, -lv[0], lv[1].full_code))
                for have, b in levels:
                    if left <= ZERO:
                        break
                    take = min(have, left)
                    out.rows.append(PickRow(b.id, b.code, b.full_code, pid, p.internal_code, p.name,
                                            p.unit.code, take))
                    left -= take
                missing = wanted - pickable + max(left, ZERO)
                if missing > ZERO:
                    out.short.append(PickShort(pid, p.internal_code, p.name, p.unit.code, wanted, missing))
        out.rows.sort(key=lambda r: (r.bin_full, r.code))
        out.short.sort(key=lambda x: x.code)
        return out

    def pick_report(self, pick: PickList):
        """The picking list as a report (for PDF / Excel export)."""
        from wms.services.reports import QTY, TEXT, Column, ReportResult
        cols = [Column("Picked", TEXT), Column("Bin", TEXT), Column("Code", TEXT), Column("Material", TEXT),
                Column("Quantity", QTY), Column("Unit", TEXT), Column("Picked qty", TEXT)]
        rows = [["[   ]", r.bin_full, r.code, r.name, r.quantity, r.uom, ""] for r in pick.rows]
        for x in pick.short:
            rows.append(["SHORT", "-", x.code, x.name, x.missing, x.uom, "not in stock"])
        what = dict(SOURCES)[pick.source]
        return ReportResult(pick.title, cols, rows,
                            f"{what}. {len(pick.rows)} pick(s) in walking order"
                            + (f", {len(pick.short)} material(s) short" if pick.short else ""))


__all__ = ["SOURCES", "AllocationService", "PickList", "PickRow", "PickShort"]
