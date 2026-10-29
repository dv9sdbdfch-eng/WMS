"""UI-side application object shared by every screen."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from wms.core.enums import EntityType
from wms.core.permissions import P
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

    def bin_options(self, available_only: bool = False, include_inactive: bool = False):
        """Active bins for posting screens. Search and history pass include_inactive=True so bins removed
        from active use can still be investigated."""
        return [(b.full_code + ("" if b.is_available else "  [quarantine]")
                 + ("" if b.is_active else "  [archived]"), b.id)
                for b in self.services.locations.bins(include_inactive=include_inactive)
                if b.is_available or not available_only]

    def find_bin_id(self, scan: str) -> int | None:
        """Bin from a scanned label / typed code (PickCombo.resolver for bin fields)."""
        b = self.services.locations.find_bin(scan)
        return b.id if b else None

    def find_product_id(self, scan: str) -> int | None:
        """Product from a scanned barcode / typed code (PickCombo.resolver for product fields)."""
        p = self.services.products.find_by_code(scan)
        return p.id if p else None

    def site_options(self, open_only: bool = True):
        """Open projects only by default; history screens pass open_only=False to include completed and
        archived ones (marked, so nobody mistakes them for live projects)."""
        from wms.core.enums import OPEN_SITE_STATUSES, SiteStatus
        sites = self.services.sites.open_sites() if open_only else self.services.sites.list()
        return [(f"{s.project_number} - {s.name}"
                 + ("" if s.status in OPEN_SITE_STATUSES else f"  [{SiteStatus(s.status).label}]"), s.id)
                for s in sites]

    def supplier_options(self, include_inactive: bool = False):
        return [(f"{s.name} ({s.code})" + ("" if s.is_active else "  [archived]"), s.id)
                for s in self.services.reference.suppliers(include_inactive=include_inactive)]

    def location_options(self):
        return [(loc.path, loc.id) for loc in self.services.locations.tree()]

    # -- navigation from search / links -------------------------------------
    def open_entity(self, entity_type: str, entity_id: int, parent: QWidget | None = None,
                    tab: str | None = None) -> None:
        """Open a record's own screen. ``tab`` picks the starting tab where a screen has several
        ("history" for a product, "materials" for a project, "products" for a supplier)."""
        if entity_type == EntityType.PRODUCT.value:
            from wms.ui.dialogs.product_dialog import ProductDialog
            ProductDialog(self, entity_id, parent, tab=tab).exec()
        elif entity_type == EntityType.DELIVERY_NOTE.value:
            from wms.ui.dialogs.documents import DeliveryNoteDialog
            DeliveryNoteDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.GOODS_ISSUE.value:
            from wms.ui.dialogs.documents import GoodsIssueDialog
            GoodsIssueDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.SITE.value:
            from wms.ui.dialogs.site_dialog import SiteDialog
            SiteDialog(self, entity_id, parent, tab=tab).exec()
        elif entity_type == EntityType.RETURN.value:
            from wms.ui.dialogs.documents import ReturnDialog
            ReturnDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.BIN.value:
            from wms.ui.dialogs.documents import BinContentsDialog
            BinContentsDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.SUPPLIER.value:
            from wms.ui.dialogs.reference_dialogs import SupplierDialog
            SupplierDialog(self, entity_id, parent, tab=tab).exec()
        elif entity_type == EntityType.PURCHASE_ORDER.value:
            from wms.ui.dialogs.purchasing import PurchaseOrderDialog
            PurchaseOrderDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.DISCREPANCY.value:
            from wms.ui.dialogs.purchasing import DiscrepancyDialog
            DiscrepancyDialog(self, entity_id, parent).exec()
        elif entity_type == EntityType.RESERVATION.value:
            v = self.services.reservations.get(entity_id)
            if v.reservation.status == "ACTIVE" and self.can(P.RESERVATIONS_EDIT):
                from wms.ui.dialogs.reservations import ReservationDialog
                ReservationDialog(self, parent, reservation_id=entity_id).exec()
            else:
                from wms.ui.dialogs.site_dialog import SiteDialog
                SiteDialog(self, v.reservation.site_id, parent, tab="reservations").exec()
        elif entity_type == EntityType.PRODUCTION.value:
            from wms.ui.dialogs.documents import ProductionDialog
            ProductionDialog(self, entity_id, parent).exec()
