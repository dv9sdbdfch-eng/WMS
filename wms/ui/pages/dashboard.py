"""Dashboard (spec 8-16): what needs my attention, and what happened today.

* ACTION REQUIRED - one list of problems, zero counts left out, grouped Critical / Action /
  Data quality. Every entry opens the screen that shows exactly those records.
* TODAY - one compact line of today's postings; each figure opens its list.
* Backup health - a quiet line while backups are fine; a failed or overdue backup becomes a
  Critical entry instead.
* RECENT ACTIVITY - code, project, bin and document cells are links; double-click a row opens
  the transaction.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from wms.core.enums import EntityType
from wms.services.dashboard import PRIORITIES
from wms.services.search import signed_qty
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DATETIME, QTY, Col, DataTable, label

#: TODAY figures: (name, page, preset). Lists open filtered to today's documents.
TODAY = (("Goods In", "delivery_notes", "today"), ("Goods Out", "goods_out", "today"),
         ("Transfers", "movements", "transfer"), ("Returns", "returns", "today"))
TODAY_PAGES = {name: page for name, page, _ in TODAY}

#: button style per priority (colour is never the only signal: each group has its own heading)
STYLES = {"critical": "AlertCritical", "action": "AlertAction", "data": "AlertData"}


def _restyle(w, name: str) -> None:
    if w.objectName() != name:
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "What needs attention, and what has happened today"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        # Goods In / Goods Out / Transfer are in the top bar on every screen (F2 / F3 / F4).

        # -- ACTION REQUIRED ------------------------------------------------------------------
        self.action_title = label("ACTION REQUIRED", "SectionTitle")
        self.body.addWidget(self.action_title)
        self.action_box = QVBoxLayout()
        self.action_box.setSpacing(6)
        self.body.addLayout(self.action_box)
        self.nothing = label("Nothing needs attention right now.", "Muted")
        self.body.addWidget(self.nothing)
        self.tiles: dict[str, QPushButton] = {}  # key -> button, for the entries shown now
        self.group_rows: dict[str, QWidget] = {}

        # -- TODAY + backup (one line) --------------------------------------------------------
        self.body.addWidget(label("TODAY", "SectionTitle"))
        bar = QFrame()
        bar.setObjectName("TodayBar")
        tl = QHBoxLayout(bar)
        tl.setContentsMargins(12, 6, 12, 6)
        tl.setSpacing(4)
        self.today_tiles: dict[str, QPushButton] = {}
        for i, (name, page, preset) in enumerate(TODAY):
            if i:
                tl.addWidget(label("|", "Muted"))
            b = QPushButton(name)
            b.setObjectName("TodayItem")
            b.setAutoDefault(False)
            b.setToolTip(f"{name} posted today - click to open the list")
            b.clicked.connect(lambda _=False, p=page, k=preset: self.open_today(p, k))
            self.today_tiles[name] = b
            tl.addWidget(b)
        tl.addStretch(1)
        self.backup_line = label("", "Muted")
        tl.addWidget(self.backup_line)
        self.body.addWidget(bar)

        # -- RECENT ACTIVITY ------------------------------------------------------------------
        self.body.addWidget(label("RECENT ACTIVITY", "SectionTitle"))
        self.t_mv = DataTable([
            Col("When", lambda m: m.occurred_at, DATETIME, 128),
            Col("Type", lambda m: m.type_label, width=95),
            Col("Code", lambda m: m.product_code, width=90, link=self.open_product),
            Col("Product", lambda m: m.product_name, width=185),
            Col("Qty", signed_qty, QTY, 55),
            Col("Bin", lambda m: m.to_bin_code or m.from_bin_code, width=190, link=self.open_bin),
            Col("Project / Supplier", lambda m: m.site_label or m.supplier_label, width=195,
                link=self.open_party),
            Col("Document", lambda m: m.doc_number or "", width=95, link=self.open_document),
            Col("By", lambda m: m.user_label)], settings_key="dashboard_recent")
        self.t_mv.activated_item.connect(self._open_movement)
        self.t_mv.setToolTip("Click a code, bin, project or document to open it. Double-click a row for the "
                             "transaction.")
        self.body.addWidget(self.t_mv, 1)
        self.data = None

    # -- actions -------------------------------------------------------------------
    def open_action(self, key: str) -> None:
        """Open the filtered list behind an ACTION REQUIRED entry (spec 12)."""
        a = next((x for x in self.data.attention() if x.key == key), None) if self.data else None
        if a is None and self.data:
            a = self.data.action(key)
        if a is None:
            return
        mw = self.main_window
        mw.go(a.page)
        page = mw.page(a.page)
        if page is None:
            return
        if a.page == "search":
            from wms.services.search import SearchCriteria
            page.apply_criteria(SearchCriteria.from_dict(a.preset or {}))
        elif a.key == "backup" and hasattr(page, "show_tab"):
            page.show_tab("Backup and restore")
        elif a.preset and hasattr(page, "apply_preset"):
            page.apply_preset(a.preset)

    def open_today(self, page_key: str, kind: str) -> None:
        mw = self.main_window
        mw.go(page_key)
        page = mw.page(page_key)
        if page is None or not hasattr(page, "apply_preset"):
            return
        today = date.today()
        if kind == "transfer":
            page.apply_preset({"mtype": "TRANSFER", "date_from": today, "date_to": today})
        else:
            page.apply_preset({"date_from": today, "date_to": today})

    # -- Recent Activity links (spec 16) ----------------------------------------------
    def open_product(self, m):
        self.app.open_entity(EntityType.PRODUCT.value, m.product_id, self)
        self.app.changed()

    def open_bin(self, m):
        bin_id = m.to_bin_id or m.from_bin_id
        if bin_id:
            self.app.open_entity(EntityType.BIN.value, bin_id, self)

    def open_party(self, m):
        if m.site_id:
            self.app.open_entity(EntityType.SITE.value, m.site_id, self, tab="materials")
        elif m.supplier_id:
            self.app.open_entity(EntityType.SUPPLIER.value, m.supplier_id, self)
        self.app.changed()

    def open_document(self, m):
        self._open_movement(m)

    def _open_movement(self, m):
        from wms.ui.dialogs.product_dialog import open_movement
        open_movement(self.app, m, self)
        self.app.changed()

    # -- refresh -----------------------------------------------------------------------
    def _clear_actions(self) -> None:
        while self.action_box.count():
            item = self.action_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.tiles = {}
        self.group_rows = {}

    def refresh(self):
        d = self.data = self.app.services.dashboard.load()
        self._clear_actions()
        items = d.attention()
        for prio, title in PRIORITIES.items():
            group = [a for a in items if a.priority == prio]
            if not group:
                continue
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            head = label(title.upper(), "AlertGroup")
            head.setFixedWidth(104)
            rl.addWidget(head)
            for a in group:
                text = f"{a.count:,}   {a.label}" if a.count else a.label
                if prio == "critical":
                    text = "!  " + text
                b = QPushButton(text)
                b.setAutoDefault(False)
                b.setToolTip(f"{a.hint}\nClick to open." if a.hint else "Click to open.")
                _restyle(b, STYLES[prio])
                b.clicked.connect(lambda _=False, k=a.key: self.open_action(k))
                self.tiles[a.key] = b
                rl.addWidget(b)
            rl.addStretch(1)
            self.group_rows[prio] = row
            self.action_box.addWidget(row)
        self.nothing.setVisible(not items)

        for name, b in self.today_tiles.items():
            n = d.today.get(name, 0)
            b.setText(f"{name}   {n:,}")
            _restyle(b, "TodayItem" if n else "TodayZero")

        if d.backup is not None:
            # quiet while healthy (spec 14); a problem is already a Critical entry above
            self.backup_line.setText(f"Last successful backup: {d.backup.last_success_text}")
            self.backup_line.setStyleSheet("" if d.backup.ok else "color: #B42318;")
        self.backup_line.setVisible(d.backup is not None)
        self.t_mv.set_items(d.recent_movements)
