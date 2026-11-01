"""Sites / projects and the materials sent to them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select

from wms.core.enums import OPEN_SITE_STATUSES, EntityType, MovementType, SiteStatus
from wms.core.exceptions import ConcurrencyError, DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import (
    FinishedProductInfo,
    GoodsIssue,
    Product,
    ProductionOrder,
    ProjectMaterial,
    Site,
    StockMovement,
    StockReturn,
)
from wms.services import audit, lifecycle
from wms.services.base import BaseService, clean_code, clean_str, like_pattern
from wms.services.lifecycle import RemovalPlan

SITE_FIELDS = ("project_number", "name", "customer", "address", "postcode", "project_manager", "contact_name",
               "contact_phone", "contact_email", "status", "start_date", "end_date", "notes")

SITE_MOVEMENTS = (MovementType.GOODS_OUT.value, MovementType.RETURN_FROM_SITE.value)


@dataclass
class SiteMaterialSummary:
    product: Product
    issued: Decimal
    returned: Decimal
    value: Decimal
    last_movement: datetime | None = None

    @property
    def id(self) -> int:
        return self.product.id

    @property
    def net(self) -> Decimal:
        return self.issued - self.returned


class SiteService(BaseService):
    def list(self, text: str | None = None, statuses: list[str] | None = None) -> list[Site]:
        q = select(Site).order_by(Site.project_number)
        if statuses:
            q = q.where(Site.status.in_(statuses))
        if text and text.strip():
            like = like_pattern(text)
            q = q.where(or_(Site.project_number.ilike(like, escape="\\"), Site.name.ilike(like, escape="\\"), Site.customer.ilike(like, escape="\\"),
                            Site.address.ilike(like, escape="\\"), Site.postcode.ilike(like, escape="\\"), Site.project_manager.ilike(like, escape="\\")))
        with self.db.read_session() as s:
            return list(s.scalars(q))

    def open_sites(self) -> list[Site]:
        return self.list(statuses=list(OPEN_SITE_STATUSES))

    def get(self, site_id: int) -> Site:
        with self.db.read_session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Site not found.")
            return site

    def save(self, site_id: int | None, data: dict, expected_version: int | None = None) -> Site:
        self.require(P.SITES_EDIT)
        number = clean_code(data.get("project_number"), "project_number", "Project number", 50)
        name = clean_str(data.get("name"), "name", required=True, max_len=200)
        try:
            status = SiteStatus(data.get("status") or SiteStatus.ACTIVE).value
        except ValueError:
            raise ValidationError("Choose a valid status.", "status") from None
        start, end = data.get("start_date") or None, data.get("end_date") or None
        if start and end and end < start:
            raise ValidationError("End date cannot be before the start date.", "end_date")
        with self.db.session() as s:
            dup = s.scalar(select(Site).where(func.upper(Site.project_number) == number))
            if dup is not None and dup.id != site_id:
                if dup.status == SiteStatus.ARCHIVED.value:
                    raise DuplicateError(f"Project number '{number}' belongs to archived project {dup.name}. "
                                         f"Restore it instead of creating it again, or choose another number.")
                raise DuplicateError(f"Project number '{number}' already exists.")
            if site_id:
                site = s.get(Site, site_id)
                if site is None:
                    raise NotFoundError("Site not found.")
                if expected_version is not None and site.version != expected_version:
                    raise ConcurrencyError("This site was changed by someone else. Reopen it and try again.")
                old = audit.snapshot(site, SITE_FIELDS)
            else:
                site = Site()
                s.add(site)
                old = {}
            site.project_number, site.name, site.status = number, name, status
            site.start_date, site.end_date = start, end
            for f in ("customer", "address", "postcode", "contact_name", "contact_phone",
                      "contact_email", "notes"):
                setattr(site, f, clean_str(data.get(f)))
            self._set_manager(s, site, data)
            s.flush()
            o, n = audit.diff(old, audit.snapshot(site, SITE_FIELDS))
            if n:
                audit.record(s, self.ctx, "SITE_UPDATED" if site_id else "SITE_CREATED", "SITE", site.id,
                             site.display, old=o, new=n)
            saved_id = site.id
        return self.get(saved_id)

    def _set_manager(self, s, site: Site, data: dict) -> None:
        """v1.15: a project's manager is a link to a manager record (``manager_id``); the name is
        kept in ``project_manager`` for display. Given ``manager_id`` wins; otherwise the typed
        name is matched to a manager (ignoring case) and a new manager is made for a new name."""
        from wms.db.models import Manager
        from wms.services.project_tree import find_or_create_manager
        if "manager_id" in data:
            mid = data.get("manager_id") or None
            m = s.get(Manager, mid) if mid else None
            if mid and m is None:
                raise NotFoundError("Manager not found.")
        else:
            m = find_or_create_manager(s, self.ctx, data.get("project_manager"))
        site.manager_id = m.id if m else None
        site.project_manager = m.name if m else None

    # -- completing / archiving / restoring projects -------------------------------
    def _plan(self, s, site: Site) -> RemovalPlan:
        plan = RemovalPlan("Project", EntityType.SITE.value, site.id, site.display,
                           already_archived=site.status == SiteStatus.ARCHIVED.value)
        plan.history = lifecycle.count(s, StockMovement.id, StockMovement.site_id == site.id)
        plan.references = sum(lifecycle.count(s, col, col == site.id) for col in (
            GoodsIssue.site_id, StockReturn.site_id, ProductionOrder.site_id, FinishedProductInfo.site_id,
            ProjectMaterial.site_id))
        plan.references += lifecycle.attachment_count(s, EntityType.SITE.value, site.id)
        from wms.services.project_tree import _contents
        units, lines = _contents(s, site.id)
        if units:
            what = f"{units:,} unit{'' if units == 1 else 's'} with {lines:,} material line{'' if lines == 1 else 's'}"
            plan.notes.append(f"Its project structure ({what}) will be deleted with it." if plan.permanent
                              else f"Its project structure ({what}) is kept with the archived project.")
        return plan

    def removal_plan(self, site_id: int) -> RemovalPlan:
        with self.db.read_session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Site not found.")
            return self._plan(s, site)

    def remove(self, site_id: int, reason: str | None = None) -> RemovalPlan:
        """Delete a project that never had material, or archive one with history (all of it is kept)."""
        self.require(P.SITES_EDIT)
        reason = lifecycle.reason_text(reason)
        with self.db.session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Site not found.")
            plan = self._plan(s, site)
            plan.check()
            lifecycle.record_removal(s, self.ctx, plan, "SITE", reason)
            if plan.permanent:
                from wms.services.project_tree import delete_project_structure
                delete_project_structure(s, site.id)
                s.delete(site)
            else:
                site.status = SiteStatus.ARCHIVED.value
        return plan

    def set_status(self, site_id: int, status: str, reason: str | None = None) -> Site:
        """Complete, reopen or archive a project. Its history is never touched."""
        self.require(P.SITES_EDIT)
        status = SiteStatus(status).value
        reason = lifecycle.reason_text(reason)
        with self.db.session() as s:
            site = s.get(Site, site_id)
            if site is None:
                raise NotFoundError("Site not found.")
            old = site.status
            if old == status:
                return site
            site.status = status
            if status == SiteStatus.COMPLETED.value and site.end_date is None:
                site.end_date = date.today()
            if status not in OPEN_SITE_STATUSES:
                from wms.services.reservations import release_for_closed_site
                release_for_closed_site(s, self.ctx, site)
            s.flush()
            if status == SiteStatus.ARCHIVED.value:
                plan = self._plan(s, site)
                plan.archive_only = True
                lifecycle.record_removal(s, self.ctx, plan, "SITE", reason)
            elif old == SiteStatus.ARCHIVED.value:
                lifecycle.record_restore(s, self.ctx, EntityType.SITE.value, site.id, site.display, "SITE", reason)
            else:
                audit.record(s, self.ctx, "SITE_STATUS_CHANGED", EntityType.SITE.value, site.id, site.display,
                             reason, old={"status": old}, new={"status": status})
        return self.get(site_id)

    def restore(self, site_id: int, reason: str | None = None) -> Site:
        """Bring an archived (or completed) project back so material can be issued to it again."""
        site = self.get(site_id)
        if site.status in OPEN_SITE_STATUSES:
            raise ValidationError(f"Project {site.project_number} is already active.")
        return self.set_status(site_id, SiteStatus.ACTIVE.value, reason)

    def movements(self, site_id: int, date_from: date | None = None, date_to: date | None = None) -> list[StockMovement]:
        """Every Goods Out and return for this site, newest first."""
        q = (select(StockMovement)
             .where(StockMovement.site_id == site_id, StockMovement.movement_type.in_(SITE_MOVEMENTS))
             .order_by(StockMovement.occurred_at.desc(), StockMovement.id.desc()))
        with self.db.read_session() as s:
            rows = list(s.scalars(q))
        if date_from:
            rows = [m for m in rows if m.occurred_at.astimezone().date() >= date_from]
        if date_to:
            rows = [m for m in rows if m.occurred_at.astimezone().date() <= date_to]
        return rows

    def material_summary(self, site_id: int) -> list[SiteMaterialSummary]:
        """Net material per product sent to the site (issued - returned, reversals netted off)."""
        totals: dict[int, SiteMaterialSummary] = {}
        for m in self.movements(site_id):
            row = totals.setdefault(m.product_id, SiteMaterialSummary(m.product, ZERO, ZERO, ZERO))
            if row.last_movement is None or m.occurred_at > row.last_movement:
                row.last_movement = m.occurred_at
            # A reversal of a Goods Out moves stock back into a bin (to_bin set).
            sign = 1 if m.from_bin_id else -1
            cost = m.unit_cost or ZERO
            if m.movement_type == MovementType.GOODS_OUT.value:
                row.issued += sign * m.quantity
                row.value += sign * m.quantity * cost
            else:
                row.returned += -sign * m.quantity
                row.value -= -sign * m.quantity * cost
        out = [r for r in totals.values() if r.issued or r.returned]
        for r in out:
            r.value = r.value.quantize(Decimal("0.01"))
        return sorted(out, key=lambda r: r.product.internal_code)
