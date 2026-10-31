"""Purchase Orders: what has been ordered from suppliers and what is still to arrive."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.enums import POStatus
from wms.core.numbers import fmt_money
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import DATE, INT, MONEY, Col, PickCombo, button


class PurchaseOrdersPage(ListPage):
    title = "Purchase Orders"
    subtitle = "Orders to suppliers. Deliveries are received against them in Goods In, in one or several parts."
    search_hint = "Search PO no., supplier, supplier reference, project, product code or name..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.PRODUCTS_VIEW):
            self.add_link("Reorder Required", "low_stock", tooltip="Materials at or below their reorder level - "
                          "create draft orders from them")
        if app.can(P.PURCHASING_EDIT):
            self.add_action(button("New purchase order", self.new_order, primary=True, shortcut="Ctrl+N"))
        self.supplier = PickCombo("All suppliers")
        self.add_filter(self.supplier, self.supplier.picked)
        self.status = QComboBox()
        self.status.addItem("Open (ordered, part received)", "OPEN")
        self.status.addItem("Overdue (past expected date)", "OVERDUE")
        self.status.addItem("All statuses", None)
        for val, text in POStatus.choices():
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        self.table.set_row_style(lambda r: "low" if r.is_overdue else "muted"
                                 if r.po.status in ("RECEIVED", "CANCELLED") else None)

    def columns(self):
        return [Col("PO No.", lambda r: r.po.po_number, width=100),
                Col("Order date", lambda r: r.po.order_date, DATE, 90),
                Col("Supplier", lambda r: r.po.supplier_label, width=200),
                Col("Project", lambda r: r.po.site_label, width=190),
                Col("Expected", lambda r: r.po.expected_date, DATE, 90),
                Col("Status", lambda r: POStatus(r.po.status).label + (" (closed short)" if r.po.closed_short else "")
                    + ("  - OVERDUE" if r.is_overdue else ""), width=170),
                Col("Lines", lambda r: r.line_count, INT, 55),
                Col("Lines to come", lambda r: r.lines_outstanding or None, INT, 95),
                Col("Value", lambda r: r.value, MONEY, 95),
                Col("Supplier ref", lambda r: r.po.supplier_reference or "", width=120),
                Col("Notes", lambda r: r.po.notes or "")]

    def refresh(self):
        if not self.supplier.count():
            self.supplier.set_options(self.app.supplier_options(include_inactive=True))
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        st = self.status.currentData()
        return self.app.services.purchasing.list(self.search.text(), None if st in ("OPEN", "OVERDUE") else st,
                                                 self.supplier.value(), open_only=st == "OPEN",
                                                 date_from=d1, date_to=d2, overdue_only=st == "OVERDUE")

    def describe(self, rows):
        overdue = sum(1 for r in rows if r.is_overdue)
        text = f"{len(rows)} order(s)   ·   value {fmt_money(sum((r.value for r in rows), 0))}"
        return text + (f"   ·   {overdue} overdue" if overdue else "")

    def new_order(self):
        from wms.ui.dialogs.purchasing import PurchaseOrderDialog
        PurchaseOrderDialog(self.app, None, self, on_receive=self.receive).exec()
        self.app.changed()
        self.refresh()

    def open_item(self, item):
        from wms.ui.dialogs.purchasing import PurchaseOrderDialog
        PurchaseOrderDialog(self.app, item.po.id, self, on_receive=self.receive).exec()
        self.refresh()

    def receive(self, po_id: int) -> None:
        """Open Goods In with this order's outstanding lines filled in."""
        mw = self.main_window
        mw.go("goods_in")
        mw.page("goods_in").load_purchase_order(po_id)
