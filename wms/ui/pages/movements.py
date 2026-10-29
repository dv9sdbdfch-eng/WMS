"""Stock Movements: the permanent ledger of every stock change."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QVBoxLayout

from wms.core.enums import EntityType, MovementType
from wms.core.numbers import fmt_datetime, fmt_money, fmt_qty
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import (
    DATETIME,
    MONEY,
    QTY,
    Col,
    PickCombo,
    ask_reason,
    button,
    h,
    label,
    run_with_override,
)

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
        rows = [("Type", mv.type_label),
                ("Product", f"{mv.product_code} - {mv.product_name}"),
                ("Quantity", f"{fmt_qty(mv.quantity)} {unit}  ({_direction(mv)})"),
                ("From bin", mv.from_bin_code),
                ("To bin", mv.to_bin_code),
                ("Site / project", mv.site_label),
                ("Supplier", mv.supplier_label),
                ("Unit cost", fmt_money(mv.unit_cost) if mv.unit_cost is not None else ""),
                ("Document", f"{mv.doc_number or ''}"), ("Reference", mv.reference or ""),
                ("Date of movement", fmt_datetime(mv.occurred_at)), ("Recorded at", fmt_datetime(mv.recorded_at)),
                ("User", mv.user_label), ("Notes", mv.notes or "")]
        if mv.is_override:
            rows.append(("Negative stock override", mv.override_reason or "Yes"))
        rec = app.services.ledger.reversal_of(mv.id)
        if rec is not None:
            other = rec.original_movement_id if rec.reversal_movement_id == mv.id else rec.reversal_movement_id
            rows += [("Reversal", f"{'Reverses' if rec.reversal_movement_id == mv.id else 'Reversed by'} "
                                  f"movement #{other}"),
                     ("Reversal reason", f"{app.services.reasons.label(rec.reason_code)}: {rec.reason}"),
                     ("Reversed by", f"{rec.reversed_by_name_snapshot or rec.reversed_by.full_name}, "
                                     f"{fmt_datetime(rec.reversed_at)}")]
        lay.addLayout(_form(rows))
        lay.addWidget(label("Movements cannot be edited or deleted. To correct a mistake, reverse it: transfers and "
                            "adjustments here, everything else on the document it belongs to.", "Muted", wrap=True))
        btns = []
        if rec is None and app.can(P.STOCK_ADJUST) and app.services.ledger.can_reverse_alone(mv):
            btns.append(button("Reverse...", self.reverse, danger=True))
        if rec is not None:
            btns.append(button(f"Open movement #{other}", lambda: self._open(other)))
        if mv.doc_type in _OPENABLE and mv.doc_id:
            btns.append(button("Open document", lambda: app.open_entity(mv.doc_type, mv.doc_id, self)))
        btns.append(button("Open product", lambda: app.open_entity("PRODUCT", mv.product_id, self)))
        lay.addLayout(h(*btns, None, button("Close", self.accept)))

    def _open(self, movement_id: int):
        MovementDetailDialog(self.app, self.app.services.stock.get_movement(movement_id), self).exec()

    def reverse(self):
        picked = ask_reason(self, "Reverse movement",
                            f"Reverse this {self.mv.type_label.lower()} of {fmt_qty(self.mv.quantity)} "
                            f"{self.mv.product_code}? A mirror movement is posted and both stay in the history.",
                            self.app.services.reasons.choices("REVERSAL"))
        if picked and run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE),
                                        self.app.services.ledger.reverse_movement, movement_id=self.mv.id,
                                        reason_code=picked[0], reason=picked[1]):
            self.app.changed()
            self.accept()


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
                Col("Type", lambda m: m.type_label, width=170),
                Col("Code", lambda m: m.product_code, width=110),
                Col("Product", lambda m: m.product_name, width=200),
                Col("Qty", lambda m: m.quantity, QTY, 80),
                Col("Unit", lambda m: m.product.unit.code, width=50),
                Col("From", lambda m: m.from_bin_code, width=180),
                Col("To", lambda m: m.to_bin_code, width=180),
                Col("Site", lambda m: m.site_number, width=90),
                Col("Document", lambda m: m.doc_number or "", width=110),
                Col("Ref", lambda m: m.reference or "", width=90),
                Col("Cost", lambda m: m.unit_cost, MONEY, 80),
                Col("User", lambda m: m.user_label, width=120),
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
