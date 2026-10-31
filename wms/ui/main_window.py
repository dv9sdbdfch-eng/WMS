"""Main window: sidebar navigation, global search, page stack."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from wms import APP_NAME, __version__
from wms.core.permissions import P
from wms.ui.widgets.common import button, label

log = logging.getLogger(__name__)


ACTION = "action:"  # sidebar entries that run something instead of opening a screen
#: groups shown folded until opened (spec 3: less frequently used groups are collapsible)
FOLDED_BY_DEFAULT = {"MORE", "ADMIN"}


def _nav():
    """(section, key, label, permission, page factory)."""
    from wms.ui.pages import (
        dashboard,
        delivery_notes,
        discrepancies,
        finished,
        goods_in,
        goods_out,
        locations,
        low_stock,
        materials,
        movements,
        purchase_orders,
        reports,
        reservations,
        returns,
        search,
        settings,
        sites,
        stocktake,
        users,
    )
    return [
        ("HOME", "dashboard", "Dashboard", None, dashboard.DashboardPage),
        ("HOME", "search", "Search", None, search.SearchPage),
        ("STOCK", "products", "Materials", P.PRODUCTS_VIEW, materials.MaterialsPage),
        ("STOCK", "finished", "Finished Products", P.PRODUCTS_VIEW, finished.FinishedPage),
        ("STOCK", "locations", "Locations & Bins", P.LOCATIONS_VIEW, locations.LocationsPage),
        ("OPERATIONS", "goods_in", "Goods In", P.GOODS_IN_POST, goods_in.GoodsInPage),
        ("OPERATIONS", "goods_out", "Goods Out", P.GOODS_OUT_VIEW, goods_out.GoodsOutPage),
        ("OPERATIONS", ACTION + "transfer", "Transfer", P.STOCK_TRANSFER, None),
        ("OPERATIONS", "returns", "Returns", P.GOODS_OUT_VIEW, returns.ReturnsPage),
        ("PROJECTS", "sites", "Projects", P.SITES_VIEW, sites.SitesPage),
        ("PROJECTS", "reservations", "Reservations", P.STOCK_VIEW, reservations.ReservationsPage),
        ("PURCHASING", "purchase_orders", "Purchase Orders", P.PURCHASING_VIEW, purchase_orders.PurchaseOrdersPage),
        ("PURCHASING", "delivery_notes", "Deliveries", P.GOODS_IN_VIEW, delivery_notes.DeliveryNotesPage),
        ("PURCHASING", "discrepancies", "Delivery Problems", P.PURCHASING_VIEW, discrepancies.DiscrepanciesPage),
        ("INVENTORY CONTROL", "stocktake", "Stocktake", P.STOCKTAKE_COUNT, stocktake.StocktakePage),
        ("INVENTORY CONTROL", "low_stock", "Low Stock", P.PRODUCTS_VIEW, low_stock.LowStockPage),
        ("MORE", "reports", "Reports", P.REPORTS_VIEW, reports.ReportsPage),
        ("MORE", "movements", "Stock Movements", P.STOCK_VIEW, movements.MovementsPage),
        ("MORE", ACTION + "import_export", "Import / Export", P.PRODUCTS_VIEW, None),
        ("MORE", ACTION + "archive", "Archive", P.PRODUCTS_VIEW, None),
        ("ADMIN", "users", "Users", P.USERS_MANAGE, users.UsersPage),
        ("ADMIN", "audit", "Audit Log", P.AUDIT_VIEW, users.AuditPage),
        ("ADMIN", ACTION + "backup", "Backup", None, None),
        ("ADMIN", "settings", "Settings", None, settings.SettingsPage),
    ]



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
        self.sections: dict[str, QListWidgetItem] = {}
        self.section_items: dict[str, list[QListWidgetItem]] = {}
        folded = self._folded_groups()
        for section, key, text, perm, factory in _nav():
            if perm and not app.can(perm):
                continue
            if key == ACTION + "backup" and not (app.can(P.BACKUP_RUN) or app.can(P.BACKUP_RESTORE)):
                continue
            if section not in self.sections:
                head = QListWidgetItem(section)
                head.setFlags(Qt.ItemFlag.ItemIsEnabled)  # clickable (fold / unfold), never "selected"
                head.setData(Qt.ItemDataRole.UserRole + 1, section)
                f = head.font()
                f.setPointSize(8)
                f.setBold(True)
                head.setFont(f)
                head.setForeground(QColor("#8FA6BF"))
                self.nav.addItem(head)
                self.sections[section] = head
                self.section_items[section] = []
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
            self.section_items[section].append(item)
            if factory is not None:
                self.factories[key] = factory
        for section in self.sections:
            self._set_folded(section, section in folded, save=False)
        self.nav.itemClicked.connect(self._nav_clicked)
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
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search everything (Ctrl+K): code, barcode, name, supplier, DN number, "
                                       "project, bin, reference...")
        self.search.setClearButtonEnabled(True)
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

        app.main_window = self
        QShortcut(QKeySequence("Ctrl+K"), self, self.focus_search)
        QShortcut(QKeySequence("Ctrl+F"), self, self.focus_search)
        QShortcut(QKeySequence("F5"), self, self.refresh_current)
        # Warehouse keys (spec 16 / 39.39): the three everyday operations without the mouse.
        hints = []
        for key, page, text in (("F2", "goods_in", "Goods In"), ("F3", "goods_out", "Goods Out")):
            if page in self.factories:
                QShortcut(QKeySequence(key), self, lambda p=page: self.go(p))
                hints.append(f"{key} {text}")
        if app.can(P.STOCK_TRANSFER):
            QShortcut(QKeySequence("F4"), self, self.transfer)
            hints.append("F4 Transfer")
        QShortcut(QKeySequence("Ctrl+D"), self, lambda: self.go("dashboard"))
        hints += ["Ctrl+K Search / scan", "Ctrl+D Dashboard", "F5 Refresh"]
        self.keys_hint = label("   ·   ".join(hints), "Muted")
        self.statusBar().addPermanentWidget(self.keys_hint)
        self.statusBar().showMessage("Ready")
        app.data_changed.connect(self._data_changed)
        self.go("dashboard")

    # -- navigation ---------------------------------------------------------
    def go(self, key: str):
        for section, items in self.section_items.items():
            for item in items:
                if item.data(Qt.ItemDataRole.UserRole) == key:
                    if item.isHidden():
                        self._set_folded(section, False)
                    self.nav.setCurrentItem(item)
                    return

    # -- sidebar groups -----------------------------------------------------
    @staticmethod
    def _qs():
        from PySide6.QtCore import QSettings
        return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")

    def _folded_groups(self) -> set[str]:
        try:
            saved = self._qs().value("sidebar/folded")
        except Exception:  # noqa: BLE001 - a convenience only
            saved = None
        if saved is None:
            return set(FOLDED_BY_DEFAULT)
        return {x for x in str(saved).split(",") if x}

    def is_folded(self, section: str) -> bool:
        items = self.section_items.get(section) or []
        return bool(items) and items[0].isHidden()

    def _set_folded(self, section: str, folded: bool, save: bool = True) -> None:
        for item in self.section_items.get(section, []):
            item.setHidden(folded)
        self.sections[section].setText(f"{'▸' if folded else '▾'}  {section}")
        if save:
            try:
                self._qs().setValue("sidebar/folded",
                                    ",".join(sorted(s for s in self.sections if self.is_folded(s))))
            except Exception:  # noqa: BLE001
                pass

    def toggle_section(self, section: str) -> None:
        self._set_folded(section, not self.is_folded(section))

    def _nav_clicked(self, item) -> None:
        section = item.data(Qt.ItemDataRole.UserRole + 1)
        if section:
            self.toggle_section(section)

    def run_action(self, name: str) -> None:
        """Sidebar entries that are not screens: Transfer (a dialog), Import / Export, Archive, Backup."""
        if name == "transfer":
            self.transfer()
        elif name == "import_export":
            self.go("products")
            page = self.page("products")
            m = QMenu(self)
            if self.app.can(P.PRODUCTS_EDIT):
                m.addAction("Import Materials from Excel...", lambda: page.import_excel())
                if self.app.can(P.SITES_EDIT):
                    m.addAction("Import Project Material List...", lambda: page.import_project_list())
                m.addAction("Download Excel Template...", lambda: page.download_template())
            if self.app.can(P.REPORTS_EXPORT):
                m.addSeparator()
                m.addAction("Export Current View...", lambda: page.export_excel("CURRENT"))
                m.addAction("Export All Materials...", lambda: page.export_excel("ALL"))
            if self.isVisible() and m.actions():
                m.popup(self.nav.mapToGlobal(self.nav.visualItemRect(self.nav.currentItem()).topRight()))
            self.import_export_menu = m
        elif name == "archive":
            self.go("products")
            self.page("products").apply_preset({"status": "ARCHIVED"})
        elif name == "backup":
            self.go("settings")
            self.page("settings").show_tab("Backup and restore")

    def page(self, key: str):
        """The page for ``key`` once it has been opened (see ``go``)."""
        return self.pages.get(key)

    def _nav_changed(self, item, _prev):
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            return
        if key.startswith(ACTION):
            # run it, and keep the screen that was showing selected
            self.nav.blockSignals(True)
            self.nav.setCurrentItem(_prev)
            self.nav.blockSignals(False)
            self.run_action(key[len(ACTION):])
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
        """Enter in the top search box (typed or scanned): open the Search screen with the results."""
        text = self.search.text().strip()
        self.search.clear()
        self.go("search")
        page = self.page("search")
        if page is not None:
            page.search_for(text) if text else page.focus_box()

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        if TransferDialog(self.app, None, self).exec():
            self.app.changed()

    def focus_search(self):
        page = self.page("search")
        if page is not None and self.stack.currentWidget() is page:
            page.focus_box()
        else:
            self.search.setFocus()
            self.search.selectAll()

    def change_password(self):
        from wms.ui.login import ChangePasswordDialog
        ChangePasswordDialog(self.app.services, parent=self).exec()

    def logout(self):
        if self.on_logout:
            self.on_logout(self)

    # -- automatic backups ----------------------------------------------------
    def start_backup_schedule(self) -> None:
        """Backup at start-up if the last good one is older than the interval, then check every hour."""
        if not self.app.ctx.config.auto_backup_hours or not self.app.ctx.config.is_sqlite:
            return
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(60 * 60 * 1000)
        self._backup_timer.timeout.connect(self.scheduled_backup)
        self._backup_timer.start()
        QTimer.singleShot(5000, self.scheduled_backup)

    def scheduled_backup(self):
        """Never silent: a failure shows a warning (and stays on the Dashboard until a backup works)."""
        svc = self.app.services.backup
        try:
            if not svc.is_due():
                return None
            self.statusBar().showMessage("Making the scheduled automatic backup...")
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                path = svc.backup_if_due()
            finally:
                QGuiApplication.restoreOverrideCursor()
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage("Automatic backup FAILED")
            QMessageBox.warning(self, "Automatic backup failed",
                                f"The automatic backup did not work:\n\n{getattr(exc, 'message', exc)}\n\n"
                                f"Your data is not damaged, but it is not protected either. Check the backup "
                                f"folder in Settings > Backup and restore, then press 'Back up now'.")
            self.app.changed()
            return None
        if path is not None:
            self.statusBar().showMessage(f"Automatic backup made: {path.name}  -  {datetime.now():%H:%M}")
            self.app.changed()
        return path

    def closeEvent(self, event):  # noqa: N802
        cfg = self.app.ctx.config
        if cfg.auto_backup_on_exit and cfg.is_sqlite and not self.app.restart_required:
            try:
                path = self.app.services.backup.create(note="Automatic backup on exit", automatic=True)
                log.info("Automatic backup written to %s", path)
            except Exception as exc:  # noqa: BLE001 - never block closing the app, but never hide it either
                log.exception("Automatic backup failed")
                QMessageBox.warning(self, "Automatic backup failed",
                                    f"The backup made when the program closes did not work:\n\n"
                                    f"{getattr(exc, 'message', exc)}\n\nThe program will still close. Next time, "
                                    f"check Settings > Backup and restore.")
        super().closeEvent(event)
