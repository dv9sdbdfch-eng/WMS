"""Figures for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import exists, func, select

from wms.core.enums import (
    OPEN_PO_STATUSES,
    CountStatus,
    DiscrepancyStatus,
    DocumentStatus,
    EntityType,
    SiteStatus,
    StockStatus,
)
from wms.core.numbers import ZERO
from wms.db.models import (
    Attachment,
    DeliveryDiscrepancy,
    DeliveryNote,
    GoodsIssue,
    PurchaseOrder,
    Site,
    StockCount,
    StockLevel,
    StockMovement,
)
from wms.services.base import BaseService
from wms.services.products import ProductRow, ProductService


@dataclass
class Alert:
    level: str  # "danger" or "warn"
    text: str
    page: str  # main-window page that resolves it


@dataclass
class DashboardData:
    total_products: int = 0
    products_in_stock: int = 0
    stock_value: Decimal = ZERO
    received_today: int = 0
    issued_today: int = 0
    low_stock: list[ProductRow] = field(default_factory=list)
    recent_deliveries: list[DeliveryNote] = field(default_factory=list)
    recent_issues: list[GoodsIssue] = field(default_factory=list)
    recent_movements: list[StockMovement] = field(default_factory=list)
    active_sites: list[Site] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    all_rows: list[ProductRow] = field(default_factory=list)
    backup: object = None  # BackupHealth (SQLite only)


class DashboardService(BaseService):
    def load(self) -> DashboardData:
        d = DashboardData()
        rows = ProductService(self.ctx).list()
        d.all_rows = rows
        d.total_products = len(rows)
        d.products_in_stock = sum(1 for r in rows if r.on_hand > ZERO)
        d.stock_value = sum((r.stock_value for r in rows if r.on_hand > ZERO), ZERO)
        d.low_stock = [r for r in rows if r.is_low]
        today = date.today()
        start = datetime.combine(today, datetime.min.time()).astimezone()
        with self.db.read_session() as s:
            d.received_today = s.scalar(select(func.count(DeliveryNote.id)).where(
                DeliveryNote.delivery_date == today, DeliveryNote.status == DocumentStatus.POSTED.value)) or 0
            d.issued_today = s.scalar(select(func.count(GoodsIssue.id)).where(
                GoodsIssue.issue_date == today, GoodsIssue.status == DocumentStatus.POSTED.value)) or 0
            d.recent_deliveries = list(s.scalars(select(DeliveryNote).order_by(DeliveryNote.created_at.desc()).limit(8)))
            d.recent_issues = list(s.scalars(select(GoodsIssue).order_by(GoodsIssue.created_at.desc()).limit(8)))
            d.recent_movements = list(s.scalars(select(StockMovement).where(StockMovement.recorded_at >= start)
                                                .order_by(StockMovement.recorded_at.desc()).limit(15)))
            if not d.recent_movements:
                d.recent_movements = list(s.scalars(select(StockMovement)
                                                    .order_by(StockMovement.recorded_at.desc()).limit(15)))
            d.active_sites = list(s.scalars(select(Site).where(Site.status == SiteStatus.ACTIVE.value)
                                            .order_by(Site.project_number)))
            d.alerts = self._alerts(s, d)
        if self.ctx.config.is_sqlite:
            from wms.services.backup import BackupService
            d.backup = BackupService(self.ctx).health()
            if not d.backup.ok:  # spec 31: backup failure must never be silent
                d.alerts.insert(0 if d.backup.state in ("FAILED", "NEVER", "STALE") else len(d.alerts),
                                Alert("danger" if d.backup.state != "OK" else "warn", d.backup.message, "settings"))
        return d

    def _alerts(self, s, d: DashboardData) -> list[Alert]:
        """Problems someone has to act on, most serious first. Cheap queries only (runs on every refresh)."""
        out: list[Alert] = []
        negative = s.scalar(select(func.count(StockLevel.id)).where(StockLevel.quantity < 0)) or 0
        if negative:
            out.append(Alert("danger", f"{negative} bin balance(s) are negative after a stock override - count "
                                       f"and adjust them.", "movements"))
        n_out = sum(1 for r in d.low_stock if r.status is StockStatus.OUT)
        if n_out:
            out.append(Alert("danger", f"{n_out} product(s) with a minimum level are out of stock.", "low_stock"))
        if len(d.low_stock) > n_out:
            out.append(Alert("warn", f"{len(d.low_stock) - n_out} product(s) need reordering (at or below their "
                                     f"minimum or reorder level).", "low_stock"))
        over = [r for r in d.all_rows if r.reserved > ZERO and r.available < ZERO]
        if over:
            out.append(Alert("danger", f"{len(over)} product(s) have more reserved for projects than is in stock - "
                                       f"check the reservations.", "reservations"))
        waiting = s.scalar(select(func.count(StockCount.id)).where(
            StockCount.status == CountStatus.SUBMITTED.value)) or 0
        if waiting:
            out.append(Alert("warn", f"{waiting} stock count(s) are waiting for approval.", "stocktake"))
        since = datetime.combine(date.today() - timedelta(days=30), datetime.min.time()).astimezone()
        no_scan = s.scalar(select(func.count(DeliveryNote.id)).where(
            DeliveryNote.status == DocumentStatus.POSTED.value, DeliveryNote.created_at >= since,
            ~exists().where(Attachment.entity_type == EntityType.DELIVERY_NOTE.value,
                            Attachment.entity_id == DeliveryNote.id, Attachment.is_deleted.is_(False)))) or 0
        if no_scan:
            out.append(Alert("warn", f"{no_scan} delivery note(s) from the last 30 days have no scan attached.",
                             "delivery_notes"))
        problems = s.scalar(select(func.count(DeliveryDiscrepancy.id)).where(
            DeliveryDiscrepancy.status == DiscrepancyStatus.OPEN.value)) or 0
        if problems:
            out.append(Alert("warn", f"{problems} delivery problem(s) (short, over, damaged, rejected or wrong goods) "
                                     f"are still open.", "discrepancies"))
        overdue = s.scalar(select(func.count(PurchaseOrder.id)).where(
            PurchaseOrder.status.in_(OPEN_PO_STATUSES), PurchaseOrder.expected_date < date.today())) or 0
        if overdue:
            out.append(Alert("warn", f"{overdue} purchase order(s) are past their expected delivery date.",
                             "purchase_orders"))
        week = datetime.combine(date.today() - timedelta(days=7), datetime.min.time()).astimezone()
        overrides = s.scalar(select(func.count(StockMovement.id)).where(
            StockMovement.is_override.is_(True), StockMovement.recorded_at >= week)) or 0
        if overrides:
            out.append(Alert("warn", f"{overrides} negative-stock override(s) in the last 7 days.", "movements"))
        return out
