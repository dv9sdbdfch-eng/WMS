"""The Materials list: search, the four primary filters, More Filters, chips, saved views, a table
that loads rows as you scroll, tick-box selection with bulk actions, and Excel export.

The same widget is used in two places:

* the Materials screen (``MaterialsView(app)``) - every material;
* Projects -> Project -> Materials (``MaterialsView(app, fixed_site_id=...)``) - the project is
  fixed, the project columns (Required / Reserved / Issued / Returned / Net) are shown and the
  project's material list can be edited.

Both run exactly the same service call (``MaterialsService``), so the two can never disagree.
The widget holds only the rows already scrolled into view; the database does the filtering,
counting and sorting, and the next 200 rows are fetched on a worker thread when the person
scrolls near the end (no "first 500 only" limit).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from wms.core.permissions import P
from wms.services.materials import (
    BIN_STATES,
    ITEM_TYPES,
    PAGE_SIZE,
    PROJECT_SORTS,
    PROJECT_STATUS_CHOICES,
    STATUS_CHOICES,
    STOCK_CHOICES,
    MaterialFilter,
    MaterialRow,
)
from wms.ui import theme
from wms.ui.widgets import tasks
from wms.ui.widgets.common import (
    DATETIME,
    QTY,
    TEXT,
    OptionalDateEdit,
    PickCombo,
    SearchBox,
    button,
    confirm,
    format_value,
    h,
    info,
    label,
    run,
    show_error,
)

log = logging.getLogger(__name__)

SELECT, PHOTO = "select", "photo"
#: the project banner: (ProjectSummary attribute, label, tooltip) - definitions in services/materials.py
PROJECT_FIGURES = (
    ("materials", "Materials", "Materials on the list, reserved, issued or returned for this project"),
    ("required", "Required", "Quantity the project needs (its material list). Changing it never changes stock."),
    ("reserved", "Reserved", "Stock set aside for the project, still in the warehouse"),
    ("issued", "Issued", "Sent from the warehouse to the project (Goods Out)"),
    ("returned", "Returned", "Physically returned from the project"),
    ("net", "Net Issued", "Issued - Returned"),
    ("remaining", "Remaining", "Required - Net Issued - Reserved (never below 0): still to reserve or issue"),
)
THUMB = 36  # photo thumbnail size in the table, pixels


@dataclass
class MCol:
    key: str
    title: str
    get: Callable[[MaterialRow], object] | None = None
    kind: str = TEXT
    width: int = 100
    sort: str | None = None
    default: bool = True  # shown unless the person hides it
    project: bool = False  # only when the list is filtered by a project


def _sup(r: MaterialRow) -> str:
    return r.product.supplier.name if r.product.supplier else ""


COLUMNS: list[MCol] = [
    MCol(SELECT, "", width=34),
    MCol(PHOTO, "Photo", width=60),
    MCol("code", "Code", lambda r: r.product.internal_code, width=110, sort="code"),
    MCol("name", "Material", lambda r: r.product.name, width=260, sort="name"),
    MCol("product_no", "Product Number", lambda r: r.product.supplier_code or "", width=130, sort="product_no"),
    MCol("supplier", "Supplier", _sup, width=140, sort="supplier"),
    MCol("required", "Required", lambda r: r.required, QTY, 85, "required", project=True),
    MCol("proj_reserved", "Reserved", lambda r: r.proj_reserved, QTY, 85, "proj_reserved", project=True),
    MCol("issued", "Issued", lambda r: r.issued, QTY, 80, "issued", project=True),
    MCol("returned", "Returned", lambda r: r.returned, QTY, 85, "returned", project=True),
    MCol("net", "Net Issued", lambda r: r.net, QTY, 90, "net", project=True, default=False),
    MCol("remaining", "Remaining", lambda r: r.remaining, QTY, 95, "remaining", project=True),
    MCol("qty", "On Hand", lambda r: r.on_hand, QTY, 85, "qty"),
    MCol("available", "Available", lambda r: r.available, QTY, 90, "available"),
    MCol("bin", "Bin", lambda r: r.bin_text, width=90, sort="bin"),
    MCol("status", "Status", lambda r: r.status_label, width=100, sort="status"),
    # optional columns (right-click the header to show them)
    MCol("unit", "Unit", lambda r: r.product.unit.code, width=60, default=False),
    MCol("reserved", "Reserved (all)", lambda r: r.reserved, QTY, 100, "reserved", default=False),
    MCol("incoming", "Incoming", lambda r: r.incoming, QTY, 90, "incoming", default=False),
    MCol("description", "Description", lambda r: r.product.description or "", width=220, default=False),
    MCol("barcode", "Barcode", lambda r: r.product.barcode or "", width=120, default=False),
    MCol("brand", "Brand", lambda r: r.product.brand or "", width=110, sort="brand", default=False),
    MCol("category", "Category", lambda r: r.product.category.full_name if r.product.category else "",
         width=150, sort="category", default=False),
    MCol("default_bin", "Default Bin", lambda r: r.product.default_bin.full_code if r.product.default_bin else "",
         width=150, default=False),
    MCol("bins", "All Bins", lambda r: r.bins_detail, width=220, default=False),
    MCol("updated", "Last Changed", lambda r: r.product.updated_at, DATETIME, 130, "updated", default=False),
]
BY_KEY = {c.key: c for c in COLUMNS}
#: shown by default on a project's Materials tab (UI refactor spec 23)
PROJECT_DEFAULT = {SELECT, "code", "name", "supplier", "required", "proj_reserved", "issued", "returned",
                   "remaining", "available"}
STATUS_COLOURS = {"OUT": theme.DANGER, "LOW": theme.WARNING, "ARCHIVED": "#98A2B3", "IN": theme.SUCCESS}


class _Thumbs:
    """Small cache of decoded thumbnails (only the size needed is decoded, so big JPEGs stay cheap)."""

    def __init__(self, folder: Path, size: int = THUMB, keep: int = 600):
        self.folder, self.size, self.keep = folder, size, keep
        self.cache: OrderedDict[str, QPixmap | None] = OrderedDict()

    def get(self, name: str | None) -> QPixmap | None:
        if not name:
            return None
        if name in self.cache:
            self.cache.move_to_end(name)
            return self.cache[name]
        pix = None
        reader = QImageReader(str(self.folder / name))
        if reader.canRead():
            s = reader.size()
            if s.isValid() and s.width() > 0 and s.height() > 0:
                reader.setScaledSize(s.scaled(QSize(self.size, self.size), Qt.AspectRatioMode.KeepAspectRatio))
            img = reader.read()
            if not img.isNull():
                pix = QPixmap.fromImage(img)
        self.cache[name] = pix
        while len(self.cache) > self.keep:
            self.cache.popitem(last=False)
        return pix


class MaterialsModel(QAbstractTableModel):
    """Rows loaded so far + the total. ``fetchMore`` asks the view for the next page."""

    checked_changed = Signal()

    def __init__(self, view: MaterialsView):
        super().__init__(view)
        self.view = view
        self.cols: list[MCol] = []
        self.rows: list[MaterialRow] = []
        self.total = 0
        self.checked: set[int] = set()
        self.sort: tuple[str, bool] = ("code", False)
        self.thumbs: _Thumbs | None = None

    # -- data -------------------------------------------------------------------------------
    def reset(self, rows: list[MaterialRow], total: int) -> None:
        self.beginResetModel()
        self.rows, self.total = list(rows), total
        self.endResetModel()

    def append(self, rows: list[MaterialRow]) -> None:
        known = {r.id for r in self.rows}
        rows = [r for r in rows if r.id not in known]
        if not rows:
            self.total = len(self.rows)  # the list shrank while scrolling: nothing more to fetch
            return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def set_columns(self, cols: list[MCol]) -> None:
        self.beginResetModel()
        self.cols = list(cols)
        self.endResetModel()

    def canFetchMore(self, parent=QModelIndex()):  # noqa: N802
        return not parent.isValid() and len(self.rows) < self.total and not self.view.loading

    def fetchMore(self, parent=QModelIndex()):  # noqa: N802
        if self.canFetchMore(parent):
            self.view.load_more()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.cols)

    def col_of(self, key: str) -> int:
        for i, c in enumerate(self.cols):
            if c.key == key:
                return i
        return -1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal or not 0 <= section < len(self.cols):
            return None
        c = self.cols[section]
        if role == Qt.ItemDataRole.DisplayRole:
            if c.key == SELECT:
                return "☑" if self.checked and len(self.checked) >= self.total > 0 else "☐"
            if c.sort and c.sort == self.sort[0]:
                return f"{c.title}  {'▼' if self.sort[1] else '▲'}"
            return c.title
        if role == Qt.ItemDataRole.ToolTipRole:
            if c.key == SELECT:
                return "Tick every material in the list (click again to clear the ticks)"
            if c.sort:
                return "Click to sort by this column"
            return "Right-click the header to choose columns"
        return None

    def flags(self, index):
        f = super().flags(index)
        if index.isValid() and self.cols[index.column()].key == SELECT:
            f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        r = self.rows[index.row()]
        c = self.cols[index.column()]
        if c.key == SELECT:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if r.id in self.checked else Qt.CheckState.Unchecked
            return None
        if c.key == PHOTO:
            if role == Qt.ItemDataRole.DecorationRole and self.thumbs is not None:
                return self.thumbs.get(r.photo_name)
            if role == Qt.ItemDataRole.ToolTipRole:
                return "Has a photo" if r.photo_name else "No photo yet"
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            try:
                v = c.get(r)
            except Exception:  # noqa: BLE001 - a broken getter must not crash the table
                log.exception("Column getter failed")
                return ""
            if c.kind == QTY and c.project and c.key == "required" and v is None:
                return "-"  # reserved / issued without being on the list
            return format_value(v, c.kind)
        if role == Qt.ItemDataRole.TextAlignmentRole and c.kind == QTY:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:
            if c.key == "status":
                return QBrush(QColor(STATUS_COLOURS[r.status_key]))
            if not r.product.is_active:
                return QBrush(QColor("#98A2B3"))
            if c.kind == QTY:
                v = c.get(r)
                if isinstance(v, Decimal) and v < 0:
                    return QBrush(QColor(theme.NEGATIVE_FG))
        if role == Qt.ItemDataRole.BackgroundRole and r.status_key == "LOW":
            return QBrush(QColor(theme.LOW_STOCK_BG))
        if role == Qt.ItemDataRole.ToolTipRole and c.key == "bin" and r.bins:
            return r.bins_detail
        if role == Qt.ItemDataRole.ToolTipRole and c.key == "required" and r.required is None:
            return "Not on the project's material list (reserved or issued only)"
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if index.isValid() and role == Qt.ItemDataRole.CheckStateRole and self.cols[index.column()].key == SELECT:
            pid = self.rows[index.row()].id
            if Qt.CheckState(value) == Qt.CheckState.Checked:
                self.checked.add(pid)
            else:
                self.checked.discard(pid)
            self.dataChanged.emit(index, index, [role])
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, index.column(), index.column())
            self.checked_changed.emit()
            return True
        return False

    def set_checked(self, ids) -> None:
        self.checked = set(ids)
        c = self.col_of(SELECT)
        if self.rows and c >= 0:
            self.dataChanged.emit(self.index(0, c), self.index(len(self.rows) - 1, c))
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, c, c)
        self.checked_changed.emit()


class MaterialsView(QWidget):
    """See the module docstring. ``fixed_site_id`` turns it into a project's Materials tab."""

    changed = Signal()  # something was saved (bulk change, project list): parents refresh
    filter_changed = Signal()  # a new filter is being shown (parents adapt their buttons)

    def __init__(self, app, parent=None, fixed_site_id: int | None = None, settings_key: str | None = None,
                 picker: bool = False):
        super().__init__(parent)
        self.app = app
        self.svc = app.services.materials
        self.fixed_site_id = fixed_site_id
        self.picker = picker  # "choose materials" mode: no bulk actions, no export
        self.settings_key = settings_key or ("project_materials" if fixed_site_id else "materials")
        self.filter = MaterialFilter(site_id=fixed_site_id, status="ALL" if fixed_site_id else "ACTIVE")
        self.loading = False
        self._gen = 0
        self._options_loaded = False
        self._include_archived_bins = False
        self.on_open: Callable[[MaterialRow], None] | None = None
        self.can_edit = app.can(P.PRODUCTS_EDIT) and not picker
        self.can_export = app.can(P.REPORTS_EXPORT) and not picker
        self.can_edit_project = app.can(P.SITES_EDIT) and not picker

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # -- search + primary filters --------------------------------------------------------
        self.search = SearchBox("Search code, product number, barcode, name, description or supplier...")
        self.search.search.connect(self._text_changed)
        self.project = PickCombo("Project: all")
        self.project.picked.connect(lambda v: self._set("site_id", v))
        self.supplier = PickCombo("Supplier: all")
        self.supplier.picked.connect(lambda v: self._set("supplier_id", v))
        self.bin = PickCombo("Bin: all")
        self.bin.picked.connect(self._bin_picked)
        self.stock = QComboBox()
        self.stock.addItem("Stock: all", None)
        for k, v in STOCK_CHOICES.items():
            self.stock.addItem(v, k)
        self.stock.currentIndexChanged.connect(lambda _: self._set("stock", self.stock.currentData()))
        self.more = button("More filters", self.more_filters, tooltip="Category, brand, location, status, "
                           "photo, supplier, dates")
        self.views_btn = QPushButton("Views")
        self.views_btn.setAutoDefault(False)
        self.views_menu = QMenu(self.views_btn)
        self.views_menu.aboutToShow.connect(self._fill_views)
        self.views_btn.setMenu(self.views_menu)
        self.columns_btn = QPushButton("Columns")
        self.columns_btn.setAutoDefault(False)
        self.columns_btn.setToolTip("Choose the columns shown (and exported with Export Current View)")
        self.columns_menu = QMenu(self.columns_btn)
        self.columns_menu.aboutToShow.connect(lambda: self._fill_column_menu(self.columns_menu))
        self.columns_btn.setMenu(self.columns_menu)
        self.project.setVisible(fixed_site_id is None)
        self.views_btn.setVisible(fixed_site_id is None and not picker)
        self.columns_btn.setVisible(not picker)
        # spec 18: the search on its own line, the primary filters under it (Project first)
        lay.addWidget(self.search)
        top = h(self.project, self.supplier, self.bin, self.stock, self.more, None, self.columns_btn,
                self.views_btn)
        for i in (0, 1, 2):
            top.setStretch(i, 2)
        top.setStretch(3, 1)
        lay.addLayout(top)

        # -- project context (spec 21 / 23): the same banner on Materials -> Project and Project -> Materials
        self.project_bar = QFrame()
        self.project_bar.setObjectName("ProjectBar")
        pb = QHBoxLayout(self.project_bar)
        pb.setContentsMargins(12, 6, 12, 6)
        pb.setSpacing(14)
        self.project_title = label("", "ProjectTitle")
        pb.addWidget(self.project_title)
        self.project_figures: dict[str, QLabel] = {}
        for key, _text, tip in PROJECT_FIGURES:
            lab = label("", "ProjectFigure")
            lab.setToolTip(tip)
            self.project_figures[key] = lab
            pb.addWidget(lab)
        pb.addStretch(1)
        self.open_project_btn = button("Open Project", self.open_project)
        self.open_project_btn.setObjectName("Link")
        self.open_project_btn.setVisible(fixed_site_id is None and not picker)
        pb.addWidget(self.open_project_btn)
        self.project_bar.setVisible(False)
        self.project_summary = None
        lay.addWidget(self.project_bar)

        # -- chips + count ------------------------------------------------------------------
        self.chips_box = QWidget()
        self.chips = QHBoxLayout(self.chips_box)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidget(self.chips_box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(34)
        scroll.setStyleSheet("background: transparent;")
        self.chips_scroll = scroll
        self.count = label("", "Count")
        lay.addLayout(h(self.count, 12, scroll))
        self.chip_buttons: dict[str, QPushButton] = {}

        # -- selection bar (shown while something is ticked) -----------------------------------
        self.sel_bar = QFrame()
        self.sel_bar.setObjectName("SelectionBar")
        sb = QHBoxLayout(self.sel_bar)
        sb.setContentsMargins(10, 4, 10, 4)
        self.sel_label = label("", "Count")
        self.sel_all = button("Select all", self.select_all)
        self.sel_all.setObjectName("Link")
        self.sel_clear = button("Clear selection", self.clear_selection)
        self.sel_clear.setObjectName("Link")
        sb.addWidget(self.sel_label)
        sb.addWidget(self.sel_all)
        sb.addWidget(self.sel_clear)
        sb.addStretch(1)
        self.sel_export = button("Export", lambda: self.export("SELECTED"))
        self.sel_export.setVisible(self.can_export)
        sb.addWidget(self.sel_export)
        # spec 38: Assign Project (to its material list - Required only, never stock)
        self.sel_project = button("Assign Project...", lambda: self.add_to_project())
        self.sel_project.setVisible(self.can_edit_project and fixed_site_id is None)
        sb.addWidget(self.sel_project)
        self.sel_supplier = button("Change Supplier...", lambda: self.bulk_change("supplier_id"))
        self.sel_category = button("Change Category...", lambda: self.bulk_change("category_id"))
        for b in (self.sel_supplier, self.sel_category):
            b.setVisible(self.can_edit)
            sb.addWidget(b)
        self.sel_more = QPushButton("More")
        self.sel_more.setAutoDefault(False)
        more = QMenu(self.sel_more)
        more.aboutToShow.connect(lambda: self._fill_more(more))
        self.sel_more.setMenu(more)
        self.sel_more.setVisible(not picker)
        sb.addWidget(self.sel_more)
        self.sel_bar.setVisible(False)
        lay.addWidget(self.sel_bar)

        # -- table --------------------------------------------------------------------------
        self.model = MaterialsModel(self)
        self.model.thumbs = _Thumbs(app.ctx.config.attachments_path)
        self.model.checked_changed.connect(self._selection_changed)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(THUMB, THUMB))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(THUMB + 4)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)  # fast scrolling
        hdr = self.table.horizontalHeader()
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.sectionClicked.connect(self._header_clicked)
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._column_menu)
        hdr.sectionResized.connect(self._remember_width)
        self.table.doubleClicked.connect(self._activated)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        lay.addWidget(self.table, 1)

        # two column choices (spec 22 / 23): the normal list, and the list while a project is chosen
        self._visible = {"normal": self._load_visible("normal"), "project": self._load_visible("project")}
        self.widths: dict[str, int] = self._load_widths()
        self._apply_columns()

    # ---- settings (which columns, their widths) ------------------------------------------
    def _qs(self) -> QSettings:
        return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")

    @property
    def mode(self) -> str:
        return "project" if (self.fixed_site_id or self.filter.site_id) else "normal"

    @property
    def visible(self) -> set[str]:
        return self._visible[self.mode]

    def _columns_key(self, mode: str) -> str:
        # a project's own tab has always used "<key>/columns"
        return f"{self.settings_key}/columns" if mode == "normal" or self.fixed_site_id \
            else f"{self.settings_key}/project_columns"

    def _default_visible(self, mode: str) -> set[str]:
        return set(PROJECT_DEFAULT) if mode == "project" else {c.key for c in COLUMNS if c.default and not c.project}

    def _load_visible(self, mode: str) -> set[str]:
        default = self._default_visible("project" if self.fixed_site_id else mode)
        try:
            saved = self._qs().value(self._columns_key(mode))
        except Exception:  # noqa: BLE001
            saved = None
        if isinstance(saved, str) and saved:
            keys = {k for k in saved.split(",") if k in BY_KEY}
            if keys:
                if "required" in keys:
                    keys.add("remaining")  # new in v1.13: show it to people who already chose columns
                if mode == "project" and not keys & {c.key for c in COLUMNS if c.project}:
                    return default
                return keys | {SELECT}
        return default

    def _load_widths(self) -> dict[str, int]:
        out = {}
        try:
            saved = self._qs().value(f"{self.settings_key}/widths") or ""
            for part in str(saved).split(","):
                k, _, w = part.partition("=")
                if k in BY_KEY and w.isdigit():
                    out[k] = int(w)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _save_settings(self) -> None:
        try:
            qs = self._qs()
            qs.setValue(self._columns_key(self.mode), ",".join(sorted(self.visible)))
            qs.setValue(f"{self.settings_key}/widths", ",".join(f"{k}={w}" for k, w in self.widths.items()))
        except Exception:  # noqa: BLE001 - settings are a convenience only
            log.debug("Could not save table settings", exc_info=True)

    def _remember_width(self, section: int, _old: int, new: int) -> None:
        if getattr(self, "_applying", False) or not 0 <= section < len(self.model.cols):
            return
        self.widths[self.model.cols[section].key] = new
        self._save_settings()

    def shown_columns(self) -> list[MCol]:
        site = bool(self.filter.site_id)
        cols = [c for c in COLUMNS if c.key in self.visible and (site or not c.project)]
        if self.picker:
            cols = [c for c in cols if not c.project]
        return cols

    def _apply_columns(self) -> None:
        self._applying = True
        try:
            self.model.set_columns(self.shown_columns())
            for i, c in enumerate(self.model.cols):
                self.table.setColumnWidth(i, self.widths.get(c.key, c.width))
        finally:
            self._applying = False

    def _column_menu(self, pos) -> None:
        m = QMenu(self)
        self._fill_column_menu(m)
        m.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def _fill_column_menu(self, m: QMenu) -> None:
        m.clear()
        m.addSection("Columns")
        for c in COLUMNS:
            if c.key == SELECT or (c.project and not self.filter.site_id):
                continue
            a = m.addAction(c.title)
            a.setCheckable(True)
            a.setChecked(c.key in self.visible)
            a.toggled.connect(lambda on, k=c.key: self.toggle_column(k, on))
        m.addSeparator()
        m.addAction("Reset columns", lambda: self.reset_columns())

    def toggle_column(self, key: str, on: bool) -> None:
        (self.visible.add if on else self.visible.discard)(key)
        self._save_settings()
        self._apply_columns()

    def reset_columns(self) -> None:
        self._visible[self.mode] = self._default_visible(self.mode)
        self.widths = {}
        self._save_settings()
        self._apply_columns()

    # ---- filter widgets <-> MaterialFilter --------------------------------------------------
    def _load_options(self) -> None:
        if self._options_loaded:
            return
        self._options_loaded = True
        self._block(True)
        try:
            if self.fixed_site_id is None:
                self.project.set_options(self.app.site_options(open_only=False))
            self.supplier.set_options(self.app.supplier_options(include_inactive=True))
            self._fill_bins()
        finally:
            self._block(False)

    def _fill_bins(self) -> None:
        opts = [(f"« {text} »", key) for key, text in BIN_STATES.items()]
        for b in self.svc.bin_choices(include_archived=self._include_archived_bins):
            opts.append((b.full_code + ("" if b.is_active else "  [archived]"), b.id))
        if not self._include_archived_bins:
            opts.append(("« Show archived bins too... »", "__archived__"))
        self.bin.set_options(opts)

    def _block(self, on: bool) -> None:
        for w in (self.search, self.project, self.supplier, self.bin, self.stock):
            w.blockSignals(on)

    def sync_widgets(self) -> None:
        """Show ``self.filter`` in the widgets (after a chip was removed or a view chosen)."""
        self._load_options()
        f = self.filter
        self._block(True)
        try:
            if self.search.text() != f.text:
                self.search.setText(f.text)
                self.search._t.stop()
            self.project.set_value(f.site_id)
            self.supplier.set_value(f.supplier_id)
            if f.bin_id and self.bin.findData(f.bin_id) < 0 and not self._include_archived_bins:
                self._include_archived_bins = True
                self._fill_bins()
            self.bin.set_value(f.bin_state or f.bin_id)
            self.stock.setCurrentIndex(max(self.stock.findData(f.stock), 0))
        finally:
            self._block(False)

    def _text_changed(self, text: str) -> None:
        if text != self.filter.text:
            self.filter.text = text
            self.refresh(reset_selection=True)

    def _set(self, name: str, value) -> None:
        if getattr(self.filter, name) == value:
            return
        setattr(self.filter, name, value)
        if name == "site_id":
            if not value and self.filter.sort in PROJECT_SORTS:
                self.filter.sort, self.filter.descending = "code", False
            self._apply_columns()
        self.refresh(reset_selection=True)

    def _bin_picked(self, value) -> None:
        if value == "__archived__":
            self._include_archived_bins = True
            self.bin.blockSignals(True)
            self._fill_bins()
            self.bin.set_value(None)
            self.bin.blockSignals(False)
            self.bin.showPopup()
            return
        state = value if isinstance(value, str) else None
        bin_id = value if isinstance(value, int) else None
        if (self.filter.bin_state, self.filter.bin_id) == (state, bin_id):
            return
        self.filter.bin_state, self.filter.bin_id = state, bin_id
        self.refresh(reset_selection=True)

    def set_filter(self, f: MaterialFilter) -> None:
        """Show a saved view / a preset."""
        f = f.copy()
        if self.fixed_site_id:
            f.site_id = self.fixed_site_id
        self.filter = f
        self._apply_columns()
        self.sync_widgets()
        self.refresh(reset_selection=True)

    def more_filters(self) -> None:
        dlg = MoreFiltersDialog(self.app, self.filter, self, show_project_status=self.fixed_site_id is None)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_filter is not None:
            self.filter = dlg.result_filter
            self.refresh(reset_selection=True)

    # ---- loading -----------------------------------------------------------------------------
    def refresh(self, reset_selection: bool = False) -> None:
        """Count + first page for the current filter, on a worker thread. Any newer request
        wins: results of an older one are dropped (``_gen``)."""
        self._load_options()
        try:
            self.filter.validate()
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        if reset_selection:
            self.model.set_checked(())
        self._gen += 1
        gen, f = self._gen, self.filter.copy()
        self.loading = True
        self.count.setText("Loading...")
        self._update_chips()
        self.filter_changed.emit()
        self.model.sort = (f.sort, f.descending)
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, max(len(self.model.cols) - 1, 0))

        def work():
            summary = self.svc.project_summary(f.site_id) if f.site_id and self.app.can(P.SITES_VIEW) else None
            return self.svc.count(f), self.svc.page(f, 0, PAGE_SIZE), summary

        tasks.background(work, lambda res: self._loaded(gen, res), lambda exc: self._failed(gen, exc))

    def _loaded(self, gen: int, res) -> None:
        if gen != self._gen:
            return  # the person changed the filter meanwhile
        total, rows, summary = res
        self.loading = False
        self.show_project_summary(summary)
        self.model.reset(rows, total)
        self.table.scrollToTop()
        self._update_count()
        self._selection_changed()

    def _failed(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self.loading = False
        self.count.setText("Could not load the list")
        show_error(self, exc)

    def load_more(self) -> None:
        if self.loading or len(self.model.rows) >= self.model.total:
            return
        self.loading = True
        gen, f, offset = self._gen, self.filter.copy(), len(self.model.rows)

        def more(rows):
            if gen != self._gen:
                return
            self.loading = False
            self.model.append(rows)
            self._update_count()

        tasks.background(lambda: self.svc.page(f, offset, PAGE_SIZE), more, lambda exc: self._failed(gen, exc))

    def load_all(self) -> None:
        """Fetch every remaining row (tests, and before a keyboard Select-All on a short list)."""
        while len(self.model.rows) < self.model.total and not self.loading:
            n = len(self.model.rows)
            self.load_more()
            if len(self.model.rows) == n:
                break

    # ---- count, chips, selection -------------------------------------------------------------
    def _update_count(self) -> None:
        n = self.model.total
        word = "material" if n == 1 else "materials"
        if self.fixed_site_id:
            text = f"{n:,} {word} on this project"
        elif self.filter.site_id:
            text = f"{n:,} {word} in {self._project_name()}"
        else:
            text = f"Showing {n:,} {word}"
        self.count.setText(text)

    def show_project_summary(self, s) -> None:
        """The project banner (spec 23). The figures are for the whole project, whatever other
        filters narrow the table."""
        self.project_summary = s
        self.project_bar.setVisible(s is not None)
        if s is None:
            return
        self.project_title.setText(f"PROJECT: {s.site.name.upper()}  ({s.site.project_number})")
        for key, text, _tip in PROJECT_FIGURES:
            v = getattr(s, key)
            self.project_figures[key].setText(f"{text}: <b>{v:,}</b>" if isinstance(v, int) else
                                              f"{text}: <b>{format_value(v, QTY) or '0'}</b>")

    def open_project(self) -> None:
        site_id = self.filter.site_id
        if site_id:
            self.app.open_entity("SITE", site_id, self, tab="materials")
            self.refresh()

    def _project_name(self) -> str:
        t = self.project.currentText().split("  [")[0]
        return t.split(" - ", 1)[1] if " - " in t else (t or "the project")

    def _update_chips(self) -> None:
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.chip_buttons = {}
        try:
            chips = self.svc.describe(self.filter)
        except Exception:  # noqa: BLE001
            log.exception("Describe filter failed")
            chips = []
        if self.fixed_site_id:
            # the project itself, and "active and archived" (a project shows everything it used), are
            # the tab's own setting, not a filter the person added
            chips = [c for c in chips if c[0] != "site_id" and not (c[0] == "status" and self.filter.status == "ALL")]
        for key, text in chips:
            b = QPushButton(f"{text}  ×")
            b.setObjectName("Chip")
            b.setAutoDefault(False)
            b.setToolTip("Remove this filter")
            b.clicked.connect(lambda _=False, k=key: self.clear_filter(k))
            self.chips.addWidget(b)
            self.chip_buttons[key] = b
        if chips:
            clear = button("Clear all", self.clear_all)
            clear.setObjectName("Link")
            self.chips.addWidget(clear)
        self.chips.addStretch(1)
        n = len([k for k, _ in chips if k not in ("site_id", "supplier_id", "bin_id", "bin_state", "stock", "text")])
        self.more.setText(f"More filters ({n})" if n else "More filters")

    def clear_filter(self, key: str) -> None:
        self.svc.clear(self.filter, key)
        if key == "status" and self.fixed_site_id:
            self.filter.status = "ALL"
        if key == "site_id":
            self._apply_columns()
        self.sync_widgets()
        self.refresh(reset_selection=True)

    def clear_all(self) -> None:
        keep_sort = (self.filter.sort, self.filter.descending)
        self.filter = MaterialFilter(site_id=self.fixed_site_id, status="ALL" if self.fixed_site_id else "ACTIVE")
        if self.filter.site_id or keep_sort[0] not in PROJECT_SORTS:
            self.filter.sort, self.filter.descending = keep_sort
        self._apply_columns()
        self.sync_widgets()
        self.refresh(reset_selection=True)

    def checked_ids(self) -> list[int]:
        """Ticked materials in display order (ticked rows not yet scrolled into view go last)."""
        shown = [r.id for r in self.model.rows if r.id in self.model.checked]
        rest = self.model.checked - set(shown)
        return shown + sorted(rest)

    def checked_rows(self) -> list[MaterialRow]:
        ids = self.checked_ids()
        loaded = {r.id: r for r in self.model.rows}
        missing = [i for i in ids if i not in loaded]
        if missing:
            for r in self.svc.rows_for(missing, self.filter.site_id):
                loaded[r.id] = r
        return [loaded[i] for i in ids if i in loaded]

    def select_all(self) -> None:
        ids = run(self, self.svc.ids, self.filter.copy())
        if ids is not None:
            self.model.set_checked(ids)

    def clear_selection(self) -> None:
        self.model.set_checked(())

    def _selection_changed(self) -> None:
        n = len(self.model.checked)
        self.sel_bar.setVisible(n > 0)
        self.sel_label.setText(f"{n:,} selected")
        self.sel_all.setVisible(n < self.model.total)
        self.sel_all.setText(f"Select all {self.model.total:,}")

    def _header_clicked(self, section: int) -> None:
        if not 0 <= section < len(self.model.cols):
            return
        c = self.model.cols[section]
        if c.key == SELECT:
            if self.model.checked:
                self.clear_selection()
            else:
                self.select_all()
            return
        if not c.sort:
            return
        numeric = c.kind in (QTY, DATETIME)
        desc = (not self.filter.descending) if c.sort == self.filter.sort else numeric
        self.filter.sort, self.filter.descending = c.sort, desc
        self.refresh()

    def current_row(self) -> MaterialRow | None:
        idx = self.table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.model.rows):
            return None
        return self.model.rows[idx.row()]

    def _activated(self, idx) -> None:
        if not idx.isValid() or self.model.cols[idx.column()].key == SELECT:
            return
        r = self.model.rows[idx.row()]
        if self.on_open is not None:
            self.on_open(r)
            return
        self.open_material(r)

    def open_material(self, r: MaterialRow | None) -> None:
        if r is None:
            return
        from wms.ui.dialogs.product_dialog import ProductDialog
        dlg = ProductDialog(self.app, r.id, self)
        dlg.exec()
        if dlg.saved:
            self.app.changed()

    def _row_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        r = self.model.rows[idx.row()] if idx.isValid() else None
        m = QMenu(self)
        if r is not None:
            m.addAction("Open", lambda: self.open_material(r))
            tick = "Untick" if r.id in self.model.checked else "Tick"
            m.addAction(tick, lambda: self._toggle(r.id))
            if self.app.can(P.GOODS_IN_POST) and r.product.is_active:
                m.addAction("Goods In...", lambda: self.app.start_posting("goods_in", r.id, r.product.supplier_id))
            if self.app.can(P.GOODS_OUT_POST) and r.product.is_active:
                m.addAction("Goods Out...", lambda: self.app.start_posting("goods_out", r.id,
                                                                            site_id=self.filter.site_id))
            m.addSeparator()
        m.addAction("Copy code", lambda: self._copy(r.product.internal_code if r else ""))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _toggle(self, pid: int) -> None:
        ids = set(self.model.checked)
        ids.symmetric_difference_update({pid})
        self.model.set_checked(ids)

    @staticmethod
    def _copy(text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)

    # ---- saved views ---------------------------------------------------------------------------
    def _fill_views(self) -> None:
        m = self.views_menu
        m.clear()
        views = run(self, self.svc.views) or []
        builtin = [v for v in views if v.id is None]
        mine = [v for v in views if v.id is not None]
        m.addSection("Views")
        for v in builtin:
            m.addAction(v.name, lambda _=False, v=v: self.set_filter(v.filter))
        if mine:
            m.addSection("My views")
            for v in mine:
                m.addAction(v.name, lambda _=False, v=v: self.set_filter(v.filter))
        m.addSeparator()
        m.addAction("Save current view...", lambda: self.save_view())
        if mine:
            dm = m.addMenu("Delete a view")
            for v in mine:
                dm.addAction(v.name, lambda _=False, v=v: self.delete_view(v))

    def save_view(self, name: str | None = None) -> bool:
        if name is None:
            name, ok = QInputDialog.getText(self, "Save view", "Name for this view (e.g. Tribeca Materials):")
            if not ok or not name.strip():
                return False
        try:
            self.svc.save_view(name, self.filter.copy())
        except Exception as exc:  # noqa: BLE001
            from wms.core.exceptions import ValidationError
            if isinstance(exc, ValidationError) and "already have" in exc.message and confirm(
                    self, "Save view", f"{exc.message}\n\nReplace it with the current filters?", yes="Replace"):
                return run(self, self.svc.save_view, name, self.filter.copy(), True) is not None
            show_error(self, exc)
            return False
        return True

    def delete_view(self, v) -> None:
        if confirm(self, "Delete view", f"Delete the view '{v.name}'? Materials are not affected.", yes="Delete"):
            run(self, self.svc.delete_view, v.id)

    # ---- bulk actions --------------------------------------------------------------------------
    def _fill_more(self, m: QMenu) -> None:
        m.clear()
        if self.can_edit_project:
            if self.fixed_site_id:
                m.addAction("Set required quantity...", lambda: self.set_required())
                m.addAction("Remove from project list", lambda: self.remove_from_project())
            else:
                m.addAction("Add to project material list...", lambda: self.add_to_project())
        m.addAction("Copy codes", lambda: self._copy("\n".join(r.product.internal_code
                                                               for r in self.checked_rows())))
        if self.can_export:
            m.addAction("Export selected to Excel...", lambda: self.export("SELECTED"))

    def bulk_change(self, field_name: str) -> None:
        ids = self.checked_ids()
        if not ids:
            return
        what = "supplier" if field_name == "supplier_id" else "category"
        if field_name == "supplier_id":
            options = self.app.supplier_options()
        else:
            options = [(c.full_name, c.id) for c in self.app.services.reference.categories()]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Change {what}")
        dlg.setMinimumWidth(440)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Set the {what} of the {len(ids):,} selected material(s). Stock is not affected; "
                            f"the change is recorded in the audit trail.", wrap=True))
        pick = PickCombo(f"(no {what})")
        pick.set_options(options)
        lay.addWidget(pick)
        lay.addLayout(h(None, button("Cancel", dlg.reject), button("Apply", dlg.accept, primary=True)))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value = pick.value()
        if value is None and not confirm(self, f"Change {what}", f"Clear the {what} on {len(ids):,} material(s)?",
                                         yes="Clear"):
            return
        n = run(self, self.svc.bulk_set, ids, field_name, value)
        if n is not None:
            info(self, f"Change {what}", f"{n:,} material(s) changed.")
            self.app.changed()
            self.changed.emit()
            self.refresh()

    def add_to_project(self, site_id: int | None = None, rows: list[MaterialRow] | None = None) -> bool:
        rows = rows if rows is not None else self.checked_rows()
        if not rows:
            return False
        dlg = ProjectListDialog(self.app, rows, site_id or self.fixed_site_id, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.saved:
            self.app.changed()
            self.changed.emit()
            self.refresh()
            return True
        return False

    def set_required(self) -> bool:
        return self.add_to_project(self.fixed_site_id)

    def remove_from_project(self) -> bool:
        ids = self.checked_ids()
        site_id = self.fixed_site_id or self.filter.site_id
        if not ids or not site_id:
            return False
        if not confirm(self, "Remove from project list",
                       f"Take {len(ids):,} material(s) off this project's material list?\n\nOnly the "
                       f"Required quantity is removed. Stock, reservations and issued history are not changed.",
                       yes="Remove"):
            return False
        n = run(self, self.svc.remove_from_project, site_id, ids)
        if n is not None:
            self.app.changed()
            self.changed.emit()
            self.refresh(reset_selection=True)
            return True
        return False

    # ---- export ---------------------------------------------------------------------------------
    def filters_text(self) -> str:
        try:
            return ", ".join(t for _, t in self.svc.describe(self.filter))
        except Exception:  # noqa: BLE001
            return ""

    def export(self, scope: str, path: str | None = None) -> Path | None:
        """Export the current view / the ticked rows / all materials. Runs on a worker thread
        with a progress window; returns the file (None if cancelled or nothing to export)."""
        from wms.exporters.materials import ExportCancelled, export_materials
        ids = self.checked_ids() if scope == "SELECTED" else None
        if scope == "SELECTED" and not ids:
            info(self, "Export to Excel", "Tick one or more materials first (the boxes on the left).")
            return None
        if scope == "CURRENT" and self.model.total == 0 and not self.loading:
            info(self, "Export to Excel", "The list is empty - nothing to export.")
            return None
        open_after = path is None
        if path is None:
            name = {"SELECTED": "Selected", "ALL": "All"}.get(scope, "Current")
            default = Path.home() / "Documents" / f"Materials_{name}_{date.today():%Y-%m-%d}.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, "Export materials", str(default), "Excel workbook (*.xlsx)")
            if not path:
                return None
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
        f, text, company = self.filter.copy(), self.filters_text(), self.app.ctx.config.company_name
        try:
            tasks.with_progress(self, "Export to Excel",
                                lambda progress: export_materials(self.svc, Path(path), scope, f, ids, text,
                                                                  company, progress),
                                "Writing materials...")
        except ExportCancelled:
            return None
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return None
        if open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return Path(path)

    def export_project(self, path: str | None = None) -> Path | None:
        from wms.exporters.materials import ExportCancelled, export_project
        site_id = self.fixed_site_id or self.filter.site_id
        if not site_id:
            return None
        open_after = path is None
        if path is None:
            default = Path.home() / "Documents" / f"Project_Materials_{date.today():%Y-%m-%d}.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, "Export project materials", str(default),
                                                  "Excel workbook (*.xlsx)")
            if not path:
                return None
            if not path.lower().endswith(".xlsx"):
                path += ".xlsx"
        company = self.app.ctx.config.company_name
        try:
            tasks.with_progress(self, "Export to Excel",
                                lambda progress: export_project(self.svc, site_id, Path(path), company, progress),
                                "Writing project materials...")
        except ExportCancelled:
            return None
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return None
        if open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return Path(path)


# --- More Filters --------------------------------------------------------------------------------

class MoreFiltersDialog(QDialog):
    """The secondary filters (spec section 3). OK applies, Reset clears them, Cancel changes nothing."""

    def __init__(self, app, f: MaterialFilter, parent=None, show_project_status: bool = True):
        super().__init__(parent)
        self.app = app
        self.base = f.copy()
        self.result_filter: MaterialFilter | None = None
        self.setWindowTitle("More filters")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.category = PickCombo("All categories")
        self.category.set_options([(c.full_name, c.id) for c in app.services.reference.categories(True)])
        self.brand = QComboBox()
        self.brand.setEditable(True)
        self.brand.addItem("")
        for b in app.services.materials.brands():
            self.brand.addItem(b)
        self.location = PickCombo("All warehouses / locations")
        self.location.set_options(app.location_options())
        self.project_status = self._combo(PROJECT_STATUS_CHOICES, "Any project status")
        self.status = self._combo(STATUS_CHOICES, None)
        self.photo = self._combo({"WITH": "Has photo", "WITHOUT": "Missing photo"}, "Photo: any")
        self.has_supplier = self._combo({"WITH": "Has supplier", "WITHOUT": "Missing supplier"}, "Supplier: any")
        self.item_type = self._combo(ITEM_TYPES, "All types")
        form.addRow("Category", self.category)
        form.addRow("Brand", self.brand)
        form.addRow("Warehouse / location", self.location)
        if show_project_status:
            form.addRow("Project status", self.project_status)
        form.addRow("Material status", self.status)
        form.addRow("Photo", self.photo)
        form.addRow("Supplier", self.has_supplier)
        form.addRow("Type", self.item_type)
        self.dates: dict[str, tuple[OptionalDateEdit, OptionalDateEdit]] = {}
        for key, text in (("received", "Last received"), ("issued", "Last issued"), ("created", "Created"),
                          ("updated", "Updated")):
            lo, hi = OptionalDateEdit(), OptionalDateEdit()
            self.dates[key] = (lo, hi)
            form.addRow(text, h(label("from", "Muted"), lo, label("to", "Muted"), hi, None))
        lay.addLayout(form)
        lay.addWidget(label("Dates are DD/MM/YYYY; leave a box empty for no limit. Every filter narrows the "
                            "list further (all of them must match).", "Muted", wrap=True))
        lay.addLayout(h(button("Reset these filters", self.reset), None, button("Cancel", self.reject),
                        button("Apply", self.apply, primary=True)))
        self._show(f)

    @staticmethod
    def _combo(choices: dict, empty: str | None) -> QComboBox:
        c = QComboBox()
        if empty is not None:
            c.addItem(empty, None)
        for k, v in choices.items():
            c.addItem(v, k)
        return c

    @staticmethod
    def _pick(c: QComboBox, value) -> None:
        c.setCurrentIndex(max(c.findData(value), 0))

    def _show(self, f: MaterialFilter) -> None:
        self.category.set_value(f.category_id)
        self.brand.setCurrentText(f.brand or "")
        self.location.set_value(f.warehouse_id)
        self._pick(self.project_status, f.project_status)
        self._pick(self.status, f.status)
        self._pick(self.photo, f.photo)
        self._pick(self.has_supplier, f.has_supplier)
        self._pick(self.item_type, f.item_type)
        for key, (lo, hi) in self.dates.items():
            lo.set_value(getattr(f, f"{key}_from"))
            hi.set_value(getattr(f, f"{key}_to"))

    def reset(self) -> None:
        blank = MaterialFilter()
        self._show(blank)

    def build(self) -> MaterialFilter:
        f = self.base.copy()
        f.category_id = self.category.value()
        f.brand = self.brand.currentText().strip() or None
        f.warehouse_id = self.location.value()
        f.project_status = self.project_status.currentData()
        f.status = self.status.currentData() or "ACTIVE"
        f.photo = self.photo.currentData()
        f.has_supplier = self.has_supplier.currentData()
        f.item_type = self.item_type.currentData()
        for key, (lo, hi) in self.dates.items():
            setattr(f, f"{key}_from", lo.value())
            setattr(f, f"{key}_to", hi.value())
        f.validate()
        return f

    def apply(self) -> None:
        try:
            self.result_filter = self.build()
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc, "Check the filters")
            return
        self.accept()


# --- project material list ------------------------------------------------------------------------

class ProjectListDialog(QDialog):
    """Put materials on a project's list with a Required quantity (spec section 7). Never changes
    stock; Goods Out stays the only way material leaves the warehouse."""

    def __init__(self, app, rows: list[MaterialRow], site_id: int | None = None, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QRadioButton, QTableWidget, QTableWidgetItem
        self.app = app
        self.rows = rows
        self.saved = False
        self.setWindowTitle("Project material list")
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        self.site = PickCombo("Choose project...", allow_empty=True)
        self.site.set_options(app.site_options(open_only=True))
        self.site.set_value(site_id)
        self.site.setEnabled(site_id is None)
        lay.addLayout(h(label("Project *"), self.site, None))
        current: dict[int, Decimal | None] = {}
        if site_id:
            current = {r.id: r.required for r in app.services.materials.rows_for([r.id for r in rows], site_id)}
        self.table = QTableWidget(len(rows), 5)
        self.table.setHorizontalHeaderLabels(["Code", "Material", "Unit", "Required now", "Required quantity *"])
        self.table.verticalHeader().setVisible(False)
        for i, r in enumerate(rows):
            for j, text in enumerate((r.product.internal_code, r.product.name, r.product.unit.code,
                                      format_value(current.get(r.id), QTY) if current.get(r.id) is not None
                                      else "-")):
                it = QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, j, it)
            now = current.get(r.id)
            q = QTableWidgetItem(format(now.normalize(), "f") if now is not None else "")
            q.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(i, 4, q)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 60)
        self.table.setColumnWidth(3, 110)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        self.same = QPushButton("Same quantity for all...")
        self.same.setAutoDefault(False)
        self.same.clicked.connect(self.fill_all)
        self.replace = QRadioButton("Set the required quantity")
        self.add = QRadioButton("Add to the quantity already required")
        self.replace.setChecked(True)
        lay.addLayout(h(self.same, None, self.replace, self.add))
        lay.addWidget(label("This list says what the project NEEDS. It does not reserve or move stock - use "
                            "Reserve or Goods Out for that.", "Muted", wrap=True))
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save list", self.save, primary=True)))

    def fill_all(self) -> None:
        text, ok = QInputDialog.getText(self, "Required quantity", "Quantity for every material listed:")
        if ok:
            for i in range(self.table.rowCount()):
                self.table.item(i, 4).setText(text.strip())

    def set_quantity(self, row: int, text: str) -> None:
        self.table.item(row, 4).setText(text)

    def items(self) -> list[tuple[int, str]]:
        out = []
        for i, r in enumerate(self.rows):
            text = self.table.item(i, 4).text().strip()
            if text:
                out.append((r.id, text))
        return out

    def save(self) -> None:
        site_id = self.site.value()
        if not site_id:
            info(self, "Project material list", "Choose the project first.")
            return
        items = self.items()
        if not items:
            info(self, "Project material list", "Enter a required quantity for at least one material.")
            return
        n = run(self, self.app.services.materials.set_required, site_id, items, None, self.replace.isChecked())
        if n is not None:
            self.saved = True
            self.accept()


class MaterialPickerDialog(QDialog):
    """Choose existing materials (tick them) - used to build a project's material list."""

    def __init__(self, app, parent=None, title: str = "Choose materials"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        self.view = MaterialsView(app, self, picker=True, settings_key="material_picker")
        self.view.on_open = lambda r: self.view._toggle(r.id)
        lay.addWidget(label("Tick the materials to add (double-click also ticks), then press Add.", "Muted"))
        lay.addWidget(self.view, 1)
        self.ok = button("Add ticked materials", self.accept, primary=True)
        lay.addLayout(h(None, button("Cancel", self.reject), self.ok))
        self.view.refresh()

    def rows(self) -> list[MaterialRow]:
        return self.view.checked_rows()


__all__ = ["COLUMNS", "MaterialPickerDialog", "MaterialsModel", "MaterialsView", "MoreFiltersDialog",
           "ProjectListDialog"]
