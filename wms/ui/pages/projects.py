"""Project Tree: Manager -> Project -> Unit -> Materials (v1.15).

The tree only shows and edits what is in the database (``ProjectTreeService``). Branches are read
when they are opened, so the screen stays quick with many managers, hundreds of units and
thousands of material lines. Every change goes through the service in one transaction and only
the changed branch is read again.

Editing without extra windows: double-click a name or a quantity to change it in place, use the
panel on the right for the other fields, or the ⋯ menu / right-click / keyboard for actions.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import CLOSED_SITE_STATUSES, OPEN_SITE_STATUSES, SiteStatus
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.services.project_tree import UNASSIGNED, TreeFilter, TreeMatches
from wms.ui.dialogs.project_tree import (
    DeleteManagerDialog,
    DemandDialog,
    DuplicateProjectDialog,
    ManagerDialog,
    MaterialPickerDialog,
    ProjectDialog,
    UnitDialog,
    ask_quantity,
    manager_combo,
    notes_edit,
)
from wms.ui.pages.base import Page
from wms.ui.widgets.common import QTY, Col, DataTable, DecimalEdit, SearchBox, button, confirm, h, info, label, run

C_NAME, C_CODE, C_QTY, C_TOTAL, C_INFO, C_MENU = range(6)
KEY, NODE, FULL, LOADED = (Qt.ItemDataRole.UserRole + i for i in range(4))
UNASSIGNED_KEY = "N:"
#: open the branches leading to search matches automatically up to this many matches
AUTO_EXPAND_MATCHES = 400
MUTED = QColor("#8A94A6")
MATCH = QColor("#FFF3C4")
_SETTINGS_KEY = "project_tree/expanded"


def _qs() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")


def kind_of(key: str | None) -> str:
    return (key or "?:")[0]


def id_of(key: str) -> int | None:
    tail = key.split(":", 1)[1]
    return int(tail) if tail else None


class ProjectsPage(Page):
    title = "Project Tree"
    subtitle = ("Manager → Project → Unit → Materials. Double-click a name or quantity to change it; right-click "
                "or ⋯ for actions. Total required = unit quantity × quantity per unit.")

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.can_edit = app.can(P.SITES_EDIT)
        self.svc = app.services.project_tree
        self.items: dict[str, QTreeWidgetItem] = {}
        self.open_keys: set[str] = self._saved_open_keys()
        self.matches: TreeMatches | None = None
        self._normal_keys: set[str] | None = None
        self.auto_expand = False
        self._loading = False
        self._editing: tuple[QTreeWidgetItem, int] | None = None
        self._skip_refresh = False

        self.add_action(button("Material Demand...", self.show_demand,
                               tooltip="Required materials of every open project, calculated from the units"))
        if app.can(P.SITES_VIEW):
            self.add_link("Project List", "sites", tooltip="The list of projects with customers, sites and stock")
        if self.can_edit:
            self.add_action(button("+  Project", lambda: self.add_project(None)))
            self.add_action(button("+  Manager", self.add_manager, primary=True, shortcut="Ctrl+N"))

        # filters (spec 8)
        self.search = SearchBox("Search manager, project, unit, product code, VPK code or material...")
        self.search.setMinimumWidth(220)
        self.search.search.connect(lambda _t: self.refresh())
        self.manager_filter = QComboBox()
        self.project_filter = QComboBox()
        self.project_filter.setMinimumContentsLength(14)
        self.project_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Open projects", list(OPEN_SITE_STATUSES))
        self.status_filter.addItem("All except archived", [s for s, _t in SiteStatus.choices()
                                                           if s != SiteStatus.ARCHIVED.value])
        self.status_filter.addItem("All projects", None)
        for val, text in SiteStatus.choices():
            self.status_filter.addItem(text, [val])
        for combo in (self.manager_filter, self.project_filter, self.status_filter):
            combo.currentIndexChanged.connect(lambda _i: self.refresh())
        self.body.addLayout(h(self.search, self.manager_filter, self.project_filter, self.status_filter,
                              button("Expand all", self.expand_all, tooltip="Open every manager, project and unit"),
                              button("Collapse all", self.collapse_all)))
        self.body.layout().setStretch(0, 0)

        # tree + side panel
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Structure", "Code / Ref", "Qty", "Total required", "Details", ""])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(22)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        for col, width in ((C_NAME, 270), (C_CODE, 100), (C_QTY, 76), (C_TOTAL, 118), (C_INFO, 200), (C_MENU, 30)):
            self.tree.setColumnWidth(col, width)
        hdr.setSectionResizeMode(C_INFO, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(C_MENU, QHeaderView.ResizeMode.Fixed)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemCollapsed.connect(self._collapsed)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDelegate().closeEditor.connect(lambda *_: QTimer.singleShot(0, self._editor_closed))
        self.tree.currentItemChanged.connect(lambda cur, _prev: self.panel.show_item(cur))
        for keys, slot in (("Ctrl+Up", lambda: self.move_selected(-1)), ("Ctrl+Down", lambda: self.move_selected(1)),
                           ("Delete", self.delete_selected), ("F2", self.rename_selected),
                           ("Insert", self.add_child_selected), ("Menu", self._menu_for_current)):
            QShortcut(QKeySequence(keys), self.tree, slot, context=Qt.ShortcutContext.WidgetShortcut)

        self.panel = NodePanel(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([700, 360])
        split.setChildrenCollapsible(False)
        self.body.addWidget(split, 1)
        self.status = label("", "Muted", wrap=True)
        self.body.addWidget(self.status)

    # ---- loading -------------------------------------------------------------------------------
    def filter(self) -> TreeFilter:
        return TreeFilter(self.manager_filter.currentData(), self.project_filter.currentData(),
                          self.status_filter.currentData())

    def _fill_filter_choices(self) -> None:
        for combo, options, first in (
                (self.manager_filter, [("Unassigned projects", UNASSIGNED)] + self.svc.manager_choices(True),
                 "All managers"),
                (self.project_filter, self.svc.project_choices(), "All projects")):
            old = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first, None)
            for text, val in options:
                combo.addItem(text, val)
            i = combo.findData(old)
            combo.setCurrentIndex(i if i >= 0 else 0)
            combo.blockSignals(False)

    def refresh(self) -> None:
        if self._skip_refresh:
            return
        self._fill_filter_choices()
        selected = self.current_key()
        text = self.search.text().strip()
        flt = self.filter()
        self.matches = self.svc.search(text, flt) if text else None
        self.auto_expand = bool(self.matches) and self.matches.total <= AUTO_EXPAND_MATCHES
        if self.matches is not None and self._normal_keys is None:
            self._normal_keys = set(self.open_keys)  # put back when the search is cleared
        if self.matches is not None:
            self.open_keys = set()
        elif self._normal_keys is not None:
            self.open_keys, self._normal_keys = self._normal_keys, None
        self._loading = True
        self.tree.clear()
        self.items.clear()
        self._loading = False
        m = self.matches
        managers = self.svc.managers(flt)
        if m is not None:
            managers = [x for x in managers if x.id in m.manager_paths or x.id in m.managers]
        elif flt.site_id is not None:
            managers = [x for x in managers if x.projects]
        for node in managers:
            self._add_manager(self.tree.invisibleRootItem(), node)
        if flt.manager_id in (None, UNASSIGNED) and (m is None or m.unassigned_path):
            n = self.svc.unassigned_count(flt)
            if n or flt.manager_id == UNASSIGNED:
                self._add_unassigned(n)
        if m is not None:
            if m.total == 0:
                self.status.setText(f"Nothing matches '{text}'.")
            else:
                more = "" if self.auto_expand else (f" - more than {AUTO_EXPAND_MATCHES}, so branches are not opened "
                                                    f"automatically: open them to see the matches")
                self.status.setText(f"{m.total} match(es): {len(m.managers)} manager(s), {len(m.projects)} project(s), "
                                    f"{len(m.units)} unit(s), {len(m.materials)} material line(s){more}. Matches are "
                                    f"highlighted.")
        else:
            self.status.setText(f"{len(managers)} manager(s). Projects without a manager are under 'Unassigned "
                                f"projects'. Ctrl+Up / Ctrl+Down moves the selected row, Insert adds below it, "
                                f"F2 renames, Delete removes.")
        if selected and selected in self.items:
            self.tree.setCurrentItem(self.items[selected])
        else:
            self.panel.show_item(self.tree.currentItem())

    # item creation ------------------------------------------------------------------------------
    def _new_item(self, parent: QTreeWidgetItem, key: str, node, texts: list[str], children: bool,
                  full: bool) -> QTreeWidgetItem:
        self._loading = True
        item = QTreeWidgetItem(texts + [""] * (5 - len(texts)) + ["⋯"])
        item.setData(0, KEY, key)
        item.setData(0, NODE, node)
        item.setData(0, FULL, full)
        item.setData(0, LOADED, False)
        item.setTextAlignment(C_QTY, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setTextAlignment(C_TOTAL, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setTextAlignment(C_MENU, Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(C_MENU, "Actions")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        if children:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        else:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        parent.addChild(item)
        self.items[key] = item
        self._loading = False
        if self._is_match(key):
            for c in range(6):
                item.setBackground(c, QBrush(MATCH))
        if key in self.open_keys or (self.auto_expand and self._on_match_path(key)):
            item.setExpanded(True)
        return item

    def _is_match(self, key: str) -> bool:
        m = self.matches
        if m is None:
            return False
        k, i = kind_of(key), id_of(key)
        return ((k == "M" and i in m.managers) or (k == "P" and i in m.projects) or (k == "U" and i in m.units)
                or (k == "L" and i in m.materials))

    def _on_match_path(self, key: str) -> bool:
        m = self.matches
        if m is None:
            return False
        k, i = kind_of(key), id_of(key)
        return ((k == "M" and i in m.manager_paths) or (k == "N" and m.unassigned_path)
                or (k == "P" and i in m.project_paths) or (k == "U" and i in m.unit_paths))

    def _style(self, item: QTreeWidgetItem, bold: bool = False, muted: bool = False) -> None:
        f = item.font(C_NAME)
        f.setBold(bold)
        item.setFont(C_NAME, f)
        for c in range(5):
            item.setForeground(c, QBrush(MUTED) if muted else QBrush())

    def _manager_texts(self, node) -> list[str]:
        info_ = f"{node.projects} project{'' if node.projects == 1 else 's'}"
        return [node.name, "", "", "", info_ + ("" if node.is_active else "  ·  inactive")]

    def _add_manager(self, parent, node) -> QTreeWidgetItem:
        full = self.matches is None or node.id in self.matches.managers
        item = self._new_item(parent, f"M:{node.id}", node, self._manager_texts(node), node.projects > 0, full)
        self._style(item, bold=True, muted=not node.is_active)
        if node.notes:
            item.setToolTip(C_NAME, node.notes)
        return item

    def _add_unassigned(self, n: int) -> QTreeWidgetItem:
        item = self._new_item(self.tree.invisibleRootItem(), UNASSIGNED_KEY, n,
                              ["Unassigned projects", "", "", "", f"{n} project{'' if n == 1 else 's'} without a manager"],
                              n > 0, self.matches is None)
        f = item.font(C_NAME)
        f.setItalic(True)
        item.setFont(C_NAME, f)
        return item

    def _project_texts(self, node) -> list[str]:
        return [node.name, node.code, "", "", f"{node.status_label}  ·  {node.units} unit{'' if node.units == 1 else 's'}"]

    def _add_project(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"P:{node.id}", node, self._project_texts(node), node.units > 0, full)
        f = item.font(C_NAME)
        f.setWeight(QFont.Weight.DemiBold)
        item.setFont(C_NAME, f)
        for c in range(5):
            item.setForeground(c, QBrush(MUTED) if node.status in CLOSED_SITE_STATUSES else QBrush())
        if node.notes:
            item.setToolTip(C_NAME, node.notes)
        return item

    def _unit_texts(self, node) -> list[str]:
        n = node.materials
        return [node.name, node.code or "", fmt_qty(node.quantity), "", f"{n} material{'' if n == 1 else 's'}"]

    def _add_unit(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"U:{node.id}", node, self._unit_texts(node), node.materials > 0, full)
        item.setToolTip(C_QTY, "How many of this unit the project needs (double-click to change)")
        if node.notes:
            item.setToolTip(C_NAME, node.notes)
        return item

    def _material_texts(self, node) -> list[str]:
        details = [x for x in (node.brand, f"VPK {node.vpk_code}" if node.vpk_code else None, node.notes) if x]
        return [node.name, node.code, f"{fmt_qty(node.quantity_per_unit)} / unit", f"{fmt_qty(node.total)} {node.uom}",
                "  ·  ".join(details)]

    def _add_material(self, parent, node) -> QTreeWidgetItem:
        item = self._new_item(parent, f"L:{node.id}", node, self._material_texts(node), False, True)
        item.setToolTip(C_TOTAL, f"{fmt_qty(node.unit_quantity)} units × {fmt_qty(node.quantity_per_unit)} per unit")
        item.setToolTip(C_QTY, "Quantity one unit needs (double-click to change)")
        if not node.product_active:
            self._style(item, muted=True)
            item.setToolTip(C_NAME, "This material has been removed from active use")
        return item

    # lazy branches ------------------------------------------------------------------------------
    def _expanded(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, KEY)
        if key is None:
            return
        if key not in self.open_keys:
            self.open_keys.add(key)
            self._save_open_keys()
        if not item.data(0, LOADED):
            self._load_children(item)

    def _collapsed(self, item: QTreeWidgetItem) -> None:
        self.open_keys.discard(item.data(0, KEY))
        self._save_open_keys()

    def _load_children(self, item: QTreeWidgetItem) -> None:
        key, full = item.data(0, KEY), bool(item.data(0, FULL))
        k, oid = kind_of(key), id_of(key)
        m = self.matches if not full else None
        item.setData(0, LOADED, True)
        flt = self.filter()
        if k in ("M", "N"):
            ids = None if m is None else (m.projects | m.project_paths)
            rows = run(self, self.svc.projects, UNASSIGNED if k == "N" else oid, flt, ids) or []
            for node in rows:
                self._add_project(item, node, full or (self.matches is not None and node.id in self.matches.projects))
        elif k == "P":
            ids = None if m is None else (m.units | m.unit_paths)
            for node in run(self, self.svc.units, oid, ids) or []:
                self._add_unit(item, node, full or (self.matches is not None and node.id in self.matches.units))
        elif k == "U":
            ids = None if m is None else m.materials
            for node in run(self, self.svc.materials, oid, ids) or []:
                self._add_material(item, node)
        if item.childCount() == 0:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)

    def _forget(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            self._forget(child)
            if self.items.get(child.data(0, KEY)) is child:
                self.items.pop(child.data(0, KEY))

    def reload_item(self, key: str, children: bool = True) -> QTreeWidgetItem | None:
        """Read one row again (its counts) and, if it was opened, its children."""
        item = self.items.get(key)
        if item is None:
            return None
        k, oid = kind_of(key), id_of(key)
        node = None
        if k == "M":
            found = [x for x in self.svc.managers(self.filter()) if x.id == oid]
            node = found[0] if found else None
            texts = self._manager_texts(node) if node else None
            has = bool(node and node.projects)
        elif k == "N":
            node = self.svc.unassigned_count(self.filter())
            texts = ["Unassigned projects", "", "", "", f"{node} project{'' if node == 1 else 's'} without a manager"]
            has = node > 0
        elif k == "P":
            node = self.svc.project(oid)
            texts, has = self._project_texts(node), node.units > 0
        elif k == "U":
            node = self.svc.unit(oid)
            texts, has = self._unit_texts(node), node.materials > 0
        else:
            node = self.svc.material(oid)
            texts, has = self._material_texts(node), False
        if node is None:
            return None
        self._loading = True
        item.setData(0, NODE, node)
        for c, t in enumerate(texts):
            item.setText(c, t)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator if has
                                     else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        self._loading = False
        if children and item.data(0, LOADED):
            self._forget(item)
            item.takeChildren()
            self._load_children(item)
        if item is self.tree.currentItem():
            self.panel.show_item(item)
        return item

    # ---- selection helpers ---------------------------------------------------------------------
    def current_key(self) -> str | None:
        item = self.tree.currentItem()
        return item.data(0, KEY) if item is not None else None

    def current_node(self):
        item = self.tree.currentItem()
        return item.data(0, NODE) if item is not None else None

    def select(self, key: str) -> None:
        item = self.items.get(key)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def _parent_key(self, key: str) -> str | None:
        item = self.items.get(key)
        if item is None or item.parent() is None:
            return None
        return item.parent().data(0, KEY)

    def _changed(self) -> None:
        """Tell the other screens (stock, project list...) without rebuilding this tree."""
        self._skip_refresh = True
        try:
            self.app.changed()
        finally:
            self._skip_refresh = False

    def _refresh_keys(self, *keys: str | None, select: str | None = None) -> None:
        for key in keys:
            if key:
                self.reload_item(key)
        if select:
            parent = self._parent_key(select) if select in self.items else None
            if parent and not self.items[parent].isExpanded():
                self.items[parent].setExpanded(True)
            self.select(select)
        self._changed()

    def expand_all(self) -> None:
        """Open every manager, project and unit (materials are the last level)."""
        def walk(item):
            if kind_of(item.data(0, KEY)) in ("M", "N", "P", "U"):
                item.setExpanded(True)
                for i in range(item.childCount()):
                    walk(item.child(i))
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

    def collapse_all(self) -> None:
        self.tree.collapseAll()
        self.open_keys.clear()
        self._save_open_keys()

    def _saved_open_keys(self) -> set[str]:
        try:
            raw = _qs().value(_SETTINGS_KEY)
        except Exception:  # noqa: BLE001 - a convenience only
            return set()
        return {k for k in str(raw or "").split(",") if k and k[0] in "MNPU"}

    def _save_open_keys(self) -> None:
        if self.matches is not None or self._loading:
            return
        try:
            _qs().setValue(_SETTINGS_KEY, ",".join(sorted(self.open_keys))[:100_000])
        except Exception:  # noqa: BLE001
            pass

    # ---- inline editing ------------------------------------------------------------------------
    def _editable(self, key: str, col: int) -> bool:
        k = kind_of(key)
        return self.can_edit and ((k in ("M", "P", "U") and col == C_NAME) or (k == "U" and col in (C_CODE, C_QTY))
                                  or (k == "L" and col == C_QTY))

    def _double_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        key = item.data(0, KEY)
        if key is None or col == C_MENU:
            return
        if self._editable(key, col):
            self.edit_inline(item, col)
        elif kind_of(key) == "L":
            self.app.open_entity("PRODUCT", item.data(0, NODE).product_id, self)

    def edit_inline(self, item: QTreeWidgetItem, col: int) -> None:
        node = item.data(0, NODE)
        self._loading = True
        if kind_of(item.data(0, KEY)) == "L":
            item.setText(col, format(node.quantity_per_unit.normalize(), "f"))
        elif col == C_QTY:
            item.setText(col, format(node.quantity.normalize(), "f"))
        self._loading = False
        self._editing = (item, col)
        self.tree.editItem(item, col)

    def _editor_closed(self) -> None:
        if self._editing is not None:  # Esc: put the formatted text back
            item, _col = self._editing
            self._editing = None
            key = item.data(0, KEY)
            if key in self.items:
                self.reload_item(key, children=False)

    def _item_changed(self, item: QTreeWidgetItem, col: int) -> None:
        if self._loading or self._editing is None or self._editing[0] is not item:
            return
        self._editing = None
        self.commit_edit(item, col, item.text(col))

    def commit_edit(self, item: QTreeWidgetItem, col: int, text: str) -> bool:
        """Save a value typed into the tree. On an error the old value comes back."""
        key = item.data(0, KEY)
        k, oid = kind_of(key), id_of(key)
        if k == "M":
            ok = run(self, self.svc.save_manager, oid, {"name": text})
        elif k == "P":
            ok = run(self, self.svc.save_project, oid, {"name": text})
        elif k == "U":
            field = {C_NAME: "name", C_CODE: "unit_code", C_QTY: "quantity"}[col]
            ok = run(self, self.svc.save_unit, oid, {field: text})
        else:
            ok = run(self, self.svc.update_material, oid, {"quantity_per_unit": text})
        if ok is None:
            self.reload_item(key, children=False)
            return False
        # quantities change the totals of the materials below / the manager's text on projects
        self.reload_item(key, children=k in ("U",))
        self._changed()
        return True

    # ---- menus ---------------------------------------------------------------------------------
    def _clicked(self, item: QTreeWidgetItem, col: int) -> None:
        if col == C_MENU:
            self.tree.setCurrentItem(item)
            rect = self.tree.visualItemRect(item)
            x = self.tree.header().sectionViewportPosition(C_MENU)
            self.show_menu(item, self.tree.viewport().mapToGlobal(QPoint(x, rect.bottom())))

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.show_menu(item, self.tree.viewport().mapToGlobal(pos))

    def _menu_for_current(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            rect = self.tree.visualItemRect(item)
            self.show_menu(item, self.tree.viewport().mapToGlobal(rect.center()))

    def build_menu(self, item: QTreeWidgetItem) -> QMenu:
        """The actions of one row - they depend on what the row is (spec 9)."""
        key, node = item.data(0, KEY), item.data(0, NODE)
        k = kind_of(key)
        m = QMenu(self)
        e = self.can_edit
        if k == "M":
            if e:
                m.addAction("Add Project...", lambda: self.add_project(node.id))
                m.addAction("Edit...", lambda: self.edit_manager(node))
                m.addSeparator()
                m.addAction("Move Up", lambda: self.move(key, -1))
                m.addAction("Move Down", lambda: self.move(key, 1))
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_manager(node))
        elif k == "N":
            if e:
                m.addAction("Add Project...", lambda: self.add_project(None))
        elif k == "P":
            ro = node.read_only
            if e and not ro:
                m.addAction("Add Unit...", lambda: self.add_unit(node.id))
            if e:
                m.addAction("Edit...", lambda: self.edit_project(node))
                m.addAction("Duplicate...", lambda: self.duplicate_project(node))
                sub = m.addMenu("Change Manager")
                for name, mid in self.svc.manager_choices():
                    act = sub.addAction(name, lambda mid=mid: self.change_manager(node, mid))
                    act.setCheckable(True)
                    act.setChecked(mid == node.manager_id)
                sub.addSeparator()
                sub.addAction("Unassign (no manager)", lambda: self.change_manager(node, None)).setEnabled(
                    node.manager_id is not None)
                if not ro and node.status not in CLOSED_SITE_STATUSES:
                    m.addAction("Update Material List from Units...", lambda: self.apply_demand(node))
            m.addAction("Open Project Record", lambda: self.open_project(node))
            if e:
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_project(node))
        elif k == "U":
            if e:
                m.addAction("Add Material...", lambda: self.add_material(node))
                m.addAction("Edit...", lambda: self.edit_unit(node))
                m.addAction("Change Quantity...", lambda: self.change_unit_quantity(node))
                m.addAction("Duplicate", lambda: self.duplicate_unit(node))
                m.addSeparator()
                m.addAction("Move Up", lambda: self.move(key, -1))
                m.addAction("Move Down", lambda: self.move(key, 1))
                m.addAction("Move to Another Project...", lambda: self.move_unit_to_project(node))
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_unit(node))
        elif k == "L":
            if e:
                m.addAction("Edit Quantity...", lambda: self.edit_material_quantity(node))
                m.addAction("Replace...", lambda: self.replace_material(node))
                m.addAction("Move Up", lambda: self.move(key, -1))
                m.addAction("Move Down", lambda: self.move(key, 1))
            m.addAction("Open Material", lambda: self.app.open_entity("PRODUCT", node.product_id, self))
            if e:
                m.addSeparator()
                m.addAction("Remove", lambda: self.remove_material(node))
        if k in ("M", "N", "P", "U"):
            m.addSeparator()
            m.addAction("Expand Branch", lambda: self._expand_branch(item))
            m.addAction("Collapse Branch", lambda: self._collapse_branch(item))
        return m

    def show_menu(self, item: QTreeWidgetItem, pos) -> None:
        self._menu = self.build_menu(item)
        if self._menu.actions():
            self._menu.popup(pos)

    def _expand_branch(self, item: QTreeWidgetItem) -> None:
        item.setExpanded(True)
        for i in range(item.childCount()):
            child = item.child(i)
            if kind_of(child.data(0, KEY)) in ("P", "U"):
                self._expand_branch(child)

    def _collapse_branch(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            self._collapse_branch(item.child(i))
        item.setExpanded(False)

    # ---- keyboard ------------------------------------------------------------------------------
    def move_selected(self, delta: int) -> None:
        key = self.current_key()
        if key and self.can_edit and kind_of(key) in ("M", "U", "L"):
            self.move(key, delta)

    def delete_selected(self) -> None:
        key, node = self.current_key(), self.current_node()
        if not key or not self.can_edit:
            return
        {"M": self.delete_manager, "P": self.delete_project, "U": self.delete_unit,
         "L": self.remove_material}.get(kind_of(key), lambda _n: None)(node)

    def rename_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None and self._editable(item.data(0, KEY), C_NAME):
            self.edit_inline(item, C_NAME)

    def add_child_selected(self) -> None:
        key, node = self.current_key(), self.current_node()
        if not key or not self.can_edit:
            return
        k = kind_of(key)
        if k == "M":
            self.add_project(node.id)
        elif k == "N":
            self.add_project(None)
        elif k == "P":
            self.add_unit(node.id)
        elif k == "U":
            self.add_material(node)

    # ---- actions: managers ---------------------------------------------------------------------
    def add_manager(self) -> None:
        dlg = ManagerDialog(self.app, None, self)
        if dlg.exec() and dlg.result_value is not None:
            self.refresh()
            self.select(f"M:{dlg.result_value.id}")
            self._changed()

    def edit_manager(self, node) -> None:
        dlg = ManagerDialog(self.app, node, self)
        if dlg.exec() and dlg.result_value is not None:
            self.reload_item(f"M:{node.id}")
            self._changed()

    def delete_manager(self, node) -> None:
        dlg = DeleteManagerDialog(self.app, node, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target = dlg.target_id()
        n = run(self, self.svc.delete_manager, node.id, target)
        if n is None:
            return
        self.refresh()
        info(self, "Manager deleted",
             f"{node.name} was deleted. " + (f"{n} project(s) moved to the chosen manager." if target
                                             else f"{n} project(s) are now under 'Unassigned projects'.")
             + " No project was deleted.")
        self._changed()

    # ---- actions: projects ---------------------------------------------------------------------
    def add_project(self, manager_id: int | None) -> None:
        dlg = ProjectDialog(self.app, None, self, manager_id=manager_id)
        if dlg.exec() and dlg.result_value is not None:
            node = dlg.result_value
            self._after_project_change(node, None)

    def edit_project(self, node) -> None:
        dlg = ProjectDialog(self.app, node, self)
        if dlg.exec() and dlg.result_value is not None:
            self._after_project_change(dlg.result_value, node.manager_id)

    def _manager_key(self, manager_id: int | None) -> str:
        return UNASSIGNED_KEY if manager_id is None else f"M:{manager_id}"

    def _after_project_change(self, node, old_manager: int | None) -> None:
        """Re-read the manager branches involved and select the project."""
        key = f"P:{node.id}"
        new_parent = self._manager_key(node.manager_id)
        if new_parent not in self.items or (UNASSIGNED_KEY not in self.items and node.manager_id is None):
            self.refresh()
        else:
            self.open_keys.add(new_parent)
            if old_manager != node.manager_id:
                self.reload_item(self._manager_key(old_manager))
            self.reload_item(new_parent)
            if not self.items[new_parent].isExpanded():
                self.items[new_parent].setExpanded(True)
        self.select(key)
        self._changed()

    def change_manager(self, node, manager_id: int | None) -> None:
        if run(self, self.svc.assign_manager, [node.id], manager_id) is None:
            return
        self._after_project_change(self.svc.project(node.id), node.manager_id)

    def duplicate_project(self, node) -> None:
        dlg = DuplicateProjectDialog(self.app, node, self)
        if dlg.exec() and dlg.result_value is not None:
            self._after_project_change(dlg.result_value, dlg.result_value.manager_id)

    def open_project(self, node) -> None:
        self.app.open_entity("SITE", node.id, self)
        self.dirty = True
        self.refresh()

    def delete_project(self, node) -> None:
        from wms.ui.dialogs.removal import delete_record
        if delete_record(self, self.app.services.sites.removal_plan, self.app.services.sites.remove, node.id):
            self.refresh()
            self._changed()

    def apply_demand(self, node) -> None:
        rows = run(self, self.app.services.project_demand.compare_with_project_list, node.id)
        if rows is None:
            return
        todo = [r for r in rows if r.from_units and not r.in_step]
        if not todo:
            info(self, "Material list", f"The material list of {node.code} already matches its units.")
            return
        lines = "\n".join(f"• {r.code}: {fmt_qty(r.on_list) if r.on_list is not None else 'not on the list'} → "
                          f"{fmt_qty(r.from_units)} {r.uom}" for r in todo[:15])
        more = f"\n... and {len(todo) - 15} more" if len(todo) > 15 else ""
        only_list = sum(1 for r in rows if not r.from_units)
        keep = (f"\n\n{only_list} material(s) on the list but not in any unit are left as they are."
                if only_list else "")
        if not confirm(self, "Update material list from units",
                       f"Set the Required quantity of {len(todo)} material(s) on project {node.code} to what its "
                       f"units need:\n\n{lines}{more}{keep}\n\nRequired feeds Remaining. Stock is not changed.",
                       yes="Update"):
            return
        n = run(self, self.app.services.project_demand.apply_to_project_list, node.id)
        if n is not None:
            info(self, "Material list updated", f"{n} Required quantit{'y' if n == 1 else 'ies'} updated on "
                                                f"{node.code}.")
            self.panel.show_item(self.tree.currentItem())
            self._changed()

    # ---- actions: units ------------------------------------------------------------------------
    def add_unit(self, site_id: int) -> None:
        dlg = UnitDialog(self.app, site_id, None, self)
        if dlg.exec() and dlg.result_value is not None:
            self.open_keys.add(f"P:{site_id}")
            self._refresh_keys(f"P:{site_id}", select=f"U:{dlg.result_value.id}")
            self._expand_to(f"P:{site_id}", f"U:{dlg.result_value.id}")

    def _expand_to(self, parent_key: str, key: str) -> None:
        parent = self.items.get(parent_key)
        if parent is not None and not parent.isExpanded():
            parent.setExpanded(True)
        self.select(key)

    def edit_unit(self, node) -> None:
        dlg = UnitDialog(self.app, node.site_id, node, self)
        if dlg.exec() and dlg.result_value is not None:
            self._refresh_keys(f"U:{node.id}", select=f"U:{node.id}")

    def change_unit_quantity(self, node) -> None:
        text = ask_quantity(self, "Unit quantity", f"How many {node.label} does the project need?", node.quantity)
        if text is not None and run(self, self.svc.save_unit, node.id, {"quantity": text}) is not None:
            self._refresh_keys(f"U:{node.id}", select=f"U:{node.id}")

    def duplicate_unit(self, node) -> None:
        new = run(self, self.svc.duplicate_unit, node.id)
        if new is not None:
            self._refresh_keys(f"P:{node.site_id}", select=f"U:{new.id}")

    def move(self, key: str, delta: int) -> None:
        k, oid = kind_of(key), id_of(key)
        fn = {"M": self.svc.move_manager, "U": self.svc.move_unit, "L": self.svc.move_material}[k]
        if run(self, fn, oid, delta) is None and k != "M":
            return
        if k == "M":
            self.refresh()
            self.select(key)
            return
        self._refresh_keys(self._parent_key(key), select=key)

    def move_unit_to_project(self, node) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Move unit {node.name}")
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Move {node.label} (with its {node.materials} material line(s)) to:", wrap=True))
        combo = QComboBox()
        for text, sid in self.svc.project_choices():
            if sid != node.site_id:
                combo.addItem(text, sid)
        lay.addWidget(combo)
        lay.addLayout(h(None, button("Cancel", dlg.reject), button("Move", dlg.accept, primary=True)))
        if dlg.exec() != QDialog.DialogCode.Accepted or combo.currentData() is None:
            return
        if run(self, self.svc.move_unit_to_project, node.id, combo.currentData()) is not None:
            self._refresh_keys(f"P:{node.site_id}", f"P:{combo.currentData()}")

    def delete_unit(self, node) -> None:
        n = node.materials
        what = (f"Unit {node.label} has {n} material line{'' if n == 1 else 's'}. They will be deleted with it."
                if n else f"Delete unit {node.label}? It has no materials.")
        if not confirm(self, "Delete unit", f"{what}\n\nThis is planning data only: stock, reservations and issued "
                                            f"material are not affected.", yes="Delete unit", danger=True):
            return
        if run(self, self.svc.delete_unit, node.id) is not None:
            self._refresh_keys(f"P:{node.site_id}", select=f"P:{node.site_id}")

    # ---- actions: materials --------------------------------------------------------------------
    def add_material(self, node) -> None:
        dlg = MaterialPickerDialog(self.app, f"Add material to {node.label}", self,
                                   unit_text=f"Pick one or more existing materials (Ctrl+click for several), then the "
                                             f"quantity one {node.label} needs. Total = {fmt_qty(node.quantity)} × "
                                             f"quantity per unit.")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
            return
        ids = run(self, self.svc.add_materials, node.id, [(pid, dlg.qty.text()) for pid in dlg.chosen],
                  dlg.vpk.text(), dlg.notes.text())
        if ids:
            self.open_keys.add(f"U:{node.id}")
            self._refresh_keys(f"U:{node.id}")
            self._expand_to(f"U:{node.id}", f"L:{ids[-1]}")
            self.reload_item(f"P:{node.site_id}", children=False)

    def edit_material_quantity(self, node) -> None:
        text = ask_quantity(self, "Quantity per unit", f"How many {node.uom} of {node.code} {node.name} does one unit "
                                                       f"need?", node.quantity_per_unit)
        if text is not None and run(self, self.svc.update_material, node.id, {"quantity_per_unit": text}):
            self._refresh_keys(f"L:{node.id}", select=f"L:{node.id}")

    def replace_material(self, node) -> None:
        dlg = MaterialPickerDialog(self.app, f"Replace {node.code}", self, multi=False, ask_quantity=False,
                                   unit_text=f"Choose the material that replaces {node.code} {node.name}. The quantity "
                                             f"per unit ({fmt_qty(node.quantity_per_unit)}) and notes stay.")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
            return
        if run(self, self.svc.replace_material, node.id, dlg.chosen[0]) is not None:
            self._refresh_keys(f"L:{node.id}", select=f"L:{node.id}")

    def remove_material(self, node) -> None:
        if not confirm(self, "Remove material", f"Remove {node.code} {node.name} from this unit?\n\nThe material "
                                                f"itself and its stock are not changed.", yes="Remove", danger=True):
            return
        unit_key = self._parent_key(f"L:{node.id}") or f"U:{node.unit_id}"
        if run(self, self.svc.remove_materials, [node.id]) is not None:
            self._refresh_keys(unit_key, select=unit_key)

    def show_demand(self) -> None:
        DemandDialog(self.app, self).exec()


class NodePanel(QStackedWidget):
    """The compact panel next to the tree: the selected row's fields and its calculated materials."""

    def __init__(self, page: ProjectsPage):
        super().__init__()
        self.page = page
        self.app = page.app
        self.svc = page.svc
        self.setMinimumWidth(330)
        self.setMaximumWidth(460)
        self.node = None
        self.empty = label("Select a manager, project, unit or material to see and change its details here.\n\n"
                           "Right-click a row (or click ⋯) for its actions.", "Muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.addWidget(self.empty)
        self.pages = {"M": self._manager_page(), "P": self._project_page(), "U": self._unit_page(),
                      "L": self._material_page()}
        for w in self.pages.values():
            self.addWidget(w)

    @staticmethod
    def _box(title: str) -> tuple[QWidget, QVBoxLayout, QFormLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 0, 0, 0)
        lay.addWidget(label(title, "SectionTitle"))
        form = QFormLayout()
        lay.addLayout(form)
        return w, lay, form

    def _manager_page(self) -> QWidget:
        w, lay, form = self._box("Manager")
        self.m_name, self.m_email, self.m_phone = QLineEdit(), QLineEdit(), QLineEdit()
        self.m_active = QCheckBox("Active")
        self.m_notes = notes_edit(height=48)
        for text, e in (("Name *", self.m_name), ("E-mail", self.m_email), ("Phone", self.m_phone),
                        ("", self.m_active), ("Notes", self.m_notes)):
            form.addRow(text, e)
        self.m_save = button("Save", self.save_manager, primary=True)
        lay.addLayout(h(None, self.m_save))
        self.m_info = label("", "Muted", wrap=True)
        lay.addWidget(self.m_info)
        self.m_table = self._demand_table()
        lay.addWidget(self.m_table, 1)
        return w

    def _project_page(self) -> QWidget:
        w, lay, form = self._box("Project")
        self.p_code, self.p_name = QLineEdit(), QLineEdit()
        self.p_manager = QComboBox()
        self.p_status = QComboBox()
        for val, text in SiteStatus.choices():
            self.p_status.addItem(text, val)
        self.p_notes = notes_edit(height=48)
        for text, e in (("Code / ref *", self.p_code), ("Name *", self.p_name), ("Manager", self.p_manager),
                        ("Status", self.p_status), ("Notes", self.p_notes)):
            form.addRow(text, e)
        self.p_save = button("Save", self.save_project, primary=True)
        self.p_apply = button("Update Material List", lambda: self.page.apply_demand(self.node),
                              tooltip="Set the project's Required quantities (used by Remaining) to what its "
                                      "units need")
        lay.addLayout(h(button("Open Record", lambda: self.page.open_project(self.node)), None, self.p_save))
        self.p_info = label("", "Muted", wrap=True)
        lay.addWidget(self.p_info)
        self.p_table = DataTable([Col("Code", lambda r: r.code, width=84),
                                  Col("Material", lambda r: r.name, width=104),
                                  Col("Need", lambda r: r.from_units, QTY, 58),
                                  Col("On list", lambda r: r.on_list, QTY, 58),
                                  Col("Unit", lambda r: r.uom)])
        self.p_table.setToolTip("Need = what the units need (unit qty x qty per unit). On list = Required on the "
                                "project's material list, which Remaining uses. Rows that differ are highlighted.")
        self.p_table.set_row_style(lambda r: None if r.in_step else "low")
        self.p_table.activated_item.connect(lambda r: self.app.open_entity("PRODUCT", r.product_id, self))
        lay.addWidget(self.p_table, 1)
        lay.addLayout(h(None, self.p_apply))
        return w

    def _unit_page(self) -> QWidget:
        w, lay, form = self._box("Unit")
        self.u_name, self.u_code = QLineEdit(), QLineEdit()
        self.u_qty = DecimalEdit()
        self.u_notes = notes_edit(height=48)
        for text, e in (("Name *", self.u_name), ("Code / ref", self.u_code), ("Quantity *", self.u_qty),
                        ("Notes", self.u_notes)):
            form.addRow(text, e)
        self.u_save = button("Save", self.save_unit, primary=True)
        lay.addLayout(h(None, self.u_save))
        self.u_info = label("", "Muted", wrap=True)
        lay.addWidget(self.u_info)
        self.u_table = self._demand_table()
        lay.addWidget(self.u_table, 1)
        return w

    def _material_page(self) -> QWidget:
        w, lay, form = self._box("Material in unit")
        self.l_title = label("", wrap=True)
        lay.insertWidget(1, self.l_title)
        self.l_qty = DecimalEdit()
        self.l_vpk = QLineEdit()
        self.l_notes = notes_edit(height=48)
        for text, e in (("Qty per unit *", self.l_qty), ("VPK code", self.l_vpk), ("Notes", self.l_notes)):
            form.addRow(text, e)
        self.l_total = label("", wrap=True)
        lay.addWidget(self.l_total)
        self.l_save = button("Save", self.save_material, primary=True)
        self.l_replace = button("Replace...", lambda: self.page.replace_material(self.node))
        self.l_remove = button("Remove", lambda: self.page.remove_material(self.node), danger=True)
        lay.addLayout(h(button("Open Material", lambda: self.app.open_entity("PRODUCT", self.node.product_id, self)),
                        None, self.l_save))
        lay.addLayout(h(self.l_replace, None, self.l_remove))
        lay.addStretch(1)
        return w

    def _demand_table(self) -> DataTable:
        t = DataTable([Col("Code", lambda d: d.code, width=90), Col("Material", lambda d: d.name, width=140),
                       Col("Required", lambda d: d.required, QTY, 75), Col("Unit", lambda d: d.uom)])
        t.activated_item.connect(lambda d: self.app.open_entity("PRODUCT", d.product_id, self))
        return t

    # ---- showing --------------------------------------------------------------------------------
    def show_item(self, item: QTreeWidgetItem | None) -> None:
        key = item.data(0, KEY) if item is not None else None
        k = kind_of(key)
        if key is None or k not in self.pages:
            self.node = None
            self.setCurrentWidget(self.empty)
            return
        self.node = item.data(0, NODE)
        getattr(self, f"_show_{k.lower()}")(self.node)
        self.setCurrentWidget(self.pages[k])

    def _enable(self, *widgets, on: bool) -> None:
        for wdg in widgets:
            wdg.setEnabled(on)

    def _show_m(self, n) -> None:
        self.m_name.setText(n.name)
        self.m_email.setText(n.email or "")
        self.m_phone.setText(n.phone or "")
        self.m_active.setChecked(n.is_active)
        self.m_notes.setPlainText(n.notes or "")
        self._enable(self.m_name, self.m_email, self.m_phone, self.m_active, self.m_notes, self.m_save,
                     on=self.page.can_edit)
        rows = run(self, self.app.services.project_demand.for_manager, n.id) or []
        self.m_table.set_items(rows)
        self.m_info.setText(f"{n.projects} project(s) in the current filter. Materials needed by this manager's open "
                            f"projects ({len(rows)}):")

    def _show_p(self, n) -> None:
        self.p_code.setText(n.code)
        self.p_name.setText(n.name)
        self.p_manager.blockSignals(True)
        self.p_manager.clear()
        combo = manager_combo(self.app, n.manager_id)
        for i in range(combo.count()):
            self.p_manager.addItem(combo.itemText(i), combo.itemData(i))
        self.p_manager.setCurrentIndex(max(0, self.p_manager.findData(n.manager_id)))
        self.p_manager.blockSignals(False)
        self.p_status.setCurrentIndex(self.p_status.findData(n.status))
        self.p_notes.setPlainText(n.notes or "")
        self._enable(self.p_code, self.p_name, self.p_manager, self.p_status, self.p_notes, self.p_save,
                     on=self.page.can_edit)
        self.p_apply.setEnabled(self.page.can_edit and n.status not in CLOSED_SITE_STATUSES)
        qty, lines = self.svc.unit_totals(n.id)
        rows = run(self, self.app.services.project_demand.compare_with_project_list, n.id) or []
        self.p_table.set_items(rows)
        off = sum(1 for r in rows if not r.in_step)
        self.p_info.setText(f"{n.units} unit type(s), {fmt_qty(qty)} unit(s), {lines} material line(s). "
                            + (f"{off} material(s) differ from the material list." if off
                               else "Material list matches the units." if rows else ""))

    def _show_u(self, n) -> None:
        self.u_name.setText(n.name)
        self.u_code.setText(n.code or "")
        self.u_qty.set_value(n.quantity)
        self.u_notes.setPlainText(n.notes or "")
        self._enable(self.u_name, self.u_code, self.u_qty, self.u_notes, self.u_save, on=self.page.can_edit)
        rows = run(self, self.app.services.project_demand.for_unit, n.id) or []
        self.u_table.set_items(rows)
        self.u_info.setText(f"Materials for {fmt_qty(n.quantity)} × {n.label} (quantity per unit × "
                            f"{fmt_qty(n.quantity)}):")

    def _show_l(self, n) -> None:
        bits = [f"<b>{n.code}</b> {n.name}"]
        bits += [x for x in (f"Product No. {n.product_no}" if n.product_no else None, n.brand,
                             (n.description or "").replace("\n", " ")[:160] or None) if x]
        self.l_title.setText("<br>".join(bits))
        self.l_qty.set_value(n.quantity_per_unit)
        self.l_vpk.setText(n.vpk_code or "")
        self.l_notes.setPlainText(n.notes or "")
        self.l_total.setText(f"Total required: {fmt_qty(n.unit_quantity)} units × {fmt_qty(n.quantity_per_unit)} "
                             f"= <b>{fmt_qty(n.total)} {n.uom}</b>")
        self._enable(self.l_qty, self.l_vpk, self.l_notes, self.l_save, self.l_replace, self.l_remove,
                     on=self.page.can_edit)

    # ---- saving ---------------------------------------------------------------------------------
    def save_manager(self) -> None:
        n = self.node
        if run(self, self.svc.save_manager, n.id, {"name": self.m_name.text(), "email": self.m_email.text(),
                                                   "phone": self.m_phone.text(), "is_active": self.m_active.isChecked(),
                                                   "notes": self.m_notes.toPlainText()}) is not None:
            self.page.reload_item(f"M:{n.id}")
            self.page._changed()

    def save_project(self) -> None:
        n = self.node
        saved = run(self, self.svc.save_project, n.id, {
            "project_number": self.p_code.text(), "name": self.p_name.text(), "manager_id": self.p_manager.currentData(),
            "status": self.p_status.currentData(), "notes": self.p_notes.toPlainText()}, n.version)
        if saved is not None:
            self.page._after_project_change(saved, n.manager_id)

    def save_unit(self) -> None:
        n = self.node
        if run(self, self.svc.save_unit, n.id, {"name": self.u_name.text(), "unit_code": self.u_code.text(),
                                                "quantity": self.u_qty.text(),
                                                "notes": self.u_notes.toPlainText()}) is not None:
            self.page._refresh_keys(f"U:{n.id}", select=f"U:{n.id}")

    def save_material(self) -> None:
        n = self.node
        if run(self, self.svc.update_material, n.id, {"quantity_per_unit": self.l_qty.text(),
                                                      "vpk_code": self.l_vpk.text(),
                                                      "notes": self.l_notes.toPlainText()}) is not None:
            self.page._refresh_keys(f"L:{n.id}", select=f"L:{n.id}")
