"""Stock from Excel (v1.38): choose the file -> see what will be added -> create an import for
approval. Nothing in stock changes here: a manager approves it on the Stocktake screen. Each row's
quantity is ADDED to the bin; the same file is not added twice by mistake."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QLineEdit, QSizePolicy, QVBoxLayout

from wms.services.stock_import import OK, PROBLEM, SAME, StockImportPlan
from wms.ui.widgets import tasks
from wms.ui.widgets.common import INT, QTY, Col, DataTable, button, confirm, h, label, show_error

STATUS_TEXT = {OK: "Will add", SAME: "Nothing (0)", PROBLEM: "Left out"}
FILTERS = (("All rows", None), ("Will add", OK), ("Left out (problems)", PROBLEM), ("To check", "WARN"),
           ("Nothing to add", SAME))


class StockImportDialog(QDialog):
    def __init__(self, app, parent=None, path: str | None = None):
        super().__init__(parent)
        self.app = app
        self.svc = app.services.stock_import
        self.plan: StockImportPlan | None = None
        self.count = None
        self.setWindowTitle("Add stock from Excel")
        self.resize(1100, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(label(
            "Each row's quantity is <b>added</b> to the stock in the bin. Columns: <b>Code</b>, <b>Bin</b> (blank = "
            "the material's default bin) and <b>Quantity</b> (the amount to add). Stock changes only when a manager "
            "approves the import. The same file cannot be added twice by mistake.",
            "InfoBanner", wrap=True))
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        self.file.setPlaceholderText("Choose an Excel workbook (.xlsx)")
        self.sheet = QComboBox()
        self.sheet.setMinimumWidth(160)
        self.sheet.activated.connect(lambda _: self.check())
        lay.addLayout(h(self.file, button("Choose file...", self.choose), label("Sheet", "Muted"), self.sheet))
        self.summary = label("", "SectionTitle", wrap=True)
        self.show_filter = QComboBox()
        for text, _ in FILTERS:
            self.show_filter.addItem(text)
        self.show_filter.currentIndexChanged.connect(lambda _: self._fill())
        self.summary.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addLayout(h(self.summary, label("Show", "Muted"), self.show_filter))
        self.note = label("", "Muted", wrap=True)
        lay.addWidget(self.note)
        self.table = DataTable([
            Col("Row", lambda r: r.row, INT, 50),
            Col("Status", lambda r: STATUS_TEXT[r.status], width=95),
            Col("Code", lambda r: r.code, width=120),
            Col("Material", lambda r: r.product_name, width=190),
            Col("Bin", lambda r: r.bin_full or r.bin_code, width=165),
            Col("In WMS", lambda r: None if r.problem else r.current, QTY, 80),
            Col("Add", lambda r: r.qty, QTY, 80),
            Col("After", lambda r: r.after, QTY, 85),
            Col("Unit", lambda r: r.unit, width=50),
            Col("Note", lambda r: r.problem or r.warning or "")])
        self.table.set_row_style(lambda r: "low" if r.problem else "muted" if r.status == SAME else None)
        lay.addWidget(self.table, 1)
        self.b_create = button("Create import for approval", self.create, primary=True)
        self.b_create.setEnabled(False)
        lay.addLayout(h(label("Rows with a problem are left out; fix them in the file and import again.", "Muted"),
                        None, button("Cancel", self.reject), self.b_create))
        if path:
            self._load(path)

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Stock quantities", str(Path.home() / "Documents"),
                                              "Excel workbook (*.xlsx *.xlsm)")
        if path:
            self._load(path)

    def _load(self, path: str) -> None:
        try:
            names = self.svc.sheets(path)
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.file.setText(path)
        self.sheet.clear()
        self.sheet.addItems(names)
        self.check()

    def check(self) -> None:
        path = self.file.text()
        if not path:
            return
        sheet = self.sheet.currentText() or None
        try:
            self.plan = tasks.with_progress(self, "Stock import", lambda p: self.svc.read(path, sheet, progress=p),
                                            text="Reading the file...")
        except tasks.Cancelled:
            return
        except Exception as exc:  # noqa: BLE001
            self.plan = None
            show_error(self, exc)
        self._fill()

    def _fill(self) -> None:
        plan = self.plan
        if plan is None:
            self.summary.setText("")
            self.note.setText("")
            self.table.set_items([])
            self.b_create.setEnabled(False)
            return
        want = FILTERS[self.show_filter.currentIndex()][1]
        self.table.set_items([r for r in plan.rows if want is None or r.status == want
                              or (want == "WARN" and r.warning and not r.problem)])
        self.summary.setText(plan.summary())
        self.note.setText(f"<b>This file was already imported as {plan.imported_before}.</b> Importing it again "
                          "adds the same quantities a second time." if plan.imported_before else "")
        self.b_create.setEnabled(bool(plan.changes))

    def create(self) -> None:
        if not self.plan or not self.plan.changes:
            return
        again = bool(self.plan.imported_before)
        if again and not confirm(self, "Import again?", f"This file was already imported as "
                                 f"{self.plan.imported_before}.\n\nAdd the same quantities a second time?",
                                 yes="Add again", danger=True):
            return
        try:
            self.count = tasks.with_progress(self, "Stock import",
                                             lambda _p: self.svc.create_count(self.plan, allow_repeat=again),
                                             text="Creating the import...")
        except tasks.Cancelled:
            return
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.accept()
