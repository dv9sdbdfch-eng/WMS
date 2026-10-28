"""Reusable widgets: data tables, inputs, pickers and message helpers."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from wms.core.exceptions import InsufficientStockError, WMSError
from wms.core.numbers import fmt_date, fmt_datetime, fmt_money, fmt_qty, to_decimal
from wms.ui import theme

log = logging.getLogger(__name__)

TEXT, QTY, MONEY, DATE, DATETIME, INT, BOOL = "text", "qty", "money", "date", "datetime", "int", "bool"


# --- messages ---------------------------------------------------------------------

def show_error(parent: QWidget | None, exc: BaseException, title: str = "Cannot complete") -> None:
    if isinstance(exc, WMSError):
        QMessageBox.warning(parent, title, exc.message)
    else:
        log.error("Unexpected error\n%s", "".join(traceback.format_exception(exc)))
        QMessageBox.critical(parent, "Unexpected error",
                             f"Something went wrong and nothing was saved.\n\n{type(exc).__name__}: {exc}\n\n"
                             f"The details have been written to the log file.")


def info(parent: QWidget | None, title: str, text: str) -> None:
    QMessageBox.information(parent, title, text)


def confirm(parent: QWidget | None, title: str, text: str, yes: str = "Yes", danger: bool = False) -> bool:
    box = QMessageBox(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question, title, text, parent=parent)
    yes_btn = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is yes_btn


def ask_text(parent: QWidget | None, title: str, label: str, required: bool = True) -> str | None:
    while True:
        text, ok = QInputDialog.getMultiLineText(parent, title, label)
        if not ok:
            return None
        if text.strip() or not required:
            return text.strip()
        QMessageBox.warning(parent, title, "Please enter a reason.")


def run(parent: QWidget | None, fn: Callable, *args, **kwargs):
    """Call a service; show any error. Returns the result, or None on failure."""
    try:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return fn(*args, **kwargs)
        finally:
            QGuiApplication.restoreOverrideCursor()
    except Exception as exc:  # noqa: BLE001 - every error must reach the user
        show_error(parent, exc)
        return None


def run_with_override(parent: QWidget | None, can_override: bool, fn: Callable, **kwargs):
    """Call a posting service; if stock would go negative, offer an authorised override."""
    try:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return fn(**kwargs)
        finally:
            QGuiApplication.restoreOverrideCursor()
    except InsufficientStockError as exc:
        if not can_override:
            QMessageBox.warning(parent, "Not enough stock",
                                f"{exc.message}\n\nNothing was posted. Check the quantity or ask a manager.")
            return None
        if not confirm(parent, "Not enough stock",
                       f"{exc.message}\n\nPosting anyway will leave NEGATIVE stock. Only do this if the goods are "
                       f"physically there and the system count is wrong.\n\nOverride and post?",
                       yes="Override", danger=True):
            return None
        reason = ask_text(parent, "Override reason", "Why is the stock being overridden? (recorded in the audit trail)")
        if not reason:
            return None
        return run(parent, fn, override_reason=reason, **kwargs)
    except Exception as exc:  # noqa: BLE001
        show_error(parent, exc)
        return None


# --- table -----------------------------------------------------------------------

@dataclass
class Col:
    title: str
    get: Callable[[Any], Any]
    kind: str = TEXT
    width: int | None = None


class ObjectTableModel(QAbstractTableModel):
    def __init__(self, columns: Sequence[Col], parent=None):
        super().__init__(parent)
        self.columns = list(columns)
        self.items: list = []
        self.row_style: Callable[[Any], str | None] | None = None  # returns "low", "negative", "muted"

    def set_items(self, items: Iterable) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].title
        return None

    def raw(self, row: int, col: int):
        try:
            return self.columns[col].get(self.items[row])
        except Exception:  # noqa: BLE001 - a broken getter must not crash the table
            log.exception("Column getter failed")
            return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = self.columns[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return format_value(self.raw(index.row(), index.column()), col.kind)
        if role == Qt.ItemDataRole.UserRole:  # sort key
            v = self.raw(index.row(), index.column())
            if isinstance(v, Decimal):
                return float(v)
            if isinstance(v, (date, datetime)):
                return v.isoformat()
            if v is None:
                return "" if col.kind in (TEXT, BOOL) else float("-inf")
            return v
        if role == Qt.ItemDataRole.TextAlignmentRole and col.kind in (QTY, MONEY, INT):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole and self.row_style:
            if self.row_style(self.items[index.row()]) == "low":
                return QBrush(QColor(theme.LOW_STOCK_BG))
        if role == Qt.ItemDataRole.ForegroundRole:
            v = self.raw(index.row(), index.column())
            if col.kind == QTY and isinstance(v, Decimal) and v < 0:
                return QBrush(QColor(theme.NEGATIVE_FG))
            if self.row_style and self.row_style(self.items[index.row()]) == "muted":
                return QBrush(QColor("#98A2B3"))
        return None


def format_value(v, kind: str) -> str:
    if v is None:
        return ""
    if kind == QTY:
        return fmt_qty(v)
    if kind == MONEY:
        return fmt_money(v)
    if kind == DATE:
        return fmt_date(v)
    if kind == DATETIME:
        return fmt_datetime(v)
    if kind == BOOL:
        return "Yes" if v else "No"
    return str(v)


class DataTable(QTableView):
    """Sortable read-only table of objects. Double-click / Enter emits ``activated_item``."""

    activated_item = Signal(object)
    selection_changed_item = Signal(object)

    def __init__(self, columns: Sequence[Col], parent=None):
        super().__init__(parent)
        self.model_ = ObjectTableModel(columns, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(28)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setHighlightSections(False)
        self.setWordWrap(False)
        self.doubleClicked.connect(lambda idx: self._emit_activated())
        self.selectionModel().currentRowChanged.connect(lambda *_: self.selection_changed_item.emit(self.current()))
        for i, c in enumerate(columns):
            if c.width:
                self.setColumnWidth(i, c.width)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, self._emit_activated,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence.StandardKey.Copy, self, self.copy_selection, context=Qt.ShortcutContext.WidgetShortcut)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._sorted_once = False

    def _menu(self, pos):
        m = QMenu(self)
        m.addAction("Open", self._emit_activated)
        m.addAction("Copy row", self.copy_selection)
        m.addAction("Copy whole table", self.copy_all)
        m.exec(self.viewport().mapToGlobal(pos))

    def _emit_activated(self):
        item = self.current()
        if item is not None:
            self.activated_item.emit(item)

    def set_items(self, items: Iterable, keep_selection: bool = True) -> None:
        current = self.current()
        self.model_.set_items(items)
        if not self._sorted_once:
            self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            self._sorted_once = True
        if keep_selection and current is not None:
            key = getattr(current, "id", None)
            for r in range(self.proxy.rowCount()):
                src = self.proxy.mapToSource(self.proxy.index(r, 0)).row()
                if getattr(self.model_.items[src], "id", object()) == key:
                    self.selectRow(r)
                    break

    def set_row_style(self, fn: Callable[[Any], str | None]) -> None:
        self.model_.row_style = fn

    def current(self):
        idx = self.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        return self.model_.items[src.row()] if 0 <= src.row() < len(self.model_.items) else None

    def items(self) -> list:
        return list(self.model_.items)

    def _rows_text(self, rows: Iterable[int]) -> str:
        cols = range(self.model_.columnCount())
        lines = []
        for r in rows:
            lines.append("\t".join(self.proxy.index(r, c).data() or "" for c in cols))
        return "\n".join(lines)

    def copy_selection(self):
        rows = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if rows:
            QGuiApplication.clipboard().setText(self._rows_text(rows))

    def copy_all(self):
        header = "\t".join(c.title for c in self.model_.columns)
        QGuiApplication.clipboard().setText(header + "\n" + self._rows_text(range(self.proxy.rowCount())))

    def fit_columns(self):
        self.resizeColumnsToContents()
        for i in range(self.model_.columnCount()):
            if self.columnWidth(i) > 360:
                self.setColumnWidth(i, 360)


# --- inputs ----------------------------------------------------------------------

class SearchBox(QLineEdit):
    """Line edit that emits ``search`` 300 ms after typing stops (or on Enter)."""

    search = Signal(str)

    def __init__(self, placeholder: str = "Search...", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self._t = QTimer(self, singleShot=True, interval=300)
        self._t.timeout.connect(lambda: self.search.emit(self.text()))
        self.textChanged.connect(lambda _: self._t.start())
        self.returnPressed.connect(lambda: (self._t.stop(), self.search.emit(self.text())))


class DecimalEdit(QLineEdit):
    """Quantity / money input. ``value()`` returns Decimal or None; invalid input is highlighted."""

    def __init__(self, places: str = "0.001", allow_negative: bool = False, parent=None):
        super().__init__(parent)
        self._places = Decimal(places)
        self._neg = allow_negative
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.textChanged.connect(self._check)

    def _check(self):
        ok = True
        if self.text().strip():
            try:
                v = to_decimal(self.text(), places=self._places)
                ok = self._neg or v >= 0
            except WMSError:
                ok = False
        self.setProperty("invalid", not ok)
        self.style().unpolish(self)
        self.style().polish(self)

    def value(self) -> Decimal | None:
        t = self.text().strip()
        return to_decimal(t, places=self._places) if t else None

    def set_value(self, v: Decimal | None):
        if v is None:
            self.setText("")
            return
        d = Decimal(v).normalize()
        if self._places < Decimal("0.001"):  # money: always show pence, e.g. 24.50
            d = d.quantize(Decimal("0.01")) if d == d.quantize(Decimal("0.01")) else d
        self.setText(format(d, "f"))


class DateEdit(QDateEdit):
    def __init__(self, value: date | None = None, parent=None, allow_future: bool = False):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDisplayFormat("dd/MM/yyyy")
        if not allow_future:
            self.setMaximumDate(QDate.currentDate())
        self.set_value(value or date.today())

    def value(self) -> date:
        return self.date().toPython()

    def set_value(self, d: date | None):
        self.setDate(QDate(d) if d else QDate.currentDate())


class OptionalDateEdit(QWidget):
    """Date that may be left empty (filters, optional fields)."""

    def __init__(self, parent=None, allow_future: bool = True):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("DD/MM/YYYY")
        self.edit.setMaximumWidth(110)
        lay.addWidget(self.edit)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.allow_future = allow_future

    def value(self) -> date | None:
        from wms.core.numbers import parse_uk_date
        t = self.edit.text().strip()
        return parse_uk_date(t) if t else None

    def set_value(self, d: date | None):
        self.edit.setText(d.strftime("%d/%m/%Y") if d else "")


class PickCombo(QComboBox):
    """Editable combo with contains-search over (label, value) items.

    Typing filters the list; ``value()`` returns the chosen value or None.
    """

    picked = Signal(object)

    def __init__(self, placeholder: str = "", allow_empty: bool = True, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lineEdit().setPlaceholderText(placeholder)
        self.allow_empty = allow_empty
        comp = self.completer()
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setMaxVisibleItems(20)
        self.setMinimumContentsLength(18)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.currentIndexChanged.connect(lambda _: self.picked.emit(self.value()))

    def set_options(self, options: Iterable[tuple[str, Any]], keep: bool = True) -> None:
        old = self.value() if keep else None
        self.blockSignals(True)
        self.clear()
        if self.allow_empty:
            self.addItem("", None)
        for label, val in options:
            self.addItem(label, val)
        self.blockSignals(False)
        self.set_value(old)

    def value(self):
        idx = self.currentIndex()
        if idx < 0:
            return None
        # If the user typed text that doesn't match the selected item, treat as nothing chosen
        if self.currentText() != self.itemText(idx):
            match = self.findText(self.currentText(), Qt.MatchFlag.MatchFixedString)
            return self.itemData(match) if match >= 0 else None
        return self.itemData(idx)

    def set_value(self, val) -> None:
        if val is None:
            self.setCurrentIndex(0 if self.allow_empty else -1)
            if self.allow_empty:
                self.setEditText("")
            return
        for i in range(self.count()):
            if self.itemData(i) == val:
                self.setCurrentIndex(i)
                if self.lineEdit() is not None:
                    self.lineEdit().setCursorPosition(0)  # show the start of long names (the code)
                return


# --- layout helpers ---------------------------------------------------------------

def h(*widgets, stretch_end: bool = False, spacing: int = 8) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif isinstance(w, (QHBoxLayout, QVBoxLayout)):
            lay.addLayout(w)
        else:
            lay.addWidget(w)
    if stretch_end:
        lay.addStretch(1)
    return lay


def button(text: str, slot: Callable | None = None, primary: bool = False, danger: bool = False,
           shortcut: str | None = None, tooltip: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setObjectName("Primary")
    if danger:
        b.setObjectName("Danger")
    if slot:
        b.clicked.connect(lambda _=False: slot())
    if shortcut:
        b.setShortcut(QKeySequence(shortcut))
        tooltip = f"{tooltip or text} ({shortcut})"
    if tooltip:
        b.setToolTip(tooltip)
    return b


def label(text: str, name: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


class Card(QFrame):
    def __init__(self, title: str, value: str = "", warn: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.value_label = label(value, "CardValueWarn" if warn else "CardValue")
        lay.addWidget(label(title, "CardLabel"))
        lay.addWidget(self.value_label)

    def set_value(self, text: str, warn: bool = False):
        self.value_label.setText(text)
        self.value_label.setObjectName("CardValueWarn" if warn else "CardValue")
        self.value_label.style().unpolish(self.value_label)
        self.value_label.style().polish(self.value_label)


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setStyleSheet("color: #E4E7EC;")
    return f
