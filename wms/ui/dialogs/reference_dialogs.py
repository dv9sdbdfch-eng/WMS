"""Suppliers, categories and units of measure."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.numbers import fmt_date
from wms.core.permissions import P
from wms.ui.widgets.common import DATE, DATETIME, INT, MONEY, QTY, Col, DataTable, PickCombo, button, h, label, run


class SupplierDialog(QDialog):
    def __init__(self, app, supplier_id: int | None, parent=None, tab: str | None = None):
        super().__init__(parent)
        self.app = app
        self.sup = next((s for s in app.services.reference.suppliers(include_inactive=True) if s.id == supplier_id),
                        None) if supplier_id else None
        self.can_edit = app.can(P.SUPPLIERS_EDIT)
        self.setWindowTitle(f"Supplier {self.sup.name}" if self.sup else "New supplier")
        self.resize(1050 if self.sup else 820, 680 if self.sup else 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        if self.sup:
            lay.addWidget(label(self.sup.name + ("" if self.sup.is_active else "  (archived)"), "PageTitle"))
            self.summary = label("", "SectionTitle")
            lay.addWidget(self.summary)
        w = QWidget()
        form = QFormLayout(w)
        self.f = {}
        for key, text in (("code", "Code *"), ("name", "Name *"), ("contact_name", "Contact"), ("phone", "Phone"),
                          ("email", "Email"), ("account_number", "Our account no.")):
            e = QLineEdit(getattr(self.sup, key) or "" if self.sup else "")
            e.setEnabled(self.can_edit)
            self.f[key] = e
            form.addRow(text, e)
        self.address = QPlainTextEdit(self.sup.address or "" if self.sup else "")
        self.notes = QPlainTextEdit(self.sup.notes or "" if self.sup else "")
        for t in (self.address, self.notes):
            t.setFixedHeight(70)
            t.setEnabled(self.can_edit)
        self.active = QCheckBox("Active")
        self.active.setChecked(self.sup.is_active if self.sup else True)
        self.active.setEnabled(self.can_edit)
        form.addRow("Address", self.address)
        form.addRow("Notes", self.notes)
        form.addRow("", self.active)
        tabs.addTab(w, "Details")
        if self.sup:
            view = app.services.search.supplier_view(self.sup.id)
            self.summary.setText(
                f"Products supplied: {view.products_supplied}   ·   Deliveries: {view.delivery_count}   ·   "
                f"Last delivery: {fmt_date(view.last_delivery) if view.last_delivery else 'none yet'}")
            pt = DataTable([Col("Code", lambda r: r.product.internal_code, width=110),
                            Col("Product", lambda r: r.product.name, width=260),
                            Col("Supplier Code", lambda r: r.product.supplier_code or "", width=110),
                            Col("Qty Received", lambda r: r.received, QTY, 100),
                            Col("Unit", lambda r: r.product.unit.code, width=55),
                            Col("Deliveries", lambda r: r.deliveries, INT, 75),
                            Col("Last Received", lambda r: r.last_received, DATETIME, 130),
                            Col("Main Supplier", lambda r: "Yes" if r.product.supplier_id == self.sup.id else "")])
            pt.set_items(view.products)
            pt.activated_item.connect(lambda r: app.open_entity("PRODUCT", r.product.id, self, tab="history"))
            tabs.addTab(pt, f"Products Supplied ({view.products_supplied})")
            self.products_tab = pt
        if self.sup and app.can(P.GOODS_IN_VIEW):
            dn = DataTable([Col("Date", lambda r: r[0].delivery_date, DATE, 95),
                            Col("Delivery Note", lambda r: r[0].dn_number, width=140),
                            Col("PO / Ref", lambda r: r[0].po_reference or "", width=110),
                            Col("Lines", lambda r: r[1], INT, 60),
                            Col("Value", lambda r: r[2], MONEY, 100),
                            Col("Received By", lambda r: r[0].received_by.full_name, width=130),
                            Col("Status", lambda r: r[0].status.title())])
            dn.set_items(app.services.goods_in.search(supplier_id=self.sup.id))
            dn.activated_item.connect(lambda r: app.open_entity("DELIVERY_NOTE", r[0].id, self))
            tabs.addTab(dn, "Delivery History")
            self.deliveries_tab = dn
        if self.sup and tab == "products":
            tabs.setCurrentWidget(self.products_tab)
        elif self.sup and tab == "deliveries" and hasattr(self, "deliveries_tab"):
            tabs.setCurrentWidget(self.deliveries_tab)
        lay.addWidget(tabs, 1)
        save = button("Save", self.save, primary=True)
        save.setVisible(self.can_edit)
        lay.addLayout(h(None, button("Close", self.reject), save))

    def save(self):
        data = {k: e.text() for k, e in self.f.items()}
        data.update(address=self.address.toPlainText(), notes=self.notes.toPlainText(),
                    is_active=self.active.isChecked())
        if run(self, self.app.services.reference.save_supplier, self.sup.id if self.sup else None, data):
            self.app.changed()
            self.accept()


class CategoryDialog(QDialog):
    def __init__(self, app, cat=None, parent=None):
        super().__init__(parent)
        self.app, self.cat = app, cat
        self.setWindowTitle("Edit category" if cat else "New category")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(cat.name if cat else "")
        self.parent_cat = PickCombo("None (top level)")
        self.parent_cat.set_options([(c.full_name, c.id) for c in app.services.reference.categories()
                                     if not cat or c.id != cat.id])
        if cat:
            self.parent_cat.set_value(cat.parent_id)
        self.active = QCheckBox("Active")
        self.active.setChecked(cat.is_active if cat else True)
        form.addRow("Name *", self.name)
        form.addRow("Parent category", self.parent_cat)
        form.addRow("", self.active)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))

    def save(self):
        if run(self, self.app.services.reference.save_category, self.cat.id if self.cat else None, self.name.text(),
               self.parent_cat.value(), self.active.isChecked()):
            self.accept()


class UnitDialog(QDialog):
    def __init__(self, app, unit=None, parent=None):
        super().__init__(parent)
        self.app, self.unit = app, unit
        self.setWindowTitle("Edit unit" if unit else "New unit of measure")
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.code = QLineEdit(unit.code if unit else "")
        self.code.setPlaceholderText("e.g. SHEET, M, KG")
        self.name = QLineEdit(unit.name if unit else "")
        self.decimal = QCheckBox("Allow part quantities (e.g. 2.5 m). Untick for whole items only.")
        self.decimal.setChecked(unit.allow_decimal if unit else False)
        self.active = QCheckBox("Active")
        self.active.setChecked(unit.is_active if unit else True)
        form.addRow("Code *", self.code)
        form.addRow("Name *", self.name)
        form.addRow("", self.decimal)
        form.addRow("", self.active)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))

    def save(self):
        if run(self, self.app.services.reference.save_unit, self.unit.id if self.unit else None, self.code.text(),
               self.name.text(), self.decimal.isChecked(), self.active.isChecked()):
            self.accept()
