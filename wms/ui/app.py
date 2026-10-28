"""UI-side application object shared by every screen."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from wms.core.enums import EntityType
from wms.services import Services
from wms.services.base import AppContext


class App(QObject):
    # Emitted after anything that changes stock or master data, so screens refresh.
    data_changed = Signal()

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.services = Services(ctx)
        # Set after a backup restore: the window closes and the program starts again.
        self.restart_required = False

    @property
    def user(self):
        return self.ctx.user

    def can(self, permission: str) -> bool:
        return self.ctx.has(permission)

    def changed(self) -> None:
        self.data_changed.emit()

    # -- pick-list options --------------------------------------------------
    def product_options(self, item_types: list[str] | None = None, include_inactive: bool = False):
        rows = self.services.products.list(item_types=item_types, include_inactive=include_inactive)
        return [(f"{r.product.internal_code} - {r.product.name}", r.product.id) for r in rows]

    def bin_options(self, available_only: bool = False):
        return [(b.full_code + ("" if b.is_available else "  [quarantine]"), b.id)
                for b in self.services.locations.bins() if b.is_available or not available_only]

    def site_options(self, open_only: bool = True):
        sites = self.services.sites.open_sites() if open_only else self.services.sites.list()
        return [(f"{s.project_number} - {s.name}", s.id) for s in sites]

    def supplier_options(self):
        return [(f"{s.name} ({s.code})", s.id) for s in self.services.reference.suppliers()]

    def location_options(self):
        return [(loc.path, loc.id) for loc in self.services.locations.tree()]

    # -- navigation from search / links -------------------------------------
    def open_entity(self, entity_type: str, entity_id: int, parent: QWidget | None = None) -> None:
        if entity_type == EntityType.PRODUCT.value:
            from wms.ui.dialogs.product_dialog import ProductDialog
            ProductDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.DELIVERY_NOTE.value:
            from wms.ui.dialogs.documents import DeliveryNoteDialog
            DeliveryNoteDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.GOODS_ISSUE.value:
            from wms.ui.dialogs.documents import GoodsIssueDialog
            GoodsIssueDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.SITE.value:
            from wms.ui.dialogs.site_dialog import SiteDialog
            SiteDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.RETURN.value:
            from wms.ui.dialogs.documents import ReturnDialog
            ReturnDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.BIN.value:
            from wms.ui.dialogs.documents import BinContentsDialog
            BinContentsDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.SUPPLIER.value:
            from wms.ui.dialogs.reference_dialogs import SupplierDialog
            SupplierDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.PRODUCTION.value:
            from wms.ui.dialogs.documents import ProductionDialog
            ProductionDialog(self, entity_id, parent).exec()
