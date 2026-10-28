"""Sites / projects and the materials sent to them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select

from wms.core.enums import MovementType, SiteStatus
from wms.core.exceptions import ConcurrencyError, DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import Product, Site, StockMovement
from wms.services import audit
from wms.services.base import BaseService, clean_code, clean_str, like_pattern

SITE_FIELDS = ("project_number", "name", "customer", "address", "postcode", "project_manager", "contact_name",
               "contact_phone", "contact_email", "status", "start_date", "end_date", "notes")

SITE_MOVEMENTS = (MovementType.GOODS_OUT.value, MovementType.RETURN_FROM_SITE.value)


@dataclass
class SiteMaterialSummary:
    product: Product
    issued: Decimal
    returned: Decimal
    value: Decimal

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
        return self.list(statuses=[SiteStatus.PLANNED.value, SiteStatus.ACTIVE.value, SiteStatus.ON_HOLD.value])

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
            dup = s.scalar(select(Site.id).where(func.upper(Site.project_number) == number))
            if dup and dup != site_id:
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
            for f in ("customer", "address", "postcode", "project_manager", "contact_name", "contact_phone",
                      "contact_email", "notes"):
                setattr(site, f, clean_str(data.get(f)))
            s.flush()
            o, n = audit.diff(old, audit.snapshot(site, SITE_FIELDS))
            if n:
                audit.record(s, self.ctx, "SITE_UPDATED" if site_id else "SITE_CREATED", "SITE", site.id,
                             site.display, old=o, new=n)
            saved_id = site.id
        return self.get(saved_id)

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
