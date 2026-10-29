"""Delivery Notes: permanent, searchable archive of every supplier delivery."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.numbers import fmt_money
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import DATE, DATETIME, INT, MONEY, Col, PickCombo


class DeliveryNotesPage(ListPage):
    title = "Delivery Notes"
    subtitle = "Every delivery received, with its lines, stock transactions and the original scan"
    search_hint = "Search delivery note no., PO, supplier, product code or name..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.supplier = PickCombo("All suppliers")
        self.add_filter(self.supplier, self.supplier.picked)
        self.status = QComboBox()
        for text, val in (("All statuses", None), ("Posted", "POSTED"), ("Reversed", "REVERSED")):
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        self.table.set_row_style(lambda r: "muted" if r[0].status == "REVERSED" else None)

    def columns(self):
        return [Col("Date", lambda r: r[0].delivery_date, DATE, 95),
                Col("Delivery Note", lambda r: r[0].dn_number, width=140),
                Col("Supplier", lambda r: r[0].supplier_label, width=220),
                Col("PO / Ref", lambda r: r[0].po_reference or "", width=110),
                Col("Lines", lambda r: r[1], INT, 60),
                Col("Value", lambda r: r[2], MONEY, 100),
                Col("Received By", lambda r: r[0].received_by.full_name, width=140),
                Col("Entered", lambda r: r[0].created_at, DATETIME, 130),
                Col("Status", lambda r: r[0].status.title(), width=90),
                Col("Notes", lambda r: r[0].notes or "")]

    def refresh(self):
        if not self.supplier.count():
            self.supplier.set_options(self.app.supplier_options(include_inactive=True))
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.goods_in.search(self.search.text(), self.supplier.value(), d1, d2,
                                                 self.status.currentData())

    def describe(self, rows):
        total = sum((r[2] for r in rows if r[0].status == "POSTED"), 0)
        return f"{len(rows)} delivery note(s)   ·   posted value {fmt_money(total)}"

    def open_item(self, item):
        self.app.open_entity("DELIVERY_NOTE", item[0].id, self)
