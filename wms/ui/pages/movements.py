"""Stock Movements: the permanent ledger of every stock change."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QVBoxLayout

from wms.core.enums import EntityType, MovementType
from wms.core.numbers import fmt_datetime, fmt_money, fmt_qty
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import DATETIME, MONEY, QTY, Col, PickCombo, button, h, label

# Documents that can be opened from a movement.
_OPENABLE = {EntityType.DELIVERY_NOTE.value, EntityType.GOODS_ISSUE.value, EntityType.RETURN.value,
             EntityType.PRODUCTION.value}


def _direction(m) -> str:
    if m.from_bin_id and m.to_bin_id:
        return "Move"
    return "In" if m.to_bin_id else "Out"


class MovementDetailDialog(QDialog):
    def __init__(self, app, mv, parent=None):
        super().__init__(parent)
        self.app, self.mv = app, mv
        self.setWindowTitle(f"Stock movement #{mv.id}")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        from wms.ui.dialogs.documents import _form
        unit = mv.product.unit.code
        rows = [("Type", MovementType(mv.movement_type).label + ("  (reversal)" if mv.reversal_of_id else "")),
                ("Product", f"{mv.product.internal_code} - {mv.product.name}"),
                ("Quantity", f"{fmt_qty(mv.quantity)} {unit}  ({_direction(mv)})"),
                ("From bin", mv.from_bin.full_code if mv.from_bin else ""),
                ("To bin", mv.to_bin.full_code if mv.to_bin else ""),
                ("Site / project", mv.site.display if mv.site else ""),
                ("Supplier", mv.supplier.name if mv.supplier else ""),
                ("Unit cost", fmt_money(mv.unit_cost) if mv.unit_cost is not None else ""),
                ("Document", f"{mv.doc_number or ''}"), ("Reference", mv.reference or ""),
                ("Date of movement", fmt_datetime(mv.occurred_at)), ("Recorded at", fmt_datetime(mv.recorded_at)),
                ("User", mv.user.full_name), ("Notes", mv.notes or "")]
        if mv.is_override:
            rows.append(("Negative stock override", mv.override_reason or "Yes"))
        lay.addLayout(_form(rows))
        lay.addWidget(label("Movements cannot be edited or deleted. To correct a mistake, reverse the document "
                            "it belongs to or post an adjustment.", "Muted", wrap=True))
        btns = []
        if mv.doc_type in _OPENABLE and mv.doc_id:
            btns.append(button("Open document", lambda: app.open_entity(mv.doc_type, mv.doc_id, self)))
        btns.append(button("Open product", lambda: app.open_entity("PRODUCT", mv.product_id, self)))
        lay.addLayout(h(*btns, None, button("Close", self.accept)))


class MovementsPage(ListPage):
    title = "Stock Movements"
    subtitle = "Every stock change, permanently recorded: who, what, when, where from and where to"
    search_hint = "Search product, document no., reference, site, bin, notes..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.STOCK_TRANSFER):
            self.add_action(button("Transfer between bins", self.transfer))
        if app.can(P.STOCK_ADJUST):
            self.add_action(button("Stock adjustment", self.adjust))
        self.mtype = QComboBox()
        self.mtype.addItem("All movement types", None)
        for val, text in MovementType.choices():
            self.mtype.addItem(text, val)
        self.add_filter(self.mtype, self.mtype.currentIndexChanged)
        self.product = PickCombo("All products")
        self.product.setMinimumWidth(220)
        self.add_filter(self.product, self.product.picked)
        self.site = PickCombo("All sites")
        self.add_filter(self.site, self.site.picked)
        self.table.set_row_style(lambda m: "muted" if m.reversal_of_id else None)

    def columns(self):
        return [Col("Date / Time", lambda m: m.occurred_at, DATETIME, 130),
                Col("Type", lambda m: MovementType(m.movement_type).label, width=150),
                Col("Code", lambda m: m.product.internal_code, width=110),
                Col("Product", lambda m: m.product.name, width=200),
                Col("Qty", lambda m: m.quantity, QTY, 80),
                Col("Unit", lambda m: m.product.unit.code, width=50),
                Col("From", lambda m: m.from_bin.full_code if m.from_bin else "", width=180),
                Col("To", lambda m: m.to_bin.full_code if m.to_bin else "", width=180),
                Col("Site", lambda m: m.site.project_number if m.site else "", width=90),
                Col("Document", lambda m: m.doc_number or "", width=110),
                Col("Ref", lambda m: m.reference or "", width=90),
                Col("Cost", lambda m: m.unit_cost, MONEY, 80),
                Col("User", lambda m: m.user.full_name, width=120),
                Col("Notes", lambda m: m.notes or "")]

    def refresh(self):
        if not self.product.count():
            self.product.set_options(self.app.product_options(include_inactive=True))
            self.site.set_options(self.app.site_options(open_only=False))
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        types = [self.mtype.currentData()] if self.mtype.currentData() else None
        return self.app.services.stock.movements(self.search.text(), types, self.product.value(), self.site.value(),
                                                 date_from=d1, date_to=d2, limit=5000)

    def describe(self, rows):
        more = "  (showing the latest 5,000 - narrow the filters to see older ones)" if len(rows) >= 5000 else ""
        return f"{len(rows)} movement(s){more}"

    def open_item(self, item):
        MovementDetailDialog(self.app, item, self).exec()

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        TransferDialog(self.app, self.product.value(), self).exec()

    def adjust(self):
        from wms.ui.dialogs.stock_dialogs import AdjustmentDialog
        AdjustmentDialog(self.app, self.product.value(), None, self).exec()
