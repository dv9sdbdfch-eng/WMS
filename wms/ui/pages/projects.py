"""Project Tree: Manager -> Project -> Block -> Level -> Unit -> Materials (v1.15).

From v1.17 every level has its own column (Project Manager, Project, Block, Level, Unit, Material)
and the Project Manager / Project headers set the expansion level (they never sort or filter).
From v1.18 the rows of the selected row's project are painted light green (by project id) and the
details panel on the right can be hidden.

Block and Level are optional: a unit can sit directly in a project or on a level of a block.

The tree only shows and edits what is in the database (``ProjectTreeService``). Branches are read
when they are opened, so the screen stays quick with many managers, hundreds of units and
thousands of material lines. Every change goes through the service in one transaction and only
the changed branch is read again.

Editing without extra windows: double-click a name or a quantity to change it in place, use the
panel on the right for the other fields, or the ⋯ menu / right-click / keyboard for actions.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QPolygonF, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import CLOSED_SITE_STATUSES, OPEN_SITE_STATUSES, SiteStatus
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.services.project_tree import DIRECT, UNASSIGNED, TreeFilter, TreeMatches
from wms.services.unit_norms import NORM_ALL
from wms.ui.dialogs.project_tree import (
    DeleteManagerDialog,
    DemandDialog,
    DuplicateProjectDialog,
    GroupDialog,
    ManagerDialog,
    MaterialPickerDialog,
    ProjectDialog,
    UnitDialog,
    ask_norm_change,
    ask_quantity,
    manager_combo,
    notes_edit,
)
from wms.ui.pages.base import Page
from wms.ui.widgets.common import QTY, Col, DataTable, DecimalEdit, SearchBox, button, confirm, h, info, label, run

#: one column per level of the hierarchy (v1.17), then Details. Since v1.29 the materials of a unit
#: are not rows of the tree: double-clicking a unit opens its Finish Schedule window.
C_MGR, C_PRJ, C_BLK, C_LVL, C_UNIT, C_INFO, C_MENU = range(7)
COLUMNS = ["Project Manager", "Project", "Block", "Level", "Unit", "Details", ""]
#: the column a row's name sits in, by row kind - a project is never shown in the manager column
NAME_COL = {"M": C_MGR, "N": C_MGR, "P": C_PRJ, "B": C_BLK, "V": C_LVL, "U": C_UNIT}
ARROW = 12  # size of the expand / collapse arrow drawn in front of a name
KEY, NODE, FULL, LOADED, PROJECT = (Qt.ItemDataRole.UserRole + i for i in range(5))
#: background of every visible row of the project the selected row belongs to (v1.18)
ACTIVE_PROJECT = QColor("#E8F5E9")
#: frame around the exact cell that was clicked, so it stands out inside the green project
CURRENT_CELL = QColor("#2F6FB2")
_PANEL_KEY = "project_tree/details_hidden"
UNASSIGNED_KEY = "N:"
#: open the branches leading to search matches automatically up to this many matches
AUTO_EXPAND_MATCHES = 400
MUTED = QColor("#8A94A6")
MATCH = QColor("#FFF3C4")
_SETTINGS_KEY = "project_tree/expanded"
#: the two clickable headers (v1.17): expansion levels, never sorting or filtering
MODE_MANAGERS, MODE_PROJECTS = "managers", "projects"
HEADER_MODES = {C_MGR: MODE_MANAGERS, C_PRJ: MODE_PROJECTS}
#: rows that hold other rows: manager, unassigned, project, block, level (V); a unit is the last row
BRANCHES = ("M", "N", "P", "B", "V")


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _qs() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")


def name_col(key: str | None) -> int:
    """Column holding the name of the row with this key."""
    return NAME_COL.get(kind_of(key), C_MGR)


def _arrow_icon(open_: bool | None) -> QIcon:
    """▸ (closed), ▾ (open) or an empty space of the same size (nothing to open)."""
    pm = QPixmap(ARROW, ARROW)
    pm.fill(Qt.GlobalColor.transparent)
    if open_ is not None:
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#5B6576"))
        pts = ([QPointF(2, 3), QPointF(10, 3), QPointF(6, 9)] if open_
               else [QPointF(3, 2), QPointF(9, 6), QPointF(3, 10)])
        p.drawPolygon(QPolygonF(pts))
        p.end()
    return QIcon(pm)


class _ArrowClicks(QObject):
    """A click on the arrow in front of a name opens / closes that row (the arrow sits in the row's
    own column, so Qt's own branch arrows, which live only in the first column, are switched off)."""

    def __init__(self, page: "ProjectsPage"):
        super().__init__(page)
        self.page = page

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt name
        if ev.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return False
        if ev.button() != Qt.MouseButton.LeftButton:
            return False
        tree = self.page.tree
        pos = ev.position().toPoint()
        item = tree.itemAt(pos)
        if item is None or not self.page.has_arrow(item):
            return False
        col = name_col(item.data(0, KEY))
        left = tree.visualRect(tree.indexFromItem(item, col)).left()
        if not left <= pos.x() <= left + ARROW + 8:
            return False
        if ev.type() == QEvent.Type.MouseButtonPress:
            tree.setCurrentItem(item)
            item.setExpanded(not item.isExpanded())
        return True  # a double-click on the arrow must not start editing the name


class _ProjectRowsDelegate(QStyledItemDelegate):
    """Paints the active project's rows light green across the whole width and frames the current
    cell. Membership is the project id stored on each row (``PROJECT``), never its text or place,
    so two projects with the same name are never mixed up. Only visible rows are painted, so rows
    that appear when a branch is opened get the colour by themselves."""

    def __init__(self, page: "ProjectsPage"):
        super().__init__(page.tree)
        self.page = page

    def paint(self, painter, option, index) -> None:
        pid = self.page.active_project
        if pid is not None and index.siblingAtColumn(0).data(PROJECT) == pid:
            painter.fillRect(option.rect, ACTIVE_PROJECT)
        super().paint(painter, option, index)
        if index == self.page.tree.currentIndex():
            painter.save()
            pen = painter.pen()
            pen.setColor(CURRENT_CELL)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(option.rect.adjusted(1, 1, -1, -1))
            painter.restore()


SAVE, DISCARD, CANCEL = "save", "discard", "cancel"


def ask_unsaved(parent) -> str:
    """Unsaved Changes: Save Changes / Discard Changes / Cancel. Returns SAVE, DISCARD or CANCEL."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Unsaved Changes")
    box.setText("You have unsaved changes. What would you like to do?")
    save = box.addButton("Save Changes", QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Discard Changes", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(save)
    box.setEscapeButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    return SAVE if clicked is save else DISCARD if clicked is discard else CANCEL


def kind_of(key: str | None) -> str:
    return (key or "?:")[0]


def id_of(key: str) -> int | None:
    tail = key.split(":", 1)[1]
    return int(tail) if tail else None


class ProjectsPage(Page):
    title = "Projects"
    subtitle = ("Manager → Project → Block → Level → Unit (Block and Level are optional). Click the Project "
                "Manager or Project header to show just managers or all projects. Double-click a unit for its "
                "Finish Schedule (the materials to make it); double-click another name to change it. Required "
                "follows the units by itself. Right-click or ⋯ for actions.")

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
            self.add_link("Project List", "sites", tooltip="All projects as one table: customers, addresses, "
                                                           "status - for searching, sorting and exporting")
        if app.can(P.STOCK_VIEW):
            self.add_link("Reservations", "reservations", tooltip="Stock set aside for projects, not yet issued")
        if self.can_edit:
            self.add_action(button("+  Project", lambda: self.add_project(None)))
            self.add_action(button("+  Manager", self.add_manager, primary=True, shortcut="Ctrl+N"))

        # search + status; the tree is opened with the two expansion buttons (v1.17)
        self.search = SearchBox("Search manager, project, block, level, unit, product code, VPK code or material...")
        self.search.setMinimumWidth(220)
        self.search.search.connect(lambda _t: self._filter_changed())
        self._last_search = ""
        self.status_filter = QComboBox()
        self.status_filter.addItem("Open projects", list(OPEN_SITE_STATUSES))
        self.status_filter.addItem("All except archived", [s for s, _t in SiteStatus.choices()
                                                           if s != SiteStatus.ARCHIVED.value])
        self.status_filter.addItem("All projects", None)
        for val, text in SiteStatus.choices():
            self.status_filter.addItem(text, [val])
        self.status_filter.currentIndexChanged.connect(lambda _i: self._filter_changed())
        self._last_status = 0
        # hide / show the details panel on the right (v1.18): the table takes the whole width
        self.panel_btn = QToolButton()
        self.panel_btn.setAutoRaise(False)
        self.panel_btn.setFixedSize(26, 26)
        self.panel_btn.setCheckable(True)
        self.panel_btn.clicked.connect(lambda checked: self.set_details_visible(not checked))
        bar = h(self.search, self.status_filter)
        bar.setStretch(0, 3)
        bar.addStretch(1)
        if self.can_edit:  # in the search row, not the title row: the title row is full at 1366 px (v1.25)
            bar.addWidget(button("Import from Excel...", self.import_structure,
                                 tooltip="Add projects, blocks, levels, units and unit materials from an Excel "
                                         "workbook (preview first; nothing is deleted)"))
        self.body.addLayout(bar)
        self.body.layout().setStretch(0, 0)

        # tree + side panel
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        # each level has its own column, so no indentation and no arrows in the first column: the
        # arrow is drawn in front of the name, in the row's own column
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(ARROW, ARROW))
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.last_mode: str | None = None
        self._arrows = {state: _arrow_icon(state) for state in (True, False, None)}
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # only the clicked cell is selected (blue + frame); the rest of its row keeps the project
        # colour, so "selected cell" and "active project" are two different looks (v1.18)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(40)
        for col, width in ((C_MGR, 125), (C_PRJ, 150), (C_BLK, 95), (C_LVL, 95), (C_UNIT, 130), (C_INFO, 230),
                           (C_MENU, 30)):
            self.tree.setColumnWidth(col, width)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)  # drag a header edge to resize
        hdr.setSectionResizeMode(C_MENU, QHeaderView.ResizeMode.Fixed)
        # the first two headers are expansion commands (not sorting): manager level / project level
        hdr.setSectionsClickable(True)
        hdr.setHighlightSections(False)
        hdr.sectionClicked.connect(self._header_clicked)
        head = self.tree.headerItem()
        head.setToolTip(C_MGR, "Click: show only the project managers (closes every row)")
        head.setToolTip(C_PRJ, "Click: open every manager to show all projects (blocks, levels, units and "
                               "units stay closed)")
        for col in (C_BLK, C_LVL):
            head.setToolTip(col, f"{COLUMNS[col]} names. Open a row with the arrow in front of its name.")
        head.setToolTip(C_UNIT, "Unit names. Double-click a unit for its Finish Schedule (materials, stock, shortage).")
        self.tree.viewport().installEventFilter(_ArrowClicks(self))
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemCollapsed.connect(self._collapsed)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemChanged.connect(self._item_changed)
        self.active_project: int | None = None
        self._panel_stale = False
        self._reverting = False
        self._rebuilding = False
        self._current_col = 0
        self.tree.setItemDelegate(_ProjectRowsDelegate(self))
        self.tree.itemDelegate().closeEditor.connect(lambda *_: QTimer.singleShot(0, self._editor_closed))
        self.tree.currentItemChanged.connect(self._current_changed)
        for keys, slot in (("Ctrl+Up", lambda: self.move_selected(-1)), ("Ctrl+Down", lambda: self.move_selected(1)),
                           ("Delete", self.delete_selected), ("F2", self.rename_selected),
                           ("Insert", self.add_child_selected), ("Menu", self._menu_for_current)):
            QShortcut(QKeySequence(keys), self.tree, slot, context=Qt.ShortcutContext.WidgetShortcut)

        self.panel = NodePanel(self)
        # the arrow button sits on the right edge of the table, next to the panel it hides
        table = QWidget()
        table_lay = QHBoxLayout(table)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.setSpacing(2)
        table_lay.addWidget(self.tree, 1)
        edge = QVBoxLayout()
        edge.setContentsMargins(0, 0, 0, 0)
        edge.addWidget(self.panel_btn)
        edge.addStretch(1)
        table_lay.addLayout(edge)
        self.table_box = table
        self.split = split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(table)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([700, 360])
        split.setChildrenCollapsible(False)
        self.body.addWidget(split, 1)
        self.set_details_visible(not self._saved_panel_hidden(), save=False)
        self.status = label("", "Muted", wrap=True)
        self.body.addWidget(self.status)

    # ---- loading -------------------------------------------------------------------------------
    def filter(self) -> TreeFilter:
        """Only the project status narrows the tree; managers and projects are reached by opening
        rows (the two expansion buttons) or by the search box."""
        return TreeFilter(None, None, self.status_filter.currentData())

    def refresh(self) -> None:
        if self._skip_refresh:
            return
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
        self._rebuilding = True
        try:
            self.tree.clear()
        finally:
            self._rebuilding = False
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
                                    f"{len(m.blocks)} block(s), {len(m.levels)} level(s), "
                                    f"{len(m.units)} unit(s), {len(m.materials)} material line(s){more}. Matches are "
                                    f"highlighted.")
        else:
            self.status.setText(f"{len(managers)} manager(s). Projects without a manager are under 'Unassigned "
                                f"projects'. Ctrl+Up / Ctrl+Down moves the selected row, Insert adds below it, "
                                f"F2 renames, Delete removes.")
        self._rebuilding = True  # re-selecting the same row must not wipe typed, unsaved panel edits
        try:
            if selected and selected in self.items:
                self.tree.setCurrentItem(self.items[selected])
            elif not self.panel.is_dirty():
                self.panel.show_item(self.tree.currentItem())
        finally:
            self._rebuilding = False

    # item creation ------------------------------------------------------------------------------
    def _new_item(self, parent: QTreeWidgetItem, key: str, node, texts: list[str], children: bool,
                  full: bool) -> QTreeWidgetItem:
        self._loading = True
        item = QTreeWidgetItem([""] * len(COLUMNS))
        item.setData(0, KEY, key)
        self._set_texts(item, texts)
        item.setText(C_MENU, "⋯")
        item.setData(0, NODE, node)
        item.setData(0, FULL, full)
        item.setData(0, LOADED, False)
        # the project this row belongs to (its own id for a project row; None for managers)
        k = kind_of(key)
        item.setData(0, PROJECT, id_of(key) if k == "P" else (None if k in ("M", "N") else parent.data(0, PROJECT)))
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
            for c in range(len(COLUMNS)):
                item.setBackground(c, QBrush(MATCH))
        if key in self.open_keys or (self.auto_expand and self._on_match_path(key)):
            item.setExpanded(True)
        self._update_arrow(item)
        return item

    def _set_texts(self, item: QTreeWidgetItem, texts: list[str]) -> None:
        """``texts`` = [name, details]; the name goes into the row's own column."""
        nc = name_col(item.data(0, KEY))
        item.setText(nc, texts[0])
        item.setToolTip(nc, texts[0])  # long names stay readable
        item.setText(C_INFO, texts[1])
        item.setToolTip(C_INFO, texts[1])

    def has_arrow(self, item: QTreeWidgetItem) -> bool:
        return kind_of(item.data(0, KEY)) in BRANCHES and (
            item.childCount() > 0
            or item.childIndicatorPolicy() == QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)

    def _update_arrow(self, item: QTreeWidgetItem) -> None:
        if kind_of(item.data(0, KEY)) == "U":
            return  # units are the last level: their materials are in the Finish Schedule
        state = item.isExpanded() if self.has_arrow(item) else None
        was = self._loading
        self._loading = True
        item.setIcon(name_col(item.data(0, KEY)), self._arrows[state])
        self._loading = was

    def _update_all_arrows(self) -> None:
        for item in self.items.values():
            self._update_arrow(item)

    def _is_match(self, key: str) -> bool:
        m = self.matches
        if m is None:
            return False
        k, i = kind_of(key), id_of(key)
        return ((k == "M" and i in m.managers) or (k == "P" and i in m.projects) or (k == "B" and i in m.blocks)
                or (k == "V" and i in m.levels) or (k == "U" and (i in m.units or i in m.unit_paths)))

    def _on_match_path(self, key: str) -> bool:
        m = self.matches
        if m is None:
            return False
        k, i = kind_of(key), id_of(key)
        return ((k == "M" and i in m.manager_paths) or (k == "N" and m.unassigned_path)
                or (k == "P" and i in m.project_paths) or (k == "B" and i in m.block_paths)
                or (k == "V" and i in m.level_paths))

    def _style(self, item: QTreeWidgetItem, bold: bool = False, muted: bool = False) -> None:
        nc = name_col(item.data(0, KEY))
        f = item.font(nc)
        f.setBold(bold)
        item.setFont(nc, f)
        for c in range(C_MENU):
            item.setForeground(c, QBrush(MUTED) if muted else QBrush())

    def _manager_texts(self, node) -> list[str]:
        info_ = f"{node.projects} project{'' if node.projects == 1 else 's'}"
        return [node.name, info_ + ("" if node.is_active else "  ·  inactive")]

    def _add_manager(self, parent, node) -> QTreeWidgetItem:
        full = self.matches is None or node.id in self.matches.managers
        item = self._new_item(parent, f"M:{node.id}", node, self._manager_texts(node), node.projects > 0, full)
        self._style(item, bold=True, muted=not node.is_active)
        if node.notes:
            item.setToolTip(C_MGR, f"{node.name}\n{node.notes}")
        return item

    def _add_unassigned(self, n: int) -> QTreeWidgetItem:
        item = self._new_item(self.tree.invisibleRootItem(), UNASSIGNED_KEY, n,
                              ["Unassigned projects", f"{n} project{'' if n == 1 else 's'} without a manager"],
                              n > 0, self.matches is None)
        f = item.font(C_MGR)
        f.setItalic(True)
        item.setFont(C_MGR, f)
        return item

    def _project_texts(self, node) -> list[str]:
        parts = [node.code, node.status_label]
        if node.blocks:
            parts.append(_n(node.blocks, "block"))
        parts.append(_n(node.units, "unit"))
        return [node.name, "  ·  ".join(p for p in parts if p)]

    def _add_project(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"P:{node.id}", node, self._project_texts(node), node.has_children, full)
        f = item.font(C_PRJ)
        f.setWeight(QFont.Weight.DemiBold)
        item.setFont(C_PRJ, f)
        for c in range(C_MENU):
            item.setForeground(c, QBrush(MUTED) if node.status in CLOSED_SITE_STATUSES else QBrush())
        if node.notes:
            item.setToolTip(C_PRJ, f"{node.name}\n{node.notes}")
        return item

    def _block_texts(self, node) -> list[str]:
        return [node.name, "  ·  ".join(p for p in (node.code, _n(node.levels, "level"), _n(node.units, "unit")) if p)]

    def _add_block(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"B:{node.id}", node, self._block_texts(node), node.levels > 0, full)
        f = item.font(C_BLK)
        f.setWeight(QFont.Weight.Medium)
        item.setFont(C_BLK, f)
        if node.notes:
            item.setToolTip(C_BLK, f"{node.name}\n{node.notes}")
        return item

    def _level_texts(self, node) -> list[str]:
        return [node.name, "  ·  ".join(p for p in (node.code, _n(node.units, "unit")) if p)]

    def _add_level(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"V:{node.id}", node, self._level_texts(node), node.units > 0, full)
        if node.notes:
            item.setToolTip(C_LVL, f"{node.name}\n{node.notes}")
        return item

    def _unit_texts(self, node) -> list[str]:
        n = node.materials
        return [node.name, "  ·  ".join(p for p in (node.code, f"× {fmt_qty(node.quantity)}",
                                                     f"{n} material{'' if n == 1 else 's'}") if p)]

    def _add_unit(self, parent, node, full: bool) -> QTreeWidgetItem:
        item = self._new_item(parent, f"U:{node.id}", node, self._unit_texts(node), False, full)
        item.setToolTip(C_UNIT, f"{node.name}\n{node.notes}\n\nDouble-click: Finish Schedule" if node.notes
                        else f"{node.name}\nDouble-click: Finish Schedule")
        return item

    # lazy branches ------------------------------------------------------------------------------
    def _expanded(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, KEY)
        if key is None or kind_of(key) not in BRANCHES:
            return
        if key not in self.open_keys:
            self.open_keys.add(key)
            self._save_open_keys()
        if not item.data(0, LOADED):
            self._load_children(item)
        self._update_arrow(item)

    def _collapsed(self, item: QTreeWidgetItem) -> None:
        self.open_keys.discard(item.data(0, KEY))
        self._save_open_keys()
        self._update_arrow(item)

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
            # blocks first, then the units that sit directly in the project
            ids = None if m is None else (m.blocks | m.block_paths)
            for node in run(self, self.svc.blocks, oid, ids) or []:
                self._add_block(item, node, full or (self.matches is not None and node.id in self.matches.blocks))
            self._load_units(item, oid, DIRECT, m, full)
        elif k == "B":
            ids = None if m is None else (m.levels | m.level_paths)
            for node in run(self, self.svc.levels, oid, ids) or []:
                self._add_level(item, node, full or (self.matches is not None and node.id in self.matches.levels))
        elif k == "V":
            self._load_units(item, None, oid, m, full)
        if item.childCount() == 0:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        self._update_arrow(item)

    def _load_units(self, item: QTreeWidgetItem, site_id, level_id, m: TreeMatches | None, full: bool) -> None:
        ids = None if m is None else (m.units | m.unit_paths)
        for node in run(self, self.svc.units, site_id, ids, level_id) or []:
            self._add_unit(item, node, full or (self.matches is not None and node.id in self.matches.units))

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
            texts, has = self._project_texts(node), node.has_children
        elif k == "B":
            node = self.svc.block(oid)
            texts, has = self._block_texts(node), node.levels > 0
        elif k == "V":
            node = self.svc.level(oid)
            texts, has = self._level_texts(node), node.units > 0
        else:
            node = self.svc.unit(oid)
            texts, has = self._unit_texts(node), False
        if node is None:
            return None
        self._loading = True
        item.setData(0, NODE, node)
        self._set_texts(item, texts)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator if has
                                     else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        self._loading = False
        self._update_arrow(item)
        if children and item.data(0, LOADED):
            self._forget(item)
            item.takeChildren()
            self._load_children(item)
        if item is self.tree.currentItem() and not self.panel.is_dirty():
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
            self.tree.setCurrentItem(item, name_col(key))
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
        """Open every manager, project, block and level (units are the last level)."""
        def walk(item):
            if kind_of(item.data(0, KEY)) in BRANCHES:
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
        self._update_all_arrows()

    # ---- expansion level: the Project Manager / Project headers (v1.17) --------------------------
    def _header_clicked(self, col: int) -> None:
        mode = HEADER_MODES.get(col)
        if mode:
            self.set_expand_mode(mode)

    def expand_mode(self) -> str | None:
        """The header command used last (manual opening of rows does not change it)."""
        return self.last_mode

    def set_expand_mode(self, mode: str) -> None:
        """Reset the tree to one expansion level, whatever was open before. Only rows are opened or
        closed: nothing is written to the database, the search and status filter stay as they are,
        and only the managers' project lists that were never opened are read.

        * ``managers`` (Project Manager header) - every row closed: just the managers.
        * ``projects`` (Project header) - every manager open, every project and anything below closed.

        The selected row stays selected even when its branch is closed."""
        if mode not in (MODE_MANAGERS, MODE_PROJECTS):
            return
        self.last_mode = mode
        selected = self.tree.currentItem()
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.collapseAll()  # closes every level at once, no signal per row
            self.open_keys.clear()
            if mode == MODE_PROJECTS:
                root = self.tree.invisibleRootItem()
                for i in range(root.childCount()):
                    top = root.child(i)
                    if self.has_arrow(top):
                        top.setExpanded(True)  # loads that manager's projects once (lazy)
                        self.open_keys.add(top.data(0, KEY))
            self._save_open_keys()
            self._update_all_arrows()
        finally:
            self.tree.setUpdatesEnabled(True)
        if selected is not None and self.tree.currentItem() is not selected:
            self.tree.setCurrentItem(selected)
        visible = selected
        while visible is not None and visible.parent() is not None and not visible.parent().isExpanded():
            visible = visible.parent()
        if visible is not None:
            self.tree.scrollToItem(visible)

    # ---- active project and the details panel (v1.18) -----------------------------------------
    def _current_changed(self, cur: QTreeWidgetItem | None, prev=None) -> None:
        if self._reverting:
            return
        if self.panel.is_dirty():
            same = cur is not None and cur.data(0, KEY) == self.panel.shown_key
            if self._rebuilding or same:
                self._apply_current(cur, refill=False)  # the edited row again: keep what was typed
                return
            if prev is not None and prev.treeWidget() is self.tree:  # (a deleted row has nothing to keep)
                target = cur.data(0, KEY) if cur is not None else None
                target_col = max(self.tree.currentColumn(), 0)
                self._revert_to(prev)  # stay on the edited row (and cell) while the user decides
                if not self.confirm_unsaved():
                    return
                if target and target in self.items:
                    self.tree.setCurrentItem(self.items[target], target_col)
                elif target is None:
                    self.tree.setCurrentItem(None)
                return
            self.panel.mark_clean()
        self._apply_current(cur)

    def _revert_to(self, item: QTreeWidgetItem) -> None:
        self._reverting = True
        try:
            self.tree.setCurrentItem(item, self._current_col)
        finally:
            self._reverting = False

    def confirm_unsaved(self) -> bool:
        """Ask what to do with unsaved panel edits. True = go on (saved or discarded), False = stay
        (Cancel, or saving failed - the typed values are kept)."""
        if not self.panel.is_dirty():
            return True
        answer = ask_unsaved(self)
        if answer == SAVE:
            return self.panel.save_current()
        if answer == DISCARD:
            self.panel.discard()
            return True
        return False

    def can_leave(self) -> bool:
        """Asked before another screen opens or the program closes."""
        return self.confirm_unsaved()

    def _filter_changed(self) -> None:
        if not self.confirm_unsaved():
            # put the search text / status back as it was, the list stays as it is
            self.search.blockSignals(True)
            self.search.setText(self._last_search)
            self.search.blockSignals(False)
            self.status_filter.blockSignals(True)
            self.status_filter.setCurrentIndex(self._last_status)
            self.status_filter.blockSignals(False)
            return
        self._last_search = self.search.text()
        self._last_status = self.status_filter.currentIndex()
        self.refresh()

    def _apply_current(self, cur: QTreeWidgetItem | None, refill: bool = True) -> None:
        self._current_col = max(self.tree.currentColumn(), 0)
        pid = cur.data(0, PROJECT) if cur is not None else None
        if pid != self.active_project:
            self.active_project = pid
            self.tree.viewport().update()  # repaint the visible rows only; nothing is read again
        if not refill:
            return
        if not self.panel.isHidden():
            self.panel.show_item(cur)
        else:
            self._panel_stale = True  # filled in when the panel is shown again

    def project_rows(self) -> list[QTreeWidgetItem]:
        """Visible rows painted as the active project (what the user sees in green)."""
        pid = self.active_project
        if pid is None:
            return []
        out = []
        for item in self.items.values():
            if item.data(0, PROJECT) != pid or item.isHidden():
                continue
            parent = item.parent()
            while parent is not None and parent.isExpanded():
                parent = parent.parent()
            if parent is None:
                out.append(item)
        return out

    def details_visible(self) -> bool:
        return not self.panel.isHidden()

    def set_details_visible(self, show: bool, save: bool = True) -> None:
        """Hide or show the details panel. Hiding keeps the panel (and anything typed in it) as it
        is; the table takes the freed width. Selecting rows never opens the panel by itself."""
        self.panel.setVisible(show)
        self.panel_btn.setChecked(not show)
        self.panel_btn.setText("◂" if not show else "▸")
        self.panel_btn.setToolTip("Show the details panel" if not show else "Hide the details panel - the table "
                                                                            "uses the whole width")
        if show and self._panel_stale and not self.panel.is_dirty():  # selection changed while hidden
            self._panel_stale = False
            self.panel.show_item(self.tree.currentItem())
        if save:
            try:
                _qs().setValue(_PANEL_KEY, "1" if not show else "0")
            except Exception:  # noqa: BLE001 - a convenience only
                pass

    def _saved_panel_hidden(self) -> bool:
        try:
            return str(_qs().value(_PANEL_KEY) or "0") == "1"
        except Exception:  # noqa: BLE001
            return False

    def _saved_open_keys(self) -> set[str]:
        try:
            raw = _qs().value(_SETTINGS_KEY)
        except Exception:  # noqa: BLE001 - a convenience only
            return set()
        return {k for k in str(raw or "").split(",") if k and k[0] in BRANCHES}

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
        return self.can_edit and k in ("M", "P", "B", "V", "U") and col == name_col(key)

    def _double_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        key = item.data(0, KEY)
        if key is None or col == C_MENU:
            return
        if kind_of(key) == "U":
            self.open_schedule(item.data(0, NODE))  # the unit card: its materials (rename with F2)
        elif self._editable(key, col):
            self.edit_inline(item, col)

    def edit_inline(self, item: QTreeWidgetItem, col: int) -> None:
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
        elif k in ("B", "V"):
            ok = run(self, self.svc.save_block if k == "B" else self.svc.save_level, oid, {"name": text})
        else:
            ok = run(self, self.svc.save_unit, oid, {"name": text})
        if ok is None:
            self.reload_item(key, children=False)
            return False
        self.reload_item(key, children=False)
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

    def project_stock(self, node) -> None:
        from wms.ui.dialogs.reservations import ProjectStockDialog
        ProjectStockDialog(self.app, node.id, self).exec()

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
                m.addAction("Add Block...", lambda: self.add_block(node))
                m.addAction("Add Unit (directly in project)...", lambda: self.add_unit(node.id))
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
                    m.addAction("Reset Required to Units...", lambda: self.apply_demand(node))
            m.addAction("Project Stock / Return to Main Stock...", lambda: self.project_stock(node))
            m.addAction("Open Project Record", lambda: self.open_project(node))
            if e:
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_project(node))
        elif k in ("B", "V"):
            if e:
                if k == "B":
                    m.addAction("Add Level...", lambda: self.add_level(node))
                else:
                    m.addAction("Add Unit...", lambda: self.add_unit(node.site_id, node.id))
                m.addAction("Edit...", lambda: self.edit_group(key, node))
                m.addAction("Duplicate" + (" Level (with units and materials)" if k == "V" else " Block (with levels)"),
                            lambda: self.duplicate_group(key, node))
                m.addSeparator()
                m.addAction("Move Up", lambda: self.move(key, -1))
                m.addAction("Move Down", lambda: self.move(key, 1))
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_group(key, node))
        elif k == "U":
            m.addAction("Finish Schedule...", lambda: self.open_schedule(node))
            if e:
                m.addAction("Add Material...", lambda: self.add_material(node))
                m.addAction("Edit...", lambda: self.edit_unit(node))
                m.addAction("Change Quantity...", lambda: self.change_unit_quantity(node))
                m.addAction("Duplicate", lambda: self.duplicate_unit(node))
                m.addSeparator()
                m.addAction("Move Up", lambda: self.move(key, -1))
                m.addAction("Move Down", lambda: self.move(key, 1))
                m.addAction("Move to...", lambda: self.move_unit_to_project(node))
                m.addSeparator()
                m.addAction("Delete...", lambda: self.delete_unit(node))
        if k in BRANCHES:
            m.addSeparator()
            # by key, not by the row object: the tree may have been rebuilt while the menu was open
            # (another change, the hourly backup), and the old row object is then gone
            m.addAction("Expand Branch", lambda: self._expand_branch(self.items.get(key)))
            m.addAction("Collapse Branch", lambda: self._collapse_branch(self.items.get(key)))
        return m

    def show_menu(self, item: QTreeWidgetItem, pos) -> None:
        self._menu = self.build_menu(item)
        if self._menu.actions():
            self._menu.popup(pos)

    def _expand_branch(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        item.setExpanded(True)
        for i in range(item.childCount()):
            child = item.child(i)
            if kind_of(child.data(0, KEY)) in ("P", "B", "V"):
                self._expand_branch(child)

    def _collapse_branch(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        for i in range(item.childCount()):
            self._collapse_branch(item.child(i))
        item.setExpanded(False)

    # ---- keyboard ------------------------------------------------------------------------------
    def move_selected(self, delta: int) -> None:
        key = self.current_key()
        if key and self.can_edit and kind_of(key) in ("M", "B", "V", "U"):
            self.move(key, delta)

    def delete_selected(self) -> None:
        key, node = self.current_key(), self.current_node()
        if not key or not self.can_edit:
            return
        k = kind_of(key)
        if k in ("B", "V"):
            self.delete_group(key, node)
            return
        {"M": self.delete_manager, "P": self.delete_project, "U": self.delete_unit}.get(k, lambda _n: None)(node)

    def rename_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None and self._editable(item.data(0, KEY), name_col(item.data(0, KEY))):
            self.edit_inline(item, name_col(item.data(0, KEY)))

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
        elif k == "B":
            self.add_level(node)
        elif k == "V":
            self.add_unit(node.site_id, node.id)
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

    def open_project(self, node, tab: str | None = None) -> None:
        self.app.open_entity("SITE", node.id, self, tab=tab)
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
            info(self, "Material list", f"The Required quantities of {node.code} already equal what its units "
                                        f"need (nothing was added by hand on top).")
            return
        lines = "\n".join(f"• {r.code}: {fmt_qty(r.on_list) if r.on_list is not None else 'not on the list'} → "
                          f"{fmt_qty(r.from_units)} {r.uom}" for r in todo[:15])
        more = f"\n... and {len(todo) - 15} more" if len(todo) > 15 else ""
        only_list = sum(1 for r in rows if not r.from_units)
        keep = (f"\n\n{only_list} material(s) on the list but not in any unit are left as they are."
                if only_list else "")
        if not confirm(self, "Reset Required to units",
                       f"Required already follows the units by itself; the differences below were typed or "
                       f"imported by hand. Set the Required quantity of {len(todo)} material(s) on project "
                       f"{node.code} back to exactly what its units need:\n\n{lines}{more}{keep}\n\nRequired "
                       f"feeds Remaining. Stock is not changed.", yes="Reset"):
            return
        n = run(self, self.app.services.project_demand.apply_to_project_list, node.id)
        if n is not None:
            info(self, "Material list updated", f"{n} Required quantit{'y' if n == 1 else 'ies'} updated on "
                                                f"{node.code}.")
            self.panel.show_item(self.tree.currentItem())
            self._changed()

    # ---- actions: units ------------------------------------------------------------------------
    def _place_keys(self, site_id: int, level_id: int | None) -> list[str]:
        """The rows above a unit whose counts change with it: its level, block and project."""
        if not level_id:
            return [f"P:{site_id}"]
        lv = self.svc.level(level_id)
        return [f"V:{level_id}", f"B:{lv.block_id}", f"P:{site_id}"]

    def _refresh_place(self, site_id: int, level_id: int | None, select: str | None = None) -> None:
        """Read again the branch that holds the units of a place and the counts above it."""
        keys = self._place_keys(site_id, level_id)
        for key in reversed(keys[1:]):
            self.reload_item(key, children=False)
        self.reload_item(keys[0])
        if select:
            self._expand_to(keys[0], select)
        self._changed()

    def add_unit(self, site_id: int, level_id: int | None = None) -> None:
        dlg = UnitDialog(self.app, site_id, None, self, level_id=level_id)
        if dlg.exec() and dlg.result_value is not None:
            parent = self._place_keys(site_id, level_id)[0]
            self.open_keys.add(parent)
            self._refresh_place(site_id, level_id, select=f"U:{dlg.result_value.id}")

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
            self._refresh_place(node.site_id, node.level_id, select=f"U:{new.id}")

    def move(self, key: str, delta: int) -> None:
        k, oid = kind_of(key), id_of(key)
        fn = {"M": self.svc.move_manager, "B": self.svc.move_block, "V": self.svc.move_level,
              "U": self.svc.move_unit}[k]
        if run(self, fn, oid, delta) is None and k != "M":
            return
        if k == "M":
            self.refresh()
            self.select(key)
            return
        self._refresh_keys(self._parent_key(key), select=key)

    def move_unit_to_project(self, node) -> None:
        """Move a unit to another level or project (or straight into a project)."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Move unit {node.name}")
        dlg.setMinimumWidth(520)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Move {node.label} (with its {node.materials} material line(s)) to:", wrap=True))
        combo = QComboBox()
        combo.setEditable(True)  # type to find a project / level
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for text, sid, lid in self.svc.unit_places():
            if (sid, lid) != (node.site_id, node.level_id):
                combo.addItem(text, (sid, lid))
        lay.addWidget(combo)
        lay.addLayout(h(None, button("Cancel", dlg.reject), button("Move", dlg.accept, primary=True)))
        if dlg.exec() != QDialog.DialogCode.Accepted or combo.currentData() is None:
            return
        sid, lid = combo.currentData()
        if run(self, self.svc.move_unit_to, node.id, sid, lid) is not None:
            src, dst = self._place_keys(node.site_id, node.level_id), self._place_keys(sid, lid)
            for key in dict.fromkeys(src[1:] + dst[1:]):  # counts above the two places
                if key not in (src[0], dst[0]):
                    self.reload_item(key, children=False)
            for key in dict.fromkeys((src[0], dst[0])):  # the two branches that lost / got the unit
                self.reload_item(key)
            self.select(f"U:{node.id}")
            self._changed()

    def delete_unit(self, node) -> None:
        n = node.materials
        what = (f"Unit {node.label} has {n} material line{'' if n == 1 else 's'}. They will be deleted with it."
                if n else f"Delete unit {node.label}? It has no materials.")
        if not confirm(self, "Delete unit", f"{what}\n\nThis is planning data only: stock, reservations and issued "
                                            f"material are not affected.", yes="Delete unit", danger=True):
            return
        if run(self, self.svc.delete_unit, node.id) is not None:
            parent = node.parent_key
            self._refresh_place(node.site_id, node.level_id)
            self.select(parent)

    # ---- actions: blocks and levels ------------------------------------------------------------
    def add_block(self, project) -> None:
        dlg = GroupDialog(self.app, "block", project.id, None, self, where=project.code)
        if dlg.exec() and dlg.result_value is not None:
            key = f"P:{project.id}"
            self.open_keys.add(key)
            self._refresh_keys(key)
            self._expand_to(key, f"B:{dlg.result_value.id}")

    def add_level(self, block) -> None:
        dlg = GroupDialog(self.app, "level", block.id, None, self, where=block.label)
        if dlg.exec() and dlg.result_value is not None:
            key = f"B:{block.id}"
            self.open_keys.add(key)
            self.reload_item(key)
            self._expand_to(key, f"V:{dlg.result_value.id}")
            self._changed()

    def edit_group(self, key: str, node) -> None:
        kind = "block" if kind_of(key) == "B" else "level"
        parent_id = node.site_id if kind == "block" else node.block_id
        dlg = GroupDialog(self.app, kind, parent_id, node, self)
        if dlg.exec() and dlg.result_value is not None:
            self.reload_item(key, children=False)
            self._changed()

    def duplicate_group(self, key: str, node) -> None:
        if kind_of(key) == "B":
            new = run(self, self.svc.duplicate_block, node.id)
            if new is not None:
                self._refresh_keys(f"P:{node.site_id}", select=f"B:{new.id}")
        else:
            new = run(self, self.svc.duplicate_level, node.id)
            if new is not None:
                self.reload_item(f"P:{node.site_id}", children=False)
                self._refresh_keys(f"B:{node.block_id}", select=f"V:{new.id}")

    def delete_group(self, key: str, node) -> None:
        is_block = kind_of(key) == "B"
        if is_block:
            levels, units, lines = run(self, self.svc.block_contents, node.id) or (0, 0, 0)
            what = f"Block {node.label} has {_n(levels, 'level')}, {_n(units, 'unit')} and {_n(lines, 'material line')}."
        else:
            units, lines = run(self, self.svc.level_contents, node.id) or (0, 0)
            what = f"Level {node.label} has {_n(units, 'unit')} and {_n(lines, 'material line')}."
        empty = not units and not (is_block and levels)
        text = (f"Delete {'block' if is_block else 'level'} {node.label}? It is empty." if empty
                else f"{what} They will all be deleted with it.")
        if not confirm(self, f"Delete {'block' if is_block else 'level'}",
                       f"{text}\n\nThis is planning data only: stock, reservations and issued material are not "
                       f"affected.", yes="Delete", danger=True):
            return
        fn = self.svc.delete_block if is_block else self.svc.delete_level
        if run(self, fn, node.id) is None:
            return
        parent = f"P:{node.site_id}" if is_block else f"B:{node.block_id}"
        if not is_block:
            self.reload_item(f"P:{node.site_id}", children=False)
        self._refresh_keys(parent, select=parent)

    # ---- actions: materials --------------------------------------------------------------------
    # ---- Finish Schedule: a unit's materials (v1.29) --------------------------------------------
    def open_schedule(self, node) -> None:
        """The unit card: materials, norm, project stock, units covered, shortage."""
        from wms.ui.dialogs.finish_schedule import FinishScheduleDialog
        FinishScheduleDialog(self, node.id).exec()

    def _unit_changed(self, unit_id: int) -> None:
        """A unit's materials changed: its row (material count) and the panel follow."""
        self._refresh_keys(f"U:{unit_id}")

    def add_material(self, node, parent=None) -> list[int] | None:
        """Add materials to a unit (``node`` is the unit). Returns the new line ids."""
        dlg = MaterialPickerDialog(self.app, f"Add material to {node.label}", parent or self,
                                   unit_text=f"Pick one or more existing materials (Ctrl+click for several), then the "
                                             f"quantity one {node.label} needs. Total = {fmt_qty(node.quantity)} × "
                                             f"quantity per unit.")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
            return None
        ids = run(parent or self, self.svc.add_materials, node.id, [(pid, dlg.qty.text()) for pid in dlg.chosen],
                  dlg.vpk.text(), dlg.notes.text())
        if ids:
            self._unit_changed(node.id)
        return ids

    def edit_material_quantity(self, node, parent=None) -> bool:
        """``node`` is a material line of a unit (MaterialNode)."""
        parent = parent or self
        text = ask_quantity(parent, "Quantity per unit", f"How many {node.uom} of {node.code} {node.name} does one "
                                                         f"unit need?", node.quantity_per_unit)
        if text is not None and self.save_norm(node.id, {"quantity_per_unit": text}, parent):
            self._unit_changed(node.unit_id)
            return True
        return False

    def save_norm(self, line_id: int, data: dict, parent=None):
        """Save a material line. A new norm on a unit type with units already made first asks whether
        to recalculate them or apply it to future units only. Returns the line, or None."""
        parent = parent or self
        choice = ask_norm_change(parent, self.app, line_id, data["quantity_per_unit"]) \
            if "quantity_per_unit" in data else (NORM_ALL, None)
        if choice is None:
            return None
        return run(parent, self.svc.update_material, line_id, data, applies_to=choice[0], reason=choice[1])

    def replace_material(self, node, parent=None) -> bool:
        parent = parent or self
        dlg = MaterialPickerDialog(self.app, f"Replace {node.code}", parent, multi=False, ask_quantity=False,
                                   unit_text=f"Choose the material that replaces {node.code} {node.name}. The quantity "
                                             f"per unit ({fmt_qty(node.quantity_per_unit)}) and notes stay.")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen:
            return False
        if run(parent, self.svc.replace_material, node.id, dlg.chosen[0]) is not None:
            self._unit_changed(node.unit_id)
            return True
        return False

    def remove_material(self, node, parent=None) -> bool:
        parent = parent or self
        if not confirm(parent, "Remove material", f"Remove {node.code} {node.name} from this unit?\n\nThe material "
                                                  f"itself and its stock are not changed.", yes="Remove", danger=True):
            return False
        if run(parent, self.svc.remove_materials, [node.id]) is not None:
            self._unit_changed(node.unit_id)
            return True
        return False

    def move_material(self, node, delta: int, parent=None) -> bool:
        if run(parent or self, lambda: self.svc.move_material(node.id, delta) or True):
            self._unit_changed(node.unit_id)
            return True
        return False

    def show_demand(self) -> None:
        DemandDialog(self.app, self).exec()

    def import_structure(self) -> None:
        """Projects, blocks, levels, units and unit materials from an Excel workbook (v1.25)."""
        from wms.ui.dialogs.structure_import import StructureImportDialog
        dlg = StructureImportDialog(self.app, self)
        dlg.exec()
        if dlg.result is not None:
            self.refresh()


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
        self.shown_key: str | None = None
        self._snapshot: tuple | None = None  # field values as last shown / saved
        self.empty = label("Select a manager, project, block, level or unit to see and change its details here.\n\n"
                           "Right-click a row (or click ⋯) for its actions.", "Muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.addWidget(self.empty)
        self.pages = {"M": self._manager_page(), "P": self._project_page(), "B": self._group_page("b", "Block"),
                      "V": self._group_page("v", "Level"), "U": self._unit_page()}
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
        self.p_apply = button("Reset Required to Units", lambda: self.page.apply_demand(self.node),
                              tooltip="Required already follows the units by itself. This also removes what was "
                                      "typed by hand on top, so Required = exactly what the units need")
        lay.addLayout(h(button("Open Record", lambda: self.page.open_project(self.node),
                               tooltip="The project's card: details, customer, documents, audit"), None, self.p_save))
        # the old Projects screen's tabs, one click from the tree
        lay.addLayout(h(*[button(text, lambda t=tab: self.page.open_project(self.node, t), tooltip=tip)
                          for text, tab, tip in (
                              ("Materials", "materials", "Required, Reserved, Issued, Returned and Remaining"),
                              ("Movements", "history", "Every Goods Out and return for this project"),
                              ("Reservations", "reservations", "Stock set aside for this project"))], None))
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

    def _group_page(self, p: str, title: str) -> QWidget:
        """Block (``p`` = "b") or level ("v"): name, code, notes and the materials it needs."""
        w, lay, form = self._box(title)
        name, code, notes = QLineEdit(), QLineEdit(), notes_edit(height=48)
        for text, e in (("Name *", name), ("Code / ref", code), ("Notes", notes)):
            form.addRow(text, e)
        save = button("Save", lambda: self.save_group(p), primary=True)
        lay.addLayout(h(None, save))
        info_ = label("", "Muted", wrap=True)
        lay.addWidget(info_)
        table = self._demand_table()
        lay.addWidget(table, 1)
        for attr, val in (("name", name), ("code", code), ("notes", notes), ("save", save), ("info", info_),
                          ("table", table)):
            setattr(self, f"{p}_{attr}", val)
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
        lay.addLayout(h(button("Finish Schedule...", lambda: self.page.open_schedule(self.node),
                               tooltip="The materials to make this unit: norm, project stock, units covered, "
                                       "shortage (also: double-click the unit)"), None, self.u_save))
        self.u_info = label("", "Muted", wrap=True)
        lay.addWidget(self.u_info)
        self.u_table = self._demand_table()
        lay.addWidget(self.u_table, 1)
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
            self.node, self.shown_key, self._snapshot = None, None, None
            self.setCurrentWidget(self.empty)
            return
        self.node = item.data(0, NODE)
        getattr(self, f"_show_{k.lower()}")(self.node)
        self.setCurrentWidget(self.pages[k])
        self.shown_key = key
        self._snapshot = self._values()

    # ---- unsaved changes (v1.19) -----------------------------------------------------------------
    def _fields(self) -> list:
        k = kind_of(self.shown_key)
        return {"M": [self.m_name, self.m_email, self.m_phone, self.m_active, self.m_notes],
                "P": [self.p_code, self.p_name, self.p_manager, self.p_status, self.p_notes],
                "B": [self.b_name, self.b_code, self.b_notes], "V": [self.v_name, self.v_code, self.v_notes],
                "U": [self.u_name, self.u_code, self.u_qty, self.u_notes]}.get(k, [])

    def _values(self) -> tuple:
        out = []
        for w in self._fields():
            if isinstance(w, QCheckBox):
                out.append(w.isChecked())
            elif isinstance(w, QComboBox):
                out.append(w.currentData())
            elif hasattr(w, "toPlainText"):
                out.append(w.toPlainText())
            else:
                out.append(w.text())
        return tuple(out)

    def is_dirty(self) -> bool:
        """Something was typed in the panel and not saved yet (compared with what was shown)."""
        return (self.page.can_edit and self.node is not None and self._snapshot is not None
                and self._values() != self._snapshot)

    def mark_clean(self) -> None:
        self._snapshot = self._values() if self.node is not None else None

    def discard(self) -> None:
        """Forget the typed values: show the saved ones again."""
        key = self.shown_key
        item = self.page.items.get(key) if key else None
        if item is not None:
            self.show_item(item)
        else:
            self.mark_clean()

    def save_current(self) -> bool:
        """Save with the panel's own Save. False (and the typed values kept) when it fails."""
        k = kind_of(self.shown_key)
        fn = {"M": self.save_manager, "P": self.save_project, "B": lambda: self.save_group("b"),
              "V": lambda: self.save_group("v"), "U": self.save_unit}.get(k)
        return bool(fn and fn())

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
        blocks = f"{_n(n.blocks, 'block')}, " if n.blocks else ""
        self.p_info.setText(f"{blocks}{n.units} unit type(s), {fmt_qty(qty)} unit(s), {lines} material line(s). "
                            + (f"{off} material(s) differ from the material list." if off
                               else "Material list matches the units." if rows else ""))

    def _show_group(self, p: str, n, rows, text: str) -> None:
        getattr(self, f"{p}_name").setText(n.name)
        getattr(self, f"{p}_code").setText(n.code or "")
        getattr(self, f"{p}_notes").setPlainText(n.notes or "")
        self._enable(*(getattr(self, f"{p}_{a}") for a in ("name", "code", "notes", "save")), on=self.page.can_edit)
        getattr(self, f"{p}_table").set_items(rows)
        getattr(self, f"{p}_info").setText(f"{text} Materials needed ({len(rows)}):")

    def _show_b(self, n) -> None:
        rows = run(self, self.app.services.project_demand.for_block, n.id) or []
        self._show_group("b", n, rows, f"{_n(n.levels, 'level')}, {_n(n.units, 'unit type')}.")

    def _show_v(self, n) -> None:
        rows = run(self, self.app.services.project_demand.for_level, n.id) or []
        where = f"In block {n.block_name}. " if n.block_name else ""
        self._show_group("v", n, rows, f"{where}{_n(n.units, 'unit type')}.")

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

    # ---- saving ---------------------------------------------------------------------------------
    def save_manager(self) -> bool:
        n = self.node
        if run(self, self.svc.save_manager, n.id, {"name": self.m_name.text(), "email": self.m_email.text(),
                                                   "phone": self.m_phone.text(), "is_active": self.m_active.isChecked(),
                                                   "notes": self.m_notes.toPlainText()}) is not None:
            self.mark_clean()
            self.page.reload_item(f"M:{n.id}")
            self.page._changed()
            return True
        return False

    def save_project(self) -> bool:
        n = self.node
        saved = run(self, self.svc.save_project, n.id, {
            "project_number": self.p_code.text(), "name": self.p_name.text(), "manager_id": self.p_manager.currentData(),
            "status": self.p_status.currentData(), "notes": self.p_notes.toPlainText()}, n.version)
        if saved is not None:
            self.mark_clean()
            self.page._after_project_change(saved, n.manager_id)
            return True
        return False

    def save_group(self, p: str) -> bool:
        n = self.node
        fn = self.svc.save_block if p == "b" else self.svc.save_level
        data = {"name": getattr(self, f"{p}_name").text(), "code": getattr(self, f"{p}_code").text(),
                "notes": getattr(self, f"{p}_notes").toPlainText()}
        if run(self, fn, n.id, data) is not None:
            self.mark_clean()
            key = f"{'B' if p == 'b' else 'V'}:{n.id}"
            self.page.reload_item(key, children=False)
            self.page._changed()
            return True
        return False

    def save_unit(self) -> bool:
        n = self.node
        if run(self, self.svc.save_unit, n.id, {"name": self.u_name.text(), "unit_code": self.u_code.text(),
                                                "quantity": self.u_qty.text(),
                                                "notes": self.u_notes.toPlainText()}) is not None:
            self.mark_clean()
            self.page._refresh_keys(f"U:{n.id}", select=f"U:{n.id}")
            return True
        return False
