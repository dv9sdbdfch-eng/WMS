"""Production dashboard (v1.24, redesigned v1.36): which units are made, dispatched and on site.

Left: the projects, one card each, with a progress bar, % made and an end-date warning. Right: the
chosen project (or all of them) as a tree Block -> Level -> Unit with running totals ("7/12") on
every group. A unit type with several pieces (Kitchen x12) is one row; its pieces are under it and
show only when the row is opened.

Click an empty Done / Dispatched / On site cell of a piece to mark it today; double-click to choose
the date or change one. Click the "7/12" of a group or unit type to mark the next units in one go.
The rows follow the Projects tree - nothing is copied, see ``services.unit_progress``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.numbers import fmt_date, fmt_datetime
from wms.core.permissions import P
from wms.db.models.hierarchy import STAGE_DISPATCHED, STAGE_DONE, STAGE_ON_SITE, STAGES
from wms.services.unit_progress import NOT_STARTED, STAGE_LABELS, STATUS_LABELS, STATUSES, PieceRow, ProjectCard
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DateEdit, SearchBox, ask_text, button, info, label, run

#: row colours of a piece by status (Ihor 13/11/2026: grey not started, yellow done, blue dispatched,
#: green on site)
ROW_COLOURS = {NOT_STARTED: "#EEF0F3", STAGE_DONE: "#FEF5D4", STAGE_DISPATCHED: "#DCEBFC", STAGE_ON_SITE: "#DDF3E2"}
#: the same stages on the progress bars (stronger, so thin bars still read)
BAR_COLOURS = {STAGE_ON_SITE: "#3BA55C", STAGE_DISPATCHED: "#4A8FE0", STAGE_DONE: "#F5C043", NOT_STARTED: "#E4E7EC"}
#: background of group rows: project / block darker, level lighter
GROUP_COLOURS = {"project": "#E3E9F2", "block": "#E8EDF5", "level": "#F3F6FA"}

COLUMNS = ("Block / Level / Unit", "Qty", "Done", "Dispatched", "On site")
STAGE_COL = {2: STAGE_DONE, 3: STAGE_DISPATCHED, 4: STAGE_ON_SITE}
COL_OF = {v: k for k, v in STAGE_COL.items()}
#: a dispatched unit not on site after this many days is flagged
STALE_DAYS = 5
#: a project ending within this many days with units still to make is flagged
WARN_DAYS = 14
ROLE_NODE = Qt.ItemDataRole.UserRole + 1
ROLE_AGG = Qt.ItemDataRole.UserRole + 2
ROLE_CAN = Qt.ItemDataRole.UserRole + 3     # True: the stage cell can be clicked (worked out once per row)

#: what the "Show" filter keeps: (stage that must be marked, stage that must not be)
SHOW = {
    "ALL": ("All units", None, None),
    "TODO": ("Not made yet", None, STAGE_DONE),
    "MADE": ("Made, not dispatched", STAGE_DONE, STAGE_DISPATCHED),
    "DISP": ("Dispatched, not on site", STAGE_DISPATCHED, STAGE_ON_SITE),
    "SITE": ("On site", STAGE_ON_SITE, None),
}


def _dot(colour: str) -> str:
    return f"<span style='color:{colour}'>■</span>"


def pct(part: int, total: int) -> int:
    return round(100 * part / total) if total else 0


def stale_dispatched(rows, today: date | None = None) -> list[PieceRow]:
    """Pieces dispatched more than ``STALE_DAYS`` days ago and still not marked on site."""
    today = today or date.today()
    return [r for r in rows if STAGE_DISPATCHED in r.marks and STAGE_ON_SITE not in r.marks
            and (today - r.marks[STAGE_DISPATCHED].on).days > STALE_DAYS]


def end_warning(card: ProjectCard, done: int, total: int, today: date | None = None) -> str:
    """'ends in 7 days, 69% not made' / '3 days overdue, ...' or '' (no end date, all made, far off)."""
    if card.end_date is None or not total or done >= total:
        return ""
    left = (card.end_date - (today or date.today())).days
    if left > WARN_DAYS:
        return ""
    todo = 100 - pct(done, total)
    if left < 0:
        return f"⚠ {-left} day(s) overdue, {todo}% not made"
    return f"⚠ ends {'today' if left == 0 else f'in {left} day(s)'}, {todo}% not made"


# ---- tree nodes -----------------------------------------------------------------------------------
@dataclass
class Node:
    """One row of the tree. ``kind``: project / block / level / type (unit with several pieces) /
    piece. ``rows`` are all pieces under it (for the totals), ``shown`` those the filters keep."""

    kind: str
    key: str
    text: str
    rows: list
    shown: list
    piece: PieceRow | None = None

    @property
    def qty(self) -> int:
        return len(self.rows)

    def count(self, stage: str) -> int:
        return sum(stage in r.marks for r in self.rows)

    def markable(self, stage: str, only_shown: bool = False) -> list:
        """The pieces that can get the stage now, in tree order (only those the filters show while
        a filter is on: "the next units" are the ones the user sees)."""
        return [r for r in (self.shown if only_shown else self.rows) if r.can_mark(stage)]

    @property
    def expandable(self) -> bool:
        return self.kind != "piece"


def unit_text(r: PieceRow) -> str:
    return f"{r.unit_name} · {r.unit_code}" if r.unit_code else r.unit_name


def _group(rows, key) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def children(node: Node) -> list[Node]:
    """The rows under a group, built from the pieces the filters keep (in tree order). Every list
    is grouped in one pass, so opening a level with 1 000 unit types stays quick."""
    if node.kind == "type":
        return [Node("piece", f"P{r.unit_id}:{r.piece_no}", f"#{r.piece_no} of {r.pieces}", [r], [r], r)
                for r in node.shown]
    out: list[Node] = []
    if node.kind == "project":
        all_blocks = _group(node.rows, lambda r: r.block_id)
        all_units = _group(node.rows, lambda r: r.unit_id)
        shown_blocks = _group(node.shown, lambda r: r.block_id)
        for bid, shown in shown_blocks.items():
            if bid:
                out.append(Node("block", f"B{bid}", shown[0].block or "—", all_blocks[bid], shown))
        loose = _group(shown_blocks.get(None, []), lambda r: r.unit_id)
        out += [_unit_node(all_units[uid], shown) for uid, shown in loose.items()]
    elif node.kind == "block":
        all_levels = _group(node.rows, lambda r: r.level_id)
        for lid, shown in _group(node.shown, lambda r: r.level_id).items():
            out.append(Node("level", f"L{lid}", shown[0].level or "—", all_levels[lid], shown))
    elif node.kind == "level":
        all_units = _group(node.rows, lambda r: r.unit_id)
        out += [_unit_node(all_units[uid], shown) for uid, shown in _group(node.shown, lambda r: r.unit_id).items()]
    return out


def _unit_node(rows: list, shown: list) -> Node:
    first = rows[0]
    if first.pieces == 1:
        return Node("piece", f"P{first.unit_id}:{first.piece_no}", unit_text(first), rows, shown, first)
    return Node("type", f"U{first.unit_id}", unit_text(first), rows, shown)


# ---- tree widget ----------------------------------------------------------------------------------
class StageDelegate(QStyledItemDelegate):
    """Group and unit-type rows: a small bar and "7/12" in each stage column."""

    def paint(self, p: QPainter, opt: QStyleOptionViewItem, idx) -> None:
        agg = idx.data(ROLE_AGG)
        if not agg:
            super().paint(p, opt, idx)
            return
        n, total, stage = agg
        o = QStyleOptionViewItem(opt)
        self.initStyleOption(o, idx)
        o.text = ""
        style = o.widget.style() if o.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, o, p, o.widget)
        r = opt.rect
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(r.left() + 6, r.center().y() - 4, min(64, max(r.width() - 60, 20)), 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BAR_COLOURS[NOT_STARTED]))
        p.drawRoundedRect(bar, 4, 4)
        if n and total:
            done = QRectF(bar.left(), bar.top(), bar.width() * n / total, bar.height())
            p.setBrush(QColor(BAR_COLOURS[stage]))
            p.drawRoundedRect(done, 4, 4)
        f = QFont(opt.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor("#067647" if n == total else "#101828")))
        text_rect = r.adjusted(int(bar.right() - r.left()) + 6, 0, -6, 0)
        p.drawText(text_rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"{n}/{total}")
        p.restore()

    def sizeHint(self, opt, idx):  # noqa: N802
        s = super().sizeHint(opt, idx)
        return QSize(max(s.width(), 130), max(s.height(), 26))


class BoardTree(QTreeWidget):
    stage_clicked = Signal(object, str)          # Node, stage
    stage_double_clicked = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels(list(COLUMNS))
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self.setIndentation(18)
        delegate = StageDelegate(self)
        for col in STAGE_COL:
            self.setItemDelegateForColumn(col, delegate)
        hdr = self.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, w in ((1, 52), (2, 140), (3, 140), (4, 140)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.setColumnWidth(col, w)
        self.setStyleSheet("QTreeWidget { selection-background-color: #B9D4F1; selection-color: #101828; }"
                           "QTreeWidget::item { padding: 0px; }")
        self.setMouseTracking(True)
        self.itemClicked.connect(lambda it, col: self._emit(it, col, self.stage_clicked))
        self.itemDoubleClicked.connect(self._double)
        self.can_edit = True

    @staticmethod
    def node(item: QTreeWidgetItem | None) -> Node | None:
        return item.data(0, ROLE_NODE) if item is not None else None

    def _emit(self, item, col, signal) -> None:
        n = self.node(item)
        if n is not None and col in STAGE_COL:
            signal.emit(n, STAGE_COL[col])

    def _double(self, item, col) -> None:
        if col in STAGE_COL:
            self._emit(item, col, self.stage_double_clicked)
        elif item.childCount():
            item.setExpanded(not item.isExpanded())

    def mouseMoveEvent(self, event):  # noqa: N802 - pointing hand over cells that can be marked
        item = self.itemAt(event.position().toPoint())
        col = self.columnAt(int(event.position().x()))
        n = self.node(item)
        hand = False
        if n is not None and col in STAGE_COL and self.can_edit:
            hand = bool(item.data(col, ROLE_CAN))
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if hand else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def selected_pieces(self) -> list[PieceRow]:
        return [n.piece for n in (self.node(i) for i in self.selectedItems()) if n is not None and n.piece]

    def items(self):
        """Every item built so far, depth first."""
        stack = [self.topLevelItem(i) for i in reversed(range(self.topLevelItemCount()))]
        while stack:
            it = stack.pop()
            yield it
            stack += [it.child(i) for i in reversed(range(it.childCount()))]


# ---- project list ---------------------------------------------------------------------------------
class ProgressBar(QWidget):
    """On site (green), dispatched (blue), done (yellow) and not started (grey), left to right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(9)
        self.parts = (0, 0, 0, 1)

    def set_parts(self, on_site: int, dispatched_only: int, done_only: int, rest: int) -> None:
        self.parts = (on_site, dispatched_only, done_only, rest)
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        total = sum(self.parts) or 1
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BAR_COLOURS[NOT_STARTED]))
        p.drawRoundedRect(QRectF(0, 0, w, h), 4, 4)
        x = 0.0
        p.setClipRect(0, 0, w, h)
        for part, key in zip(self.parts[:3], (STAGE_ON_SITE, STAGE_DISPATCHED, STAGE_DONE)):
            if part:
                pw = w * part / total
                p.setBrush(QColor(BAR_COLOURS[key]))
                p.drawRect(QRectF(x, 0, pw, h))
                x += pw
        p.end()


class ProjectCardWidget(QFrame):
    """One project in the list on the left (or "All projects" with ``site_id`` None)."""

    clicked = Signal(object)

    def __init__(self, site_id, parent=None):
        super().__init__(parent)
        self.site_id = site_id
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.name = label("", "CardName")
        self.name.setStyleSheet("font-weight: 700;")
        self.pct = label("")
        self.pct.setStyleSheet("font-weight: 700;")
        top.addWidget(self.name, 1)
        top.addWidget(self.pct)
        lay.addLayout(top)
        self.sub = label("", "CardLabel", wrap=True)
        self.sub.setStyleSheet("font-size: 8pt; color: #475467;")
        lay.addWidget(self.sub)
        self.bar = ProgressBar()
        lay.addWidget(self.bar)
        self.nums = label("")
        self.nums.setStyleSheet("font-size: 8pt; color: #344054;")
        lay.addWidget(self.nums)
        self.warn = label("")
        self.warn.setStyleSheet("font-size: 8pt; color: #B42318; font-weight: 700;")
        self.warn.hide()
        lay.addWidget(self.warn)
        self.set_active(False)

    def set_warning(self, text: str) -> None:
        self.warn.setText(text)
        self.warn.setVisible(bool(text))

    def set_active(self, on: bool) -> None:
        self.active = on
        self.setStyleSheet("QFrame#Card { background: %s; border: %s; border-radius: 7px; }"
                           % (("#EAF2FB", "2px solid #2F6FB2") if on else ("white", "1px solid #E4E7EC")))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.site_id)
        super().mousePressEvent(event)


# ---- dialogs --------------------------------------------------------------------------------------
class MarkDialog(QDialog):
    """Date (today by default, never in the future) and an optional note for a mark. With ``most``
    it also asks how many units to mark (the next ones, in tree order)."""

    def __init__(self, parent, title: str, when: date | None = None, note: str | None = None, what: str = "",
                 most: int | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        if what:
            lay.addWidget(label(what, wrap=True))
        form = QFormLayout()
        self.count = QSpinBox()
        if most is not None:
            self.count.setRange(1, most)
            self.count.setValue(most)
            self.count.setSuffix(f" of {most}")
            form.addRow("Units *", self.count)
        self.date = DateEdit(when or date.today())
        self.note = QLineEdit(note or "")
        self.note.setPlaceholderText("optional")
        form.addRow("Date *", self.date)
        form.addRow("Note", self.note)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Cancel", self.reject))
        row.addWidget(button("Save", self.accept, primary=True))
        lay.addLayout(row)


# ---- page -----------------------------------------------------------------------------------------
class ProductionBoardPage(Page):
    title = "Production Dashboard"
    subtitle = "Click a count to mark the next units · open a unit for single pieces · right-click for more"
    margins = (16, 10, 16, 8)
    spacing = 6

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self._skip_refresh = False
        self.board = None
        self.cards: dict = {}
        self.site_id = None            # project shown (None: all)
        self._expanded: set[str] = set()
        self._seen: set[str] = set()   # groups already shown once (opened by default the first time)
        self._remember = True
        self._shown_count = 0
        self.titles_widget.setToolTip(
            "Every unit of every project: made (Done), dispatched, on site.\n"
            "Click an empty cell of a unit to mark it with today's date; double-click to choose the date "
            "or change it.\nClick the count of a group (e.g. 7/12) to mark the next units in one go.\n"
            "Pick a project on the left to see only its units.")
        self.add_action(button("Export to Excel", self.export))
        self.add_action(button("Projects tree", lambda: self.main_window.go("projects")
                               if getattr(self, "main_window", None) else None))
        self.closed = QCheckBox("Closed projects too")
        self.closed.setToolTip("Also show completed and cancelled projects (archived ones never show)")
        self.closed.toggled.connect(lambda _: self.refresh())
        self.add_action(self.closed)

        # summary of the project shown
        self.summary = QHBoxLayout()
        self.summary.setSpacing(6)
        self.chips: dict[str, QWidget] = {}
        for key in ("title", "done", "disp", "site", "stale"):
            c = label("")
            c.setTextFormat(Qt.TextFormat.RichText)
            self.chips[key] = c
            self.summary.addWidget(c)
        self.summary.addStretch(1)
        self.body.addLayout(self.summary)

        split = QHBoxLayout()
        split.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(4)
        self.find_project = SearchBox("Find project...")
        self.find_project.textChanged.connect(lambda _: self._filter_projects())
        left.addWidget(self.find_project)
        self.plist = QListWidget()
        self.plist.setFixedWidth(250)
        self.plist.setSpacing(2)
        self.plist.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.plist.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.plist.setStyleSheet("QListWidget { background: #F9FAFB; border: 1px solid #E4E7EC; border-radius: 6px; }"
                                 "QListWidget::item { padding: 0px; margin: 0px; border: none; }")
        left.addWidget(self.plist, 1)
        split.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(4)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.search = SearchBox("Find unit, code, block or level...")
        self.search.search.connect(lambda _: self._rebuild())
        tools.addWidget(self.search, 1)
        self.show_combo = QComboBox()
        for key, (text, _m, _n) in SHOW.items():
            self.show_combo.addItem(text, key)
        self.show_combo.setToolTip("Show only the units at this stage (the totals still count every unit)")
        self.show_combo.currentIndexChanged.connect(lambda _: self._rebuild())
        tools.addWidget(self.show_combo)
        tools.addWidget(button("Expand all", lambda: self.expand_all(True)))
        tools.addWidget(button("Collapse", lambda: self.expand_all(False)))
        right.addLayout(tools)
        self.tree = BoardTree()
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.stage_clicked.connect(self._cell_clicked)
        self.tree.stage_double_clicked.connect(self._cell_double_clicked)
        self.tree.itemExpanded.connect(self._expanded_item)
        self.tree.itemCollapsed.connect(lambda it: self._expanded.discard(self.tree.node(it).key)
                                        if self.tree.node(it) else None)
        right.addWidget(self.tree, 1)
        split.addLayout(right, 1)
        self.body.addLayout(split, 1)

        foot = QHBoxLayout()
        self.count = label("", "Muted")
        foot.addWidget(self.count)
        legend = "  ".join(f"{_dot('#C9CED6' if s == NOT_STARTED else BAR_COLOURS[s])} {STATUS_LABELS[s]}"
                           for s in STATUSES)
        foot.addWidget(label("   ·   " + legend, "Muted"))
        foot.addStretch(1)
        self.body.addLayout(foot)

    # -- data -------------------------------------------------------------------------------------
    @property
    def svc(self):
        return self.app.services.unit_progress

    @property
    def can_edit(self) -> bool:
        return self.app.ctx.has(P.SITES_EDIT)

    @property
    def rows(self) -> list[PieceRow]:
        return self.board.rows if self.board else []

    def refresh(self) -> None:
        if self._skip_refresh:
            return
        board = run(self, self.svc.board, self.closed.isChecked())
        if board is None:
            return
        self.board = board
        self.tree.can_edit = self.can_edit
        if self.site_id is not None and not any(c.site_id == self.site_id for c in board.cards):
            self.site_id = None
        self._build_cards()
        self._rebuild()

    def _rows_of(self, site_id) -> list[PieceRow]:
        return [r for r in self.rows if site_id is None or r.site_id == site_id]

    # -- project list -----------------------------------------------------------------------------
    def _build_cards(self) -> None:
        self.plist.clear()
        self.cards = {}
        for card in [None, *self.board.cards]:
            sid = card.site_id if card else None
            w = ProjectCardWidget(sid)
            if card is None:
                w.name.setText("All projects" if self.closed.isChecked() else "All open projects")
            else:
                w.name.setText(card.title)
                w.name.setToolTip(f"{card.project_number} - {card.title}")
            w.clicked.connect(self.pick_project)
            item = QListWidgetItem(self.plist)
            item.setData(Qt.ItemDataRole.UserRole, sid)
            self.plist.setItemWidget(item, w)
            self.cards[sid] = w
        self._update_cards()
        self._filter_projects()

    def _update_cards(self) -> None:
        """Card figures from the rows in memory (after a mark they change without a full reload)."""
        per: dict = defaultdict(lambda: [0, 0, 0, 0])
        for r in self.rows:
            t = per[r.site_id]
            t[0] += 1
            t[1] += STAGE_DONE in r.marks
            t[2] += STAGE_DISPATCHED in r.marks
            t[3] += STAGE_ON_SITE in r.marks
        for c in self.board.cards:
            w = self.cards.get(c.site_id)
            if w is None:
                continue
            total, done, disp, site = per[c.site_id]
            ends = f" · ends {c.end_date:%d/%m}" if c.end_date else ""
            w.sub.setText(f"{c.manager or 'No manager'} · {total} units{ends}")
            self._fill_card(w, total, done, disp, site)
            w.set_warning(end_warning(c, done, total))
        total = len(self.rows)
        done = sum(STAGE_DONE in r.marks for r in self.rows)
        disp = sum(STAGE_DISPATCHED in r.marks for r in self.rows)
        site = sum(STAGE_ON_SITE in r.marks for r in self.rows)
        allc = self.cards[None]
        allc.sub.setText(f"{len(self.board.cards)} project(s) · {total} units")
        self._fill_card(allc, total, done, disp, site)
        width = self.plist.width() - 2 * self.plist.spacing() - 18     # room for the scroll bar
        for i in range(self.plist.count()):
            item = self.plist.item(i)
            w = self.plist.itemWidget(item)
            w.ensurePolished()
            w.setFixedWidth(width)
            lay = w.layout()
            height = lay.heightForWidth(width) if lay.hasHeightForWidth() else lay.sizeHint().height()
            item.setSizeHint(QSize(width, max(height, w.minimumSizeHint().height()) + 2))
        self._mark_active_card()

    @staticmethod
    def _fill_card(w: ProjectCardWidget, total: int, done: int, disp: int, site: int) -> None:
        w.pct.setText(f"{pct(done, total)}%")
        w.pct.setToolTip(f"Made (Done): {done} of {total} units")
        w.bar.set_parts(site, max(disp - site, 0), max(done - disp, 0), max(total - done, 0))
        w.nums.setText(f"{_dot(BAR_COLOURS[STAGE_DONE])} Done <b>{done}</b> &nbsp;"
                       f"{_dot(BAR_COLOURS[STAGE_DISPATCHED])} Disp. <b>{disp}</b> &nbsp;"
                       f"{_dot(BAR_COLOURS[STAGE_ON_SITE])} Site <b>{site}</b> &nbsp;/ {total}")

    def _filter_projects(self) -> None:
        words = self.find_project.text().casefold().split()
        by_id = {c.site_id: c for c in self.board.cards} if self.board else {}
        for i in range(self.plist.count()):
            item = self.plist.item(i)
            c = by_id.get(item.data(Qt.ItemDataRole.UserRole))
            hay = f"{c.project_number} {c.title} {c.manager}".casefold() if c else ""
            item.setHidden(c is not None and not all(w in hay for w in words))

    def _mark_active_card(self) -> None:
        for key, w in self.cards.items():
            w.set_active(key == self.site_id)

    def pick_project(self, site_id) -> None:
        """Card click: only that project's units (click the active card or All projects to show all)."""
        if site_id is not None and site_id == self.site_id:
            site_id = None
        self.site_id = site_id
        self._mark_active_card()
        self._rebuild()

    def apply_preset(self, preset: dict) -> None:
        if "site_id" in preset:
            self.on_show()
            self.site_id = None
            self.pick_project(preset["site_id"])

    # -- tree -------------------------------------------------------------------------------------
    def _keeper(self):
        """The filter as a function (the filter boxes are read once, not per row)."""
        _t, must, must_not = SHOW[self.show_combo.currentData() or "ALL"]
        words = self.search.text().casefold().split()

        def keeps(r: PieceRow) -> bool:
            if must and must not in r.marks:
                return False
            if must_not and must_not in r.marks:
                return False
            if words:
                hay = f"{r.unit_name} {r.unit_code or ''} {r.block} {r.level}".casefold()
                if not all(w in hay for w in words):
                    return False
            return True
        return keeps

    @property
    def filtering(self) -> bool:
        return bool(self.search.text().strip()) or (self.show_combo.currentData() or "ALL") != "ALL"

    def _top_nodes(self) -> list[Node]:
        rows = self._rows_of(self.site_id)
        keeps = self._keeper()
        shown = [r for r in rows if keeps(r)]
        self._shown_count = len(shown)
        if self.site_id is not None:
            root = Node("project", f"S{self.site_id}", "", rows, shown)
            return children(root)
        by_site: dict = {}
        for r in shown:
            by_site.setdefault(r.site_id, []).append(r)
        all_of: dict = defaultdict(list)
        for r in rows:
            all_of[r.site_id].append(r)
        titles = {c.site_id: c for c in self.board.cards}
        return [Node("project", f"S{sid}", f"{titles[sid].title}  ({titles[sid].project_number})",
                     all_of[sid], s) for sid, s in by_site.items()]

    def _rebuild(self) -> None:
        """Build the tree again (after a filter, a project pick or a mark), keeping what was open and
        where it was scrolled. Groups get their rows only when opened, so 5 000 units stay quick."""
        if self.board is None:
            return
        bar = self.tree.verticalScrollBar().value()
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            nodes = self._top_nodes()
            for n in nodes:
                self.tree.addTopLevelItem(self._item(n))
            few = len(nodes) <= 3
            stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            while stack:
                it = stack.pop()
                n = self.tree.node(it)
                if n is None or not n.expandable:
                    continue
                first_time = n.key not in self._seen
                if not self.filtering:        # seen only through a filter: still opens by default later
                    self._seen.add(n.key)
                # opened the first time: blocks and levels of one project, projects if only a few
                default = n.kind in ("block", "level") and (self.site_id is not None or few) \
                    or (n.kind == "project" and few)
                keep = n.key in self._expanded or (first_time and default)
                if keep or (self.filtering and n.kind != "type"):
                    # opened only to show what a filter found: not remembered once the filter is cleared
                    self._remember = keep
                    it.setExpanded(True)          # builds its rows (see _expanded_item)
                    self._remember = True
                    stack += [it.child(i) for i in range(it.childCount())]
        finally:
            self.tree.setUpdatesEnabled(True)
        self.tree.verticalScrollBar().setValue(bar)
        self._update_summary()

    def _item(self, n: Node) -> QTreeWidgetItem:
        it = QTreeWidgetItem()
        it.setData(0, ROLE_NODE, n)
        it.setText(0, n.text)
        it.setText(1, str(n.qty))
        it.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if n.kind == "piece":
            r = n.piece
            it.setToolTip(0, f"{r.project_number} / {r.block or '-'} / {r.level or '-'} / {r.unit}")
            self._paint_piece(it, r)
        else:
            for col, stage in STAGE_COL.items():
                it.setData(col, ROLE_AGG, (n.count(stage), n.qty, stage))
                it.setData(col, ROLE_CAN, bool(n.markable(stage, self.filtering)))
                it.setToolTip(col, self._agg_tip(n, stage))
            f = QFont()
            f.setBold(n.kind != "type")
            it.setFont(0, f)
            colour = GROUP_COLOURS.get(n.kind)
            if colour:
                for col in range(len(COLUMNS)):
                    it.setBackground(col, QBrush(QColor(colour)))
            if n.kind == "type":
                it.setText(0, f"{n.text}  ×{n.rows[0].pieces}")
            # the rows under it are made when it is opened
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            it.addChild(QTreeWidgetItem(["…"]))
        return it

    def _agg_tip(self, n: Node, stage: str) -> str:
        can = len(n.markable(stage, self.filtering))
        tip = f"{STAGE_LABELS[stage]}: {n.count(stage)} of {n.qty} units"
        if can and self.can_edit:
            tip += f"\nClick to mark the next units {STAGE_LABELS[stage]} ({can} can be marked now)"
        return tip

    def _paint_piece(self, it: QTreeWidgetItem, r: PieceRow) -> None:
        bg = QBrush(QColor(ROW_COLOURS[r.status]))
        for col in range(len(COLUMNS)):
            it.setBackground(col, bg)
        bold = QFont()
        bold.setBold(True)
        for col, stage in STAGE_COL.items():
            m = r.marks.get(stage)
            if m:
                it.setText(col, f"✔ {fmt_date(m.on)}")
                it.setForeground(col, QBrush(QColor("#067647")))
                it.setFont(col, bold)
                tip = f"{STAGE_LABELS[stage]} on {fmt_date(m.on)}" + (f" by {m.user}" if m.user else "")
                if m.note:
                    tip += f"\n{m.note}"
                tip += ("\nRecorded for several units together (unit card)." if m.batch
                        else "\nDouble-click to change the date")
            elif r.can_mark(stage) and self.can_edit:
                it.setData(col, ROLE_CAN, True)
                it.setText(col, "○ Mark")
                it.setForeground(col, QBrush(QColor("#667085")))
                tip = f"Click to mark {STAGE_LABELS[stage]} today; double-click to choose the date"
            else:
                it.setText(col, "")
                prev = {STAGE_DISPATCHED: STAGE_DONE, STAGE_ON_SITE: STAGE_DISPATCHED}.get(stage)
                tip = f"Mark {STAGE_LABELS[prev]} first" if prev else ""
            it.setToolTip(col, tip)

    def _expanded_item(self, it: QTreeWidgetItem) -> None:
        n = self.tree.node(it)
        if n is None:
            return
        if self._remember:
            self._expanded.add(n.key)
        if it.childCount() == 1 and self.tree.node(it.child(0)) is None:
            it.takeChild(0)
            it.addChildren([self._item(c) for c in children(n)])

    def expand_all(self, on: bool) -> None:
        self.tree.setUpdatesEnabled(False)
        try:
            if on:
                # open groups down to the unit types (their pieces stay closed: there may be hundreds)
                stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
                while stack:
                    it = stack.pop()
                    n = self.tree.node(it)
                    if n is None or n.kind in ("type", "piece"):
                        continue
                    it.setExpanded(True)
                    stack += [it.child(i) for i in range(it.childCount())]
            else:
                self._expanded.clear()
                self.tree.collapseAll()
        finally:
            self.tree.setUpdatesEnabled(True)

    def _update_summary(self) -> None:
        rows = self._rows_of(self.site_id)
        total = len(rows)
        done = sum(STAGE_DONE in r.marks for r in rows)
        disp = sum(STAGE_DISPATCHED in r.marks for r in rows)
        site = sum(STAGE_ON_SITE in r.marks for r in rows)
        stale = stale_dispatched(rows)
        chip = ("QLabel { background: white; border: 1px solid #D0D5DD; border-radius: 6px; padding: 3px 9px; }")
        red = ("QLabel { background: #FEF3F2; border: 1px solid #FDA29B; border-radius: 6px; padding: 3px 9px; "
               "color: #B42318; }")
        card = next((c for c in self.board.cards if c.site_id == self.site_id), None)
        if card is not None:
            end = f" · ends {fmt_date(card.end_date)}" if card.end_date else ""
            title = f"<b>{card.title}</b> <span style='color:#667085'>{card.project_number}{end}</span>"
        else:
            title = f"<b>{'All projects' if self.closed.isChecked() else 'All open projects'}</b>"
        self.chips["title"].setText(title)
        self.chips["done"].setText(f"Done <b>{done}/{total}</b> {pct(done, total)}%")
        self.chips["disp"].setText(f"Dispatched <b>{disp}</b>")
        self.chips["site"].setText(f"On site <b>{site}</b>")
        self.chips["stale"].setText(f"Dispatched over {STALE_DAYS} days, not on site: <b>{len(stale)}</b>")
        self.chips["stale"].setToolTip("\n".join(f"{r.project}: {r.unit} - dispatched "
                                                 f"{fmt_date(r.marks[STAGE_DISPATCHED].on)}" for r in stale[:25]))
        for key, w in self.chips.items():
            w.setStyleSheet(red if key == "stale" and stale else chip)
        self.chips["stale"].setVisible(bool(stale))
        self.count.setText(f"{self._shown_count} of {total} units shown" if self.filtering else f"{total} units")

    # -- marking ----------------------------------------------------------------------------------
    def _cell_clicked(self, n: Node, stage: str) -> None:
        if not self.can_edit:
            return
        if n.piece is None:
            self.mark_next(n, stage)
            return
        if not n.piece.can_mark(stage):
            return
        selected = self.tree.selected_pieces()
        if len(selected) > 1 and n.piece in selected:
            self.mark_rows(selected, stage)
        else:
            self.mark_rows([n.piece], stage)

    def _cell_double_clicked(self, n: Node, stage: str) -> None:
        if not self.can_edit or n.piece is None:
            return
        r = n.piece
        # a double-click on an empty cell first marks it today (the single click), then opens the
        # date so it can be changed straight away
        if r.marks.get(stage) is not None:
            self.change_date(r, stage)
        elif r.can_mark(stage):
            self.mark_rows([r], stage, ask=True)

    def mark_next(self, n: Node, stage: str, count: int | None = None, when: date | None = None) -> None:
        """Mark the next units of a group / unit type (the first ones in tree order that can have the
        stage). Asks how many and the date unless ``count`` is given."""
        todo = n.markable(stage, self.filtering)
        if not todo:
            prev = {STAGE_DISPATCHED: STAGE_DONE, STAGE_ON_SITE: STAGE_DISPATCHED}.get(stage)
            why = f" Mark them {STAGE_LABELS[prev]} first." if prev and n.count(stage) < n.qty else ""
            shown = " shown" if self.filtering else ""
            info(self, STAGE_LABELS[stage], f"No{shown} unit of {n.text or 'this group'} can be marked "
                                            f"{STAGE_LABELS[stage]} now.{why}")
            return
        note = None
        if count is None:
            dlg = MarkDialog(self, f"Mark {STAGE_LABELS[stage]} - {n.text}",
                             what=f"{n.text}: {n.count(stage)} of {n.qty} units are {STAGE_LABELS[stage]}. "
                                  f"Mark the next units (first ones in the list"
                                  f"{', only those shown by the filter' if self.filtering else ''}):",
                             most=len(todo))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            count, when, note = dlg.count.value(), dlg.date.value(), dlg.note.text().strip() or None
        self._mark(todo[:count], stage, when or date.today(), note)

    def mark_rows(self, rows: list[PieceRow], stage: str, ask: bool = False) -> None:
        todo = [r for r in rows if r.can_mark(stage)]
        if not todo:
            info(self, STAGE_LABELS[stage], f"None of the selected units can be marked {STAGE_LABELS[stage]} now.")
            return
        when, note = date.today(), None
        if ask:
            what = todo[0].unit if len(todo) == 1 else f"{len(todo)} units"
            dlg = MarkDialog(self, f"{STAGE_LABELS[stage]} - {what}")
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            when, note = dlg.date.value(), dlg.note.text().strip() or None
        self._mark(todo, stage, when, note)

    def _mark(self, rows: list[PieceRow], stage: str, when: date, note: str | None) -> None:
        marks = run(self, self.svc.mark_many, [r.key for r in rows], stage, when, note, skip_done=len(rows) > 1)
        if marks is None:
            return
        self._reload({r.site_id for r in rows})
        if len(marks) < len(rows):
            # another PC marked some of them in the meantime (they are left as they were)
            info(self, STAGE_LABELS[stage], f"{len(marks)} of {len(rows)} unit(s) marked {STAGE_LABELS[stage]}. "
                                            f"The other {len(rows) - len(marks)} had been marked already "
                                            "(on another PC) or cannot be marked yet.")

    def change_date(self, r: PieceRow, stage: str) -> None:
        m = r.marks.get(stage)
        if m is None:
            return
        if m.batch:
            info(self, "Change date", "This unit was recorded as done together with others on the unit card: "
                                      "change it there.")
            return
        dlg = MarkDialog(self, f"{STAGE_LABELS[stage]} - {r.unit}", m.on, m.note)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if run(self, self.svc.change_date, m.id, dlg.date.value(), dlg.note.text().strip() or None) is not None:
            self._reload({r.site_id})

    def cancel_mark(self, r: PieceRow) -> None:
        stage = r.status
        m = r.marks.get(stage)
        if m is None:
            return
        reason = ask_text(self, f"Cancel {STAGE_LABELS[stage]}",
                          f"Cancel the {STAGE_LABELS[stage]} mark of {fmt_date(m.on)} on {r.unit}?\n"
                          "It stays in the history. Reason:")
        if reason is None:
            return
        run(self, self.svc.cancel, m.id, reason)
        self._reload({r.site_id})

    def show_history(self, r: PieceRow) -> None:
        hist = run(self, self.svc.history, r.unit_id, r.piece_no)
        if hist is None:
            return
        if not hist:
            info(self, "Mark history", f"{r.unit}: nothing recorded yet.")
            return
        lines = []
        for h in hist:
            what = STAGE_LABELS[h.stage] + (f" ({h.batch_qty} units together)" if h.batch_qty else "")
            line = f"{fmt_date(h.on)}  {what}  - {h.user or '?'}, entered {fmt_datetime(h.recorded_at)}"
            if h.note:
                line += f"  - {h.note}"
            if h.cancelled:
                line += f"  [CANCELLED: {h.cancel_reason}]"
            lines.append(line)
        info(self, f"Mark history - {r.unit}", "\n".join(lines))

    def _reload(self, sites: set[int]) -> None:
        """Re-read the projects that changed (only those) and draw the tree again."""
        fresh = run(self, self.svc.board, self.closed.isChecked(), sites)
        if fresh is None:
            return
        new_rows: dict = defaultdict(list)
        for r in fresh.rows:
            new_rows[r.site_id].append(r)
        order = list(dict.fromkeys(r.site_id for r in self.rows))
        old: dict = defaultdict(list)
        for r in self.rows:
            old[r.site_id].append(r)
        self.board.rows = [r for sid in order for r in (new_rows[sid] if sid in sites else old[sid])]
        cards = {c.site_id: c for c in fresh.cards}
        self.board.cards = [cards.get(c.site_id, c) for c in self.board.cards]
        self._update_cards()
        self._rebuild()
        self._skip_refresh = True
        try:
            self.app.changed()
        finally:
            self._skip_refresh = False

    # -- menu -------------------------------------------------------------------------------------
    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        n = self.tree.node(item)
        if n is None:
            return
        m = QMenu(self)
        if n.piece is None:
            if self.can_edit:
                for stage in STAGES:
                    can = len(n.markable(stage, self.filtering))
                    if can:
                        m.addAction(f"Mark next {STAGE_LABELS[stage]}... ({can} can be marked)",
                                    lambda s=stage: self.mark_next(n, s))
                m.addSeparator()
            m.addAction("Open all below", lambda: self._open_below(item))
            if n.kind == "project":
                sid = n.rows[0].site_id
                m.addAction("Show only this project", lambda: self.pick_project(sid))
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if item not in self.tree.selectedItems():
            self.tree.clearSelection()
            item.setSelected(True)
        selected = self.tree.selected_pieces() or [n.piece]
        r = n.piece
        if self.can_edit:
            many = len(selected) > 1
            for stage in STAGES:
                ok = [p for p in selected if p.can_mark(stage)]
                if not ok:
                    continue
                suffix = f" ({len(ok)} units)" if many else ""
                m.addAction(f"Mark {STAGE_LABELS[stage]} today{suffix}", lambda s=stage, rs=ok: self.mark_rows(rs, s))
                m.addAction(f"Mark {STAGE_LABELS[stage]} with a date...{suffix}",
                            lambda s=stage, rs=ok: self.mark_rows(rs, s, ask=True))
            m.addSeparator()
            for stage in STAGES:
                if stage in r.marks and not r.marks[stage].batch:
                    m.addAction(f"Change {STAGE_LABELS[stage]} date...", lambda s=stage: self.change_date(r, s))
            if r.status != NOT_STARTED and not r.marks[r.status].batch:
                m.addAction(f"Cancel {STAGE_LABELS[r.status]} mark...", lambda: self.cancel_mark(r))
            m.addSeparator()
        m.addAction("Mark history...", lambda: self.show_history(r))
        if self.site_id is None:
            m.addAction(f"Show only {r.project}", lambda: self.pick_project(r.site_id))
        m.addAction("Open in Projects tree", lambda: self._open_tree(r))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_below(self, item: QTreeWidgetItem) -> None:
        stack = [item]
        while stack:
            it = stack.pop()
            n = self.tree.node(it)
            if n is None or n.kind == "piece":
                continue
            it.setExpanded(True)
            stack += [it.child(i) for i in range(it.childCount())]

    def _open_tree(self, r: PieceRow) -> None:
        mw = getattr(self, "main_window", None)
        if mw is None:
            return
        mw.go("projects")
        page = mw.page("projects")
        if page is not None and hasattr(page, "search"):
            page.search.setText(r.unit_code or r.unit_name)

    # -- export -----------------------------------------------------------------------------------
    def shown_rows(self) -> list[PieceRow]:
        """The units the project choice and the filters leave, one row per piece."""
        keeps = self._keeper()
        return [r for r in self._rows_of(self.site_id) if keeps(r)]

    def export(self) -> None:
        from pathlib import Path

        from wms.exporters.export import export_xlsx
        from wms.services.reports import DATE, TEXT, Column, ReportResult
        rows = self.shown_rows()
        if not rows:
            info(self, "Export", "There are no units to export.")
            return
        default = Path.home() / f"Production_{date.today():%Y-%m-%d}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Export production dashboard", str(default), "Excel (*.xlsx)")
        if not path:
            return
        cols = [Column("Project", TEXT), Column("Manager", TEXT), Column("Block", TEXT), Column("Level", TEXT),
                Column("Unit", TEXT), Column("Quantity", TEXT), Column("Status", TEXT),
                Column("Done", DATE), Column("Dispatched", DATE), Column("On site", DATE)]
        data = [[r.project, r.manager, r.block, r.level, r.unit, "1", STATUS_LABELS[r.status],
                 r.date_of(STAGE_DONE), r.date_of(STAGE_DISPATCHED), r.date_of(STAGE_ON_SITE)] for r in rows]
        result = ReportResult("Production dashboard", cols, data, self.count.text())
        if run(self, export_xlsx, result, Path(path), self.app.ctx.config.company_name) is not None:
            info(self, "Export", f"Saved {len(rows)} unit(s) to\n{path}")
