"""Project material demand: what the Manager -> Project -> Unit -> Materials structure needs.

For every material::

    Required = sum over units of (unit quantity x quantity per unit)

Nothing here is stored: the figures are worked out from the structure every time, so they can
never disagree with it. The same functions work for one unit, one project, one manager, any set
of projects, or every open project, and know nothing about screens - the Projects tree, the
Dashboard, Remaining or a report can all reuse them.

Exact arithmetic: quantities are stored as whole thousandths, so the products are summed in the
database as whole numbers (millionths) and divided once at the end - no rounding drift however
many units are added together.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Integer, func, select, type_coerce
from sqlalchemy.orm import Session

from wms.core.enums import OPEN_SITE_STATUSES
from wms.core.exceptions import NotFoundError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import Product, ProjectMaterial, ProjectUnit, Site, Unit, UnitMaterial
from wms.services.base import BaseService

_PLACES = Decimal("0.001")
_RAW_SCALE = Decimal(1_000_000)  # thousandths x thousandths


def line_total(unit_quantity: Decimal | None, quantity_per_unit: Decimal | None) -> Decimal:
    """Total required for one material line: unit quantity x quantity per unit (3 decimals)."""
    return ((unit_quantity or ZERO) * (quantity_per_unit or ZERO)).quantize(_PLACES, rounding=ROUND_HALF_UP)


def _from_raw(value) -> Decimal:
    if value is None:
        return ZERO
    return (Decimal(int(value)) / _RAW_SCALE).quantize(_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class DemandScope:
    """Which part of the structure to add up. Empty fields do not filter.

    ``manager_id`` - projects of one manager; ``unassigned`` - projects without a manager;
    ``statuses`` - project statuses (default: every status)."""

    unit_ids: Iterable[int] | None = None
    site_ids: Iterable[int] | None = None
    manager_id: int | None = None
    unassigned: bool = False
    statuses: Iterable[str] | None = None
    product_ids: Iterable[int] | None = None


@dataclass
class DemandLine:
    product_id: int
    code: str
    name: str
    product_no: str | None
    brand: str | None
    uom: str
    required: Decimal
    unit_lines: int  # how many unit material lines add up to it
    projects: int  # in how many projects it is needed


def _conditions(scope: DemandScope) -> list:
    conds = []
    if scope.unit_ids is not None:
        conds.append(ProjectUnit.id.in_(list(scope.unit_ids)))
    if scope.site_ids is not None:
        conds.append(ProjectUnit.site_id.in_(list(scope.site_ids)))
    if scope.manager_id is not None:
        conds.append(Site.manager_id == scope.manager_id)
    if scope.unassigned:
        conds.append(Site.manager_id.is_(None))
    if scope.statuses is not None:
        conds.append(Site.status.in_(list(scope.statuses)))
    if scope.product_ids is not None:
        conds.append(UnitMaterial.product_id.in_(list(scope.product_ids)))
    return conds


def _raw_product():
    return type_coerce(ProjectUnit.quantity, Integer) * type_coerce(UnitMaterial.quantity_per_unit, Integer)


def demand(s: Session, scope: DemandScope) -> list[DemandLine]:
    """Required quantity per material for the scope, sorted by material code."""
    q = (select(UnitMaterial.product_id, func.sum(_raw_product()), func.count(UnitMaterial.id),
                func.count(func.distinct(ProjectUnit.site_id)))
         .join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
         .join(Site, Site.id == ProjectUnit.site_id)
         .where(*_conditions(scope))
         .group_by(UnitMaterial.product_id))
    totals = {pid: (raw, n, p) for pid, raw, n, p in s.execute(q)}
    if not totals:
        return []
    out = []
    ids = list(totals)
    for i in range(0, len(ids), 900):  # SQLite parameter limit
        chunk = ids[i:i + 900]
        for pid, code, name, pno, brand, uom in s.execute(
                select(Product.id, Product.internal_code, Product.name, Product.supplier_code, Product.brand,
                       Unit.code).join(Unit, Unit.id == Product.unit_id).where(Product.id.in_(chunk))):
            raw, n, p = totals[pid]
            out.append(DemandLine(pid, code, name, pno, brand, uom, _from_raw(raw), n, p))
    return sorted(out, key=lambda d: d.code)


def demand_by_project(s: Session, scope: DemandScope) -> dict[tuple[int, int], Decimal]:
    """{(site_id, product_id): required} - for per-project figures (Remaining, shortages...)."""
    q = (select(ProjectUnit.site_id, UnitMaterial.product_id, func.sum(_raw_product()))
         .join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
         .join(Site, Site.id == ProjectUnit.site_id)
         .where(*_conditions(scope))
         .group_by(ProjectUnit.site_id, UnitMaterial.product_id))
    return {(sid, pid): _from_raw(raw) for sid, pid, raw in s.execute(q)}


@dataclass
class ListComparison:
    """One material: what the units need against the project's material list (Required)."""

    product_id: int
    code: str
    name: str
    brand: str | None
    uom: str
    from_units: Decimal
    on_list: Decimal | None  # None: not on the project's material list

    @property
    def difference(self) -> Decimal:
        return self.from_units - (self.on_list or ZERO)

    @property
    def in_step(self) -> bool:
        return self.on_list is not None and self.difference == ZERO


class ProjectDemandService(BaseService):
    """Screen-facing wrapper: permission check and one read transaction per call."""

    def calculate(self, scope: DemandScope) -> list[DemandLine]:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            return demand(s, scope)

    def for_unit(self, unit_id: int) -> list[DemandLine]:
        return self.calculate(DemandScope(unit_ids=[unit_id]))

    def for_project(self, site_id: int) -> list[DemandLine]:
        return self.calculate(DemandScope(site_ids=[site_id]))

    def for_manager(self, manager_id: int, open_only: bool = True) -> list[DemandLine]:
        return self.calculate(DemandScope(manager_id=manager_id,
                                          statuses=OPEN_SITE_STATUSES if open_only else None))

    def for_active_projects(self) -> list[DemandLine]:
        """Every open project (planned, active, on hold)."""
        return self.calculate(DemandScope(statuses=OPEN_SITE_STATUSES))

    def compare_with_project_list(self, site_id: int) -> list[ListComparison]:
        """Units' demand next to the project's material list (the Required figure used by
        Remaining). Materials on either side are shown."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            if s.get(Site, site_id) is None:
                raise NotFoundError("Project not found.")
            rows = {d.product_id: ListComparison(d.product_id, d.code, d.name, d.brand, d.uom, d.required, None)
                    for d in demand(s, DemandScope(site_ids=[site_id]))}
            for pid, req, code, name, brand, uom in s.execute(
                    select(ProjectMaterial.product_id, ProjectMaterial.required_qty, Product.internal_code,
                           Product.name, Product.brand, Unit.code)
                    .join(Product, Product.id == ProjectMaterial.product_id)
                    .join(Unit, Unit.id == Product.unit_id)
                    .where(ProjectMaterial.site_id == site_id)):
                row = rows.get(pid)
                if row is None:
                    rows[pid] = ListComparison(pid, code, name, brand, uom, ZERO, req)
                else:
                    row.on_list = req
        return sorted(rows.values(), key=lambda r: r.code)

    def apply_to_project_list(self, site_id: int) -> int:
        """Set the project's material list (Required) to what its units need. Materials only on
        the list are left as they are. Never changes stock. Returns the number of lines changed."""
        from wms.services.materials import upsert_project_materials
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            lines = demand(s, DemandScope(site_ids=[site_id]))
            if not lines:
                return 0
            return upsert_project_materials(s, self.ctx, site_id, [(d.product_id, d.required) for d in lines],
                                            replace=True, source="from project units")


__all__ = ["DemandLine", "DemandScope", "ListComparison", "ProjectDemandService", "demand", "demand_by_project",
           "line_total"]
