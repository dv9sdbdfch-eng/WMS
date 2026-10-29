"""Returns from site and returns to supplier."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.enums import ReturnType
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import DATE, INT, Col, button


class ReturnsPage(ListPage):
    title = "Returns"
    subtitle = "Material coming back from site, and goods sent back to suppliers"
    search_hint = "Search return no., site, supplier, reference, reason..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.RETURNS_POST):
            self.add_action(button("Return from site", lambda: self.new(ReturnType.FROM_SITE.value), primary=True))
            self.add_action(button("Return to supplier", lambda: self.new(ReturnType.TO_SUPPLIER.value)))
        self.rtype = QComboBox()
        self.rtype.addItem("All returns", None)
        for val, text in ReturnType.choices():
            self.rtype.addItem(text, val)
        self.add_filter(self.rtype, self.rtype.currentIndexChanged)
        self.table.set_row_style(lambda r: "muted" if r.status == "REVERSED" else None)

    def columns(self):
        return [Col("Date", lambda r: r.return_date, DATE, 95),
                Col("Number", lambda r: r.return_number, width=100),
                Col("Type", lambda r: ReturnType(r.return_type).label, width=150),
                Col("From / To", lambda r: r.party_label,
                    width=260),
                Col("Reference", lambda r: r.reference or "", width=110),
                Col("Lines", lambda r: len(r.lines), INT, 60),
                Col("By", lambda r: r.user.full_name, width=130),
                Col("Status", lambda r: r.status.title(), width=90),
                Col("Reason", lambda r: r.reason or "")]

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.stock.returns(self.search.text(), self.rtype.currentData(), d1, d2)

    def describe(self, rows):
        return f"{len(rows)} return(s)"

    def new(self, rtype):
        from wms.ui.dialogs.stock_dialogs import ReturnEntryDialog
        if ReturnEntryDialog(self.app, rtype, self).exec():
            self.refresh()

    def open_item(self, item):
        self.app.open_entity("RETURN", item.id, self)
