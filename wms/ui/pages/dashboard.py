"""Dashboard (spec 27): ACTION REQUIRED first, then TODAY, then RECENT ACTIVITY.

Every counter is a button: click it (or Tab to it and press Enter) to open the list of exactly
those records.
"""

from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton

from wms.core.permissions import P
from wms.services.search import signed_qty
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DATETIME, QTY, Col, DataTable, button, label

#: TODAY counters and the list each one opens.
TODAY_PAGES = {"Goods In": "delivery_notes", "Goods Out": "goods_out", "Transfers": "movements",
               "Returns": "returns"}


def _tile(slot) -> QPushButton:
    b = QPushButton()
    b.setAutoDefault(False)
    b.clicked.connect(lambda _=False: slot())
    return b


def _set_tile(b: QPushButton, count: int, text: str, hint: str, style: str) -> None:
    b.setText(f"{count:,}   {text}")
    b.setToolTip(f"{hint}\nClick to open the list." if hint else "Click to open the list.")
    if b.objectName() != style:
        b.setObjectName(style)
        b.style().unpolish(b)
        b.style().polish(b)


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "What needs action first, then today's work"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.GOODS_IN_POST):
            self.add_action(button("New Goods In", lambda: self.main_window.go("goods_in"), primary=True))
        if app.can(P.GOODS_OUT_POST):
            self.add_action(button("New Goods Out", lambda: self.main_window.go("goods_out"), primary=True))
        if app.can(P.STOCK_TRANSFER):
            self.add_action(button("Transfer", self.transfer))
        # Problems described in words (backup, negative stock...). Double-click opens the screen that fixes it.
        self.t_alerts = DataTable([Col("", lambda a: "!" if a.level == "danger" else "", width=24),
                                   Col("Needs attention", lambda a: a.text)])
        self.t_alerts.set_row_style(lambda a: "low" if a.level == "danger" else None)
        self.t_alerts.activated_item.connect(lambda a: self.main_window.go(a.page))
        self.t_alerts.setMaximumHeight(130)
        self.body.addWidget(self.t_alerts)
        self.backup_line = label("", "Muted")
        self.body.addWidget(self.backup_line)

        self.action_title = label("ACTION REQUIRED", "SectionTitle")
        self.body.addWidget(self.action_title)
        self.action_grid = QGridLayout()
        self.action_grid.setSpacing(10)
        self.body.addLayout(self.action_grid)
        self.tiles: dict[str, QPushButton] = {}

        self.body.addWidget(label("TODAY", "SectionTitle"))
        today = QHBoxLayout()
        today.setSpacing(10)
        self.today_tiles: dict[str, QPushButton] = {}
        for name, page in TODAY_PAGES.items():
            t = _tile(lambda p=page: self.main_window.go(p))
            self.today_tiles[name] = t
            today.addWidget(t)
        self.body.addLayout(today)

        self.body.addWidget(label("RECENT ACTIVITY", "SectionTitle"))
        self.t_mv = DataTable([Col("When", lambda m: m.occurred_at, DATETIME, 125),
                               Col("Type", lambda m: m.type_label, width=170),
                               Col("Code", lambda m: m.product_code, width=100),
                               Col("Product", lambda m: m.product_name, width=240),
                               Col("Qty", signed_qty, QTY, 80),
                               Col("Bin", lambda m: m.to_bin_code or m.from_bin_code, width=150),
                               Col("Project / Supplier", lambda m: m.site_label or m.supplier_label, width=180),
                               Col("Document", lambda m: m.doc_number or "", width=100),
                               Col("By", lambda m: m.user_label)])
        self.t_mv.activated_item.connect(self._open_movement)
        self.body.addWidget(self.t_mv, 1)
        self.data = None

    # -- actions -------------------------------------------------------------------
    def open_action(self, key: str) -> None:
        """Open the filtered list behind an ACTION REQUIRED counter."""
        a = self.data.action(key) if self.data else None
        if a is None:
            return
        mw = self.main_window
        mw.go(a.page)
        page = mw.page(a.page)
        if a.page == "search":
            from wms.services.search import SearchCriteria
            page.apply_criteria(SearchCriteria.from_dict(a.preset or {}))
        elif a.preset and hasattr(page, "apply_preset"):
            page.apply_preset(a.preset)

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        if TransferDialog(self.app, None, self).exec():
            self.app.changed()

    def _open_movement(self, m):
        from wms.ui.dialogs.product_dialog import open_movement
        open_movement(self.app, m, self)
        self.app.changed()

    def refresh(self):
        d = self.data = self.app.services.dashboard.load()
        self.t_alerts.set_items(d.alerts)
        self.t_alerts.setVisible(bool(d.alerts))
        if d.backup is not None:
            self.backup_line.setText(f"Last successful backup: {d.backup.last_success_text}")
            self.backup_line.setStyleSheet("" if d.backup.ok else "color: #B3261E; font-weight: bold;")
        self.backup_line.setVisible(d.backup is not None)

        for i, a in enumerate(d.actions):
            t = self.tiles.get(a.key)
            if t is None:
                t = self.tiles[a.key] = _tile(lambda k=a.key: self.open_action(k))
                self.action_grid.addWidget(t, i // 4, i % 4)
            style = "TileZero" if not a.count else "TileDanger" if a.level == "danger" else "TileWarn"
            _set_tile(t, a.count, a.label, a.hint, style)
        waiting = d.action_count
        self.action_title.setText("ACTION REQUIRED" + ("   -   nothing waiting" if not waiting else ""))
        self.action_title.setVisible(bool(d.actions))

        for name, t in self.today_tiles.items():
            _set_tile(t, d.today.get(name, 0), name, f"{name} posted today", "Tile")
        self.t_mv.set_items(d.recent_movements)
