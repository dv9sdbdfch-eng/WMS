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
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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


def ask_reason(parent: QWidget | None, title: str, prompt: str, choices: Sequence[tuple[str, str]]
               ) -> tuple[str, str] | None:
    """Pick a reason code and write a short explanation. Returns (code, text) or None if cancelled."""
    from PySide6.QtWidgets import QDialog, QFormLayout, QPlainTextEdit
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)
    lay.addWidget(label(prompt, wrap=True))
    combo = QComboBox()
    combo.addItem("Choose a reason...", None)
    for code, text in choices:
        combo.addItem(text, code)
    notes = QPlainTextEdit()
    notes.setPlaceholderText("What happened? (required)")
    notes.setFixedHeight(90)
    form = QFormLayout()
    form.addRow("Reason *", combo)
    form.addRow("Details *", notes)
    lay.addLayout(form)
    lay.addLayout(h(None, button("Cancel", dlg.reject), button("OK", dlg.accept, primary=True)))
    while dlg.exec() == QDialog.DialogCode.Accepted:
        if combo.currentData() and notes.toPlainText().strip():
            return combo.currentData(), notes.toPlainText().strip()
        QMessageBox.warning(dlg, title, "Choose a reason and describe what happened.")
    return None


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
    sort: str | None = None  # server-side sort key (tables created with server_sort=True)
    #: a single click on a non-empty cell of this column opens a related record (spec 16):
    #: link(item) is called; the cell is drawn as a link
    link: Callable[[Any], None] | None = None


class ObjectTableModel(QAbstractTableModel):
    def __init__(self, columns: Sequence[Col], parent=None):
        super().__init__(parent)
        self.columns = list(columns)
        self.items: list = []
        self.row_style: Callable[[Any], str | None] | None = None  # returns "low", "negative", "muted"
        self.sort_mark: tuple[int, bool] | None = None  # (column, descending) shown as an arrow in the title

    def set_items(self, items: Iterable) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        marked = self.sort_mark is not None and self.sort_mark[0] == section
        if role == Qt.ItemDataRole.DisplayRole:
            title = self.columns[section].title
            return f"{title}  {'▼' if self.sort_mark[1] else '▲'}" if marked else title
        if role == Qt.ItemDataRole.ToolTipRole and self.columns[section].sort:
            if marked:
                return ("Sorted Z-A / largest / newest first" if self.sort_mark[1]
                        else "Sorted A-Z / smallest / oldest first") + " - click to reverse"
            return "Click to sort by this column"
        return None

    def set_sort_mark(self, mark: tuple[int, bool] | None) -> None:
        self.sort_mark = mark
        if self.columns:
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.columns) - 1)

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
        if role == Qt.ItemDataRole.ToolTipRole:
            # the full value, for cells cut off by a narrow column (spec 17)
            text = format_value(self.raw(index.row(), index.column()), col.kind)
            if len(text) > 12:
                return text + ("\nClick to open" if col.link else "")
            return "Click to open" if col.link and text else None
        if role == Qt.ItemDataRole.FontRole and col.link:
            f = QFont()
            f.setUnderline(True)
            return f
        if role == Qt.ItemDataRole.TextAlignmentRole and col.kind in (QTY, MONEY, INT):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.BackgroundRole and self.row_style:
            if self.row_style(self.items[index.row()]) == "low":
                return QBrush(QColor(theme.LOW_STOCK_BG))
        if role == Qt.ItemDataRole.ForegroundRole:
            if col.link:
                return QBrush(QColor(theme.ACCENT))
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
    """Sortable read-only table of objects. Double-click / Enter emits ``activated_item``.

    With ``server_sort=True`` clicking a header does not sort the rows on screen; it emits
    ``sort_requested(key, descending)`` so the caller can ask the database for the correct
    order (needed when only the first N of many results are shown). Columns without a
    ``sort`` key are not sortable. The header arrow always shows the current sort.
    """

    activated_item = Signal(object)
    selection_changed_item = Signal(object)
    sort_requested = Signal(str, bool)

    def __init__(self, columns: Sequence[Col], parent=None, server_sort: bool = False,
                 settings_key: str | None = None):
        super().__init__(parent)
        self.model_ = ObjectTableModel(columns, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model_)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.setModel(self.proxy)
        self.server_sort = server_sort
        self.sort_key: str | None = None
        self.sort_desc = False
        if server_sort:
            hdr = self.horizontalHeader()
            hdr.setSectionsClickable(True)
            # The style's own arrow points different ways on different platforms, so the
            # sorted column gets an unambiguous ▲ (A-Z, oldest first) / ▼ (newest first) instead.
            hdr.setSortIndicatorShown(False)
            hdr.sectionClicked.connect(self._header_clicked)
        else:
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
        self.settings_key = None
        if settings_key:
            self.remember_widths(settings_key)
        if any(c.link for c in columns):
            self.setMouseTracking(True)
            self.clicked.connect(self._cell_clicked)
        QShortcut(QKeySequence(Qt.Key.Key_Return), self, self._emit_activated,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence.StandardKey.Copy, self, self.copy_selection, context=Qt.ShortcutContext.WidgetShortcut)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self._extra_actions: list[tuple[str, Callable]] = []
        self._sorted_once = False

    # -- links and remembered widths (spec 16 / 17) ---------------------------------------
    def _cell_clicked(self, idx) -> None:
        if not idx.isValid():
            return
        col = self.model_.columns[idx.column()]
        if col.link is None or not idx.data():
            return
        item = self.model_.items[self.proxy.mapToSource(idx).row()]
        try:
            col.link(item)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)

    def mouseMoveEvent(self, event):  # noqa: N802 - pointing hand over link cells
        idx = self.indexAt(event.position().toPoint())
        link = idx.isValid() and self.model_.columns[idx.column()].link is not None and bool(idx.data())
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if link else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    @staticmethod
    def _qs():
        from PySide6.QtCore import QSettings
        return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")

    def remember_widths(self, key: str) -> None:
        """Keep the column widths the person set (per PC), under ``key``."""
        self.settings_key = key
        try:
            saved = self._qs().value(f"tables/{key}")
            widths = dict(x.split("=", 1) for x in str(saved).split(";") if "=" in x) if saved else {}
        except Exception:  # noqa: BLE001 - a convenience only
            widths = {}
        for i, c in enumerate(self.model_.columns):
            w = widths.get(c.title)
            if w and w.isdigit() and 20 <= int(w) <= 2000:
                self.setColumnWidth(i, int(w))
        self.horizontalHeader().sectionResized.connect(self._save_widths)

    def _save_widths(self, *_):
        if not self.settings_key:
            return
        cols = self.model_.columns
        last = len(cols) - 1
        text = ";".join(f"{c.title}={self.columnWidth(i)}" for i, c in enumerate(cols) if i != last and c.title)
        try:
            self._qs().setValue(f"tables/{self.settings_key}", text)
        except Exception:  # noqa: BLE001
            pass

    def _header_clicked(self, section: int) -> None:
        key = self.model_.columns[section].sort
        if not key:
            self.show_sort(self.sort_key, self.sort_desc)  # not sortable: keep the arrow where it was
            return
        # First click: text A-Z, numbers / dates biggest or newest first. Next click reverses.
        first_desc = self.model_.columns[section].kind in (QTY, MONEY, INT, DATE, DATETIME)
        desc = (not self.sort_desc) if key == self.sort_key else first_desc
        self.show_sort(key, desc)
        self.sort_requested.emit(key, desc)

    def show_sort(self, key: str | None, descending: bool) -> None:
        """Put the header arrow on the column sorted by ``key`` (server-sorted tables)."""
        self.sort_key, self.sort_desc = key, descending
        cols = [c.sort for c in self.model_.columns]
        self.model_.set_sort_mark((cols.index(key), descending) if key and key in cols else None)

    def add_menu_action(self, text: str, slot: Callable) -> None:
        """Extra right-click action (applies to the current row)."""
        self._extra_actions.append((text, slot))

    def _menu(self, pos):
        m = QMenu(self)
        m.addAction("Open", self._emit_activated)
        for text, slot in self._extra_actions:
            if text == "-":
                m.addSeparator()
            else:
                m.addAction(text, slot)
        m.addSeparator()
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
        if not self._sorted_once and not self.server_sort:
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

    def allow_multi_select(self) -> None:
        """Ctrl / Shift + click selects several rows (export the selected products)."""
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def selected_items(self) -> list:
        """The selected rows, in the order shown on screen."""
        rows = sorted({i.row() for i in self.selectionModel().selectedRows()})
        return [self.model_.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()] for r in rows]

    def shown_items(self) -> list:
        """Every row in the order shown on screen (after the person sorted a column)."""
        return [self.model_.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

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

    Enter (what a barcode scanner sends after each scan) resolves the typed text: an exact code
    ("B04", "P-1001", "MDF-18" - the part of a label before " - ") or, through ``resolver``, a
    barcode / any lookup the screen supplies. ``entered`` then fires so the screen can move the
    cursor to the next field; unknown text beeps and is selected, ready for the next scan.
    """

    picked = Signal(object)
    entered = Signal(object)

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
        self.resolver: Callable[[str], Any] | None = None
        self.lineEdit().returnPressed.connect(self._return_pressed)

    def _return_pressed(self) -> None:
        if self.resolve_text():
            self.entered.emit(self.value())

    def resolve_text(self) -> bool:
        """Pick the item the typed / scanned text names. False (with a beep) when nothing matches."""
        text = self.currentText().strip()
        if not text:
            return True  # empty: "none" / "automatic" - the screen decides whether that is allowed
        if self.value() is not None:
            return True
        val = self.resolver(text) if self.resolver is not None else None
        if val is None:
            val = self._value_by_code(text)
        if val is not None and self.findData(val) >= 0:
            self.set_value(val)
            return True
        QApplication.beep()
        self.lineEdit().selectAll()
        return False

    def _value_by_code(self, text: str):
        want = text.upper()
        found = [self.itemData(i) for i in range(self.count()) if self.itemData(i) is not None
                 and (self.itemText(i).upper() == want or self.itemText(i).split(" - ")[0].strip().upper() == want
                      or self.itemText(i).split("  [")[0].split(" / ")[-1].strip().upper() == want)]
        return found[0] if len(found) == 1 else None

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
    # Never let Enter in a field "click" a dialog button: barcode scanners send Enter after every
    # scan, and an operative pressing Enter after a quantity must not post (or cancel) a document.
    b.setAutoDefault(False)
    b.setDefault(False)
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


def related_bar(items: Sequence[tuple[str, Callable | None]]) -> QHBoxLayout:
    """'Related:  Supplier EGGER  ·  Purchase Order PO-1028  ·  7 materials ...' - each a link to
    the related record (UI refactor spec 41-42), so nobody has to search for it again."""
    lay = QHBoxLayout()
    lay.setSpacing(2)
    lay.addWidget(label("Related:", "Muted"))
    first = True
    for text, slot in items:
        if not text:
            continue
        if not first:
            lay.addWidget(label("·", "Muted"))
        first = False
        if slot is None:
            lay.addWidget(label(text, "Muted"))
        else:
            b = button(text, slot)
            b.setObjectName("Link")
            lay.addWidget(b)
    lay.addStretch(1)
    return lay


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
