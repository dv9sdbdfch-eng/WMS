"""Figures for the dashboard: what needs action first, then today's work, then recent activity (spec 27).

Everything here is counted in SQL. The dashboard is shown at start-up and after every posting, so
it must not load every product (10,000 products used to cost about a second).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, exists, func, select

from wms.core.enums import (
    OPEN_PO_STATUSES,
    CountStatus,
    DiscrepancyStatus,
    DocumentStatus,
    EntityType,
    MovementType,
    POStatus,
)
from wms.core.permissions import P
from wms.db.models import (
    Attachment,
    DeliveryDiscrepancy,
    DeliveryNote,
    GoodsIssue,
    Product,
    PurchaseOrder,
    StockCount,
    StockCountLine,
    StockLevel,
    StockMovement,
    StockReturn,
)
from wms.services.base import BaseService


@dataclass
class Alert:
    """A problem described in words (shown above the counters, most serious first)."""

    level: str  # "danger" or "warn"
    text: str
    page: str  # main-window page that resolves it


@dataclass
class ActionItem:
    """One "ACTION REQUIRED" counter. Clicking it opens ``page`` showing exactly these records:
    ``preset`` is a search (page "search") or filter values for that page."""

    key: str
    label: str
    count: int
    page: str
    preset: dict | None = None
    hint: str = ""
    level: str = "warn"  # "danger" when it stops work (out of stock)


@dataclass
class DashboardData:
    actions: list[ActionItem] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    today: dict[str, int] = field(default_factory=dict)  # Goods In / Goods Out / Transfers / Returns
    recent_movements: list[StockMovement] = field(default_factory=list)
    backup: object = None  # BackupHealth (SQLite only)

    def action(self, key: str) -> ActionItem | None:
        return next((a for a in self.actions if a.key == key), None)

    @property
    def action_count(self) -> int:
        return sum(1 for a in self.actions if a.count)


class DashboardService(BaseService):
    def load(self, recent: int = 20) -> DashboardData:
        d = DashboardData()
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).astimezone()
        with self.db.read_session() as s:
            d.actions = self._actions(s, today)
            d.alerts = self._alerts(s)
            posted = DocumentStatus.POSTED.value
            d.today = {
                "Goods In": s.scalar(select(func.count(DeliveryNote.id)).where(
                    DeliveryNote.delivery_date == today, DeliveryNote.status == posted)) or 0,
                "Goods Out": s.scalar(select(func.count(GoodsIssue.id)).where(
                    GoodsIssue.issue_date == today, GoodsIssue.status == posted)) or 0,
                "Transfers": s.scalar(select(func.count(StockMovement.id)).where(
                    StockMovement.movement_type == MovementType.TRANSFER.value,
                    StockMovement.reversal_of_id.is_(None), StockMovement.occurred_at >= start)) or 0,
                "Returns": s.scalar(select(func.count(StockReturn.id)).where(
                    StockReturn.return_date == today, StockReturn.status == posted)) or 0,
            }
            if self.ctx.has(P.STOCK_VIEW):
                # Newest recorded first: ids only grow, and the primary key needs no extra index.
                d.recent_movements = list(s.scalars(select(StockMovement)
                                                    .order_by(StockMovement.id.desc()).limit(recent)))
        if self.ctx.config.is_sqlite:
            from wms.services.backup import BackupService
            d.backup = BackupService(self.ctx).health()
            if not d.backup.ok:  # spec 31: backup failure must never be silent
                d.alerts.insert(0 if d.backup.state in ("FAILED", "NEVER", "STALE") else len(d.alerts),
                                Alert("danger" if d.backup.state != "OK" else "warn", d.backup.message, "settings"))
        return d

    def _actions(self, s, today: date) -> list[ActionItem]:
        """The counters of spec 27, in its order. Zero counters stay visible (the reassurance that
        nothing is waiting), the screen greys them."""
        from wms.services.search import SearchCriteria, SearchService
        can = self.ctx.has
        out: list[ActionItem] = []
        if can(P.PRODUCTS_VIEW):
            search = SearchService(self.ctx)
            low = search.count_products(SearchCriteria(stock_status="LOW_STOCK"))
            gone = search.count_products(SearchCriteria(stock_status="LOW_STOCK", quick="OUT_OF_STOCK")) if low else 0
            out.append(ActionItem("low_stock", "Low stock - reorder", low, "low_stock",
                                  hint=f"{gone} of them out of stock" if gone else
                                  "Available at or below the reorder level", level="danger" if gone else "warn"))
        if can(P.PURCHASING_VIEW):
            out.append(ActionItem("overdue_po", "Overdue purchase orders", s.scalar(select(func.count(
                PurchaseOrder.id)).where(PurchaseOrder.status.in_(OPEN_PO_STATUSES),
                                         PurchaseOrder.expected_date < today)) or 0,
                "purchase_orders", {"status": "OVERDUE"}, "Past the expected delivery date"))
            out.append(ActionItem("partial_po", "Partial deliveries", s.scalar(select(func.count(
                PurchaseOrder.id)).where(PurchaseOrder.status == POStatus.PARTIALLY_RECEIVED.value)) or 0,
                "purchase_orders", {"status": POStatus.PARTIALLY_RECEIVED.value}, "Orders with the rest still to come"))
        if can(P.STOCKTAKE_COUNT):
            out.append(ActionItem("stocktake", "Stocktake differences", s.scalar(
                select(func.count(StockCountLine.id)).join(StockCount, StockCount.id == StockCountLine.count_id)
                .where(StockCount.status == CountStatus.SUBMITTED.value, StockCountLine.counted_qty.is_not(None),
                       StockCountLine.counted_qty != StockCountLine.system_qty)) or 0,
                "stocktake", hint="Counted lines waiting for approval"))
        if can(P.PURCHASING_VIEW):
            out.append(ActionItem("discrepancies", "Delivery discrepancies", s.scalar(select(func.count(
                DeliveryDiscrepancy.id)).where(DeliveryDiscrepancy.status == DiscrepancyStatus.OPEN.value)) or 0,
                "discrepancies", hint="Short, over, damaged, rejected or wrong goods"))
        if can(P.PRODUCTS_VIEW):
            for key, text, crit, hint in (
                    ("no_bin", "Products without bin", {"default_bin": "WITHOUT"}, "No default storage bin set"),
                    ("no_photo", "Products missing photos", {"photo": "WITHOUT"}, "No product photo")):
                out.append(ActionItem(key, text, search.count_products(SearchCriteria.from_dict(crit)), "search",
                                      crit, hint))
        return out

    def _alerts(self, s) -> list[Alert]:
        """Problems described in words, most serious first. Cheap queries only (runs on every refresh)."""
        out: list[Alert] = []
        negative = s.scalar(select(func.count(StockLevel.id)).where(StockLevel.quantity < 0)) or 0
        if negative:
            out.append(Alert("danger", f"{negative} bin balance(s) are negative after a stock override - count "
                                       f"and adjust them.", "movements"))
        if self.ctx.has(P.PRODUCTS_VIEW):
            from wms.services.search import StockColumns
            st = StockColumns()
            over = s.scalar(select(func.count()).select_from(
                st.join(select(Product.id)).where(and_(Product.is_active.is_(True), st.reserved > 0,
                                                       st.available < 0)).subquery())) or 0
            if over:
                out.append(Alert("danger", f"{over} product(s) have more reserved for projects than is in stock - "
                                           f"check the reservations.", "reservations"))
        since = datetime.combine(date.today() - timedelta(days=30), datetime.min.time()).astimezone()
        no_scan = s.scalar(select(func.count(DeliveryNote.id)).where(
            DeliveryNote.status == DocumentStatus.POSTED.value, DeliveryNote.created_at >= since,
            ~exists().where(Attachment.entity_type == EntityType.DELIVERY_NOTE.value,
                            Attachment.entity_id == DeliveryNote.id, Attachment.is_deleted.is_(False)))) or 0
        if no_scan:
            out.append(Alert("warn", f"{no_scan} delivery note(s) from the last 30 days have no scan attached.",
                             "delivery_notes"))
        week = datetime.combine(date.today() - timedelta(days=7), datetime.min.time()).astimezone()
        overrides = s.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.is_override.is_(True), StockMovement.recorded_at >= week)) or 0
        if overrides:
            out.append(Alert("warn", f"{overrides} negative-stock override(s) in the last 7 days.", "movements"))
        return out
