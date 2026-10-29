"""Transfers, adjustments, returns and production entry."""

from __future__ import annotations

import uuid
from datetime import date

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QLineEdit, QPlainTextEdit, QVBoxLayout

from wms.core.enums import ItemType, ReturnType
from wms.core.exceptions import ReturnExceedsIssuedError, ValidationError
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.services.production import MaterialInput
from wms.services.stock_ops import ReturnLineInput
from wms.ui.widgets.common import (
    DateEdit,
    DecimalEdit,
    PickCombo,
    button,
    confirm,
    h,
    info,
    label,
    run,
    run_with_override,
    show_error,
)
from wms.ui.widgets.lines import LinesEditor


class TransferDialog(QDialog):
    """Move stock between bins (not Goods Out)."""

    def __init__(self, app, product_id: int | None = None, parent=None, from_bin_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex  # the same dialog can never post twice
        self.setWindowTitle("Transfer between bins")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Moves stock from one bin to another. The total quantity in stock does not change.",
                            "InfoBanner", wrap=True))
        form = QFormLayout()
        self.product = PickCombo("Type code or name...")
        self.product.set_options(app.product_options())
        self.product.picked.connect(lambda _: self._update())
        self.src = PickCombo("Choose bin...")
        self.src.picked.connect(lambda _: self._show_available())
        self.dst = PickCombo("Choose bin...")
        self.dst.set_options(app.bin_options())
        self.qty = DecimalEdit()
        self.available = label("", "Muted")
        self.notes = QLineEdit()
        form.addRow("Product *", self.product)
        form.addRow("From bin *", self.src)
        form.addRow("", self.available)
        form.addRow("To bin *", self.dst)
        form.addRow("Quantity *", self.qty)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        ok = button("Transfer", self.save, primary=True)
        lay.addLayout(h(None, button("Cancel", self.reject), ok))
        self._levels = {}
        if product_id:
            self.product.set_value(product_id)
            self._update()
        if from_bin_id:
            self.src.set_value(from_bin_id)
            self._show_available()

    def _update(self):
        pid = self.product.value()
        levels = self.app.services.products.stock_by_bin(pid) if pid else []
        self._levels = {l.bin_id: l for l in levels}
        old = self.src.value()
        self.src.set_options([(f"{l.bin.full_code}   ({fmt_qty(l.quantity)} {l.product.unit.code})", l.bin_id)
                              for l in levels if l.quantity > 0], keep=False)
        if old in self._levels:
            self.src.set_value(old)
        self._show_available()

    def _show_available(self):
        lvl = self._levels.get(self.src.value())
        self.available.setText(f"In this bin: {fmt_qty(lvl.quantity)} {lvl.product.unit.code}" if lvl else "")

    def save(self):
        try:
            qty = self.qty.value()
        except ValidationError as e:
            show_error(self, e)
            return
        if not (self.product.value() and self.src.value() and self.dst.value() and qty):
            info(self, "Transfer", "Choose the product, both bins and the quantity.")
            return
        if run(self, self.app.services.stock.transfer, self.product.value(), self.src.value(), self.dst.value(),
               qty, self.notes.text(), self._token):
            self.app.changed()
            self.accept()


class AdjustmentDialog(QDialog):
    """Set a bin's quantity to what was physically counted."""

    def __init__(self, app, product_id: int | None = None, bin_id: int | None = None, parent=None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex  # the same dialog can never post twice
        self.setWindowTitle("Stock adjustment")
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Use when the physical stock differs from the system. The previous quantity, new "
                            "quantity, difference, reason and your name are recorded permanently.",
                            "InfoBanner", wrap=True))
        form = QFormLayout()
        self.product = PickCombo("Type code or name...")
        self.product.set_options(app.product_options())
        self.product.picked.connect(lambda _: self._update())
        self.bin = PickCombo("Choose bin...")
        self.bin.set_options(app.bin_options())
        self.bin.picked.connect(lambda _: self._update())
        self.current = label("", "SectionTitle")
        self.new_qty = DecimalEdit()
        self.new_qty.textChanged.connect(lambda _: self._update_diff())
        self.diff = label("", "Muted")
        self.reason_code = QComboBox()
        self.reason_code.addItem("Choose a reason...", None)
        for val, text in app.services.reasons.choices("ADJUSTMENT"):
            self.reason_code.addItem(text, val)
        self.reason = QPlainTextEdit()
        self.reason.setFixedHeight(70)
        self.reason.setPlaceholderText("Explain what was found, e.g. 'Monthly count: 2 sheets damaged by forklift'")
        form.addRow("Product *", self.product)
        form.addRow("Bin *", self.bin)
        form.addRow("Current quantity", self.current)
        form.addRow("Counted (new) quantity *", self.new_qty)
        form.addRow("Difference", self.diff)
        form.addRow("Reason *", self.reason_code)
        form.addRow("Details *", self.reason)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Post adjustment", self.save, primary=True)))
        self._prev = None
        if product_id:
            self.product.set_value(product_id)
        if bin_id:
            self.bin.set_value(bin_id)
        self._update()

    def _update(self):
        pid, bid = self.product.value(), self.bin.value()
        self._prev = None
        if pid and bid:
            lvl = next((l for l in self.app.services.products.stock_by_bin(pid) if l.bin_id == bid), None)
            self._prev = lvl.quantity if lvl else 0
            unit = self.app.services.products.get(pid).unit.code
            self.current.setText(f"{fmt_qty(self._prev)} {unit}")
        else:
            self.current.setText("-")
        self._update_diff()

    def _update_diff(self):
        try:
            v = self.new_qty.value()
        except ValidationError:
            v = None
        if v is None or self._prev is None:
            self.diff.setText("")
            return
        d = v - self._prev
        self.diff.setText(("+" if d > 0 else "") + fmt_qty(d))

    def save(self):
        try:
            new = self.new_qty.value()
        except ValidationError as e:
            show_error(self, e)
            return
        if not (self.product.value() and self.bin.value()) or new is None:
            info(self, "Stock adjustment", "Choose the product and bin and enter the counted quantity.")
            return
        if not confirm(self, "Confirm adjustment",
                       f"Change the quantity from {fmt_qty(self._prev)} to {fmt_qty(new)}?\n\nThis is permanent and "
                       f"recorded in the audit trail."):
            return
        if run(self, self.app.services.stock.adjust, self.product.value(), self.bin.value(), new,
               self.reason_code.currentData(), self.reason.toPlainText(), self._prev, self._token):
            self.app.changed()
            self.accept()


class ReturnEntryDialog(QDialog):
    def __init__(self, app, return_type: str = ReturnType.FROM_SITE.value, parent=None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex  # the same dialog can never post twice
        self.setWindowTitle("New return")
        self.resize(1000, 640)
        lay = QVBoxLayout(self)
        form = self.form = QFormLayout()
        self.rtype = QComboBox()
        for val, text in ReturnType.choices():
            self.rtype.addItem(text, val)
        self.rtype.setCurrentIndex(self.rtype.findData(return_type))
        self.rtype.currentIndexChanged.connect(self._type_changed)
        self.date = DateEdit()
        self.site = PickCombo("Choose site...")
        self.site.set_options(app.site_options(open_only=False))
        self.supplier = PickCombo("Choose supplier...")
        self.supplier.set_options(app.supplier_options())
        self.reference = QLineEdit()
        self.reason = QLineEdit()
        form.addRow("Return type *", self.rtype)
        form.addRow("Date *", self.date)
        form.addRow("From site / project *", self.site)
        form.addRow("To supplier *", self.supplier)
        form.addRow("Reference", self.reference)
        form.addRow("Reason", self.reason)
        lay.addLayout(form)
        self.hint = label("", "InfoBanner", wrap=True)
        lay.addWidget(self.hint)
        self.lines = LinesEditor(app, show_bin=True, show_condition=True, show_stock=True)
        lay.addWidget(self.lines, 1)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Post return", self.save, primary=True)))
        self.lines.add_row()
        self._type_changed()

    def _type_changed(self):
        from_site = self.rtype.currentData() == ReturnType.FROM_SITE.value
        self.form.setRowVisible(self.site, from_site)
        self.form.setRowVisible(self.supplier, not from_site)
        self.hint.setText("Material coming back from site is added to the chosen bin. Put damaged material in the "
                          "QUARANTINE bin so it cannot be issued." if from_site else
                          "Goods going back to the supplier are taken out of the chosen bin.")

    def save(self):
        try:
            lines = self.lines.values()
        except ValidationError as e:
            show_error(self, e)
            return
        if not lines:
            info(self, "Return", "Add at least one line.")
            return
        header = {"return_type": self.rtype.currentData(), "return_date": self.date.value(),
                  "site_id": self.site.value(), "supplier_id": self.supplier.value(),
                  "reference": self.reference.text(), "reason": self.reason.text(), "client_token": self._token}
        inputs = [ReturnLineInput(v.product_id, v.quantity, v.bin_id, v.condition or "USABLE", v.notes) for v in lines]

        def post(header, lines, override_reason=None):
            try:
                return self.app.services.stock.post_return(header, lines, override_reason)
            except ReturnExceedsIssuedError as exc:
                if not confirm(self, "More than was issued", f"{exc.message}\n\nThis is usually a typing mistake "
                               f"or the wrong site. Post anyway (e.g. material bought directly for the site)?",
                               yes="Post anyway", danger=True):
                    return None
                return self.app.services.stock.post_return({**header, "allow_more_than_issued": True}, lines,
                                                           override_reason)

        ret = run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE), post, header=header, lines=inputs)
        if ret:
            self.app.changed()
            info(self, "Return posted", f"Return {ret.return_number} has been posted.")
            self.accept()


class ProductionEntryDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex  # the same dialog can never post twice
        self.setWindowTitle("Record production")
        self.resize(1000, 660)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Materials listed below are taken out of stock and the finished product is added to "
                            "stock in one step.", "InfoBanner", wrap=True))
        form = QFormLayout()
        self.product = PickCombo("Finished product...")
        self.product.set_options(app.product_options([ItemType.FINISHED.value]))
        self.qty = DecimalEdit()
        self.bin = PickCombo("Default bin of the product")
        self.bin.set_options(app.bin_options())
        self.date = DateEdit(date.today())
        self.site = PickCombo("None")
        self.site.set_options(app.site_options())
        self.notes = QLineEdit()
        form.addRow("Finished product *", self.product)
        form.addRow("Quantity produced *", self.qty)
        form.addRow("Store in bin", self.bin)
        form.addRow("Production date *", self.date)
        form.addRow("Project / site", self.site)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        lay.addWidget(label("Materials consumed", "SectionTitle"))
        self.lines = LinesEditor(app, show_bin=True, bin_optional=True, show_stock=True,
                                 product_types=[ItemType.RAW_MATERIAL.value, ItemType.COMPONENT.value,
                                                ItemType.CONSUMABLE.value])
        lay.addWidget(self.lines, 1)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Post production", self.save, primary=True)))
        self.lines.add_row()

    def save(self):
        try:
            lines = self.lines.values()
            qty = self.qty.value()
        except ValidationError as e:
            show_error(self, e)
            return
        if not lines and not confirm(self, "No materials", "No materials are listed. Record the finished product "
                                     "without consuming any material?"):
            return
        header = {"finished_product_id": self.product.value(), "quantity": qty, "output_bin_id": self.bin.value(),
                  "production_date": self.date.value(), "site_id": self.site.value(), "notes": self.notes.text(),
                  "client_token": self._token}
        order = run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE), self.app.services.production.post,
                                  header=header, materials=[MaterialInput(v.product_id, v.quantity, v.bin_id)
                                                            for v in lines])
        if order:
            self.app.changed()
            info(self, "Production recorded", f"{order.order_number} has been posted.")
            self.accept()
