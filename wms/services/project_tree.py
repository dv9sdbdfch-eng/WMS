"""Projects module: Manager -> Project -> Unit -> Materials (v1.15).

The tree on screen is only a view of these tables; every change is one transaction here, written
to the audit trail, or nothing changes at all.

* Projects are the existing projects (``sites``) - the same records Goods Out, reservations,
  returns and Remaining already use. Nothing is duplicated; a project simply gains an optional
  manager and a list of units.
* Unit materials link to the existing product master records (never copies).
* Removing a manager never removes a project: its projects become unassigned, or move to another
  manager when the user chooses so.
* Planning data only: no stock level or movement is touched by anything in this module.

Loading is per branch (managers, then the projects of one manager, then the units of one project,
then the materials of one unit) with counts worked out in SQL, so the screen never has to read the
whole structure at once.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Integer, delete, func, or_, select, type_coerce, update
from sqlalchemy.orm import Session

from wms.core.enums import CLOSED_SITE_STATUSES, EntityType, SiteStatus
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Manager, Product, ProjectUnit, Site, Unit, UnitMaterial
from wms.services import audit
from wms.services.base import BaseService, clean_code, clean_str, like_pattern
from wms.services.project_demand import line_total

#: manager filter value for "projects without a manager"
UNASSIGNED = -1
MANAGER_FIELDS = ("name", "email", "phone", "notes", "is_active")
UNIT_FIELDS = ("unit_code", "name", "quantity", "notes")
MATERIAL_FIELDS = ("quantity_per_unit", "vpk_code", "notes")
_CHUNK = 900  # SQLite parameter limit


# ---- what the screen shows ---------------------------------------------------------------------
@dataclass
class ManagerNode:
    id: int
    name: str
    email: str | None
    phone: str | None
    notes: str | None
    is_active: bool
    sort_order: int
    projects: int = 0


@dataclass
class ProjectNode:
    id: int
    code: str
    name: str
    status: str
    manager_id: int | None
    manager_name: str | None
    customer: str | None
    notes: str | None
    version: int
    units: int = 0

    @property
    def status_label(self) -> str:
        return SiteStatus(self.status).label

    @property
    def read_only(self) -> bool:
        return self.status == SiteStatus.ARCHIVED.value


@dataclass
class UnitNode:
    id: int
    site_id: int
    code: str | None
    name: str
    quantity: Decimal
    sort_order: int
    notes: str | None
    materials: int = 0

    @property
    def label(self) -> str:
        return f"{self.name} {self.code}" if self.code and self.code not in self.name else self.name


@dataclass
class MaterialNode:
    id: int
    unit_id: int
    product_id: int
    code: str
    product_no: str | None
    vpk_code: str | None
    name: str
    description: str | None
    brand: str | None
    uom: str
    quantity_per_unit: Decimal
    unit_quantity: Decimal
    notes: str | None
    sort_order: int
    product_active: bool = True

    @property
    def total(self) -> Decimal:
        """Total required = unit quantity x quantity per unit (calculated, never stored)."""
        return line_total(self.unit_quantity, self.quantity_per_unit)


@dataclass
class TreeMatches:
    """Search result: what matched, and the branches that lead to it."""

    text: str
    managers: set[int] = field(default_factory=set)  # the manager itself matched
    projects: set[int] = field(default_factory=set)  # the project itself matched
    units: set[int] = field(default_factory=set)  # the unit itself matched
    materials: set[int] = field(default_factory=set)  # material lines that matched
    # branches to show: projects / units holding a match further down
    project_paths: set[int] = field(default_factory=set)
    unit_paths: set[int] = field(default_factory=set)
    manager_paths: set[int] = field(default_factory=set)
    unassigned_path: bool = False

    @property
    def total(self) -> int:
        return len(self.managers) + len(self.projects) + len(self.units) + len(self.materials)


@dataclass
class TreeFilter:
    """Manager (id, ``UNASSIGNED`` or None for all), one project, project statuses."""

    manager_id: int | None = None
    site_id: int | None = None
    statuses: list[str] | None = None

    def site_conditions(self) -> list:
        conds = []
        if self.manager_id == UNASSIGNED:
            conds.append(Site.manager_id.is_(None))
        elif self.manager_id is not None:
            conds.append(Site.manager_id == self.manager_id)
        if self.site_id is not None:
            conds.append(Site.id == self.site_id)
        if self.statuses:
            conds.append(Site.status.in_(self.statuses))
        return conds


def _chunks(ids: Iterable[int]):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _name_key(name: str | None) -> str:
    return " ".join((name or "").split()).upper()


def find_or_create_manager(s: Session, ctx, name: str | None) -> Manager | None:
    """The manager called ``name`` (ignoring case and extra spaces), created if there is none.
    Used when a manager's name is typed on the project form."""
    clean = " ".join((name or "").split())
    if not clean:
        return None
    if len(clean) > 120:
        raise ValidationError("Manager name must be at most 120 characters.", "project_manager")
    m = s.scalar(select(Manager).where(func.upper(Manager.name) == clean.upper()).order_by(Manager.id).limit(1))
    if m is None:
        order = (s.scalar(select(func.max(Manager.sort_order))) or 0) + 1
        m = Manager(name=clean, sort_order=order, is_active=True)
        s.add(m)
        s.flush()
        audit.record(s, ctx, "MANAGER_CREATED", EntityType.MANAGER.value, m.id, m.name,
                     "Created from a project's 'Project manager' field", new={"name": m.name})
    return m


class ProjectTreeService(BaseService):
    # ---- reading, one branch at a time -----------------------------------------------------------
    def managers(self, flt: TreeFilter | None = None, include_inactive: bool = True) -> list[ManagerNode]:
        """Managers in their order, each with the number of its projects that pass the filter."""
        self.require(P.SITES_VIEW)
        flt = flt or TreeFilter()
        if flt.manager_id == UNASSIGNED:
            return []
        counts = (select(Site.manager_id, func.count(Site.id).label("n"))
                  .where(Site.manager_id.is_not(None), *TreeFilter(None, flt.site_id, flt.statuses).site_conditions())
                  .group_by(Site.manager_id).subquery())
        q = (select(Manager, func.coalesce(counts.c.n, 0)).outerjoin(counts, counts.c.manager_id == Manager.id)
             .order_by(Manager.sort_order, func.upper(Manager.name), Manager.id))
        if not include_inactive:
            q = q.where(Manager.is_active.is_(True))
        if flt.manager_id not in (None, UNASSIGNED):
            q = q.where(Manager.id == flt.manager_id)
        with self.db.read_session() as s:
            return [ManagerNode(m.id, m.name, m.email, m.phone, m.notes, m.is_active, m.sort_order, n)
                    for m, n in s.execute(q)]

    def unassigned_count(self, flt: TreeFilter | None = None) -> int:
        self.require(P.SITES_VIEW)
        flt = flt or TreeFilter()
        with self.db.read_session() as s:
            return s.scalar(select(func.count(Site.id)).where(
                Site.manager_id.is_(None), *TreeFilter(None, flt.site_id, flt.statuses).site_conditions())) or 0

    def manager(self, manager_id: int) -> ManagerNode:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            m = s.get(Manager, manager_id)
            if m is None:
                raise NotFoundError("Manager not found.")
            n = s.scalar(select(func.count(Site.id)).where(Site.manager_id == manager_id)) or 0
            return ManagerNode(m.id, m.name, m.email, m.phone, m.notes, m.is_active, m.sort_order, n)

    def manager_choices(self, include_inactive: bool = False) -> list[tuple[str, int]]:
        """(name, id) for drop-downs."""
        return [(m.name, m.id) for m in self.managers(include_inactive=include_inactive)]

    def projects(self, manager_id: int | None, flt: TreeFilter | None = None,
                 ids: Iterable[int] | None = None) -> list[ProjectNode]:
        """Projects of one manager (``UNASSIGNED``: without a manager; None: every project)."""
        self.require(P.SITES_VIEW)
        flt = flt or TreeFilter()
        units = (select(ProjectUnit.site_id, func.count(ProjectUnit.id).label("n"))
                 .group_by(ProjectUnit.site_id).subquery())
        q = (select(Site, func.coalesce(units.c.n, 0), Manager.name)
             .outerjoin(units, units.c.site_id == Site.id).outerjoin(Manager, Manager.id == Site.manager_id)
             .where(*TreeFilter(manager_id, flt.site_id, flt.statuses).site_conditions())
             .order_by(Site.project_number))
        with self.db.read_session() as s:
            if ids is not None:
                out = []
                for chunk in _chunks(ids):
                    out += [self._project_node(site, n, mname) for site, n, mname in
                            s.execute(q.where(Site.id.in_(chunk)))]
                return sorted(out, key=lambda p: p.code)
            return [self._project_node(site, n, mname) for site, n, mname in s.execute(q)]

    @staticmethod
    def _project_node(site: Site, n: int, manager_name: str | None) -> ProjectNode:
        return ProjectNode(site.id, site.project_number, site.name, site.status, site.manager_id,
                           manager_name, site.customer, site.notes, site.version, n)

    def project(self, site_id: int) -> ProjectNode:
        found = self.projects(None, ids=[site_id])
        if not found:
            raise NotFoundError("Project not found.")
        return found[0]

    def project_choices(self) -> list[tuple[str, int]]:
        """(code - name, id) of every project except archived ones."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            return [(f"{code} - {name}", sid) for sid, code, name in s.execute(
                select(Site.id, Site.project_number, Site.name)
                .where(Site.status != SiteStatus.ARCHIVED.value).order_by(Site.project_number))]

    def units(self, site_id: int, ids: Iterable[int] | None = None) -> list[UnitNode]:
        self.require(P.SITES_VIEW)
        mats = (select(UnitMaterial.unit_id, func.count(UnitMaterial.id).label("n"))
                .group_by(UnitMaterial.unit_id).subquery())
        q = (select(ProjectUnit, func.coalesce(mats.c.n, 0)).outerjoin(mats, mats.c.unit_id == ProjectUnit.id)
             .where(ProjectUnit.site_id == site_id).order_by(ProjectUnit.sort_order, ProjectUnit.id))
        if ids is not None:
            q = q.where(ProjectUnit.id.in_(list(ids)))
        with self.db.read_session() as s:
            return [UnitNode(u.id, u.site_id, u.unit_code, u.name, u.quantity, u.sort_order, u.notes, n)
                    for u, n in s.execute(q)]

    def unit(self, unit_id: int) -> UnitNode:
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            n = s.scalar(select(func.count(UnitMaterial.id)).where(UnitMaterial.unit_id == unit_id)) or 0
            return UnitNode(u.id, u.site_id, u.unit_code, u.name, u.quantity, u.sort_order, u.notes, n)

    def materials(self, unit_id: int, ids: Iterable[int] | None = None) -> list[MaterialNode]:
        self.require(P.SITES_VIEW)
        q = (select(UnitMaterial.id, UnitMaterial.unit_id, UnitMaterial.product_id, Product.internal_code,
                    Product.supplier_code, UnitMaterial.vpk_code, Product.name, Product.description, Product.brand,
                    Unit.code, UnitMaterial.quantity_per_unit, ProjectUnit.quantity, UnitMaterial.notes,
                    UnitMaterial.sort_order, Product.is_active)
             .join(Product, Product.id == UnitMaterial.product_id).join(Unit, Unit.id == Product.unit_id)
             .join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
             .where(UnitMaterial.unit_id == unit_id).order_by(UnitMaterial.sort_order, UnitMaterial.id))
        if ids is not None:
            q = q.where(UnitMaterial.id.in_(list(ids)))
        with self.db.read_session() as s:
            return [MaterialNode(*row) for row in s.execute(q)]

    def material(self, line_id: int) -> MaterialNode:
        with self.db.read_session() as s:
            line = s.get(UnitMaterial, line_id)
            if line is None:
                raise NotFoundError("Material line not found.")
            unit_id = line.unit_id
        found = self.materials(unit_id, ids=[line_id])
        return found[0]

    def find_products(self, text: str, limit: int = 200) -> list[Product]:
        """Existing materials for the 'Add material' picker: code, product number, barcode, name,
        description, brand or supplier. Active materials only."""
        from wms.services.materials import material_text_filter
        self.require(P.SITES_VIEW)
        q = (select(Product).where(Product.is_active.is_(True), material_text_filter(text or ""))
             .order_by(Product.internal_code).limit(limit))
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    def last_vpk_code(self, product_id: int) -> str | None:
        """The VPK code used for this material on another line, to offer it again."""
        with self.db.read_session() as s:
            return s.scalar(select(UnitMaterial.vpk_code).where(UnitMaterial.product_id == product_id,
                                                                UnitMaterial.vpk_code.is_not(None))
                            .order_by(UnitMaterial.updated_at.desc()).limit(1))

    # ---- search ------------------------------------------------------------------------------------
    def search(self, text: str, flt: TreeFilter | None = None) -> TreeMatches:
        """Find managers, projects (name / code), units (name / code) and material lines (product
        code, product number, VPK code, material name / description / brand, line notes), and the
        branches leading to each so the screen can open exactly those."""
        self.require(P.SITES_VIEW)
        flt = flt or TreeFilter()
        res = TreeMatches(text=(text or "").strip())
        if not res.text:
            return res
        like = like_pattern(res.text)

        def ilike(col):
            return col.ilike(like, escape="\\")

        site_conds = flt.site_conditions()
        with self.db.read_session() as s:
            mq = select(Manager.id).where(ilike(Manager.name))
            if flt.manager_id not in (None, UNASSIGNED):
                mq = mq.where(Manager.id == flt.manager_id)
            if flt.manager_id != UNASSIGNED and flt.site_id is None:
                res.managers = set(s.scalars(mq))
            for sid, mid in s.execute(select(Site.id, Site.manager_id).where(
                    or_(ilike(Site.name), ilike(Site.project_number)), *site_conds)):
                res.projects.add(sid)
                self._add_path(res, mid)
            for uid, sid, mid in s.execute(
                    select(ProjectUnit.id, Site.id, Site.manager_id).join(Site, Site.id == ProjectUnit.site_id)
                    .where(or_(ilike(ProjectUnit.name), ilike(ProjectUnit.unit_code)), *site_conds)):
                res.units.add(uid)
                res.project_paths.add(sid)
                self._add_path(res, mid)
            for lid, uid, sid, mid in s.execute(
                    select(UnitMaterial.id, ProjectUnit.id, Site.id, Site.manager_id)
                    .join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
                    .join(Site, Site.id == ProjectUnit.site_id)
                    .join(Product, Product.id == UnitMaterial.product_id)
                    .where(or_(ilike(Product.internal_code), ilike(Product.supplier_code), ilike(Product.name),
                               ilike(Product.description), ilike(Product.brand), ilike(UnitMaterial.vpk_code),
                               ilike(UnitMaterial.notes)), *site_conds)):
                res.materials.add(lid)
                res.unit_paths.add(uid)
                res.project_paths.add(sid)
                self._add_path(res, mid)
        return res

    @staticmethod
    def _add_path(res: TreeMatches, manager_id: int | None) -> None:
        if manager_id is None:
            res.unassigned_path = True
        else:
            res.manager_paths.add(manager_id)

    # ---- managers ---------------------------------------------------------------------------------
    def save_manager(self, manager_id: int | None, data: dict) -> ManagerNode:
        """Create a manager or change one (only the fields given). Renaming also updates the name
        shown on the manager's projects."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            if manager_id:
                m = s.get(Manager, manager_id)
                if m is None:
                    raise NotFoundError("Manager not found.")
                old = audit.snapshot(m, MANAGER_FIELDS)
            else:
                m = Manager(is_active=True, sort_order=(s.scalar(select(func.max(Manager.sort_order))) or 0) + 1)
                old = {}
                if "name" not in data:
                    raise ValidationError("Name is required.", "name")
            if "name" in data or not manager_id:
                name = " ".join((clean_str(data.get("name"), "name", required=True, max_len=120) or "").split())
                dup = s.scalar(select(Manager).where(func.upper(Manager.name) == name.upper(),
                                                     Manager.id != (manager_id or 0)).limit(1))
                if dup is not None:
                    raise DuplicateError(f"There is already a manager called {dup.name}."
                                         + ("" if dup.is_active else " It is inactive - edit it and tick Active."))
                m.name = name
            for f, n in (("email", 200), ("phone", 50)):
                if f in data:
                    setattr(m, f, clean_str(data.get(f), f, max_len=n))
            if "notes" in data:
                m.notes = clean_str(data.get("notes"))
            if "is_active" in data:
                m.is_active = bool(data.get("is_active"))
            if not manager_id:
                s.add(m)
            s.flush()
            o, n = audit.diff(old, audit.snapshot(m, MANAGER_FIELDS))
            if n:
                audit.record(s, self.ctx, "MANAGER_UPDATED" if manager_id else "MANAGER_CREATED",
                             EntityType.MANAGER.value, m.id, m.name, old=o, new=n)
            if manager_id and "name" in n:
                # display copy on the projects; version bump so an open project form cannot write the old name back
                s.execute(update(Site).where(Site.manager_id == m.id)
                          .values(project_manager=m.name, version=Site.version + 1, updated_at=utcnow())
                          .execution_options(synchronize_session=False))
            saved = m.id
        return self.manager(saved)

    def move_manager(self, manager_id: int, delta: int) -> None:
        """Move a manager up (-1) or down (+1) in the list."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            rows = list(s.scalars(select(Manager).order_by(Manager.sort_order, func.upper(Manager.name), Manager.id)))
            _shift(rows, manager_id, delta, "Manager")

    def delete_manager(self, manager_id: int, move_projects_to: int | None = None) -> int:
        """Delete a manager. Its projects are kept: unassigned, or moved to ``move_projects_to``.
        Returns the number of projects that were moved / unassigned."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            m = s.get(Manager, manager_id)
            if m is None:
                raise NotFoundError("Manager not found.")
            target = None
            if move_projects_to is not None:
                if move_projects_to == manager_id:
                    raise ValidationError("Choose a different manager to take over the projects.")
                target = s.get(Manager, move_projects_to)
                if target is None:
                    raise NotFoundError("The manager chosen to take over the projects no longer exists.")
            sites = list(s.scalars(select(Site).where(Site.manager_id == manager_id)))
            for site in sites:
                site.manager_id = target.id if target else None
                site.project_manager = target.name if target else None
            s.flush()
            what = (f"{len(sites)} project(s) moved to {target.name}" if target
                    else f"{len(sites)} project(s) left without a manager")
            audit.record(s, self.ctx, "MANAGER_DELETED", EntityType.MANAGER.value, m.id, m.name,
                         f"Manager deleted; {what}. No project was deleted.",
                         old=audit.snapshot(m, MANAGER_FIELDS),
                         new={"projects": [x.project_number for x in sites][:500],
                              "moved_to": target.name if target else None})
            s.delete(m)
            return len(sites)

    # ---- projects ---------------------------------------------------------------------------------
    def assign_manager(self, site_ids: Iterable[int], manager_id: int | None) -> int:
        """Assign projects to a manager, move them to another one, or (None) unassign them."""
        self.require(P.SITES_EDIT)
        ids = list(site_ids)
        if not ids:
            raise ValidationError("Choose at least one project.")
        changed = 0
        with self.db.session() as s:
            m = None
            if manager_id is not None:
                m = s.get(Manager, manager_id)
                if m is None:
                    raise NotFoundError("Manager not found.")
            for chunk in _chunks(ids):
                for site in s.scalars(select(Site).where(Site.id.in_(chunk))):
                    if site.manager_id == manager_id:
                        continue
                    old = {"manager": site.project_manager}
                    site.manager_id = manager_id
                    site.project_manager = m.name if m else None
                    audit.record(s, self.ctx, "PROJECT_MANAGER_CHANGED", EntityType.SITE.value, site.id,
                                 site.display, old=old, new={"manager": site.project_manager})
                    changed += 1
        return changed

    def save_project(self, site_id: int | None, data: dict, expected_version: int | None = None) -> ProjectNode:
        """Create a project or change the fields given (code, name, manager, status, notes); every
        other field of the project record is kept as it is."""
        from wms.services.sites import SITE_FIELDS, SiteService
        merged: dict = {}
        if site_id:
            site = SiteService(self.ctx).get(site_id)
            merged = {f: getattr(site, f) for f in SITE_FIELDS}
            merged["manager_id"] = site.manager_id
        for key in ("project_number", "name", "status", "notes", "customer", "manager_id"):
            if key in data:
                merged[key] = data[key]
        saved = SiteService(self.ctx).save(site_id, merged, expected_version)
        return self.project(saved.id)

    def duplicate_project(self, site_id: int, project_number: str, name: str) -> ProjectNode:
        """A new project with the same manager, details, units and unit materials. Its status starts
        as Planned. Reservations, issues, documents and the material list are not copied."""
        from wms.services.sites import SITE_FIELDS
        self.require(P.SITES_EDIT)
        number = clean_code(project_number, "project_number", "Project number", 50)
        name = clean_str(name, "name", required=True, max_len=200)
        with self.db.session() as s:
            src = s.get(Site, site_id)
            if src is None:
                raise NotFoundError("Project not found.")
            if s.scalar(select(Site.id).where(func.upper(Site.project_number) == number)):
                raise DuplicateError(f"Project number '{number}' already exists.")
            new = Site(**{f: getattr(src, f) for f in SITE_FIELDS if f not in ("project_number", "name", "status",
                                                                                   "start_date", "end_date")})
            new.project_number, new.name, new.status = number, name, SiteStatus.PLANNED.value
            new.manager_id = src.manager_id
            s.add(new)
            s.flush()
            units, lines = self._copy_units(s, src.id, new.id)
            audit.record(s, self.ctx, "PROJECT_DUPLICATED", EntityType.SITE.value, new.id, new.display,
                         f"Copied from {src.display}: {units} unit(s), {lines} material line(s)",
                         new={"from": src.project_number, "units": units, "lines": lines})
            new_id = new.id
        return self.project(new_id)

    def _copy_units(self, s: Session, from_site: int, to_site: int) -> tuple[int, int]:
        user_id = self.ctx.user.id if self.ctx.user else None
        units = lines = 0
        for u in s.scalars(select(ProjectUnit).where(ProjectUnit.site_id == from_site)
                           .order_by(ProjectUnit.sort_order, ProjectUnit.id)):
            copy = ProjectUnit(site_id=to_site, unit_code=u.unit_code, name=u.name, quantity=u.quantity,
                               sort_order=u.sort_order, notes=u.notes, created_by_id=user_id)
            s.add(copy)
            s.flush()
            lines += self._copy_lines(s, u.id, copy.id)
            units += 1
        return units, lines

    def _copy_lines(self, s: Session, from_unit: int, to_unit: int) -> int:
        user_id = self.ctx.user.id if self.ctx.user else None
        n = 0
        for line in s.scalars(select(UnitMaterial).where(UnitMaterial.unit_id == from_unit)
                              .order_by(UnitMaterial.sort_order, UnitMaterial.id)):
            s.add(UnitMaterial(unit_id=to_unit, product_id=line.product_id, quantity_per_unit=line.quantity_per_unit,
                               vpk_code=line.vpk_code, sort_order=line.sort_order, notes=line.notes,
                               created_by_id=user_id))
            n += 1
        s.flush()
        return n

    def project_contents(self, site_id: int) -> tuple[int, int]:
        """(units, material lines) of a project - shown before it is deleted."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            return _contents(s, site_id)

    # ---- units ------------------------------------------------------------------------------------
    @staticmethod
    def _editable_site(s: Session, site_id: int) -> Site:
        site = s.get(Site, site_id)
        if site is None:
            raise NotFoundError("Project not found.")
        if site.status == SiteStatus.ARCHIVED.value:
            raise ValidationError(f"Project {site.project_number} is archived: restore it before changing its units.")
        return site

    def save_unit(self, unit_id: int | None, data: dict, site_id: int | None = None) -> UnitNode:
        """Add a unit to a project (``site_id``) or change the fields given on an existing one."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            if unit_id:
                u = s.get(ProjectUnit, unit_id)
                if u is None:
                    raise NotFoundError("Unit not found.")
                site = self._editable_site(s, u.site_id)
                old = audit.snapshot(u, UNIT_FIELDS)
            else:
                if site_id is None:
                    raise ValidationError("Choose the project for the unit.")
                site = self._editable_site(s, site_id)
                order = (s.scalar(select(func.max(ProjectUnit.sort_order)).where(ProjectUnit.site_id == site_id))
                         or 0) + 1
                u = ProjectUnit(site_id=site_id, sort_order=order, quantity=Decimal(1),
                                created_by_id=self.ctx.user.id if self.ctx.user else None)
                old = {}
                if "name" not in data:
                    raise ValidationError("Unit name is required.", "name")
            if "name" in data:
                u.name = clean_str(data.get("name"), "name", required=True, max_len=200)
            if "unit_code" in data:
                u.unit_code = clean_str(data.get("unit_code"), "unit_code", max_len=50)
            if "quantity" in data:
                u.quantity = _quantity(data.get("quantity"), "Unit quantity")
            if "notes" in data:
                u.notes = clean_str(data.get("notes"))
            if not unit_id:
                s.add(u)
            s.flush()
            o, n = audit.diff(old, audit.snapshot(u, UNIT_FIELDS))
            if n:
                audit.record(s, self.ctx, "UNIT_UPDATED" if unit_id else "UNIT_CREATED", EntityType.PROJECT_UNIT.value,
                             u.id, f"{site.project_number} / {u.name}", old=o, new=n)
            saved = u.id
        return self.unit(saved)

    def duplicate_unit(self, unit_id: int) -> UnitNode:
        """A copy of the unit (with its materials) straight after it in the same project."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            site = self._editable_site(s, u.site_id)
            rows = list(s.scalars(select(ProjectUnit).where(ProjectUnit.site_id == u.site_id)
                                  .order_by(ProjectUnit.sort_order, ProjectUnit.id)))
            copy = ProjectUnit(site_id=u.site_id, unit_code=u.unit_code, name=f"{u.name} (copy)"[:200],
                               quantity=u.quantity, notes=u.notes,
                               created_by_id=self.ctx.user.id if self.ctx.user else None, sort_order=0)
            s.add(copy)
            s.flush()
            rows.insert(rows.index(u) + 1, copy)
            for i, r in enumerate(rows, start=1):
                r.sort_order = i
            n = self._copy_lines(s, u.id, copy.id)
            audit.record(s, self.ctx, "UNIT_DUPLICATED", EntityType.PROJECT_UNIT.value, copy.id,
                         f"{site.project_number} / {copy.name}", f"Copy of {u.name} with {n} material line(s)")
            saved = copy.id
        return self.unit(saved)

    def move_unit(self, unit_id: int, delta: int) -> None:
        """Move a unit up (-1) or down (+1) within its project."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            self._editable_site(s, u.site_id)
            rows = list(s.scalars(select(ProjectUnit).where(ProjectUnit.site_id == u.site_id)
                                  .order_by(ProjectUnit.sort_order, ProjectUnit.id)))
            _shift(rows, unit_id, delta, "Unit")

    def set_unit_position(self, unit_id: int, index: int) -> None:
        """Put a unit at position ``index`` (0 = first) in its project - for drag and drop."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            self._editable_site(s, u.site_id)
            rows = [r for r in s.scalars(select(ProjectUnit).where(ProjectUnit.site_id == u.site_id)
                                         .order_by(ProjectUnit.sort_order, ProjectUnit.id)) if r.id != unit_id]
            rows.insert(max(0, min(index, len(rows))), u)
            for i, r in enumerate(rows, start=1):
                r.sort_order = i

    def move_unit_to_project(self, unit_id: int, site_id: int) -> UnitNode:
        """Move a unit (with its materials) to the end of another project."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            src = self._editable_site(s, u.site_id)
            dst = self._editable_site(s, site_id)
            if src.id == dst.id:
                return self.unit(unit_id)
            u.site_id = dst.id
            u.sort_order = (s.scalar(select(func.max(ProjectUnit.sort_order))
                                     .where(ProjectUnit.site_id == dst.id)) or 0) + 1
            audit.record(s, self.ctx, "UNIT_MOVED", EntityType.PROJECT_UNIT.value, u.id, u.name,
                         f"Moved from {src.project_number} to {dst.project_number}",
                         old={"project": src.project_number}, new={"project": dst.project_number})
        return self.unit(unit_id)

    def delete_unit(self, unit_id: int) -> int:
        """Delete a unit and its material lines (planning data only). Returns the lines removed."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            site = self._editable_site(s, u.site_id)
            n = s.execute(delete(UnitMaterial).where(UnitMaterial.unit_id == unit_id)).rowcount or 0
            audit.record(s, self.ctx, "UNIT_DELETED", EntityType.PROJECT_UNIT.value, u.id,
                         f"{site.project_number} / {u.name}", f"Unit deleted with {n} material line(s)",
                         old=audit.snapshot(u, UNIT_FIELDS) | {"material_lines": n})
            s.delete(u)
            return n

    # ---- unit materials ---------------------------------------------------------------------------
    def add_materials(self, unit_id: int, items: list[tuple[int, object]], vpk_code: str | None = None,
                      notes: str | None = None) -> list[int]:
        """Add existing materials to a unit: [(product_id, quantity per unit)]. Returns new line ids."""
        self.require(P.SITES_EDIT)
        if not items:
            raise ValidationError("Choose at least one material.")
        with self.db.session() as s:
            u = s.get(ProjectUnit, unit_id)
            if u is None:
                raise NotFoundError("Unit not found.")
            site = self._editable_site(s, u.site_id)
            order = s.scalar(select(func.max(UnitMaterial.sort_order)).where(UnitMaterial.unit_id == unit_id)) or 0
            new = []
            for pid, qty in items:
                p = s.get(Product, pid)
                if p is None:
                    raise NotFoundError("A chosen material no longer exists.")
                order += 1
                line = UnitMaterial(unit_id=unit_id, product_id=pid,
                                    quantity_per_unit=_quantity(qty, "Quantity per unit"),
                                    vpk_code=clean_str(vpk_code, "vpk_code", max_len=80), notes=clean_str(notes),
                                    sort_order=order, created_by_id=self.ctx.user.id if self.ctx.user else None)
                s.add(line)
                s.flush()
                new.append(line.id)
                audit.record(s, self.ctx, "UNIT_MATERIAL_ADDED", EntityType.PROJECT_UNIT.value, unit_id,
                             f"{site.project_number} / {u.name}",
                             f"{p.internal_code} x {line.quantity_per_unit} per unit",
                             new={"product": p.internal_code, "quantity_per_unit": str(line.quantity_per_unit)})
            return new

    def update_material(self, line_id: int, data: dict) -> MaterialNode:
        """Change quantity per unit, VPK code or notes (only the fields given)."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            line, u, site = self._line(s, line_id)
            old = audit.snapshot(line, MATERIAL_FIELDS)
            if "quantity_per_unit" in data:
                line.quantity_per_unit = _quantity(data.get("quantity_per_unit"), "Quantity per unit")
            if "vpk_code" in data:
                line.vpk_code = clean_str(data.get("vpk_code"), "vpk_code", max_len=80)
            if "notes" in data:
                line.notes = clean_str(data.get("notes"))
            s.flush()
            o, n = audit.diff(old, audit.snapshot(line, MATERIAL_FIELDS))
            if n:
                audit.record(s, self.ctx, "UNIT_MATERIAL_UPDATED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{site.project_number} / {u.name}", line.product.internal_code, old=o, new=n)
        return self.material(line_id)

    def replace_material(self, line_id: int, product_id: int) -> MaterialNode:
        """Swap the material on a line for another existing one; quantity and notes stay."""
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            line, u, site = self._line(s, line_id)
            p = s.get(Product, product_id)
            if p is None:
                raise NotFoundError("Material not found.")
            old = line.product.internal_code
            if line.product_id != product_id:
                line.product_id = product_id
                s.flush()
                audit.record(s, self.ctx, "UNIT_MATERIAL_REPLACED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{site.project_number} / {u.name}", f"{old} replaced by {p.internal_code}",
                             old={"product": old}, new={"product": p.internal_code})
        return self.material(line_id)

    def remove_materials(self, line_ids: Iterable[int]) -> int:
        self.require(P.SITES_EDIT)
        ids = list(line_ids)
        n = 0
        with self.db.session() as s:
            for lid in ids:
                line, u, site = self._line(s, lid)
                audit.record(s, self.ctx, "UNIT_MATERIAL_REMOVED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{site.project_number} / {u.name}", line.product.internal_code,
                             old={"product": line.product.internal_code,
                                  "quantity_per_unit": str(line.quantity_per_unit)})
                s.delete(line)
                n += 1
        return n

    def move_material(self, line_id: int, delta: int) -> None:
        self.require(P.SITES_EDIT)
        with self.db.session() as s:
            line, _u, _site = self._line(s, line_id)
            rows = list(s.scalars(select(UnitMaterial).where(UnitMaterial.unit_id == line.unit_id)
                                  .order_by(UnitMaterial.sort_order, UnitMaterial.id)))
            _shift(rows, line_id, delta, "Material")

    def _line(self, s: Session, line_id: int):
        line = s.get(UnitMaterial, line_id)
        if line is None:
            raise NotFoundError("Material line not found.")
        u = s.get(ProjectUnit, line.unit_id)
        site = self._editable_site(s, u.site_id)
        return line, u, site

    # ---- totals for the side panel ----------------------------------------------------------------
    def unit_totals(self, site_id: int) -> tuple[Decimal, int]:
        """(sum of unit quantities, material lines) of a project."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            qty = s.scalar(select(func.sum(type_coerce(ProjectUnit.quantity, Integer)))
                           .where(ProjectUnit.site_id == site_id))
            lines = s.scalar(select(func.count(UnitMaterial.id)).join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
                             .where(ProjectUnit.site_id == site_id)) or 0
        return (Decimal(int(qty or 0)) / 1000).quantize(Decimal("0.001")), lines


def _contents(s: Session, site_id: int) -> tuple[int, int]:
    units = s.scalar(select(func.count(ProjectUnit.id)).where(ProjectUnit.site_id == site_id)) or 0
    lines = s.scalar(select(func.count(UnitMaterial.id)).join(ProjectUnit, ProjectUnit.id == UnitMaterial.unit_id)
                     .where(ProjectUnit.site_id == site_id)) or 0
    return units, lines


def delete_project_structure(s: Session, site_id: int) -> tuple[int, int]:
    """Remove a project's units and their material lines (inside the caller's transaction)."""
    units, lines = _contents(s, site_id)
    unit_ids = select(ProjectUnit.id).where(ProjectUnit.site_id == site_id).scalar_subquery()
    s.execute(delete(UnitMaterial).where(UnitMaterial.unit_id.in_(unit_ids)))
    s.execute(delete(ProjectUnit).where(ProjectUnit.site_id == site_id))
    return units, lines


def _quantity(value, label: str) -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"{label} is required.")
    q = to_qty(value, label.lower())
    if q < ZERO:
        raise ValidationError(f"{label} cannot be negative.")
    return q


def _shift(rows: list, row_id: int, delta: int, what: str) -> None:
    """Move one row up / down in an ordered list and number the list 1..n again."""
    ids = [r.id for r in rows]
    if row_id not in ids:
        raise NotFoundError(f"{what} not found.")
    i = ids.index(row_id)
    j = max(0, min(len(rows) - 1, i + delta))
    rows.insert(j, rows.pop(i))
    for n, r in enumerate(rows, start=1):
        r.sort_order = n


__all__ = ["CLOSED_SITE_STATUSES", "UNASSIGNED", "ManagerNode", "MaterialNode", "ProjectNode", "ProjectTreeService",
           "TreeFilter", "TreeMatches", "UnitNode", "delete_project_structure", "find_or_create_manager"]
