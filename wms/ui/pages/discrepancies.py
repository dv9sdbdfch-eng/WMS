"""Delivery Problems: short, over, damaged, rejected and wrong deliveries until they are resolved."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import PickCombo, button


class DiscrepanciesPage(ListPage):
    title = "Delivery Problems"
    subtitle = "Short, over, damaged, rejected or wrong goods, with photos. Open ones show on the dashboard."
    search_hint = "Search number, supplier, PO, delivery note, product or notes..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.GOODS_IN_POST) or app.can(P.PURCHASING_EDIT):
            self.add_action(button("Report a problem...", self.new_problem, primary=True))
        self.supplier = PickCombo("All suppliers")
        self.add_filter(self.supplier, self.supplier.picked)
        self.status = QComboBox()
        for text, val in (("Open", "OPEN"), ("Resolved", "RESOLVED"), ("All", None)):
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        from wms.ui.dialogs.purchasing import discrepancy_style
        self.table.set_row_style(discrepancy_style)

    def columns(self):
        from wms.ui.dialogs.purchasing import discrepancy_columns
        return discrepancy_columns()

    def refresh(self):
        if not self.supplier.count():
            self.supplier.set_options(self.app.supplier_options(include_inactive=True))
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.purchasing.discrepancies(self.search.text(), self.status.currentData(),
                                                          self.supplier.value(), date_from=d1, date_to=d2)

    def describe(self, rows):
        return f"{len(rows)} delivery problem(s)"

    def new_problem(self):
        from wms.ui.dialogs.purchasing import NewDiscrepancyDialog
        if NewDiscrepancyDialog(self.app, self).exec():
            self.app.changed()
            self.refresh()

    def open_item(self, item):
        from wms.ui.dialogs.purchasing import DiscrepancyDialog
        DiscrepancyDialog(self.app, item.id, self).exec()
        self.refresh()
