"""Reserve remaining, picking list and "Load from project" for Goods Out.

Three jobs that start from what a project (or a part of it) still needs:

* **Reserve remaining** - one click sets aside everything a project still has To Allocate
  (Remaining - Reserved), as far as stock is Available. Each material becomes an ordinary
  reservation, all created in one transaction: either every line is reserved or none is.
* **Picking list** - what to take off the shelves for the project, with the bins to pick from in
  walking order (sorted by the bin's full location code: warehouse, zone, rack, shelf, bin).
* **Load from project** - the same pick lines put straight onto a Goods Out form.

Areas
-----
The work can be limited to a block, a level or a unit of the project ("BLOCK:<id>",
"LEVEL:<id>", "UNIT:<id>"; empty = the whole project). Reservations and Goods Out are recorded
per project, not per unit, so for an area the quantity is the area's own requirement (unit
quantity x quantity per unit), capped by what the whole project still needs. Nothing is ever
reserved or picked beyond the project's figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select

from wms.core.enums import OPEN_SITE_STATUSES, EntityType
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    Product,
    ProjectBlock,
    ProjectLevel,
    ProjectMaterial,
    ProjectUnit,
    Site,
    StockLevel,
    StockReservation,
)
from wms.services import audit
from wms.services.base import BaseService, check_client_token
from wms.services.project_demand import DemandScope, demand

#: what a picking list / Goods Out is loaded from
SOURCES = (("RESERVED", "Reserved for the project"), ("REMAINING", "Remaining (Required - Net Issued)"))


def _whole(qty: Decimal, allow_decimal: bool) -> Decimal:
    return qty if allow_decimal else qty.to_integral_value(rounding=ROUND_FLOOR)


@dataclass
class ReserveLine:
    """One material of a "Reserve remaining" preview."""

    product_id: int
    code: str
    name: str
    uom: str
    need: Decimal  # still to be reserved (To Allocate, capped by the area's requirement)
    available: Decimal  # free stock: usable on hand - reserved for any project
    quantity: Decimal  # what will be reserved: the smaller of the two

    @property
    def short(self) -> Decimal:
        return max(self.need - self.quantity, ZERO)


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

    # -- Reserve remaining ---------------------------------------------------------------------
    def reserve_plan(self, site_id: int, area: str | None = None) -> list[ReserveLine]:
        """What "Reserve remaining" would set aside, material by material (nothing is saved)."""
        self.require(P.RESERVATIONS_EDIT)
        with self.db.read_session() as s:
            self._site(s, site_id)
            _, area_need = self._area(s, site_id, area)
            ids = list(s.scalars(select(ProjectMaterial.product_id).where(ProjectMaterial.site_id == site_id)))
        if area_need is not None:
            ids = [i for i in ids if area_need.get(i, ZERO) > ZERO]
        out = []
        for r in self._figures(site_id, ids).values():
            need = r.to_allocate or ZERO
            if area_need is not None:
                need = min(need, area_need.get(r.id, ZERO))
            if need <= ZERO or not r.product.is_active:
                continue
            free = max(r.available, ZERO)
            qty = _whole(min(need, free), r.product.unit.allow_decimal)
            out.append(ReserveLine(r.id, r.product.internal_code, r.product.name, r.product.unit.code,
                                   need, free, qty))
        return sorted(out, key=lambda x: x.code)

    def reserve_remaining(self, site_id: int, lines: list[tuple[int, Decimal]], required_by: date | None = None,
                          notes: str | None = None, client_token: str | None = None) -> list[str]:
        """Create one reservation per line, all in one transaction. Returns the reservation numbers."""
        self.require(P.RESERVATIONS_EDIT)
        lines = [(pid, Decimal(q)) for pid, q in lines if q and Decimal(q) > ZERO]
        if not lines:
            raise ValidationError("Nothing to reserve: every material is already reserved or out of stock.")
        token = (client_token or "")[:32]
        from wms.services.reservations import ReservationService
        rs = ReservationService(self.ctx)
        numbers = []
        with self.db.session() as s:
            if token:
                check_client_token(s, StockReservation, f"{token}:1", "number", EntityType.RESERVATION.value,
                                   "reservation")
            site = self._site(s, site_id)
            if site.status not in OPEN_SITE_STATUSES:
                raise ValidationError(f"Project {site.display} is closed - reopen it to reserve stock for it.")
            for n, (pid, qty) in enumerate(lines, 1):
                product = s.get(Product, pid)
                if product is None or not product.is_active:
                    raise ValidationError("A material on the list has been archived - refresh and try again.")
                qty = rs._qty(product, qty)
                r = rs._create(s, product, site, qty, required_by, notes or "Reserve remaining",
                               f"{token}:{n}" if token else None)
                numbers.append(r.number)
            audit.record(s, self.ctx, "RESERVE_REMAINING", EntityType.SITE.value, site.id, site.project_number,
                         f"{len(numbers)} reservation(s) created for {site.display}: {', '.join(numbers)}")
        return numbers

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


__all__ = ["SOURCES", "AllocationService", "PickList", "PickRow", "PickShort", "ReserveLine"]
