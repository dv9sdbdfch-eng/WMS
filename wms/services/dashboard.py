"""Figures for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from wms.core.enums import DocumentStatus, SiteStatus
from wms.core.numbers import ZERO
from wms.db.models import DeliveryNote, GoodsIssue, Site, StockMovement
from wms.services.base import BaseService
from wms.services.products import ProductRow, ProductService


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


class DashboardService(BaseService):
    def load(self) -> DashboardData:
        d = DashboardData()
        rows = ProductService(self.ctx).list()
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
        return d
