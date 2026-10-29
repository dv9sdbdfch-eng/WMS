"""Purchase order and delivery problem screens."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import OPEN_PO_STATUSES, EntityType, POStatus
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_date, fmt_datetime, fmt_money
from wms.core.permissions import P
from wms.services.purchasing import POLineInput
from wms.ui.widgets.attachments import AttachmentPanel, FilePreview
from wms.ui.widgets.common import (
    DATE,
    MONEY,
    QTY,
    Col,
    DataTable,
    DateEdit,
    DecimalEdit,
    OptionalDateEdit,
    PickCombo,
    ask_text,
    button,
    confirm,
    h,
    info,
    label,
    run,
    show_error,
)
from wms.ui.widgets.lines import LinesEditor

PHOTO_FILTER = "Photos and documents (*.jpg *.jpeg *.png *.pdf);;All files (*)"


def discrepancy_columns() -> list[Col]:
    return [Col("No.", lambda d: d.number, width=95),
                   Col("Recorded", lambda d: d.created_at.date(), DATE, 90),
                   Col("Problem", lambda d: d.kind_label, width=130),
                   Col("Supplier", lambda d: d.supplier_label, width=160),
                   Col("PO", lambda d: d.purchase_order.po_number if d.purchase_order else "", width=95),
                   Col("Delivery Note", lambda d: d.delivery_note.dn_number if d.delivery_note else "", width=110),
                   Col("Product", lambda d: f"{d.product.internal_code} - {d.product.name}" if d.product else "",
                       width=220),
                   Col("Expected", lambda d: d.expected_qty, QTY, 75),
                   Col("Received", lambda d: d.received_qty, QTY, 75),
                   Col("Qty", lambda d: d.quantity, QTY, 65),
                   Col("Status", lambda d: d.status.title(), width=80),
                   Col("Notes", lambda d: d.notes or "")]


def discrepancy_style(d) -> str | None:
    return "low" if d.status == "OPEN" else "muted"


def discrepancy_table() -> DataTable:
    t = DataTable(discrepancy_columns())
    t.set_row_style(discrepancy_style)
    return t


class PurchaseOrderDialog(QDialog):
    """New or existing purchase order. ``on_receive(po_id)`` (from the Purchase Orders screen) opens
    Goods In with the order's outstanding lines."""

    def __init__(self, app, po_id: int | None = None, parent=None, on_receive=None):
        super().__init__(parent)
        self.app, self.po_id, self.on_receive = app, po_id, on_receive
        self._token = uuid.uuid4().hex
        self.resize(1250, 800)
        self.lay = QVBoxLayout(self)
        self._build()

    # -- layout --------------------------------------------------------------------------------
    def _build(self):
        app = self.app
        v = app.services.purchasing.get(self.po_id) if self.po_id else None
        self.view = v
        po = v.po if v else None
        status = po.status if po else POStatus.DRAFT.value
        self.editable = app.can(P.PURCHASING_EDIT) and status in (POStatus.DRAFT.value, *OPEN_PO_STATUSES)
        title = f"Purchase order {po.po_number}" if po else "New purchase order"
        self.setWindowTitle(title)
        self.lay.addWidget(label(f"{title}  -  {po.supplier_label}" if po else title, "PageTitle"))
        if po and po.status == POStatus.CANCELLED.value:
            self.lay.addWidget(label(f"CANCELLED on {fmt_datetime(po.closed_at)}: {po.close_reason}", "Banner",
                                     wrap=True))
        elif po and po.closed_short:
            self.lay.addWidget(label(f"Closed short on {fmt_datetime(po.closed_at)}: {po.close_reason}", "Banner",
                                     wrap=True))

        form = QFormLayout()
        self.supplier = PickCombo("Choose supplier...")
        self.supplier.set_options(app.supplier_options(include_inactive=po is not None))
        self.site = PickCombo("No project (stock)")
        self.site.set_options(app.site_options(open_only=po is None))
        self.order_date = DateEdit(date.today())
        self.expected = OptionalDateEdit()
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Supplier's quote or order confirmation number")
        self.notes = QLineEdit()
        if po:
            self.supplier.set_value(po.supplier_id)
            self.site.set_value(po.site_id)
            self.order_date.set_value(po.order_date)
            self.expected.set_value(po.expected_date)
            self.reference.setText(po.supplier_reference or "")
            self.notes.setText(po.notes or "")
        self.supplier.setEnabled(self.editable and status == POStatus.DRAFT.value)
        for w in (self.site, self.order_date, self.expected, self.reference, self.notes):
            w.setEnabled(self.editable)
        form.addRow("Supplier *", self.supplier)
        form.addRow("For project", self.site)
        form.addRow("Order date *", self.order_date)
        form.addRow("Expected delivery", self.expected)
        form.addRow("Supplier reference", self.reference)
        form.addRow("Notes", self.notes)
        if po:
            form.addRow(label("Status", "Muted"), label(POStatus(po.status).label + (
                f"   ·   value {fmt_money(v.value)}   ·   created by {po.created_by.full_name} "
                f"{fmt_datetime(po.created_at)}")))
        top = QWidget()
        top.setLayout(form)
        self.lay.addWidget(top)

        tabs = QTabWidget()
        if self.editable:
            self.lines = LinesEditor(app, show_bin=False, show_cost=True)
            for lv in v.lines if v else []:
                self.lines.add_row(lv.line.product_id, lv.ordered)
                r = self.lines.table.rowCount() - 1
                cost = self.lines.table.cellWidget(r, self.lines._col("Unit Cost"))
                cost.set_value(lv.line.unit_cost)
                self.lines.table.cellWidget(r, self.lines._col("Notes")).setText(lv.line.notes or "")
            if not v or not v.lines:
                self.lines.add_row()
            tabs.addTab(self.lines, "Products ordered")
        if v:
            prog = DataTable([Col("#", lambda lv: lv.line.line_no, width=40),
                              Col("Code", lambda lv: lv.line.product.internal_code, width=110),
                              Col("Product", lambda lv: lv.line.product.name, width=240),
                              Col("Ordered", lambda lv: lv.ordered, QTY, 85),
                              Col("Received", lambda lv: lv.received, QTY, 85),
                              Col("Outstanding", lambda lv: lv.outstanding, QTY, 110),
                              Col("Unit", lambda lv: lv.line.product.unit.code, width=55),
                              Col("Unit cost", lambda lv: lv.line.unit_cost, MONEY, 85),
                              Col("Notes", lambda lv: lv.line.notes or "")])
            prog.set_row_style(lambda lv: "muted" if lv.outstanding == 0 else None)
            prog.set_items(v.lines)
            prog.activated_item.connect(lambda lv: app.open_entity("PRODUCT", lv.line.product_id, self))
            tabs.addTab(prog, "Received / outstanding")
            dns = DataTable([Col("Date", lambda d: d.delivery_date, DATE, 90),
                             Col("Delivery Note", lambda d: d.dn_number, width=140),
                             Col("Lines", lambda d: len(d.lines), width=60),
                             Col("Status", lambda d: d.status.title(), width=90),
                             Col("Notes", lambda d: d.notes or "")])
            dns.set_items(v.deliveries)
            dns.activated_item.connect(lambda d: app.open_entity("DELIVERY_NOTE", d.id, self))
            tabs.addTab(dns, f"Deliveries ({len(v.deliveries)})")
            probs = discrepancy_table()
            probs.set_items(v.discrepancies)
            probs.activated_item.connect(lambda d: self._open_problem(d.id))
            tabs.addTab(probs, f"Delivery problems ({len(v.discrepancies)})")
            docs = AttachmentPanel(app, EntityType.PURCHASE_ORDER.value, None, thumbs=False)
            docs.set_entity(po.id)
            tabs.addTab(docs, "Documents")
            tabs.setCurrentIndex(1 if self.editable else 0)
        self.lay.addWidget(tabs, 1)

        btns = []
        if self.editable:
            btns.append(button("Save", self.save, primary=True, shortcut="Ctrl+S"))
        if po and app.can(P.PURCHASING_EDIT):
            if po.status == POStatus.DRAFT.value:
                btns.append(button("Mark as ordered", self.mark_ordered, primary=True))
            if po.status in (POStatus.DRAFT.value, POStatus.ORDERED.value) and not any(lv.received for lv in v.lines):
                btns.append(button("Cancel order...", self.cancel_order, danger=True))
            if po.status == POStatus.PARTIALLY_RECEIVED.value:
                btns.append(button("Close short...", self.close_short))
            if po.closed_short:
                btns.append(button("Reopen", self.reopen))
        if po and po.status in OPEN_PO_STATUSES and self.on_receive and app.can(P.GOODS_IN_POST):
            btns.append(button("Receive delivery...", self.receive, primary=True))
        if po and (app.can(P.GOODS_IN_POST) or app.can(P.PURCHASING_EDIT)):
            btns.append(button("Report problem...", self.report_problem))
        if po:
            btns.append(button("Print", self.print_order))
        self.lay.addLayout(h(*btns, None, button("Close", self.reject)))

    def _rebuild(self):
        while self.lay.count():
            item = self.lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear_layout(item.layout())
        self._build()

    # -- actions -------------------------------------------------------------------------------
    def _values(self):
        try:
            lines = [POLineInput(v.product_id, v.quantity, v.unit_cost, v.notes) for v in self.lines.values()]
            expected = self.expected.value()
        except ValidationError as e:
            show_error(self, e)
            return None
        header = {"supplier_id": self.supplier.value(), "site_id": self.site.value(),
                  "order_date": self.order_date.value(), "expected_date": expected,
                  "supplier_reference": self.reference.text(), "notes": self.notes.text(),
                  "client_token": self._token}
        return header, lines

    def save(self) -> bool:
        vals = self._values()
        if vals is None:
            return False
        po = run(self, self.app.services.purchasing.save, self.po_id, *vals)
        if po is None:
            return False
        self.po_id = po.id
        self.app.changed()
        self._rebuild()
        return True

    def mark_ordered(self):
        if self.editable and not self.save():
            return
        if confirm(self, "Place order", "Mark this order as sent to the supplier? Goods can then be received "
                                        "against it in Goods In.") and run(
                self, self.app.services.purchasing.mark_ordered, self.po_id):
            self.app.changed()
            self._rebuild()

    def cancel_order(self):
        reason = ask_text(self, "Cancel order", "Why is the order being cancelled?")
        if reason and run(self, self.app.services.purchasing.cancel, self.po_id, reason):
            self.app.changed()
            self._rebuild()

    def close_short(self):
        if not confirm(self, "Close short", "Stop waiting for the rest of this order? The quantities still "
                                            "outstanding are recorded as Missing delivery problems so they can "
                                            "be chased with the supplier."):
            return
        reason = ask_text(self, "Close short", "Why will the rest not arrive?")
        if reason and run(self, self.app.services.purchasing.close_short, self.po_id, reason):
            self.app.changed()
            self._rebuild()

    def reopen(self):
        if run(self, self.app.services.purchasing.reopen, self.po_id):
            self.app.changed()
            self._rebuild()

    def receive(self):
        po_id = self.po_id
        self.accept()
        self.on_receive(po_id)

    def report_problem(self):
        if NewDiscrepancyDialog(self.app, self, purchase_order_id=self.po_id).exec():
            self.app.changed()
            self._rebuild()

    def _open_problem(self, discrepancy_id: int):
        DiscrepancyDialog(self.app, discrepancy_id, self).exec()
        self.app.changed()
        self._rebuild()

    def print_order(self):
        from wms.services.reports import MONEY as RM
        from wms.services.reports import QTY as RQ
        from wms.services.reports import Column, ReportResult
        from wms.ui.dialogs.documents import _print
        v = self.app.services.purchasing.get(self.po_id)
        po = v.po
        res = ReportResult(f"Purchase Order {po.po_number}",
                           [Column("Code"), Column("Supplier code"), Column("Product"), Column("Quantity", RQ),
                            Column("Unit"), Column("Unit cost", RM), Column("Received", RQ)],
                           [[lv.line.product.internal_code, lv.line.product.supplier_code or "", lv.line.product.name,
                             lv.ordered, lv.line.product.unit.code, lv.line.unit_cost, lv.received]
                            for lv in v.lines],
                           f"Supplier: {po.supplier_label}; Order date: {fmt_date(po.order_date)}; "
                           f"Expected: {fmt_date(po.expected_date) if po.expected_date else '-'}; "
                           f"Project: {po.site_label or '-'}; Value: {fmt_money(v.value)}")
        _print(self, res)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())


class NewDiscrepancyDialog(QDialog):
    """Report a delivery problem (short, over, damaged, rejected, wrong goods) with photos."""

    def __init__(self, app, parent=None, purchase_order_id: int | None = None, delivery_note_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex
        self.setWindowTitle("Report a delivery problem")
        self.resize(900, 600)
        self.po_id, self.dn_id = purchase_order_id, delivery_note_id
        lay = QVBoxLayout(self)
        split = QSplitter()
        left = QWidget()
        form = QFormLayout(left)
        products = None
        if delivery_note_id:
            dn = app.services.goods_in.get(delivery_note_id)
            self.po_id = self.po_id or dn.purchase_order_id
            form.addRow(label("Delivery note", "Muted"), label(f"{dn.dn_number} - {dn.supplier_label}"))
            products = [(f"{l.product_code} - {l.product_name}", l.product_id) for l in dn.lines]
        if self.po_id:
            v = app.services.purchasing.get(self.po_id)
            form.addRow(label("Purchase order", "Muted"), label(f"{v.po.po_number} - {v.po.supplier_label}"))
            if products is None:
                products = [(f"{lv.line.product.internal_code} - {lv.line.product.name}", lv.line.product_id)
                            for lv in v.lines]
        self.supplier = PickCombo("Choose supplier...")
        if not (self.po_id or self.dn_id):
            self.supplier.set_options(app.supplier_options())
            form.addRow("Supplier *", self.supplier)
        self.kind = QComboBox()
        self.kind.addItem("Choose...", None)
        for code, text in app.services.reasons.choices("DISCREPANCY"):
            self.kind.addItem(text, code)
        self.product = PickCombo("Whole delivery / not product specific")
        self.product.set_options(products if products is not None else app.product_options())
        self.expected, self.received, self.qty = DecimalEdit(), DecimalEdit(), DecimalEdit()
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("What is wrong? What did the driver / supplier say?")
        form.addRow("Problem *", self.kind)
        form.addRow("Product", self.product)
        form.addRow("Expected qty", self.expected)
        form.addRow("Received qty", self.received)
        form.addRow("Qty affected *", self.qty)
        form.addRow("Details", self.notes)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(label("Photos", "SectionTitle"))
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        self.files.currentRowChanged.connect(self._preview)
        rl.addWidget(self.files)
        rl.addLayout(h(button("Add photo...", self.attach), button("Remove", self.remove_file), None))
        self.preview = FilePreview()
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        lay.addWidget(split, 1)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))
        self._paths: list[str] = []

    def attach(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add photos", "", PHOTO_FILTER)
        for p in paths:
            self.add_file(p)

    def add_file(self, p: str) -> None:
        try:
            self.app.services.attachments.validate_file(p)
        except ValidationError as e:
            show_error(self, e)
            return
        if p not in self._paths:
            self._paths.append(p)
            self.files.addItem(Path(p).name)
            self.files.setCurrentRow(self.files.count() - 1)

    def remove_file(self):
        r = self.files.currentRow()
        if r >= 0:
            self._paths.pop(r)
            self.files.takeItem(r)

    def _preview(self, row: int):
        self.preview.show_file(Path(self._paths[row]) if 0 <= row < len(self._paths) else None)

    def save(self):
        try:
            data = {"purchase_order_id": self.po_id, "delivery_note_id": self.dn_id,
                    "supplier_id": self.supplier.value(), "reason_code": self.kind.currentData(),
                    "product_id": self.product.value(), "expected_qty": self.expected.value(),
                    "received_qty": self.received.value(), "quantity": self.qty.value(),
                    "notes": self.notes.toPlainText(), "client_token": self._token}
        except ValidationError as e:
            show_error(self, e)
            return
        if data["quantity"] is None:
            info(self, "Delivery problem", "Enter the quantity affected (0 if it is not about a quantity).")
            return
        d = run(self, self.app.services.purchasing.record_discrepancy, data, list(self._paths))
        if d:
            self.created = d
            info(self, "Delivery problem", f"Recorded as {d.number}. It stays on the dashboard until resolved.")
            self.accept()


class DiscrepancyDialog(QDialog):
    def __init__(self, app, discrepancy_id: int, parent=None):
        super().__init__(parent)
        self.app, self.d_id = app, discrepancy_id
        d = app.services.purchasing.get_discrepancy(discrepancy_id)
        self.setWindowTitle(f"Delivery problem {d.number}")
        self.resize(1000, 640)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{d.number}  -  {d.kind_label}  -  {d.supplier_label}", "PageTitle"))
        if d.status == "RESOLVED":
            lay.addWidget(label(f"RESOLVED {fmt_datetime(d.resolved_at)} by "
                                f"{d.resolved_by.full_name if d.resolved_by else '-'}: {d.resolution}", "Banner",
                                wrap=True))
        split = QSplitter()
        left = QWidget()
        form = QFormLayout(left)
        for k, val in (("Purchase order", d.purchase_order.po_number if d.purchase_order else ""),
                       ("Delivery note", d.delivery_note.dn_number if d.delivery_note else ""),
                       ("Product", f"{d.product.internal_code} - {d.product.name}" if d.product else ""),
                       ("Expected", _q(d.expected_qty)), ("Received", _q(d.received_qty)),
                       ("Qty affected", _q(d.quantity)),
                       ("Recorded", f"{fmt_datetime(d.created_at)} by {d.created_by.full_name}"
                        + (" (automatically)" if d.is_automatic else "")),
                       ("Details", d.notes or "")):
            form.addRow(label(k, "Muted"), label(val or "-", wrap=True))
        links = []
        if d.purchase_order_id:
            links.append(button("Open order", lambda: app.open_entity("PURCHASE_ORDER", d.purchase_order_id, self)))
        if d.delivery_note_id:
            links.append(button("Open delivery note",
                                lambda: app.open_entity("DELIVERY_NOTE", d.delivery_note_id, self)))
        form.addRow(h(*links, None))
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(label("Photos", "SectionTitle"))
        self.docs = AttachmentPanel(app, EntityType.DISCREPANCY.value, None, thumbs=False)
        self.docs.setMaximumHeight(170)
        self.docs.set_entity(d.id)
        self.preview = FilePreview()
        self.docs.list.currentRowChanged.connect(self._preview)
        rl.addWidget(self.docs)
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        lay.addWidget(split, 1)
        if self.docs.list.count():
            self.docs.list.setCurrentRow(0)
        btns = []
        if d.status == "OPEN" and app.can(P.PURCHASING_EDIT):
            btns.append(button("Resolve...", self.resolve, primary=True))
        lay.addLayout(h(*btns, None, button("Close", self.accept)))

    def _preview(self, _row: int):
        att = self.docs.current()
        self.preview.show_file(self.app.services.attachments.path_of(att) if att else None)

    def resolve(self):
        text = ask_text(self, "Resolve", "How was it resolved? (credit note, replacement delivered, accepted...)")
        if text and run(self, self.app.services.purchasing.resolve_discrepancy, self.d_id, text):
            self.app.changed()
            self.accept()


def _q(v) -> str:
    from wms.core.numbers import fmt_qty
    return fmt_qty(v) if v is not None else ""
