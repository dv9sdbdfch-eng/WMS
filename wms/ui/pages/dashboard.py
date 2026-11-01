"""Dashboard = the home workspace (v1.14): the stock table gets the screen, the summaries fold away.

Top to bottom:

* **Dashboard** (folds; folded by default) - Recent Activity. The header keeps the quiet
  "Last successful backup" line visible while folded. Code, project, bin and document cells are links;
  double-click a row opens the transaction.
* **Action Required (n)** (folds) - one list of problems, zero counts left out, grouped Critical /
  Action / Data quality; every entry opens the screen that shows exactly those records. The number of
  entries stays in the header while folded. With nothing to do it starts folded.
* **Search + filters** and the **Remaining / Stock table** (the Materials list) - takes every pixel
  that is left, so folding a section above or resizing the window gives the table more rows at once.

Folded / unfolded states, the table's columns, widths and sort order are kept on this PC between
sessions. Search text and filters are not: the screen always reopens showing all active stock.
The old TODAY line was removed (spec v1.14 section 1); Goods In / Out lists keep their own date filters.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from wms.core.enums import EntityType
from wms.core.permissions import P
from wms.services.dashboard import PRIORITIES
from wms.services.search import signed_qty
from wms.ui.pages.base import Page
from wms.ui.widgets.collapsible import CollapsibleSection
from wms.ui.widgets.common import DATETIME, QTY, Col, DataTable, label

#: button style per priority (colour is never the only signal: each group has its own heading)
STYLES = {"critical": "AlertCritical", "action": "AlertAction", "data": "AlertData"}
#: QSettings keys of the two folding sections (per PC)
DASH_KEY, ACTION_KEY = "home/dashboard", "home/action_required"
#: Recent Activity is a preview inside a folding section: at most this many rows tall
RECENT_HEIGHT = 196


def _restyle(w, name: str) -> None:
    if w.objectName() != name:
        w.setObjectName(name)
        w.style().unpolish(w)
        w.style().polish(w)


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = ""
    show_header = False  # the "Dashboard" section header is the title
    margins = (16, 8, 16, 8)
    spacing = 0

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        # Goods In / Goods Out / Transfer are in the top bar on every screen (F2 / F3 / F4).
        self.body.setSpacing(6)

        # -- DASHBOARD (folded by default) ------------------------------------------------------
        self.dash_section = CollapsibleSection("Dashboard", DASH_KEY, expanded=False,
                                               tooltip="Recent activity. Click to show or hide.")
        self.backup_line = label("", "Muted")
        self.dash_section.add_header_widget(self.backup_line)
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
        self.dash_section.body.addWidget(label("RECENT ACTIVITY", "AlertGroup"))
        self.dash_section.body.addWidget(self.t_mv)
        self.dash_section.toggled.connect(lambda _on: self._update_hints())
        self.body.addWidget(self.dash_section)

        # -- ACTION REQUIRED (n) --------------------------------------------------------------
        self.action_section = CollapsibleSection("Action Required", ACTION_KEY, expanded=True,
                                                 tooltip="Problems that need someone. Click to show or hide.")
        self.action_title = self.action_section.title_label
        self.action_box = QVBoxLayout()
        self.action_box.setSpacing(4)
        self.action_section.body.addLayout(self.action_box)
        self.nothing = label("Nothing needs attention right now.", "Muted")
        self.action_section.body.addWidget(self.nothing)
        self.action_section.toggled.connect(lambda _on: self._update_hints())
        self.body.addWidget(self.action_section)
        self.tiles: dict[str, QPushButton] = {}  # key -> button, for the entries shown now
        self.group_rows: dict[str, QWidget] = {}
        self._counts: dict[str, int] = {}

        # -- REMAINING / STOCK: search + filters + the table, all the height that is left ---------
        self.stock = None
        if app.can(P.PRODUCTS_VIEW):
            from wms.ui.widgets.materials_view import MaterialsView
            self.stock = MaterialsView(app, self, settings_key="home_stock", compact=True)
            self.body.addWidget(self.stock, 1)
        else:  # no stock rights: Recent Activity may use the room instead
            self.t_mv.setMaximumHeight(16777215)
            self.dash_section.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
            self.body.addStretch(1)
        self._size_recent()
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
            rl.setSpacing(6)
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
        self._counts = {p: sum(1 for a in items if a.priority == p) for p in PRIORITIES}
        severity = "critical" if self._counts["critical"] else "action" if self._counts["action"] else ""
        self.action_section.set_count(len(items), severity)
        # nothing to do: folded; otherwise as the person left it (unfolded the first time)
        saved = self.action_section.saved_state()
        self.action_section.set_expanded(bool(items) and (saved is None or saved), save=False)

        if d.backup is not None:
            # quiet while healthy (spec 14); a problem is already a Critical entry above
            self.backup_line.setText(f"Last successful backup: {d.backup.last_success_text}")
            self.backup_line.setStyleSheet("" if d.backup.ok else "color: #B42318;")
        self.backup_line.setVisible(d.backup is not None)
        self.t_mv.set_items(d.recent_movements)
        self._update_hints()
        if self.stock is not None:
            self.stock.refresh()

    def _update_hints(self) -> None:
        """What a folded section holds, in words, so folding never hides that something is there."""
        if self.action_section.expanded or not self._counts:
            self.action_section.set_hint("")
        else:
            parts = [f"{n} {PRIORITIES[p].lower()}" for p, n in self._counts.items() if n]
            self.action_section.set_hint("  ·  ".join(parts) if parts else "nothing needs attention")
        self.dash_section.set_hint("" if self.dash_section.expanded else "Recent activity")

    def _size_recent(self) -> None:
        if self.stock is not None:
            self.t_mv.setFixedHeight(RECENT_HEIGHT)
