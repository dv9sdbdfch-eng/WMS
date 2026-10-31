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

#: alert priorities (spec 11), most urgent first. Data quality never looks as urgent as an operational problem.
CRITICAL, ACTION, DATA = "critical", "action", "data"
PRIORITIES = {CRITICAL: "Critical", ACTION: "Action", DATA: "Data quality"}


@dataclass
class Alert:
    """A problem described in words, most serious first. ``key`` / ``count`` / ``label`` make it an
    ACTION REQUIRED entry; ``preset`` filters the screen it opens."""

    level: str  # "danger" or "warn"
    text: str
    page: str  # main-window page that resolves it
    key: str = ""
    count: int = 0
    label: str = ""
    priority: str = ACTION
    preset: dict | None = None


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
    priority: str = ACTION  # CRITICAL / ACTION / DATA (spec 11)


@dataclass
class DashboardData:
    actions: list[ActionItem] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    today: dict[str, int] = field(default_factory=dict)  # Goods In / Goods Out / Transfers / Returns
    recent_movements: list[StockMovement] = field(default_factory=list)
    backup: object = None  # BackupHealth (SQLite only)

    def action(self, key: str) -> ActionItem | None:
        return next((a for a in self.actions if a.key == key), None)

    def attention(self) -> list[ActionItem]:
        """Everything that needs attention now (spec 10): problems in words and counters, one list,
        zero counts left out, most urgent priority first (stable inside a priority)."""
        items = [ActionItem(a.key or f"alert{i}", a.label or a.text, a.count, a.page, a.preset, a.text,
                            "danger" if a.level == "danger" else "warn", a.priority)
                 for i, a in enumerate(self.alerts)]
        items += [a for a in self.actions if a.count]
        order = list(PRIORITIES)
        return sorted(items, key=lambda a: order.index(a.priority))

    @property
    def action_count(self) -> int:
        return sum(1 for a in self.actions if a.count)


SCAN_DAYS = 30  # delivery notes this recent without a scan are flagged
_BACKUP_LABELS = {"FAILED": "Backup Failed", "NEVER": "No Backup Made Yet", "STALE": "Backup Overdue"}


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
            if not d.backup.ok:  # spec 31 / 14: a failed or overdue backup is promoted to ACTION REQUIRED
                serious = d.backup.state in ("FAILED", "NEVER", "STALE")
                d.alerts.insert(0 if serious else len(d.alerts),
                                Alert("danger" if d.backup.state != "OK" else "warn", d.backup.message, "settings",
                                      "backup", 0, _BACKUP_LABELS.get(d.backup.state, "Backup Needs Attention"),
                                      CRITICAL if serious else ACTION))
        return d

    def _actions(self, s, today: date) -> list[ActionItem]:
        """The counters of spec 27, in its order. Zero counters stay visible (the reassurance that
        nothing is waiting), the screen greys them."""
        from wms.services.search import SearchCriteria, SearchService
        can = self.ctx.has
        out: list[ActionItem] = []
        if can(P.PRODUCTS_VIEW):
            # Low Stock is a state of Materials (spec 3, 12): the number is counted with the same
            # filter the Materials screen then shows, so they always agree.
            from wms.services.materials import MaterialFilter, MaterialsService
            search = SearchService(self.ctx)
            mats = MaterialsService(self.ctx)
            low = mats.count(MaterialFilter(stock="LOW_STOCK"))
            gone = search.count_products(SearchCriteria(stock_status="LOW_STOCK", quick="OUT_OF_STOCK")) if low else 0
            out.append(ActionItem("low_stock", "Low Stock", low, "products", {"stock": "LOW_STOCK"},
                                  hint=f"{gone} of them out of stock" if gone else
                                  "Available at or below the reorder level", level="danger" if gone else "warn"))
        if can(P.PURCHASING_VIEW):
            out.append(ActionItem("overdue_po", "Overdue Purchase Orders", s.scalar(select(func.count(
                PurchaseOrder.id)).where(PurchaseOrder.status.in_(OPEN_PO_STATUSES),
                                         PurchaseOrder.expected_date < today)) or 0,
                "purchase_orders", {"status": "OVERDUE"}, "Past the expected delivery date"))
            out.append(ActionItem("partial_po", "Partial Deliveries", s.scalar(select(func.count(
                PurchaseOrder.id)).where(PurchaseOrder.status == POStatus.PARTIALLY_RECEIVED.value)) or 0,
                "purchase_orders", {"status": POStatus.PARTIALLY_RECEIVED.value}, "Orders with the rest still to come"))
        if can(P.STOCKTAKE_COUNT):
            out.append(ActionItem("stocktake", "Stocktake Differences", s.scalar(
                select(func.count(StockCountLine.id)).join(StockCount, StockCount.id == StockCountLine.count_id)
                .where(StockCount.status == CountStatus.SUBMITTED.value, StockCountLine.counted_qty.is_not(None),
                       StockCountLine.counted_qty != StockCountLine.system_qty)) or 0,
                "stocktake", hint="Counted lines waiting for approval"))
        if can(P.PURCHASING_VIEW):
            out.append(ActionItem("discrepancies", "Delivery Discrepancies", s.scalar(select(func.count(
                DeliveryDiscrepancy.id)).where(DeliveryDiscrepancy.status == DiscrepancyStatus.OPEN.value)) or 0,
                "discrepancies", hint="Short, over, damaged, rejected or wrong goods"))
        if can(P.PRODUCTS_VIEW):
            for key, text, crit, hint in (
                    ("no_bin", "Products Without Bin", {"default_bin": "WITHOUT"}, "No default storage bin set"),
                    ("no_photo", "Products Missing Photo", {"photo": "WITHOUT"}, "No product photo")):
                out.append(ActionItem(key, text, search.count_products(SearchCriteria.from_dict(crit)), "search",
                                      crit, hint, priority=DATA))
        return out

    def _alerts(self, s) -> list[Alert]:
        """Problems described in words, most serious first. Cheap queries only (runs on every refresh)."""
        out: list[Alert] = []
        negative = s.scalar(select(func.count(StockLevel.id)).where(StockLevel.quantity < 0)) or 0
        if negative:
            out.append(Alert("danger", f"{negative} bin balance(s) are negative after a stock override - count "
                                       f"and adjust them.", "movements", "negative_stock", negative,
                             "Negative Bin Balances", CRITICAL))
        if self.ctx.has(P.PRODUCTS_VIEW):
            from wms.services.search import StockColumns
            st = StockColumns()
            over = s.scalar(select(func.count()).select_from(
                st.join(select(Product.id)).where(and_(Product.is_active.is_(True), st.reserved > 0,
                                                       st.available < 0)).subquery())) or 0
            if over:
                out.append(Alert("danger", f"{over} product(s) have more reserved for projects than is in stock - "
                                           f"check the reservations.", "reservations", "over_reserved", over,
                                 "Over-Reserved Materials", CRITICAL))
        since = date.today() - timedelta(days=SCAN_DAYS)
        no_scan = s.scalar(select(func.count(DeliveryNote.id)).where(
            DeliveryNote.status == DocumentStatus.POSTED.value, DeliveryNote.delivery_date >= since,
            ~exists().where(Attachment.entity_type == EntityType.DELIVERY_NOTE.value,
                            Attachment.entity_id == DeliveryNote.id, Attachment.is_deleted.is_(False)))) or 0
        if no_scan:
            # opens Deliveries filtered the same way, so the list shows exactly these notes (spec 12)
            out.append(Alert("warn", f"{no_scan} delivery note(s) from the last {SCAN_DAYS} days have no scan "
                                     f"attached.", "delivery_notes", "no_scan", no_scan,
                             "Delivery Notes Missing Scan", DATA,
                             {"scan": "MISSING", "status": DocumentStatus.POSTED.value, "date_from": since}))
        week = datetime.combine(date.today() - timedelta(days=7), datetime.min.time()).astimezone()
        overrides = s.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.is_override.is_(True), StockMovement.recorded_at >= week)) or 0
        if overrides:
            out.append(Alert("warn", f"{overrides} negative-stock override(s) in the last 7 days.", "movements",
                             "overrides", overrides, "Stock Overrides (7 days)", ACTION))
        return out
