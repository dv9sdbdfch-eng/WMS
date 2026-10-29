"""Reports with filters and Excel / CSV / PDF export."""

from __future__ import annotations

from datetime import date
from operator import itemgetter
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import ItemType
from wms.core.permissions import P
from wms.services.reports import ReportFilters
from wms.ui.pages.base import Page
from wms.ui.widgets.common import (
    Col,
    DataTable,
    OptionalDateEdit,
    PickCombo,
    button,
    format_value,
    h,
    label,
    run,
    show_error,
)


class ReportsPage(Page):
    title = "Reports"
    subtitle = "Choose a report, set the filters, run it and export to Excel, CSV or PDF"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.defs = app.services.reports.definitions()
        split = QSplitter()
        self.list = QListWidget()
        self.list.setMaximumWidth(260)
        for d in self.defs:
            it = QListWidgetItem(d.title)
            it.setData(Qt.ItemDataRole.UserRole, d.key)
            it.setToolTip(d.description)
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._selected)
        split.addWidget(self.list)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 0, 0, 0)
        self.r_title = label("", "SectionTitle")
        self.r_desc = label("", "Muted", wrap=True)
        rl.addWidget(self.r_title)
        rl.addWidget(self.r_desc)
        self.form = QFormLayout()
        self.date_from, self.date_to = OptionalDateEdit(), OptionalDateEdit()
        self.date_from.set_value(date.today().replace(day=1))
        self.date_to.set_value(date.today())
        self.dates_row = QWidget()
        self.dates_row.setLayout(h(self.date_from, label("to", "Muted"), self.date_to, None))
        self.product = PickCombo("All products")
        self.supplier = PickCombo("All suppliers")
        self.site = PickCombo("All sites")
        self.location = PickCombo("All locations")
        self.item_type = QComboBox()
        self.item_type.addItem("All types", None)
        for val, text in ItemType.choices():
            self.item_type.addItem(text, val)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Optional search text")
        self.text.returnPressed.connect(self.run_report)
        self.filter_widgets = {"dates": self.dates_row, "product": self.product, "supplier": self.supplier,
                               "site": self.site, "location": self.location, "item_type": self.item_type,
                               "text": self.text}
        for key, text in (("dates", "Dates"), ("product", "Product"), ("supplier", "Supplier"), ("site", "Site"),
                          ("location", "Location"), ("item_type", "Type"), ("text", "Search")):
            self.form.addRow(text, self.filter_widgets[key])
        rl.addLayout(self.form)
        buttons = [button("Run report", self.run_report, primary=True, shortcut="Ctrl+R")]
        self.can_export = app.can(P.REPORTS_EXPORT)
        if self.can_export:
            buttons += [button("Export to Excel", lambda: self.export(".xlsx")),
                        button("Export to CSV", lambda: self.export(".csv")),
                        button("Export to PDF", lambda: self.export(".pdf"))]
        rl.addLayout(h(*buttons, None))
        self.table = DataTable([])
        rl.addWidget(self.table, 1)
        self.summary = label("", "Muted", wrap=True)
        rl.addWidget(self.summary)
        split.addWidget(right)
        split.setSizes([240, 1100])
        self.body.addWidget(split, 1)
        self.result = None
        self._loaded = False

    def refresh(self):
        if not self._loaded:
            self.product.set_options(self.app.product_options(include_inactive=True))
            self.supplier.set_options(self.app.supplier_options(include_inactive=True))
            self.site.set_options(self.app.site_options(open_only=False))
            self.location.set_options(self.app.location_options())
            self._loaded = True
            if self.list.currentRow() < 0:
                self.list.setCurrentRow(0)

    def select(self, key: str):
        for i, d in enumerate(self.defs):
            if d.key == key:
                self.list.setCurrentRow(i)
                self.run_report()

    def _current(self):
        r = self.list.currentRow()
        return self.defs[r] if 0 <= r < len(self.defs) else None

    def _selected(self, _row):
        d = self._current()
        if d is None:
            return
        self.r_title.setText(d.title)
        self.r_desc.setText(d.description)
        for key, w in self.filter_widgets.items():
            self.form.setRowVisible(w, key in d.filters)
        self.result = None
        self.table.set_items([])
        self.summary.setText("Set the filters and press Run report.")

    def _filters(self) -> ReportFilters:
        d = self._current()
        f = ReportFilters()
        if "dates" in d.filters:
            f.date_from, f.date_to = self.date_from.value(), self.date_to.value()
        if "product" in d.filters:
            f.product_id = self.product.value()
        if "supplier" in d.filters:
            f.supplier_id = self.supplier.value()
        if "site" in d.filters:
            f.site_id = self.site.value()
        if "location" in d.filters:
            f.location_id = self.location.value()
        if "item_type" in d.filters:
            f.item_type = self.item_type.currentData()
        if "text" in d.filters:
            f.text = self.text.text().strip() or None
        return f

    def run_report(self):
        d = self._current()
        if d is None:
            return
        try:
            filters = self._filters()
        except Exception as e:  # noqa: BLE001 - bad date typed
            show_error(self, e)
            return
        result = run(self, self.app.services.reports.run, d.key, filters)
        if result is None:
            return
        self.result = result
        cols = [Col(c.title, itemgetter(i), c.kind, 110 if c.kind != "text" else 160)
                for i, c in enumerate(result.columns)]
        new = DataTable(cols)
        lay = self.table.parentWidget().layout()
        lay.replaceWidget(self.table, new)
        self.table.deleteLater()
        self.table = new
        self.table.set_items(result.rows)
        self.table.fit_columns()
        totals = [f"{c.title}: {format_value(t, c.kind)}" for c, t in zip(result.columns, result.totals())
                  if t is not None]
        text = f"{len(result.rows)} row(s)"
        if totals:
            text += "   ·   Totals - " + ",  ".join(totals)
        if result.filters_text:
            text += f"\nFilters: {result.filters_text}"
        self.summary.setText(text)

    def export(self, ext: str):
        if self.result is None:
            self.run_report()
            if self.result is None:
                return
        from wms.exporters.export import export
        name = f"{self.result.title.replace(' ', '_')}_{date.today():%Y-%m-%d}{ext}"
        default = Path.home() / "Documents" / name
        filt = {".xlsx": "Excel workbook (*.xlsx)", ".csv": "CSV file (*.csv)", ".pdf": "PDF document (*.pdf)"}[ext]
        path, _ = QFileDialog.getSaveFileName(self, "Export report", str(default), filt)
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        out = run(self, export, self.result, Path(path), self.app.ctx.config.company_name)
        if out:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
