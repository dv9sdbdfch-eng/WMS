"""Material norms per unit and completed units (v1.23).

A *norm* is how much of a material one unit needs ("Kitchen K01 needs 3 sheets of H3311").
The current norm of a material line is ``unit_materials.quantity_per_unit``; every value it ever
had is kept in ``unit_material_norms`` with the moment it was set and what it applies to:

* ``ALL``    - every unit of the unit type, also the ones already made: they are recalculated
  with the new norm (this is how a correction of a wrong norm works);
* ``FUTURE`` - only units not made yet; units already made keep the norm they were made with.

Units made / installed are recorded in ``unit_completions`` ("6 kitchens done on 12/11/2026").
Which norm a completed batch used is decided by one rule::

    the latest norm row that was set before the batch was recorded,
    or that applies to ALL units (whenever it was set)

So for one material line of one unit type::

    used by units made  = sum over batches (batch quantity x the norm of that batch)
    needed by the rest  = (units - units made) x current norm
    total required      = used by units made + needed by the rest

With no completed units, or only ALL changes, this is exactly ``units x current norm`` - the
figure the Projects tree and Required already use. Planning data only: nothing here changes stock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wms.core.enums import EntityType
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Product, ProjectUnit, Site, Unit, UnitCompletion, UnitMaterial, UnitMaterialNorm
from wms.db.models.hierarchy import NORM_ALL, NORM_FUTURE, STAGE_DONE
from wms.services import audit
from wms.services.base import BaseService, clean_str

_PLACES = Decimal("0.001")
APPLIES_TO = (NORM_ALL, NORM_FUTURE)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_PLACES, rounding=ROUND_HALF_UP)


def _naive(t: datetime) -> datetime:
    """Compare stored (naive UTC) and fresh (aware UTC) times on the same footing."""
    return t.replace(tzinfo=None) if t.tzinfo else t


# ---- the rule, as plain functions (no database) - also used by reports later --------------------
@dataclass(frozen=True)
class NormPoint:
    """One value of a line's norm: ``qty`` set at ``at`` for ALL or FUTURE units."""

    id: int
    qty: Decimal
    at: datetime
    applies_to: str


def norm_for_batch(points: list[NormPoint], recorded_at: datetime) -> Decimal | None:
    """The norm a batch of units recorded at ``recorded_at`` was made with (see the module rule)."""
    t = _naive(recorded_at)
    best: NormPoint | None = None
    for p in points:
        if _naive(p.at) <= t or p.applies_to == NORM_ALL:
            if best is None or (_naive(p.at), p.id) > (_naive(best.at), best.id):
                best = p
    return best.qty if best else None


@dataclass
class LineNeed:
    """What one material line of one unit type needs, split into units made and units to make."""

    line_id: int
    product_id: int
    product_code: str
    product_name: str
    norm: Decimal                 # current norm (per unit)
    units: Decimal                # units of this type in the project
    units_done: Decimal           # units made / installed
    used_by_done: Decimal         # material the units made used (with their own norms)
    needed_by_rest: Decimal       # material the units still to make need (current norm)

    @property
    def units_left(self) -> Decimal:
        return self.units - self.units_done

    @property
    def total(self) -> Decimal:
        return self.used_by_done + self.needed_by_rest


def line_need(line_id: int, product_id: int, code: str, name: str, norm: Decimal, units: Decimal,
              points: list[NormPoint], batches: Iterable[tuple[Decimal, datetime]]) -> LineNeed:
    done = ZERO
    used = ZERO
    for qty, recorded_at in batches:
        n = norm_for_batch(points, recorded_at)
        done += qty
        used += qty * (norm if n is None else n)
    rest = max(units - done, ZERO)
    return LineNeed(line_id, product_id, code, name, norm, units, done, _q(used), _q(rest * norm))


@dataclass
class ScheduleLine:
    """One row of a unit's Finish Schedule (unit card): a material, its norm and its coverage.

    The project stock is the project's whole stock of the material (delivered for it or reserved,
    less issued) - shared by every unit type of the project that uses the material."""

    need: LineNeed
    brand: str | None
    vpk_code: str | None
    notes: str | None
    uom: str
    product_active: bool
    project_stock: Decimal

    @property
    def id(self) -> int:
        return self.need.line_id

    @property
    def still_needed(self) -> Decimal:
        return self.need.needed_by_rest

    @property
    def units_covered(self) -> Decimal:
        """Whole units still to make that the project stock is enough for (never more than are left)."""
        if self.need.norm <= ZERO:
            return self.need.units_left
        return min(self.need.units_left, (self.project_stock / self.need.norm).to_integral_value(rounding=ROUND_DOWN))

    @property
    def shortage(self) -> Decimal:
        return max(self.still_needed - self.project_stock, ZERO)

    @property
    def coverage(self) -> str:
        """GREEN: enough for every unit left; YELLOW: part; RED: nothing (with units still to make)."""
        if self.still_needed <= ZERO or self.shortage == ZERO:
            return "GREEN"
        return "YELLOW" if self.project_stock > ZERO else "RED"


GREEN, YELLOW, RED, NO_NEED = "GREEN", "YELLOW", "RED", "NONE"


@dataclass
class Coverage:
    """How far the project's stock covers what part of a project still needs (v1.30).

    ``lines`` counts materials (per unit for a unit; per material for a level / block / project);
    ``covered_lines`` those fully covered. Quantities of different materials are never added up."""

    lines: int = 0
    covered_lines: int = 0
    any_stock: bool = False
    short: dict[str, Decimal] | None = None  # material code -> shortage

    @property
    def status(self) -> str:
        if self.lines == 0:
            return NO_NEED
        if self.covered_lines == self.lines:
            return GREEN
        return YELLOW if self.any_stock else RED

    @property
    def text(self) -> str:
        if self.status == NO_NEED:
            return "Nothing left to make"
        head = {GREEN: "Covered", YELLOW: "Part covered", RED: "Not covered"}[self.status]
        out = f"{head}: {self.covered_lines} of {self.lines} material(s) fully covered by the project's stock"
        if self.short:
            worst = sorted(self.short.items())[:6]
            out += "\nShort: " + ", ".join(f"{c} {_fmt(q)}" for c, q in worst) + (" ..." if len(self.short) > 6 else "")
        return out


@dataclass
class ProjectCoverage:
    units: dict[int, Coverage]
    levels: dict[int, Coverage]
    blocks: dict[int, Coverage]
    project: Coverage


def _summarise(needs: dict[tuple[int, int], tuple[str, Decimal, Decimal]], keys: list[tuple[int, int]],
               per_product: bool) -> Coverage:
    """``needs`` = {(unit_id, product_id): (code, still needed, allocated)}."""
    groups: dict = {}
    for key in keys:
        code, need, got = needs[key]
        g = key[1] if per_product else key
        c, n, a = groups.get(g, (code, ZERO, ZERO))
        groups[g] = (c, n + need, a + got)
    cov = Coverage(short={})
    for code, need, got in groups.values():
        if need <= ZERO:
            continue
        cov.lines += 1
        cov.any_stock = cov.any_stock or got > ZERO
        if got >= need:
            cov.covered_lines += 1
        else:
            cov.short[code] = cov.short.get(code, ZERO) + need - got
    return cov


def project_coverage(s: Session, site_id: int) -> ProjectCoverage:
    """Share the project's stock out over its units in the order of the tree (Block, Level, Unit
    as sorted; units directly in the project last) and say per unit, level, block and the project
    whether what they still need is covered: GREEN all, YELLOW part, RED none."""
    from wms.db.models import ProjectBlock, ProjectLevel
    from wms.services.reservations import project_stock
    order = list(s.execute(
        select(ProjectUnit.id, ProjectLevel.id, ProjectBlock.id)
        .outerjoin(ProjectLevel, ProjectLevel.id == ProjectUnit.level_id)
        .outerjoin(ProjectBlock, ProjectBlock.id == ProjectLevel.block_id)
        .where(ProjectUnit.site_id == site_id)
        .order_by(ProjectBlock.id.is_(None), ProjectBlock.sort_order, ProjectBlock.id, ProjectLevel.sort_order,
                  ProjectLevel.id, ProjectUnit.sort_order, ProjectUnit.id)))
    lines = UnitNormService._needs(s, [u for u, _l, _b in order])
    stock = {pid: line.total for pid, line in project_stock(s, site_id).items()}
    needs: dict[tuple[int, int], tuple[str, Decimal, Decimal]] = {}
    for uid, _lid, _bid in order:
        for n in lines[uid]:
            key = (uid, n.product_id)
            code, need, got = needs.get(key, (n.product_code, ZERO, ZERO))
            free = stock.get(n.product_id, ZERO)
            take = min(free, n.needed_by_rest)
            stock[n.product_id] = free - take
            needs[key] = (code, need + n.needed_by_rest, got + take)
    by_unit: dict[int, list] = {}
    by_level: dict[int, list] = {}
    by_block: dict[int, list] = {}
    for key in needs:
        by_unit.setdefault(key[0], []).append(key)
    for uid, lid, bid in order:
        keys = by_unit.get(uid, [])
        if lid:
            by_level.setdefault(lid, []).extend(keys)
            by_block.setdefault(bid, []).extend(keys)
    return ProjectCoverage(
        units={uid: _summarise(needs, by_unit.get(uid, []), False) for uid, _l, _b in order},
        levels={lid: _summarise(needs, keys, True) for lid, keys in by_level.items()},
        blocks={bid: _summarise(needs, keys, True) for bid, keys in by_block.items()},
        project=_summarise(needs, list(needs), True))


# ---- helpers used inside other services' transactions ------------------------------------------
def record_norm(s: Session, ctx, line: UnitMaterial, previous: Decimal | None, applies_to: str = NORM_ALL,
                reason: str | None = None) -> UnitMaterialNorm:
    """Write one row of norm history for ``line`` (its current ``quantity_per_unit``)."""
    if applies_to not in APPLIES_TO:
        raise ValidationError("Choose whether the new norm applies to all units or only to future ones.",
                              "applies_to")
    user = ctx.user
    row = UnitMaterialNorm(unit_material_id=line.id, quantity_per_unit=line.quantity_per_unit,
                           previous_qty=previous, applies_to=applies_to, changed_at=utcnow(),
                           reason=clean_str(reason), changed_by_id=user.id if user else None,
                           changed_by_name_snapshot=user.full_name if user else None)
    s.add(row)
    return row


def units_done(s: Session, unit_id: int) -> Decimal:
    raw = s.scalar(select(func.sum(UnitCompletion.quantity))
                   .where(UnitCompletion.unit_id == unit_id, UnitCompletion.cancelled_at.is_(None),
                          UnitCompletion.stage == STAGE_DONE))
    return Decimal(raw or 0)


def highest_marked_piece(s: Session, unit_id: int) -> int:
    """The highest piece number of a unit type with a live Production dashboard mark (0: none)."""
    return s.scalar(select(func.max(UnitCompletion.piece_no))
                    .where(UnitCompletion.unit_id == unit_id, UnitCompletion.cancelled_at.is_(None))) or 0


def units_with_completions(s: Session, *conds) -> int:
    """How many units matching ``conds`` (on ProjectUnit) have completed units recorded (any stage:
    Done, Dispatched or On site marks all protect a unit from being deleted)."""
    return s.scalar(select(func.count(func.distinct(UnitCompletion.unit_id)))
                    .join(ProjectUnit, ProjectUnit.id == UnitCompletion.unit_id)
                    .where(UnitCompletion.cancelled_at.is_(None), *conds)) or 0


@dataclass
class CompletionRow:
    id: int
    quantity: Decimal
    completed_on: date
    recorded_at: datetime
    user: str
    notes: str | None
    cancelled: bool
    cancel_reason: str | None


@dataclass
class NormRow:
    id: int
    quantity_per_unit: Decimal
    previous_qty: Decimal | None
    applies_to: str
    changed_at: datetime
    user: str
    reason: str | None

    @property
    def applies_label(self) -> str:
        return "All units (recalculated)" if self.applies_to == NORM_ALL else "Future units only"


class UnitNormService(BaseService):
    """Norm history of unit material lines and completed units of a unit type."""

    # ---- norms ----------------------------------------------------------------------------------
    def norm_history(self, line_id: int) -> list[NormRow]:
        """Every norm of a material line, oldest first."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            if s.get(UnitMaterial, line_id) is None:
                raise NotFoundError("Material line not found.")
            return [NormRow(r.id, r.quantity_per_unit, r.previous_qty, r.applies_to, r.changed_at,
                            r.changed_by_name_snapshot or "", r.reason)
                    for r in s.scalars(select(UnitMaterialNorm).where(UnitMaterialNorm.unit_material_id == line_id)
                                       .order_by(UnitMaterialNorm.changed_at, UnitMaterialNorm.id))]

    def change_norm(self, line_id: int, quantity_per_unit, applies_to: str = NORM_ALL,
                    reason: str | None = None) -> NormRow | None:
        """Set a new norm on a line. ``applies_to`` ALL recalculates units already made, FUTURE keeps
        them as they were. Returns the history row, or None when the norm did not change."""
        from wms.services.project_tree import ProjectTreeService
        ProjectTreeService(self.ctx).update_material(line_id, {"quantity_per_unit": quantity_per_unit},
                                                     applies_to=applies_to, reason=reason)
        rows = self.norm_history(line_id)
        return rows[-1] if rows else None

    # ---- completed units ------------------------------------------------------------------------
    def record_completion(self, unit_id: int, quantity, completed_on: date | None = None,
                          notes: str | None = None) -> CompletionRow:
        """N units of this unit type were made / installed (cannot exceed the units not done yet)."""
        self.require(P.SITES_EDIT)
        qty = to_qty(quantity, "quantity")
        if qty <= ZERO:
            raise ValidationError("Units done must be greater than zero.", "quantity")
        when = completed_on or date.today()
        if when > date.today():
            raise ValidationError("The completion date cannot be in the future.", "completed_on")
        with self.db.session() as s:
            u, site = self._unit(s, unit_id)
            done = units_done(s, unit_id)
            if done + qty > u.quantity:
                raise ValidationError(f"Only {_fmt(u.quantity - done)} of {_fmt(u.quantity)} unit(s) are left to do "
                                      f"on {u.name}.", "quantity")
            user = self.ctx.user
            row = UnitCompletion(unit_id=unit_id, quantity=qty, completed_on=when, recorded_at=utcnow(),
                                 notes=clean_str(notes), user_id=user.id if user else None,
                                 user_name_snapshot=user.full_name if user else None)
            s.add(row)
            s.flush()
            _sync(s, self.ctx, site.id)
            audit.record(s, self.ctx, "UNIT_COMPLETION_RECORDED", EntityType.PROJECT_UNIT.value, unit_id,
                         f"{site.project_number} / {u.name}", f"{_fmt(qty)} unit(s) done on {when:%d/%m/%Y}",
                         new={"quantity": str(qty), "completed_on": when.isoformat()})
            return _completion_row(row)

    def cancel_completion(self, completion_id: int, reason: str) -> None:
        """Cancel a wrongly recorded batch. It stays in the history, marked cancelled, and no longer counts."""
        self.require(P.SITES_EDIT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            row = s.get(UnitCompletion, completion_id)
            if row is None:
                raise NotFoundError("Completed units record not found.")
            if row.cancelled_at is not None:
                raise ValidationError("This record is already cancelled.")
            u, site = self._unit(s, row.unit_id)
            row.cancelled_at = utcnow()
            row.cancelled_by_id = self.ctx.user.id if self.ctx.user else None
            row.cancel_reason = reason
            s.flush()
            _sync(s, self.ctx, site.id)
            audit.record(s, self.ctx, "UNIT_COMPLETION_CANCELLED", EntityType.PROJECT_UNIT.value, u.id,
                         f"{site.project_number} / {u.name}",
                         f"{_fmt(row.quantity)} unit(s) of {row.completed_on:%d/%m/%Y} cancelled: {reason}",
                         old={"quantity": str(row.quantity), "completed_on": row.completed_on.isoformat()})

    def completions(self, unit_id: int, include_cancelled: bool = True) -> list[CompletionRow]:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            q = select(UnitCompletion).where(UnitCompletion.unit_id == unit_id, UnitCompletion.stage == STAGE_DONE)
            if not include_cancelled:
                q = q.where(UnitCompletion.cancelled_at.is_(None))
            return [_completion_row(r) for r in s.scalars(q.order_by(UnitCompletion.recorded_at, UnitCompletion.id))]

    def units_done(self, unit_id: int) -> Decimal:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            return units_done(s, unit_id)

    # ---- what a unit type needs, split into made / to make --------------------------------------
    def unit_needs(self, unit_id: int) -> list[LineNeed]:
        """Per material line of a unit type: norm, units made, material used by them, still needed."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            return self._needs(s, [u.id])[u.id]

    def project_needs(self, site_id: int) -> dict[int, list[LineNeed]]:
        """{unit_id: [LineNeed]} for every unit type of a project (one pass, three queries)."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            ids = list(s.scalars(select(ProjectUnit.id).where(ProjectUnit.site_id == site_id)))
            return self._needs(s, ids)

    @staticmethod
    def _needs(s: Session, unit_ids: list[int]) -> dict[int, list[LineNeed]]:
        out: dict[int, list[LineNeed]] = {uid: [] for uid in unit_ids}
        if not unit_ids:
            return out
        lines, points, batches = [], {}, {}
        for i in range(0, len(unit_ids), 900):  # SQLite parameter limit
            chunk = unit_ids[i:i + 900]
            lines += list(s.execute(
                select(UnitMaterial.id, UnitMaterial.unit_id, UnitMaterial.product_id, Product.internal_code,
                       Product.name, UnitMaterial.quantity_per_unit, ProjectUnit.quantity)
                .join(Product, Product.id == UnitMaterial.product_id)
                .join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
                .where(UnitMaterial.unit_id.in_(chunk))
                .order_by(UnitMaterial.unit_id, UnitMaterial.sort_order, UnitMaterial.id)))
            for uid, qty, at in s.execute(
                    select(UnitCompletion.unit_id, UnitCompletion.quantity, UnitCompletion.recorded_at)
                    .where(UnitCompletion.unit_id.in_(chunk), UnitCompletion.cancelled_at.is_(None),
                           UnitCompletion.stage == STAGE_DONE)):
                batches.setdefault(uid, []).append((qty, at))
            for r in s.execute(
                    select(UnitMaterialNorm.id, UnitMaterialNorm.unit_material_id, UnitMaterialNorm.quantity_per_unit,
                           UnitMaterialNorm.changed_at, UnitMaterialNorm.applies_to)
                    .join(UnitMaterial, UnitMaterial.id == UnitMaterialNorm.unit_material_id)
                    .where(UnitMaterial.unit_id.in_(chunk))):
                points.setdefault(r[1], []).append(NormPoint(r[0], r[2], r[3], r[4]))
        for lid, uid, pid, code, name, norm, units in lines:
            out[uid].append(line_need(lid, pid, code, name, norm, units, points.get(lid, []), batches.get(uid, [])))
        return out

    # ---- Finish Schedule: the unit card ---------------------------------------------------------
    def finish_schedule(self, unit_id: int) -> list[ScheduleLine]:
        """The materials one unit type is made of, with the project's stock of each: per line the
        norm, units, units made, still needed, project stock, units it covers and the shortage."""
        from wms.services.reservations import project_stock
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            needs = self._needs(s, [u.id])[u.id]
            extra = {lid: (vpk, notes, brand, uom, active) for lid, vpk, notes, brand, uom, active in s.execute(
                select(UnitMaterial.id, UnitMaterial.vpk_code, UnitMaterial.notes, Product.brand, Unit.code,
                       Product.is_active)
                .join(Product, Product.id == UnitMaterial.product_id).join(Unit, Unit.id == Product.unit_id)
                .where(UnitMaterial.unit_id == u.id))}
            stock = project_stock(s, u.site_id, sorted({n.product_id for n in needs})) if needs else {}
            out = []
            for n in needs:
                vpk, notes, brand, uom, active = extra[n.line_id]
                held = stock[n.product_id].total if n.product_id in stock else ZERO
                out.append(ScheduleLine(n, brand, vpk, notes, uom, active, held))
            return out

    def project_coverage(self, site_id: int) -> ProjectCoverage:
        """Green / yellow / red coverage of every unit, level and block of a project (v1.30)."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            return project_coverage(s, site_id)

    # ---- internals ------------------------------------------------------------------------------
    @staticmethod
    def _unit(s: Session, unit_id: int) -> tuple[ProjectUnit, Site]:
        from wms.services.project_tree import ProjectTreeService
        u = s.get(ProjectUnit, unit_id)
        if u is None:
            raise NotFoundError("Unit not found.")
        return u, ProjectTreeService._editable_site(s, u.site_id)


def _sync(s: Session, ctx, site_id: int) -> None:
    """Units made change Required when they keep an older norm: let the project's list follow."""
    from wms.services.project_demand import sync_project_lists
    sync_project_lists(s, ctx, [site_id])


def _completion_row(r: UnitCompletion) -> CompletionRow:
    return CompletionRow(r.id, r.quantity, r.completed_on, r.recorded_at, r.user_name_snapshot or "", r.notes,
                         r.cancelled_at is not None, r.cancel_reason)


def _fmt(q: Decimal) -> str:
    return f"{q.normalize():f}"
