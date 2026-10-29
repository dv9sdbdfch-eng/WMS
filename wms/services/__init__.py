"""Business logic layer. The UI only ever calls these services."""

from __future__ import annotations

from functools import cached_property

from wms.services.base import AppContext


class Services:
    """One instance per logged-in session; screens reach every service through it."""

    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    @cached_property
    def auth(self):
        from wms.services.users import AuthService
        return AuthService(self.ctx)

    @cached_property
    def users(self):
        from wms.services.users import UserService
        return UserService(self.ctx)

    @cached_property
    def reference(self):
        from wms.services.reference import ReferenceService
        return ReferenceService(self.ctx)

    @cached_property
    def locations(self):
        from wms.services.locations import LocationService
        return LocationService(self.ctx)

    @cached_property
    def products(self):
        from wms.services.products import ProductService
        return ProductService(self.ctx)

    @cached_property
    def attachments(self):
        from wms.services.attachments import AttachmentService
        return AttachmentService(self.ctx)

    @cached_property
    def goods_in(self):
        from wms.services.goods_in import GoodsInService
        return GoodsInService(self.ctx)

    @cached_property
    def purchasing(self):
        from wms.services.purchasing import PurchasingService
        return PurchasingService(self.ctx)

    @cached_property
    def reservations(self):
        from wms.services.reservations import ReservationService
        return ReservationService(self.ctx)

    @cached_property
    def excel_import(self):
        from wms.services.excel_import import ExcelImportService
        return ExcelImportService(self.ctx)

    @cached_property
    def goods_out(self):
        from wms.services.goods_out import GoodsOutService
        return GoodsOutService(self.ctx)

    @cached_property
    def sites(self):
        from wms.services.sites import SiteService
        return SiteService(self.ctx)

    @cached_property
    def stock(self):
        from wms.services.stock_ops import StockOpsService
        return StockOpsService(self.ctx)

    @cached_property
    def production(self):
        from wms.services.production import ProductionService
        return ProductionService(self.ctx)

    @cached_property
    def search(self):
        from wms.services.search import SearchService
        return SearchService(self.ctx)

    @cached_property
    def dashboard(self):
        from wms.services.dashboard import DashboardService
        return DashboardService(self.ctx)

    @cached_property
    def reports(self):
        from wms.services.reports import ReportService
        return ReportService(self.ctx)

    @cached_property
    def audit(self):
        from wms.services.audit import AuditService
        return AuditService(self.ctx)

    @cached_property
    def settings(self):
        from wms.services.settings import SettingsService
        return SettingsService(self.ctx)

    @cached_property
    def stocktake(self):
        from wms.services.stocktake import StocktakeService
        return StocktakeService(self.ctx)

    @cached_property
    def ledger(self):
        from wms.services.ledger import LedgerService
        return LedgerService(self.ctx)

    @cached_property
    def reasons(self):
        from wms.services.reasons import ReasonCodeService
        return ReasonCodeService(self.ctx)

    @cached_property
    def backup(self):
        from wms.services.backup import BackupService
        return BackupService(self.ctx)
