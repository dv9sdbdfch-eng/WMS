from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QComboBox

from wms.core.enums import ItemType, StockStatus
from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.widgets.common import MONEY, QTY, Col, DataTable, PickCombo, SearchBox, button, h, label


def product_columns():
    return [
        Col("Code", lambda r: r.product.internal_code, width=120),
        Col("Name", lambda r: r.product.name, width=280),
        Col("Type", lambda r: ItemType(r.product.item_type).label, width=120),
        Col("Category", lambda r: r.product.category.full_name if r.product.category else "", width=150),
        Col("On Hand", lambda r: r.on_hand, QTY, 90),
        Col("Reserved", lambda r: r.reserved, QTY, 85),
        Col("Available", lambda r: r.available, QTY, 90),
        Col("Unit", lambda r: r.product.unit.code, width=60),
        Col("Min", lambda r: r.product.min_stock, QTY, 70),
        Col("Status", lambda r: "" if r.status is StockStatus.NO_MINIMUM else r.status.label, width=95),
        Col("Default Bin", lambda r: r.product.default_bin.full_code if r.product.default_bin else "", width=200),
        Col("Supplier", lambda r: r.product.supplier.name if r.product.supplier else "", width=150),
        Col("Supplier Code", lambda r: r.product.supplier_code or "", width=110),
        Col("Unit Cost", lambda r: r.product.unit_cost, MONEY, 90),
    ]


class ProductsPage(Page):
    title = "Products"
    subtitle = "Raw materials, components, consumables and finished products"
    item_types: list[str] | None = None

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.PRODUCTS_EDIT):
            self.add_action(button("New product", self.new, primary=True, shortcut="Ctrl+N"))
        self.search = SearchBox("Search code, name, description, supplier, supplier code, category, bin...")
        self.search.search.connect(lambda _: self.refresh())
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", None)
        for val, text in ItemType.choices():
            self.type_filter.addItem(text, val)
        self.type_filter.currentIndexChanged.connect(lambda _: self.refresh())
        self.category = PickCombo("All categories")
        self.category.picked.connect(lambda _: self.refresh())
        self.supplier = PickCombo("All suppliers")
        self.supplier.picked.connect(lambda _: self.refresh())
        self.location = PickCombo("All locations")
        self.location.picked.connect(lambda _: self.refresh())
        self.low_only = QCheckBox("Low stock only")
        self.low_only.toggled.connect(lambda _: self.refresh())
        self.inactive = QCheckBox("Show inactive")
        self.inactive.toggled.connect(lambda _: self.refresh())
        self.body.addLayout(h(self.search))
        filters = [self.type_filter, self.category, self.supplier, self.location, self.low_only, self.inactive, None]
        if self.item_types:
            filters.remove(self.type_filter)
            self.type_filter.hide()
        self.body.addLayout(h(*filters))
        self.table = DataTable(self.columns())
        self.table.set_row_style(lambda r: "low" if r.is_low else ("muted" if not r.product.is_active else None))
        self.table.activated_item.connect(self.open)
        self.body.addWidget(self.table, 1)
        self.count = label("", "Muted")
        self.body.addLayout(h(self.count, None, button("Open", lambda: self.open(self.table.current()))))
        self._filters_loaded = False

    def columns(self):
        return product_columns()

    def _load_filters(self):
        self.category.set_options([(c.full_name, c.id) for c in self.app.services.reference.categories()])
        self.supplier.set_options(self.app.supplier_options(include_inactive=True))
        self.location.set_options(self.app.location_options())

    def refresh(self):
        if not self._filters_loaded:
            self._load_filters()
            self._filters_loaded = True
        types = self.item_types or ([self.type_filter.currentData()] if self.type_filter.currentData() else None)
        rows = self.app.services.products.list(
            text=self.search.text(), item_types=types, category_id=self.category.value(),
            supplier_id=self.supplier.value(), location_id=self.location.value(), low_only=self.low_only.isChecked(),
            include_inactive=self.inactive.isChecked())
        self.table.set_items(rows)
        low = sum(1 for r in rows if r.is_low)
        self.count.setText(f"{len(rows)} product(s)" + (f"   ·   {low} low stock (highlighted)" if low else ""))

    def new(self):
        from wms.ui.dialogs.product_dialog import ProductDialog
        dlg = ProductDialog(self.app, None, self, default_type=(self.item_types or [None])[0])
        dlg.exec()
        if dlg.saved:
            self._filters_loaded = False
            self.app.changed()

    def open(self, row):
        if row is None:
            return
        from wms.ui.dialogs.product_dialog import ProductDialog
        dlg = ProductDialog(self.app, row.product.id, self)
        dlg.exec()
        if dlg.saved:
            self.app.changed()
