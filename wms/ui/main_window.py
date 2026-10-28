"""Main window: sidebar navigation, global search, page stack."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from wms import APP_NAME, __version__
from wms.core.permissions import P
from wms.ui.widgets.common import Col, DataTable, SearchBox, button, label

log = logging.getLogger(__name__)


def _nav():
    """(section, key, label, permission, page factory)."""
    from wms.ui.pages import (
        dashboard,
        delivery_notes,
        finished,
        goods_in,
        goods_out,
        locations,
        low_stock,
        movements,
        products,
        reports,
        returns,
        settings,
        sites,
        users,
    )
    return [
        ("", "dashboard", "Dashboard", None, dashboard.DashboardPage),
        ("STOCK", "products", "Products", P.PRODUCTS_VIEW, products.ProductsPage),
        ("STOCK", "finished", "Finished Products", P.PRODUCTS_VIEW, finished.FinishedPage),
        ("STOCK", "locations", "Locations / Bins", P.LOCATIONS_VIEW, locations.LocationsPage),
        ("STOCK", "low_stock", "Low Stock", P.PRODUCTS_VIEW, low_stock.LowStockPage),
        ("OPERATIONS", "goods_in", "Goods In", P.GOODS_IN_POST, goods_in.GoodsInPage),
        ("OPERATIONS", "delivery_notes", "Delivery Notes", P.GOODS_IN_VIEW, delivery_notes.DeliveryNotesPage),
        ("OPERATIONS", "goods_out", "Goods Out", P.GOODS_OUT_VIEW, goods_out.GoodsOutPage),
        ("OPERATIONS", "returns", "Returns", P.GOODS_OUT_VIEW, returns.ReturnsPage),
        ("OPERATIONS", "movements", "Stock Movements", P.STOCK_VIEW, movements.MovementsPage),
        ("OPERATIONS", "sites", "Sites / Projects", P.SITES_VIEW, sites.SitesPage),
        ("ADMIN", "reports", "Reports", P.REPORTS_VIEW, reports.ReportsPage),
        ("ADMIN", "users", "Users", P.USERS_MANAGE, users.UsersPage),
        ("ADMIN", "audit", "Audit Trail", P.AUDIT_VIEW, users.AuditPage),
        ("ADMIN", "settings", "Settings", None, settings.SettingsPage),
    ]


class SearchResultsDialog(QDialog):
    def __init__(self, app, text: str, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"Search: {text}")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        self.box = SearchBox("Search everything: code, name, supplier, delivery note, site, bin, reference...")
        self.box.setText(text)
        self.box.search.connect(self.do_search)
        lay.addWidget(self.box)
        self.summary = label("", "Muted")
        lay.addWidget(self.summary)
        self.table = DataTable([Col("Type", lambda h: h.kind, width=120), Col("Record", lambda h: h.title, width=360),
                                Col("Details", lambda h: h.detail)])
        self.table.activated_item.connect(self.open_hit)
        lay.addWidget(self.table, 1)
        lay.addWidget(label("Double-click or press Enter to open a result.", "Muted"))
        self.do_search(text)

    def do_search(self, text: str):
        hits = self.app.services.search.search(text)
        self.table.set_items(hits, keep_selection=False)
        self.summary.setText(f"{len(hits)} result(s)" if text.strip() else "")
        if hits:
            self.table.selectRow(0)

    def open_hit(self, hit):
        if hit.entity_type == "MOVEMENT":
            from wms.ui.pages.movements import MovementDetailDialog
            MovementDetailDialog(self.app, self.app.services.stock.get_movement(hit.entity_id), self).exec()
            return
        self.app.open_entity(hit.entity_type, hit.entity_id, self)


class MainWindow(QMainWindow):
    def __init__(self, app, on_logout=None):
        super().__init__()
        self.app = app
        self.on_logout = on_logout
        self.setWindowTitle(f"{APP_NAME} - {app.ctx.config.company_name}")
        self.resize(1440, 900)
        self.pages: dict[str, QWidget] = {}
        self.factories = {}

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        # Sidebar
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 12)
        sl.setSpacing(0)
        sl.addWidget(label(APP_NAME, "AppTitle"))
        sl.addWidget(label(app.ctx.config.company_name, "AppSubtitle"))
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        last_section = None
        for section, key, text, perm, factory in _nav():
            if perm and not app.can(perm):
                continue
            if section and section != last_section:
                head = QListWidgetItem(section)
                head.setFlags(Qt.ItemFlag.NoItemFlags)
                head.setForeground(Qt.GlobalColor.gray)
                f = head.font()
                f.setPointSize(8)
                f.setBold(True)
                head.setFont(f)
                self.nav.addItem(head)
                last_section = section
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
            self.factories[key] = factory
        self.nav.currentItemChanged.connect(self._nav_changed)
        sl.addWidget(self.nav, 1)
        sl.addWidget(label(f"v{__version__}", "AppSubtitle"))
        rl.addWidget(side)

        # Main column
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        top = QWidget()
        top.setObjectName("TopBar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(24, 10, 24, 10)
        self.search = SearchBox("Search everything (Ctrl+K): product, code, supplier, DN number, site, bin, ref...")
        self.search.setObjectName("GlobalSearch")
        self.search.returnPressed.connect(self.global_search)
        tl.addWidget(self.search, 1)
        tl.addSpacing(16)
        self.user_label = label(f"{app.user.full_name}  ·  {app.user.role.name}", "Muted")
        tl.addWidget(self.user_label)
        tl.addWidget(button("Change password", self.change_password))
        tl.addWidget(button("Log out", self.logout))
        ml.addWidget(top)
        self.stack = QStackedWidget()
        ml.addWidget(self.stack, 1)
        rl.addWidget(main, 1)
        self.setCentralWidget(root)

        self.statusBar().showMessage("Ready")
        QShortcut(QKeySequence("Ctrl+K"), self, lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence("F5"), self, self.refresh_current)
        app.data_changed.connect(self._data_changed)
        self.go("dashboard")

    # -- navigation ---------------------------------------------------------
    def go(self, key: str):
        for i in range(self.nav.count()):
            if self.nav.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.nav.setCurrentRow(i)
                return

    def page(self, key: str):
        """The page for ``key`` once it has been opened (see ``go``)."""
        return self.pages.get(key)

    def _nav_changed(self, item, _prev):
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            return
        page = self.pages.get(key)
        if page is None:
            page = self.factories[key](self.app)
            page.main_window = self
            self.pages[key] = page
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        try:
            page.on_show()
        except Exception:  # noqa: BLE001
            log.exception("Page refresh failed")
        self.statusBar().showMessage(f"{item.text()}  -  {datetime.now():%d/%m/%Y %H:%M}")

    def _data_changed(self):
        for p in self.pages.values():
            p.dirty = True
        self.refresh_current()

    def refresh_current(self):
        page = self.stack.currentWidget()
        if page is not None:
            page.dirty = True
            page.on_show()

    def global_search(self):
        text = self.search.text().strip()
        if len(text) < 2:
            return
        SearchResultsDialog(self.app, text, self).exec()
        self._data_changed()

    def change_password(self):
        from wms.ui.login import ChangePasswordDialog
        ChangePasswordDialog(self.app.services, parent=self).exec()

    def logout(self):
        if self.on_logout:
            self.on_logout(self)

    def closeEvent(self, event):  # noqa: N802
        cfg = self.app.ctx.config
        if cfg.auto_backup_on_exit and cfg.is_sqlite and not self.app.restart_required:
            try:
                path = self.app.services.backup.create(note="Automatic backup on exit", automatic=True)
                log.info("Automatic backup written to %s", path)
            except Exception:  # noqa: BLE001 - never block closing the app
                log.exception("Automatic backup failed")
        super().closeEvent(event)
