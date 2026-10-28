"""Products at or below their minimum stock level."""

from __future__ import annotations

from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.pages.products import product_columns
from wms.ui.widgets.common import QTY, Col, DataTable, button, h, label


class LowStockPage(Page):
    title = "Low Stock"
    subtitle = "Products at or below their minimum level, with the quantity to reorder"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.REPORTS_EXPORT):
            self.add_action(button("Export...", self.export))
        cols = product_columns()
        cols.insert(6, Col("Shortfall", lambda r: r.shortfall, QTY, 90))
        cols.insert(7, Col("Reorder Qty", lambda r: r.product.reorder_qty, QTY, 90))
        self.table = DataTable(cols)
        self.table.activated_item.connect(self.open)
        self.body.addWidget(self.table, 1)
        self.count = label("", "Muted")
        self.body.addLayout(h(self.count, None, button("Copy list", self.table.copy_all)))

    def refresh(self):
        rows = self.app.services.products.low_stock()
        self.table.set_items(rows)
        self.count.setText(f"{len(rows)} product(s) need reordering" if rows else "Nothing is below minimum level.")

    def open(self, row):
        if row is not None:
            self.app.open_entity("PRODUCT", row.product.id, self)
            self.app.changed()

    def export(self):
        self.main_window.go("reports")
        page = self.main_window.page("reports")
        if page is not None:
            page.select("low_stock")
