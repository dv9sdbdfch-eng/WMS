"""Finished products: stock list plus production records."""

from __future__ import annotations

from wms.core.enums import FinishedStatus, ItemType
from wms.core.permissions import P
from wms.ui.pages.products import ProductsPage
from wms.ui.widgets.common import DATE, QTY, Col, DataTable, button, label


class FinishedPage(ProductsPage):
    title = "Finished Products"
    subtitle = "Furniture made in the workshop: stock, location, status and production history"
    item_types = [ItemType.FINISHED.value]

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.PRODUCTION_POST):
            self.add_action(button("Record production", self.record))
        self.body.addWidget(label("Production history", "SectionTitle"))
        self.orders = DataTable([
            Col("Date", lambda o: o.production_date, DATE, 95),
            Col("Number", lambda o: o.order_number, width=100),
            Col("Product", lambda o: f"{o.product_code} - {o.product_name}", width=300),
            Col("Qty", lambda o: o.quantity, QTY, 70),
            Col("Bin", lambda o: o.output_bin_code, width=200),
            Col("Project", lambda o: o.site_label, width=220),
            Col("Materials", lambda o: len(o.lines), width=80),
            Col("Status", lambda o: o.status.title())])
        self.orders.setMaximumHeight(240)
        self.orders.set_row_style(lambda o: "muted" if o.status == "REVERSED" else None)
        self.orders.activated_item.connect(self._open_order)
        self.body.addWidget(self.orders)

    def columns(self):
        cols = [c for c in super().columns() if c.title not in ("Type", "Supplier", "Supplier Code", "Min")]
        cols.insert(3, Col("Status", lambda r: FinishedStatus(r.product.finished_info.status).label
                           if r.product.finished_info else "", width=110))
        cols.insert(4, Col("Project / Customer", lambda r: _project(r.product), width=200))
        return cols

    def refresh(self):
        super().refresh()
        self.orders.set_items(self.app.services.production.list())

    def record(self):
        from wms.ui.dialogs.stock_dialogs import ProductionEntryDialog
        ProductionEntryDialog(self.app, self).exec()

    def _open_order(self, order):
        self.app.open_entity("PRODUCTION", order.id, self)
        self.app.changed()


def _project(product) -> str:
    fi = product.finished_info
    if not fi:
        return ""
    return fi.site.display if getattr(fi, "site", None) else (fi.customer or "")
