"""Production dashboard (v1.24): project cards with progress and one row per piece of every unit.

Click an empty Done / Dispatched / On site cell to mark it with today's date; double-click a date
to change it. Click a project card to show only that project's pieces. The rows follow the
Projects tree (project, block, level, unit) - nothing is copied, see ``services.unit_progress``.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter
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
    QMenu,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from wms.core.numbers import fmt_date, fmt_datetime
from wms.core.permissions import P
from wms.db.models.hierarchy import STAGE_DISPATCHED, STAGE_DONE, STAGE_ON_SITE, STAGES
from wms.services.unit_progress import NOT_STARTED, STAGE_LABELS, STATUS_LABELS, STATUSES, PieceRow
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DateEdit, SearchBox, ask_text, button, info, label, run

#: row colours by status (Ihor 13/11/2026: grey not started, yellow done, blue dispatched, green on site)
ROW_COLOURS = {NOT_STARTED: "#EEF0F3", STAGE_DONE: "#FEF5D4", STAGE_DISPATCHED: "#DCEBFC", STAGE_ON_SITE: "#DDF3E2"}
#: the same stages on the cards' progress bars (stronger, so thin bars still read)
BAR_COLOURS = {STAGE_ON_SITE: "#3BA55C", STAGE_DISPATCHED: "#4A8FE0", STAGE_DONE: "#F5C043", NOT_STARTED: "#E4E7EC"}

COLUMNS = ("Project", "Block", "Level", "Unit", "Quantity", "Done", "Dispatched", "On site")
STAGE_COL = {5: STAGE_DONE, 6: STAGE_DISPATCHED, 7: STAGE_ON_SITE}
COL_OF = {v: k for k, v in STAGE_COL.items()}
ALL = "__all__"


# ---- table ----------------------------------------------------------------------------------------
class BoardModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[PieceRow] = []
        self.can_edit = True

    def set_rows(self, rows: list[PieceRow]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def row_changed(self, i: int) -> None:
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        col = index.column()
        stage = STAGE_COL.get(col)
        if role == Qt.ItemDataRole.DisplayRole:
            if stage:
                m = r.marks.get(stage)
                if m:
                    return f"✔ {fmt_date(m.on)}"
                return "○ Mark" if r.can_mark(stage) and self.can_edit else ""
            return (r.project, r.block or "—", r.level or "—", r.unit, "1")[col]
        if role == Qt.ItemDataRole.BackgroundRole:
            return QBrush(QColor(ROW_COLOURS[r.status]))
        if role == Qt.ItemDataRole.ForegroundRole and stage:
            if stage in r.marks:
                return QBrush(QColor("#067647"))
            return QBrush(QColor("#667085"))
        if role == Qt.ItemDataRole.FontRole and stage and stage in r.marks:
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 4:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if stage:
                m = r.marks.get(stage)
                if m:
                    tip = f"{STAGE_LABELS[stage]} on {fmt_date(m.on)}"
                    if m.user:
                        tip += f" by {m.user}"
                    if m.note:
                        tip += f"\n{m.note}"
                    if m.batch:
                        tip += "\nRecorded for several units together (unit card)."
                    return tip + ("" if m.batch else "\nDouble-click to change the date")
                if r.can_mark(stage):
                    return f"Click to mark {STAGE_LABELS[stage]} today; double-click to choose the date"
                prev = {STAGE_DISPATCHED: STAGE_DONE, STAGE_ON_SITE: STAGE_DISPATCHED}.get(stage)
                return f"Mark {STAGE_LABELS[prev]} first" if prev else None
            if col == 3:
                return f"{r.project_number} / {r.block or '-'} / {r.level or '-'} / {r.unit}"
        return None


class BoardFilter(QSortFilterProxyModel):
    """Project / manager / block / level / status / text filters, kept in tree order (no sorting)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.site_id = None
        self.manager = None
        self.block = None
        self.level = None
        self.status = None
        self.text = ""

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.refilter()

    def refilter(self) -> None:
        """Run the filters again (Qt 6.10+ has begin/endFilterChange; older ones invalidateFilter)."""
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:  # pragma: no cover - older Qt
            self.invalidateFilter()

    def accepts(self, r: PieceRow, skip: str | None = None) -> bool:
        if self.site_id is not None and r.site_id != self.site_id and skip != "site":
            return False
        if self.manager is not None and r.manager != self.manager and skip != "manager":
            return False
        if self.block is not None and (r.block or "—") != self.block and skip != "block":
            return False
        if self.level is not None and (r.level or "—") != self.level and skip != "level":
            return False
        if self.status is not None and r.status != self.status:
            return False
        if self.text:
            hay = f"{r.project_number} {r.project} {r.manager} {r.block} {r.level} {r.unit}".casefold()
            if not all(w in hay for w in self.text.casefold().split()):
                return False
        return True

    def filterAcceptsRow(self, source_row, source_parent):  # noqa: N802
        return self.accepts(self.sourceModel().rows[source_row])


class BoardTable(QTableView):
    stage_clicked = Signal(int, str)        # source row, stage
    stage_double_clicked = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = BoardModel(self)
        self.proxy = BoardFilter(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        hdr = self.horizontalHeader()
        hdr.setHighlightSections(False)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        for i, w in enumerate((180, 110, 100, 230, 82, 125, 125)):
            self.setColumnWidth(i, w)
        self.setWordWrap(False)
        self.setStyleSheet("QTableView { selection-background-color: #B9D4F1; selection-color: #101828; }")
        self.setMouseTracking(True)
        self.clicked.connect(lambda idx: self._emit(idx, self.stage_clicked))
        self.doubleClicked.connect(lambda idx: self._emit(idx, self.stage_double_clicked))

    def _emit(self, idx, signal) -> None:
        stage = STAGE_COL.get(idx.column())
        if idx.isValid() and stage:
            signal.emit(self.proxy.mapToSource(idx).row(), stage)

    def mouseMoveEvent(self, event):  # noqa: N802 - pointing hand over cells that can be marked
        idx = self.indexAt(event.position().toPoint())
        hand = False
        if idx.isValid() and idx.column() in STAGE_COL and self.model_.can_edit:
            r = self.model_.rows[self.proxy.mapToSource(idx).row()]
            hand = r.can_mark(STAGE_COL[idx.column()])
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if hand else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def source_rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(i).row() for i in self.selectionModel().selectedRows()})

    def shown(self) -> list[PieceRow]:
        return [self.model_.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()]
                for i in range(self.proxy.rowCount())]


# ---- project cards --------------------------------------------------------------------------------
class ProgressBar(QWidget):
    """On site (green), dispatched (blue), done (yellow) and not started (grey), left to right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
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
        p.drawRoundedRect(QRectF(0, 0, w, h), 5, 5)
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
    clicked = Signal(object)  # site id, or None for "All projects"

    def __init__(self, site_id, parent=None):
        super().__init__(parent)
        self.site_id = site_id
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(126 if site_id is None else 238)
        self.setFixedHeight(84)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(3)
        self.name = label("", "CardName")
        self.name.setStyleSheet("font-weight: 700;")
        self.sub = label("", "CardLabel")
        lay.addWidget(self.name)
        lay.addWidget(self.sub)
        self.bar = ProgressBar()
        self.nums = label("")
        self.nums.setStyleSheet("font-size: 8pt; color: #344054;")
        if site_id is not None:
            lay.addWidget(self.bar)
        lay.addWidget(self.nums)
        lay.addStretch(1)
        self.set_active(False)

    def set_active(self, on: bool) -> None:
        self.active = on
        self.setStyleSheet("QFrame#Card { background: %s; border: %s; border-radius: 8px; }"
                           % (("#F5FAFF", "2px solid #2F6FB2") if on else ("white", "1px solid #E4E7EC")))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.site_id)
        super().mousePressEvent(event)


def _dot(colour: str) -> str:
    return f"<span style='color:{colour}'>■</span>"


# ---- dialog ---------------------------------------------------------------------------------------
class MarkDialog(QDialog):
    """Date (today by default, never in the future) and an optional note for a mark."""

    def __init__(self, parent, title: str, when: date | None = None, note: str | None = None, what: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(340)
        lay = QVBoxLayout(self)
        if what:
            lay.addWidget(label(what, wrap=True))
        form = QFormLayout()
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
    subtitle = ("Every unit of every project: made, dispatched, on site. Click an empty cell to mark it with "
                "today's date; double-click a date to change it. Click a project card to show only its units.")
    margins = (16, 10, 16, 8)
    spacing = 6

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self._skip_refresh = False
        self.cards: dict = {}
        self.add_action(button("Export to Excel", self.export))
        self.add_action(button("Projects tree", lambda: self.main_window.go("projects")
                               if getattr(self, "main_window", None) else None))

        # project cards: one row, scrolls sideways when there are many projects
        self.card_area = QScrollArea()
        self.card_area.setWidgetResizable(True)
        self.card_area.setFrameShape(QFrame.Shape.NoFrame)
        self.card_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.card_area.setFixedHeight(100)
        holder = QWidget()
        self.card_row = QHBoxLayout(holder)
        self.card_row.setContentsMargins(0, 0, 0, 0)
        self.card_row.setSpacing(8)
        self.card_area.setWidget(holder)
        self.body.addWidget(self.card_area)

        # filters
        self.f_project = QComboBox()
        self.f_manager = QComboBox()
        self.f_block = QComboBox()
        self.f_level = QComboBox()
        self.f_status = QComboBox()
        self.f_status.addItem("Status: All", None)
        for s in STATUSES:
            self.f_status.addItem(STATUS_LABELS[s], s)
        self.search = SearchBox("Search unit, code, project, block, level...")
        self.closed = QCheckBox("Closed projects too")
        self.closed.setToolTip("Also show completed and cancelled projects (archived ones never show)")
        frow = QHBoxLayout()
        frow.setSpacing(6)
        for w in (self.f_project, self.f_manager, self.f_block, self.f_level, self.f_status):
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            w.setMaximumWidth(220)
            frow.addWidget(w)
        frow.addWidget(self.search, 1)
        frow.addWidget(self.closed)
        frow.addWidget(button("Clear filters", self.clear_filters))
        self.body.addLayout(frow)
        self.f_project.currentIndexChanged.connect(lambda _: self._filter_changed("project"))
        self.f_manager.currentIndexChanged.connect(lambda _: self._filter_changed("manager"))
        self.f_block.currentIndexChanged.connect(lambda _: self._filter_changed("block"))
        self.f_level.currentIndexChanged.connect(lambda _: self._filter_changed("level"))
        self.f_status.currentIndexChanged.connect(lambda _: self._filter_changed("status"))
        self.search.search.connect(lambda _: self._filter_changed("text"))
        self.closed.toggled.connect(lambda _: self.refresh())

        self.table = BoardTable()
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.stage_clicked.connect(self._cell_clicked)
        self.table.stage_double_clicked.connect(self._cell_double_clicked)
        self.body.addWidget(self.table, 1)

        foot = QHBoxLayout()
        self.count = label("", "Muted")
        foot.addWidget(self.count)
        legend = "  ".join(f"{_dot('#C9CED6' if s == NOT_STARTED else BAR_COLOURS[s])} {STATUS_LABELS[s]}"
                           for s in STATUSES)
        lg = label(legend + "   ·   Right-click a row: mark several, change a date, cancel a mark, history", "Muted")
        foot.addWidget(lg)
        foot.addStretch(1)
        self.body.addLayout(foot)
        self._filling = False

    # -- data -------------------------------------------------------------------------------------
    @property
    def svc(self):
        return self.app.services.unit_progress

    @property
    def can_edit(self) -> bool:
        return self.app.ctx.has(P.SITES_EDIT)

    @property
    def rows(self) -> list[PieceRow]:
        return self.table.model_.rows

    def refresh(self) -> None:
        if self._skip_refresh:
            return
        board = run(self, self.svc.board, self.closed.isChecked())
        if board is None:
            return
        self.board = board
        self.table.model_.can_edit = self.can_edit
        self.table.model_.set_rows(board.rows)
        self._build_cards()
        self._fill_filters()
        self._apply()

    def _build_cards(self) -> None:
        while self.card_row.count():
            w = self.card_row.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.cards = {}
        allc = ProjectCardWidget(None)
        allc.name.setText("All projects")
        allc.clicked.connect(self.pick_project)
        self.card_row.addWidget(allc)
        self.cards[None] = allc
        for c in self.board.cards:
            w = ProjectCardWidget(c.site_id)
            w.name.setText(c.title)
            w.name.setToolTip(f"{c.project_number} - {c.title}")
            w.clicked.connect(self.pick_project)
            self.card_row.addWidget(w)
            self.cards[c.site_id] = w
        self.card_row.addStretch(1)
        self._update_cards()

    def _update_cards(self) -> None:
        """Card figures from the rows on screen (after a mark they change without a reload)."""
        per: dict = {}
        for r in self.rows:
            t = per.setdefault(r.site_id, [0, 0, 0, 0])
            t[0] += 1
            t[1] += STAGE_DONE in r.marks
            t[2] += STAGE_DISPATCHED in r.marks
            t[3] += STAGE_ON_SITE in r.marks
        for c in self.board.cards:
            w = self.cards.get(c.site_id)
            if w is None:
                continue
            total, done, disp, site = per.get(c.site_id, [0, 0, 0, 0])
            w.sub.setText(f"{c.manager or 'No manager'} · {total} units")
            w.bar.set_parts(site, max(disp - site, 0), max(done - disp, 0), max(total - done, 0))
            w.nums.setText(f"{_dot(BAR_COLOURS[STAGE_DONE])} Done <b>{done}</b> &nbsp;"
                           f"{_dot(BAR_COLOURS[STAGE_DISPATCHED])} Disp. <b>{disp}</b> &nbsp;"
                           f"{_dot(BAR_COLOURS[STAGE_ON_SITE])} Site <b>{site}</b> &nbsp;/ {total}")
        allc = self.cards[None]
        total = len(self.rows)
        allc.sub.setText(f"{len(self.board.cards)} project(s)")
        allc.nums.setText(f"<b>{total}</b> units<br>{sum(STAGE_ON_SITE in r.marks for r in self.rows)} on site")
        self._mark_active_card()

    def _mark_active_card(self) -> None:
        sid = self.f_project.currentData()
        sid = None if sid in (None, ALL) else sid
        for key, w in self.cards.items():
            w.set_active(key == sid)

    # -- filters ----------------------------------------------------------------------------------
    def _fill_filters(self) -> None:
        self._filling = True
        try:
            keep = {w: w.currentData() for w in (self.f_project, self.f_manager, self.f_block, self.f_level)}
            p = self.table.proxy
            self._set_options(self.f_project, "Project", [(f"{c.title}", c.site_id) for c in self.board.cards],
                              keep[self.f_project])
            managers = sorted({r.manager for r in self.rows if r.manager})
            self._set_options(self.f_manager, "Manager", [(m, m) for m in managers], keep[self.f_manager])
            # blocks / levels offered: those of the rows the other filters leave
            in_scope = [r for r in self.rows if p.accepts(r, skip="block")]
            blocks = list(dict.fromkeys((r.block or "—") for r in in_scope))
            self._set_options(self.f_block, "Block", [(b, b) for b in blocks], keep[self.f_block])
            in_scope = [r for r in self.rows if p.accepts(r, skip="level")]
            levels = list(dict.fromkeys((r.level or "—") for r in in_scope))
            self._set_options(self.f_level, "Level", [(lv, lv) for lv in levels], keep[self.f_level])
        finally:
            self._filling = False

    @staticmethod
    def _set_options(combo: QComboBox, name: str, options, keep) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"{name}: All", ALL)
        for text, val in options:
            combo.addItem(text, val)
        i = combo.findData(keep) if keep is not None else -1
        combo.setCurrentIndex(max(i, 0))
        combo.blockSignals(False)

    @staticmethod
    def _val(combo: QComboBox):
        v = combo.currentData()
        return None if v in (None, ALL) else v

    def _filter_changed(self, which: str) -> None:
        if self._filling:
            return
        self._apply()
        if which in ("project", "manager", "block", "level"):
            self._fill_filters()
            self._apply()

    def _apply(self) -> None:
        self.table.proxy.set(site_id=self._val(self.f_project), manager=self._val(self.f_manager),
                             block=self._val(self.f_block), level=self._val(self.f_level),
                             status=self.f_status.currentData(), text=self.search.text().strip())
        for combo in (self.f_project, self.f_manager, self.f_block, self.f_level):
            combo.setStyleSheet("QComboBox { border: 1px solid #2F6FB2; background: #EFF8FF; color: #175CD3; "
                                "font-weight: 600; }" if self._val(combo) is not None else "")
        self.f_status.setStyleSheet("QComboBox { border: 1px solid #2F6FB2; background: #EFF8FF; color: #175CD3; "
                                    "font-weight: 600; }" if self.f_status.currentData() else "")
        shown = self.table.proxy.rowCount()
        self.count.setText(f"{shown} of {len(self.rows)} units shown")
        self._mark_active_card()

    def pick_project(self, site_id) -> None:
        """Card click: only that project's units (click the active card or All projects to show all)."""
        if site_id is not None and self._val(self.f_project) == site_id:
            site_id = None
        self.f_block.setCurrentIndex(0)
        self.f_level.setCurrentIndex(0)
        i = self.f_project.findData(site_id if site_id is not None else ALL)
        self.f_project.setCurrentIndex(max(i, 0))

    def clear_filters(self) -> None:
        self._filling = True
        for combo in (self.f_project, self.f_manager, self.f_block, self.f_level, self.f_status):
            combo.setCurrentIndex(0)
        self.search.clear()
        self._filling = False
        self._fill_filters()
        self._apply()

    def apply_preset(self, preset: dict) -> None:
        if "site_id" in preset:
            self.on_show()
            self.pick_project(preset["site_id"])

    # -- marking ----------------------------------------------------------------------------------
    def _cell_clicked(self, i: int, stage: str) -> None:
        r = self.rows[i]
        if not self.can_edit or not r.can_mark(stage):
            return
        selected = self.table.source_rows()
        if len(selected) > 1 and i in selected:
            self.mark_rows(selected, stage)
        else:
            self.mark_rows([i], stage)

    def _cell_double_clicked(self, i: int, stage: str) -> None:
        if not self.can_edit:
            return
        r = self.rows[i]
        # a double-click on an empty cell first marks it today (the single click), then opens the
        # date so it can be changed straight away
        if r.marks.get(stage) is not None:
            self.change_date(i, stage)
        elif r.can_mark(stage):
            self.mark_rows([i], stage, ask=True)

    def mark_rows(self, rows: list[int], stage: str, ask: bool = False) -> None:
        todo = [i for i in rows if self.rows[i].can_mark(stage)]
        if not todo:
            info(self, STAGE_LABELS[stage], f"None of the selected units can be marked {STAGE_LABELS[stage]} now.")
            return
        when, note = date.today(), None
        if ask:
            what = (self.rows[todo[0]].unit if len(todo) == 1 else f"{len(todo)} units")
            dlg = MarkDialog(self, f"{STAGE_LABELS[stage]} - {what}", what="")
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            when, note = dlg.date.value(), dlg.note.text().strip() or None
        marks = run(self, self.svc.mark_many, [self.rows[i].key for i in todo], stage, when, note,
                    skip_done=len(todo) > 1)
        if marks is None:
            return
        self._reload_rows(todo)

    def change_date(self, i: int, stage: str) -> None:
        r = self.rows[i]
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
            self._reload_rows([i])

    def cancel_mark(self, i: int) -> None:
        r = self.rows[i]
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
        self._reload_rows([i])

    def show_history(self, i: int) -> None:
        r = self.rows[i]
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

    def _reload_rows(self, rows: list[int]) -> None:
        """Re-read the marks of the changed pieces (whole units: a batch can move between pieces)."""
        units = {self.rows[i].unit_id for i in rows}
        sites = {self.rows[i].site_id for i in rows}
        fresh = run(self, self.svc.board, self.closed.isChecked(), sites)
        if fresh is None:
            return
        by_key = {r.key: r for r in fresh.rows}
        for i, r in enumerate(self.rows):
            if r.unit_id in units and r.key in by_key:
                r.marks = by_key[r.key].marks
                self.table.model_.row_changed(i)
        self.table.proxy.refilter()
        self._update_cards()
        self._apply()
        self._skip_refresh = True
        try:
            self.app.changed()
        finally:
            self._skip_refresh = False

    # -- menu -------------------------------------------------------------------------------------
    def _menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        i = self.table.proxy.mapToSource(idx).row()
        selected = self.table.source_rows()
        if i not in selected:
            self.table.selectRow(idx.row())
            selected = [i]
        r = self.rows[i]
        m = QMenu(self)
        if self.can_edit:
            many = len(selected) > 1
            for stage in STAGES:
                ok = [j for j in selected if self.rows[j].can_mark(stage)]
                if not ok:
                    continue
                n = f" ({len(ok)} units)" if many else ""
                m.addAction(f"Mark {STAGE_LABELS[stage]} today{n}", lambda s=stage, rs=ok: self.mark_rows(rs, s))
                m.addAction(f"Mark {STAGE_LABELS[stage]} with a date...{n}",
                            lambda s=stage, rs=ok: self.mark_rows(rs, s, ask=True))
            m.addSeparator()
            for stage in STAGES:
                if stage in r.marks and not r.marks[stage].batch:
                    m.addAction(f"Change {STAGE_LABELS[stage]} date...", lambda s=stage: self.change_date(i, s))
            if r.status != NOT_STARTED and not r.marks[r.status].batch:
                m.addAction(f"Cancel {STAGE_LABELS[r.status]} mark...", lambda: self.cancel_mark(i))
            m.addSeparator()
        m.addAction("Mark history...", lambda: self.show_history(i))
        m.addAction(f"Show only {r.project}", lambda: self.pick_project(r.site_id))
        m.addAction("Open in Projects tree", lambda: self._open_tree(r))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _open_tree(self, r: PieceRow) -> None:
        mw = getattr(self, "main_window", None)
        if mw is None:
            return
        mw.go("projects")
        page = mw.page("projects")
        if page is not None and hasattr(page, "search"):
            page.search.setText(r.unit_code or r.unit_name)

    # -- export -----------------------------------------------------------------------------------
    def export(self) -> None:
        from pathlib import Path

        from wms.exporters.export import export_xlsx
        from wms.services.reports import DATE, TEXT, Column, ReportResult
        rows = self.table.shown()
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
