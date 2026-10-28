"""Goods Out: issuing material to sites / projects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from wms.core.enums import DocumentStatus, EntityType, MovementType, SiteStatus
from wms.core.exceptions import InsufficientStockError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, GoodsIssue, GoodsIssueLine, Product, Site, StockLevel, StockMovement
from wms.services import audit
from wms.services.base import BaseService, clean_str
from wms.services.goods_in import doc_datetime, validate_doc_date
from wms.services.numbering import next_number
from wms.services.stock_engine import StockEngine

CLOSED_SITE_STATUSES = {SiteStatus.COMPLETED.value, SiteStatus.CANCELLED.value}


@dataclass
class IssueLine:
    product_id: int
    quantity: Decimal | str
    bin_id: int | None = None  # None = pick automatically from available bins
    notes: str | None = None


def allocate(s: Session, product: Product, qty: Decimal) -> list[tuple[Bin, Decimal]]:
    """Choose bins to pick from: default bin first, then bins with the most stock.

    Only available (non-quarantine) bins are used. Raises InsufficientStockError
    with the total available if there is not enough.
    """
    levels = list(
        s.scalars(
            select(StockLevel)
            .join(Bin)
            .where(StockLevel.product_id == product.id, StockLevel.quantity > ZERO,
                   Bin.is_available.is_(True), Bin.is_active.is_(True))
        )
    )
    levels.sort(key=lambda l: (l.bin_id != product.default_bin_id, -l.quantity))
    picks: list[tuple[Bin, Decimal]] = []
    remaining = qty
    for lvl in levels:
        if remaining <= ZERO:
            break
        take = min(lvl.quantity, remaining)
        picks.append((lvl.bin, take))
        remaining -= take
    if remaining > ZERO:
        total = sum((l.quantity for l in levels), ZERO)
        raise InsufficientStockError(product.internal_code, "all available bins", total, qty)
    return picks


class GoodsOutService(BaseService):
    def post(self, header: dict, lines: list[IssueLine], override_reason: str | None = None) -> GoodsIssue:
        """Issue materials to a site. All lines post or none do."""
        self.require(P.GOODS_OUT_POST)
        issue_date = validate_doc_date(header.get("issue_date"), "issue_date")
        if not lines:
            raise ValidationError("Add at least one product line.")
        with self.db.session() as s:
            site = s.get(Site, header.get("site_id")) if header.get("site_id") else None
            if site is None:
                raise ValidationError("Destination site / project is required.", "site_id")
            if site.status in CLOSED_SITE_STATUSES:
                raise ValidationError(f"Site {site.project_number} is {SiteStatus(site.status).label.lower()} - "
                                      f"reopen it before issuing material.", "site_id")
            issued_by = clean_str(header.get("issued_by"), max_len=120) or self.user.full_name
            issue = GoodsIssue(
                issue_number=next_number(s, "GOODS_ISSUE"),
                issue_date=issue_date,
                site_id=site.id,
                requested_by=clean_str(header.get("requested_by"), max_len=120),
                issued_by=issued_by,
                reference=clean_str(header.get("reference"), max_len=80),
                notes=clean_str(header.get("notes")),
                user_id=self.user.id,
                status=DocumentStatus.POSTED.value,
            )
            s.add(issue)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(issue_date)
            line_no = 0
            for i, ln in enumerate(lines, start=1):
                product = s.get(Product, ln.product_id)
                if product is None:
                    raise ValidationError(f"Line {i}: product not found.")
                qty = to_qty(ln.quantity, f"line {i} quantity")
                if qty <= ZERO:
                    raise ValidationError(f"Line {i} ({product.internal_code}): quantity must be greater than zero.")
                if ln.bin_id:
                    b = s.get(Bin, ln.bin_id)
                    if b is None:
                        raise ValidationError(f"Line {i}: bin not found.")
                    if not b.is_available:
                        raise ValidationError(f"Line {i}: bin {b.full_code} is a quarantine bin - "
                                              f"stock there cannot be issued.")
                    picks = [(b, qty)]
                else:
                    try:
                        picks = allocate(s, product, qty)
                    except InsufficientStockError:
                        if not override_reason:
                            raise
                        fallback = s.get(Bin, product.default_bin_id) if product.default_bin_id else None
                        if fallback is None:
                            raise ValidationError(f"{product.internal_code} has no default bin to issue from.") from None
                        picks = [(fallback, qty)]
                for b, take in picks:
                    mv = engine.post(
                        movement_type=MovementType.GOODS_OUT, product=product, quantity=take, from_bin=b,
                        occurred_at=when, site_id=site.id, doc_type=EntityType.GOODS_ISSUE.value, doc_id=issue.id,
                        doc_number=issue.issue_number, reference=issue.reference, notes=clean_str(ln.notes),
                        override_reason=override_reason,
                    )
                    line_no += 1
                    issue.lines.append(GoodsIssueLine(line_no=line_no, product_id=product.id, quantity=take,
                                                      unit_cost=product.unit_cost, bin_id=b.id,
                                                      notes=clean_str(ln.notes), movement_id=mv.id))
            s.flush()
            audit.record(s, self.ctx, "GOODS_OUT_POSTED", EntityType.GOODS_ISSUE.value, issue.id,
                         f"{issue.issue_number} to {site.project_number}", f"{line_no} line(s) issued.",
                         new={"site": site.display, "reference": issue.reference, "requested_by": issue.requested_by,
                              "issued_by": issued_by,
                              "lines": [{"product_id": l.product_id, "qty": l.quantity, "bin_id": l.bin_id}
                                        for l in issue.lines]})
            issue_id = issue.id
        return self.get(issue_id)

    def reverse(self, issue_id: int, reason: str) -> GoodsIssue:
        """Cancel an issue posted by mistake (material never left). Use Returns for material coming back."""
        self.require(P.GOODS_OUT_REVERSE)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            issue = s.get(GoodsIssue, issue_id)
            if issue is None:
                raise NotFoundError("Goods Out record not found.")
            if issue.status != DocumentStatus.POSTED.value:
                raise ValidationError("This Goods Out has already been reversed.")
            engine = StockEngine(s, self.ctx)
            for line in issue.lines:
                engine.reverse(s.get(StockMovement, line.movement_id), f"{issue.issue_number} reversed: {reason}")
            issue.status = DocumentStatus.REVERSED.value
            issue.reversed_at = utcnow()
            issue.reversed_by_id = self.user.id
            issue.reversal_reason = reason
            audit.record(s, self.ctx, "GOODS_OUT_REVERSED", EntityType.GOODS_ISSUE.value, issue.id,
                         issue.issue_number, reason, old={"status": "POSTED"}, new={"status": "REVERSED"})
        return self.get(issue_id)

    def get(self, issue_id: int) -> GoodsIssue:
        with self.db.read_session() as s:
            issue = s.get(GoodsIssue, issue_id)
            if issue is None:
                raise NotFoundError("Goods Out record not found.")
            _ = [(l.product, l.bin) for l in issue.lines]
            return issue

    def search(self, text: str | None = None, site_id: int | None = None, date_from: date | None = None,
               date_to: date | None = None, status: str | None = None, product_id: int | None = None,
               limit: int = 1000) -> list[GoodsIssue]:
        q = (select(GoodsIssue).options(selectinload(GoodsIssue.lines))
             .order_by(GoodsIssue.issue_date.desc(), GoodsIssue.id.desc()).limit(limit))
        if site_id:
            q = q.where(GoodsIssue.site_id == site_id)
        if date_from:
            q = q.where(GoodsIssue.issue_date >= date_from)
        if date_to:
            q = q.where(GoodsIssue.issue_date <= date_to)
        if status:
            q = q.where(GoodsIssue.status == status)
        if product_id:
            q = q.where(GoodsIssue.id.in_(select(GoodsIssueLine.issue_id).where(GoodsIssueLine.product_id == product_id)))
        if text and text.strip():
            like = f"%{text.strip()}%"
            q = q.where(or_(
                GoodsIssue.issue_number.ilike(like), GoodsIssue.reference.ilike(like),
                GoodsIssue.requested_by.ilike(like), GoodsIssue.issued_by.ilike(like), GoodsIssue.notes.ilike(like),
                GoodsIssue.site_id.in_(select(Site.id).where(or_(Site.name.ilike(like), Site.project_number.ilike(like),
                                                                 Site.customer.ilike(like)))),
                GoodsIssue.id.in_(select(GoodsIssueLine.issue_id).join(Product).where(
                    or_(Product.internal_code.ilike(like), Product.name.ilike(like)))),
            ))
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    def available_for_issue(self, product_id: int) -> Decimal:
        with self.db.read_session() as s:
            return s.scalar(
                select(func.coalesce(func.sum(StockLevel.quantity), 0)).join(Bin)
                .where(StockLevel.product_id == product_id, Bin.is_available.is_(True), Bin.is_active.is_(True))
            ) or ZERO
