"""Stocktake: count -> review differences -> approve (posts adjustments)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLineEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import CountStatus
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_datetime, fmt_qty
from wms.core.permissions import P
from wms.services.stocktake import CountEntry
from wms.ui.pages.base import Page
from wms.ui.widgets.common import (
    DATETIME,
    INT,
    Col,
    DataTable,
    DecimalEdit,
    PickCombo,
    ask_text,
    button,
    confirm,
    h,
    info,
    label,
    run,
    show_error,
)


class NewCountDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New stock count")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Every product with stock in the chosen area is put on the count sheet with today's "
                            "system quantity. Stock does not change until the count is approved.", "InfoBanner",
                            wrap=True))
        form = QFormLayout()
        self.location = PickCombo("Whole warehouse")
        self.location.set_options(app.location_options())
        self.description = QLineEdit()
        self.description.setPlaceholderText("e.g. Monthly count - Zone A")
        self.zero = QCheckBox("Also list products whose default bin is in this area but shows no stock")
        form.addRow("Area", self.location)
        form.addRow("Description", self.description)
        form.addRow("", self.zero)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Start count", self.accept, primary=True)))


class StocktakePage(Page):
    title = "Stocktake"
    subtitle = "Physical count: system quantity -> counted -> difference -> review -> approved adjustment"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.can_count = app.can(P.STOCKTAKE_COUNT)
        self.can_approve = app.can(P.STOCK_ADJUST)
        if self.can_count:
            self.add_action(button("New count", self.new, primary=True, shortcut="Ctrl+N"))
        self.status = QComboBox()
        for text, val in (("Open and awaiting approval", "active"), ("All counts", None), ("Open", "OPEN"),
                          ("Awaiting approval", "SUBMITTED"), ("Approved", "APPROVED"), ("Cancelled", "CANCELLED")):
            self.status.addItem(text, val)
        self.status.currentIndexChanged.connect(lambda _: self.refresh())

        split = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addLayout(h(label("Show", "Muted"), self.status, None))
        self.counts = DataTable([
            Col("Number", lambda c: c.count_number, width=100),
            Col("Status", lambda c: CountStatus(c.status).label, width=130),
            Col("Area", lambda c: c.location.path if c.location else "Whole warehouse", width=260),
            Col("Description", lambda c: c.description or "", width=240),
            Col("Lines", lambda c: len(c.lines), INT, 60),
            Col("Counted", lambda c: sum(1 for l in c.lines if l.counted_qty is not None), INT, 70),
            Col("Differences", lambda c: sum(1 for l in c.lines if l.difference), INT, 85),
            Col("Started", lambda c: c.created_at, DATETIME, 130),
            Col("By", lambda c: c.created_by.full_name)])
        self.counts.activated_item.connect(self.load)
        self.counts.selectionModel().currentRowChanged.connect(lambda *_: self.load(self.counts.current()))
        tl.addWidget(self.counts, 1)
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.header = label("Select a count above, or start a new one.", "SectionTitle")
        self.summary = label("", "Muted", wrap=True)
        bl.addWidget(self.header)
        bl.addWidget(self.summary)
        self.lines = QTableWidget(0, 8)
        self.lines.setHorizontalHeaderLabels(["Bin", "Code", "Product", "Unit", "System", "Counted", "Difference",
                                              "Notes"])
        hh = self.lines.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for col, width in enumerate((240, 110, 260, 55, 80, 95, 85)):
            self.lines.setColumnWidth(col, width)
        self.lines.verticalHeader().setDefaultSectionSize(32)
        bl.addWidget(self.lines, 1)
        self.b_save = button("Save counts", self.save, primary=True, shortcut="Ctrl+S")
        self.b_add = button("Add item found...", self.add_line)
        self.b_submit = button("Submit for approval", self.submit)
        self.b_recheck = button("Re-check moved lines", self.recheck)
        self.b_back = button("Send back for recount", self.send_back)
        self.b_approve = button("Approve and post adjustments", self.approve, primary=True)
        self.b_cancel = button("Cancel count", self.cancel, danger=True)
        self.b_sheet = button("Count sheet (PDF / Excel)...", self.sheet)
        bl.addLayout(h(self.b_save, self.b_add, self.b_submit, self.b_recheck, None, self.b_sheet, self.b_back,
                       self.b_approve, self.b_cancel))
        split.addWidget(bottom)
        split.setSizes([260, 520])
        self.body.addWidget(split, 1)
        self.current = None
        self._show(None)

    # -- list ---------------------------------------------------------------
    def refresh(self):
        val = self.status.currentData()
        rows = self.app.services.stocktake.list(None if val in (None, "active") else val)
        if val == "active":
            rows = [c for c in rows if c.status in (CountStatus.OPEN.value, CountStatus.SUBMITTED.value)]
        # Only the list is reloaded here: the open count keeps any quantities typed but not yet saved.
        self.counts.set_items(rows)

    def load(self, count):
        if count is None:
            return
        self._show(self.app.services.stocktake.get(count.id))

    # -- detail -------------------------------------------------------------
    def _show(self, c):
        self.current = c
        status = CountStatus(c.status) if c else None
        editable = bool(c) and status is CountStatus.OPEN and self.can_count
        # Blind count: operatives see the system quantity only once the count is with the reviewer.
        show_system = bool(c) and (self.can_approve or status is not CountStatus.OPEN)
        self.lines.setColumnHidden(4, not show_system)
        self.lines.setColumnHidden(6, not show_system)
        self.lines.setRowCount(0)
        for b, ok in ((self.b_save, editable), (self.b_add, editable), (self.b_submit, editable),
                      (self.b_recheck, bool(c) and status in (CountStatus.OPEN, CountStatus.SUBMITTED)
                       and self.can_count),
                      (self.b_back, bool(c) and status is CountStatus.SUBMITTED and self.can_approve),
                      (self.b_approve, bool(c) and status is CountStatus.SUBMITTED and self.can_approve),
                      (self.b_cancel, bool(c) and status in (CountStatus.OPEN, CountStatus.SUBMITTED)
                       and self.can_count),
                      (self.b_sheet, bool(c))):
            b.setVisible(ok)
        if c is None:
            self.summary.setText("")
            return
        self.header.setText(f"{c.count_number} - {c.location.path if c.location else 'Whole warehouse'}"
                            f"  ({status.label})")
        counted = sum(1 for l in c.lines if l.counted_qty is not None)
        diffs = [l for l in c.lines if l.difference]
        extra = ""
        if status is CountStatus.APPROVED:
            extra = f" Approved by {c.closed_by.full_name} {fmt_datetime(c.closed_at)}."
        elif status is CountStatus.CANCELLED:
            extra = f" Cancelled by {c.closed_by.full_name}: {c.close_reason}"
        elif status is CountStatus.SUBMITTED:
            extra = f" Submitted by {c.submitted_by.full_name} {fmt_datetime(c.submitted_at)}."
        self.summary.setText(f"{counted} of {len(c.lines)} line(s) counted"
                             + (f", {len(diffs)} difference(s)." if show_system else ".") + extra
                             + (" Type the quantity physically found; enter 0 if it is not there." if editable
                                else ""))
        self.lines.setRowCount(len(c.lines))
        for r, l in enumerate(c.lines):
            for col, text in enumerate((l.bin.full_code, l.product.internal_code, l.product.name,
                                        l.product.unit.code, fmt_qty(l.system_qty))):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.lines.setItem(r, col, item)
            q = DecimalEdit()
            q.set_value(l.counted_qty)
            q.setReadOnly(not editable)
            q.returnPressed.connect(lambda row=r: self._next(row))
            self.lines.setCellWidget(r, 5, q)
            diff = QTableWidgetItem("" if l.difference is None else
                                    (("+" if l.difference > 0 else "") + fmt_qty(l.difference)))
            diff.setFlags(diff.flags() & ~Qt.ItemFlag.ItemIsEditable)
            diff.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.lines.setItem(r, 6, diff)
            n = QLineEdit(l.notes or "")
            n.setReadOnly(not editable)
            self.lines.setCellWidget(r, 7, n)
        if editable and c.lines:
            self.lines.cellWidget(0, 5).setFocus()

    def _next(self, row: int):
        if row + 1 < self.lines.rowCount():
            w = self.lines.cellWidget(row + 1, 5)
            w.setFocus()
            w.selectAll()

    def _entries(self) -> list[CountEntry]:
        out = []
        for r, l in enumerate(self.current.lines):
            try:
                qty = self.lines.cellWidget(r, 5).value()
            except ValidationError:
                raise ValidationError(f"Line {r + 1} ({l.product.internal_code}): invalid number.") from None
            out.append(CountEntry(l.id, qty, self.lines.cellWidget(r, 7).text()))
        return out

    # -- actions ------------------------------------------------------------
    def new(self):
        dlg = NewCountDialog(self.app, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        c = run(self, self.app.services.stocktake.create, dlg.location.value(), dlg.description.text(),
                dlg.zero.isChecked())
        if c:
            self.status.setCurrentIndex(0)
            self.refresh()
            self._show(c)

    def save(self, quiet: bool = False) -> bool:
        if self.current is None:
            return False
        try:
            entries = self._entries()
        except ValidationError as e:
            show_error(self, e)
            return False
        c = run(self, self.app.services.stocktake.record_counts, self.current.id, entries)
        if c:
            self._show(c)
            self.refresh()
            if not quiet:
                info(self, "Stocktake", "Counts saved. Stock has not changed yet.")
        return bool(c)

    def add_line(self):
        if self.current is None or not self.save(quiet=True):
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Item found that is not on the sheet")
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        prod, b = PickCombo("Type code or name..."), PickCombo("Choose bin...")
        prod.set_options(self.app.product_options())
        b.set_options(self.app.bin_options())
        form.addRow("Product *", prod)
        form.addRow("Bin *", b)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", dlg.reject), button("Add", dlg.accept, primary=True)))
        if dlg.exec() == QDialog.DialogCode.Accepted and prod.value() and b.value():
            c = run(self, self.app.services.stocktake.add_line, self.current.id, prod.value(), b.value())
            if c:
                self._show(c)

    def submit(self):
        if self.current is None or not self.save(quiet=True):
            return
        if confirm(self, "Submit count", "Submit this count for approval? It cannot be edited afterwards unless the "
                                         "reviewer sends it back."):
            c = run(self, self.app.services.stocktake.submit, self.current.id)
            if c:
                self._show(c)
                self.refresh()

    def recheck(self):
        if self.current is None:
            return
        moved = run(self, self.app.services.stocktake.changed_since_count, self.current.id)
        if not moved:
            info(self, "Stocktake", "No stock has moved in the counted bins since the count started.")
            return
        names = "\n".join(f"  {l.product.internal_code} in {l.bin.full_code}" for l in moved[:15])
        if confirm(self, "Stock moved during the count",
                   f"Stock moved in {len(moved)} counted bin(s) after this count started:\n{names}\n\n"
                   "Update their system quantity and clear their count so they are counted again?"):
            run(self, self.app.services.stocktake.refresh_system_qty, self.current.id)
            self.load(self.current)
            self.refresh()

    def send_back(self):
        reason = ask_text(self, "Send back for recount", "What needs to be counted again?")
        if reason and run(self, self.app.services.stocktake.send_back, self.current.id, reason):
            self.load(self.current)
            self.refresh()

    def approve(self):
        c = self.current
        diffs = [l for l in c.lines if l.difference]
        value = sum(((l.difference or 0) * (l.product.unit_cost or 0) for l in diffs), 0)
        if not confirm(self, "Approve count",
                       f"Post {len(diffs)} stock adjustment(s) from {c.count_number}?\n\nNet value of the differences: "
                       f"£{value:,.2f}. Each adjustment is recorded with reason 'Stock Count Difference' and your name.",
                       yes="Approve"):
            return
        done = run(self, self.app.services.stocktake.approve, c.id)
        if done:
            self.app.changed()
            self._show(done)
            self.refresh()
            info(self, "Stocktake", f"{c.count_number} approved: {len(diffs)} adjustment(s) posted.")

    def cancel(self):
        reason = ask_text(self, "Cancel count", "Why is this count being cancelled?")
        if reason and run(self, self.app.services.stocktake.cancel, self.current.id, reason):
            self.load(self.current)
            self.refresh()

    def sheet(self):
        if self.current is None:
            return
        from wms.exporters.export import export
        res = self.app.services.stocktake.count_sheet(self.current.id,
                                                      show_system_qty=self.current.status != CountStatus.OPEN.value)
        default = Path.home() / "Documents" / f"Stock_count_{self.current.count_number}_{date.today():%Y-%m-%d}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Count sheet", str(default),
                                              "PDF document (*.pdf);;Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith((".pdf", ".xlsx")):
            path += ".pdf"
        out = run(self, export, res, Path(path), self.app.ctx.config.company_name)
        if out:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
