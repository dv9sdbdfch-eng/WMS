"""Goods In: enter a supplier delivery note and receive its lines into stock."""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtWidgets import QFileDialog, QFormLayout, QLineEdit, QListWidget, QSplitter, QVBoxLayout, QWidget

from wms.core.exceptions import ValidationError
from wms.services.goods_in import ReceiptLine
from wms.ui.pages.base import Page
from wms.ui.widgets.attachments import FilePreview
from wms.ui.widgets.common import DateEdit, PickCombo, button, confirm, h, info, label, run, show_error
from wms.ui.widgets.lines import LinesEditor

FILE_FILTER = "Scans and documents (*.pdf *.jpg *.jpeg *.png);;All files (*)"


class GoodsInPage(Page):
    title = "Goods In"
    subtitle = "Receive a supplier delivery. Every line adds stock to the chosen bin."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.add_action(button("Clear form", self.clear))
        self.add_action(button("Post delivery", self.post, primary=True, shortcut="Ctrl+S"))

        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.supplier = PickCombo("Choose supplier...")
        self.supplier.picked.connect(lambda _: self._check_duplicate())
        self.dn_number = QLineEdit()
        self.dn_number.setPlaceholderText("Number printed on the supplier's delivery note")
        self.dn_number.editingFinished.connect(self._check_duplicate)
        self.date = DateEdit(date.today())
        self.po = QLineEdit()
        self.po.setPlaceholderText("Optional purchase order number")
        self.notes = QLineEdit()
        form.addRow("Supplier *", self.supplier)
        form.addRow("Delivery note no. *", self.dn_number)
        form.addRow("Delivery date *", self.date)
        form.addRow("PO / reference", self.po)
        form.addRow("Notes", self.notes)
        ll.addLayout(form)
        self.dup_warning = label("", "Banner", wrap=True)
        self.dup_warning.hide()
        ll.addWidget(self.dup_warning)
        ll.addWidget(label("Products received", "SectionTitle"))
        self.lines = LinesEditor(app, show_bin=True, bin_optional=True, show_cost=True)
        ll.addWidget(self.lines, 1)
        ll.addWidget(label("If the bin is left empty the product's default bin is used. Unit cost updates the "
                           "product's current cost.", "Muted", wrap=True))
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(label("Original delivery note (scan / photo / PDF)", "SectionTitle"))
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        self.files.currentRowChanged.connect(self._preview)
        rl.addWidget(self.files)
        rl.addLayout(h(button("Attach file...", self.attach), button("Remove", self.remove_file), None))
        self.preview = FilePreview()
        self.preview.empty.setText("Attach the scanned delivery note so it can be checked against the lines.")
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setSizes([1050, 380])
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.body.addWidget(split, 1)
        self._paths: list[str] = []
        self.lines.add_row()

    def refresh(self):
        self.supplier.set_options(self.app.supplier_options())
        self.lines.reload_options()

    # -- duplicate protection ----------------------------------------------
    def _check_duplicate(self):
        sup, number = self.supplier.value(), self.dn_number.text().strip()
        existing = self.app.services.goods_in.find_existing(sup, number) if sup and number else None
        if existing:
            self.dup_warning.setText(f"Delivery note {existing.dn_number} from this supplier was already entered on "
                                     f"{existing.delivery_date:%d/%m/%Y}. Open it from Delivery Notes instead.")
        self.dup_warning.setVisible(bool(existing))
        return existing

    # -- attachments ---------------------------------------------------------
    def attach(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach delivery note", "", FILE_FILTER)
        for p in paths:
            try:
                self.app.services.attachments.validate_file(p)
            except ValidationError as e:
                show_error(self, e)
                continue
            if p not in self._paths:
                self._paths.append(p)
                self.files.addItem(p.replace("\\", "/").rsplit("/", 1)[-1])
        if self._paths:
            self.files.setCurrentRow(self.files.count() - 1)

    def remove_file(self):
        r = self.files.currentRow()
        if r >= 0:
            self._paths.pop(r)
            self.files.takeItem(r)
            if not self._paths:
                self.preview.show_file(None)

    def _preview(self, row: int):
        from pathlib import Path
        self.preview.show_file(Path(self._paths[row]) if 0 <= row < len(self._paths) else None)

    # -- posting -------------------------------------------------------------
    def clear(self):
        self.dn_number.clear()
        self.po.clear()
        self.notes.clear()
        self.date.set_value(date.today())
        self.lines.clear()
        self.lines.add_row()
        self._paths.clear()
        self.files.clear()
        self.preview.show_file(None)
        self.dup_warning.hide()

    def post(self):
        try:
            values = self.lines.values()
        except ValidationError as e:
            show_error(self, e)
            return
        if not self.supplier.value() or not self.dn_number.text().strip():
            info(self, "Goods In", "Choose the supplier and enter the delivery note number.")
            return
        if not values:
            info(self, "Goods In", "Add at least one product line.")
            return
        if self._check_duplicate():
            info(self, "Already entered", self.dup_warning.text())
            return
        d = self.date.value()
        if d < date.today() - timedelta(days=30) and not confirm(
                self, "Old delivery date", f"The delivery date {d:%d/%m/%Y} is more than 30 days ago. Is it correct?"):
            return
        dups = self.lines.duplicate_products()
        if dups and not confirm(self, "Repeated products",
                                "These products appear on more than one line: " + ", ".join(dups) +
                                "\n\nThat is fine if they go into different bins. Continue?"):
            return
        if not self._paths and not confirm(self, "No scan attached",
                                           "No scan of the delivery note is attached. Post without it? "
                                           "You can attach it later from Delivery Notes."):
            return
        header = {"supplier_id": self.supplier.value(), "dn_number": self.dn_number.text(), "delivery_date": d,
                  "po_reference": self.po.text(), "notes": self.notes.text()}
        lines = [ReceiptLine(v.product_id, v.quantity, v.bin_id, v.unit_cost, v.notes) for v in values]
        dn = run(self, self.app.services.goods_in.post, header, lines, list(self._paths))
        if dn:
            self.app.changed()
            info(self, "Delivery posted", f"Delivery note {dn.dn_number} has been posted: {len(dn.lines)} line(s) "
                                          f"added to stock.")
            self.clear()
