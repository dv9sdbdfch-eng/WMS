"""Read-only views of posted documents, with reversal and print."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFormLayout, QSplitter, QTabWidget, QVBoxLayout, QWidget

from wms.core.enums import AttachmentKind, EntityType, MovementType, ReturnCondition, ReturnType
from wms.core.numbers import fmt_date, fmt_datetime, fmt_money
from wms.core.permissions import P
from wms.ui.widgets.attachments import AttachmentPanel, FilePreview
from wms.ui.widgets.common import (
    DATETIME,
    MONEY,
    QTY,
    Col,
    DataTable,
    ask_text,
    button,
    confirm,
    h,
    info,
    label,
    run,
    run_with_override,
)


def movement_table() -> DataTable:
    return DataTable([
        Col("Date / Time", lambda m: m.occurred_at, DATETIME, 130),
        Col("Type", lambda m: MovementType(m.movement_type).label, width=150),
        Col("Code", lambda m: m.product.internal_code, width=110),
        Col("Qty", lambda m: m.quantity, QTY, 80),
        Col("From", lambda m: m.from_bin.full_code if m.from_bin else "", width=200),
        Col("To", lambda m: m.to_bin.full_code if m.to_bin else "", width=200),
        Col("User", lambda m: m.user.full_name, width=120),
        Col("Recorded", lambda m: m.recorded_at, DATETIME, 130),
        Col("Notes", lambda m: m.notes or "")])


def _form(rows: list[tuple[str, str]]) -> QFormLayout:
    f = QFormLayout()
    for k, v in rows:
        lab = label(v or "-")
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lab.setWordWrap(True)
        f.addRow(label(k, "Muted"), lab)
    return f


def _status_banner(doc) -> QWidget | None:
    if doc.status == "REVERSED":
        return label(f"REVERSED on {fmt_datetime(doc.reversed_at)}. Reason: {doc.reversal_reason}", "Banner", wrap=True)
    return None


def _print(parent, result):
    from wms.exporters.export import export_pdf
    out = Path(tempfile.gettempdir()) / f"{result.title.replace(' ', '_').replace('/', '-')}.pdf"
    try:
        export_pdf(result, out, parent.app.ctx.config.company_name)
    except PermissionError:
        info(parent, "Print", f"Close the open PDF '{out.name}' first and try again.")
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))


class DeliveryNoteDialog(QDialog):
    def __init__(self, app, dn_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        self.dn_id = dn_id
        self.resize(1300, 820)
        self.lay = QVBoxLayout(self)
        self._build()

    def _build(self):
        app = self.app
        dn = app.services.goods_in.get(self.dn_id)
        self.dn = dn
        self.setWindowTitle(f"Delivery note {dn.dn_number}")
        self.lay.addWidget(label(f"Delivery note {dn.dn_number}  -  {dn.supplier.name}", "PageTitle"))
        banner = _status_banner(dn)
        if banner:
            self.lay.addWidget(banner)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        total = sum(((l.unit_cost or 0) * l.quantity for l in dn.lines), 0)
        ll.addLayout(_form([("Supplier", dn.supplier.name), ("Delivery date", fmt_date(dn.delivery_date)),
                            ("PO / reference", dn.po_reference or ""), ("Received by", dn.received_by.full_name),
                            ("Entered", fmt_datetime(dn.created_at)), ("Status", dn.status.title()),
                            ("Total value", fmt_money(total)), ("Notes", dn.notes or "")]))
        tabs = QTabWidget()
        lines = DataTable([Col("#", lambda l: l.line_no, width=40),
                           Col("Code", lambda l: l.product.internal_code, width=110),
                           Col("Product", lambda l: l.product.name, width=220),
                           Col("Qty received", lambda l: l.quantity, QTY, 100),
                           Col("Unit", lambda l: l.product.unit.code, width=55),
                           Col("Bin", lambda l: l.bin.full_code, width=200),
                           Col("Unit cost", lambda l: l.unit_cost, MONEY, 85),
                           Col("Notes", lambda l: l.notes or "")])
        lines.set_items(dn.lines)
        lines.activated_item.connect(lambda l: app.open_entity("PRODUCT", l.product_id, self))
        tabs.addTab(lines, f"Products received ({len(dn.lines)})")
        mv = movement_table()
        mv.set_items(app.services.goods_in.movements(dn.id))
        tabs.addTab(mv, "Stock transactions")
        ll.addWidget(tabs, 1)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(label("Original delivery note", "SectionTitle"))
        self.docs = AttachmentPanel(app, EntityType.DELIVERY_NOTE.value, None, thumbs=False)
        self.docs.setMaximumHeight(170)
        self.docs.set_entity(dn.id)
        self.preview = FilePreview()
        self.docs.list.currentRowChanged.connect(self._preview)
        rl.addWidget(self.docs)
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setSizes([700, 600])
        self.lay.addWidget(split, 1)
        if self.docs.list.count():
            self.docs.list.setCurrentRow(0)
        else:
            self.preview.empty.setText("No scan attached. Use 'Attach file...' to add the original delivery note.")

        buttons = [button("Print", self.print_note)]
        if dn.status == "POSTED" and app.can(P.GOODS_IN_POST):
            buttons.append(button("Edit PO / notes...", self.edit_header))
        if dn.status == "POSTED" and app.can(P.GOODS_IN_REVERSE):
            buttons.append(button("Reverse delivery note...", self.reverse, danger=True))
        self.lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def edit_header(self):
        from PySide6.QtWidgets import QLineEdit
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit delivery note {self.dn.dn_number}")
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        po, notes = QLineEdit(self.dn.po_reference or ""), QLineEdit(self.dn.notes or "")
        form = QFormLayout()
        v.addLayout(form)
        form.addRow("PO / reference", po)
        form.addRow("Notes", notes)
        v.addWidget(label("Supplier, number, date and lines cannot be changed after posting - reverse the note and "
                          "enter it again instead. This change is recorded in the audit trail.", "Muted", wrap=True))
        v.addLayout(h(None, button("Cancel", dlg.reject), button("Save", dlg.accept, primary=True)))
        if dlg.exec() == QDialog.DialogCode.Accepted and run(
                self, self.app.services.goods_in.update_header, self.dn.id, po.text(), notes.text()):
            self.app.changed()
            self.accept()

    def _preview(self, row: int):
        att = self.docs.current()
        self.preview.show_file(self.app.services.attachments.path_of(att) if att else None)

    def print_note(self):
        from wms.services.reports import QTY as RQ
        from wms.services.reports import Column, ReportResult
        dn = self.dn
        res = ReportResult(f"Delivery Note {dn.dn_number}", [Column("Code"), Column("Product"), Column("Quantity", RQ),
                                                             Column("Unit"), Column("Bin")],
                           [[l.product.internal_code, l.product.name, l.quantity, l.product.unit.code,
                             l.bin.full_code] for l in dn.lines],
                           f"Supplier: {dn.supplier.name}; Date: {fmt_date(dn.delivery_date)}; "
                           f"Received by: {dn.received_by.full_name}")
        _print(self, res)

    def reverse(self):
        if not confirm(self, "Reverse delivery note",
                       f"Reverse delivery note {self.dn.dn_number}?\n\nAll {len(self.dn.lines)} line(s) will be taken "
                       f"back out of stock. The note stays on record marked REVERSED.", yes="Reverse", danger=True):
            return
        reason = ask_text(self, "Reason", "Why is this delivery note being reversed?")
        if not reason:
            return
        if run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE), self.app.services.goods_in.reverse,
                             dn_id=self.dn.id, reason=reason):
            self.app.changed()
            self.accept()


class GoodsIssueDialog(QDialog):
    def __init__(self, app, issue_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        gi = self.gi = app.services.goods_out.get(issue_id)
        self.setWindowTitle(f"Goods Out {gi.issue_number}")
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"Goods Out {gi.issue_number}  -  {gi.site.display}", "PageTitle"))
        banner = _status_banner(gi)
        if banner:
            lay.addWidget(banner)
        lay.addLayout(_form([("Date", fmt_date(gi.issue_date)), ("Site / project", gi.site.display),
                             ("Reference / job no.", gi.reference or ""), ("Requested by", gi.requested_by or ""),
                             ("Issued by", gi.issued_by), ("Entered by", f"{gi.user.full_name}, "
                                                                         f"{fmt_datetime(gi.created_at)}"),
                             ("Notes", gi.notes or "")]))
        t = DataTable([Col("#", lambda l: l.line_no, width=40), Col("Code", lambda l: l.product.internal_code, width=110),
                       Col("Product", lambda l: l.product.name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("From bin", lambda l: l.bin.full_code, width=220),
                       Col("Value", lambda l: (l.unit_cost or 0) * l.quantity, MONEY, 90),
                       Col("Notes", lambda l: l.notes or "")])
        t.set_items(gi.lines)
        lay.addWidget(t, 1)
        buttons = [button("Print issue note", self.print_note)]
        if gi.status == "POSTED" and app.can(P.GOODS_OUT_REVERSE):
            buttons.append(button("Reverse (posted in error)...", self.reverse, danger=True))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def print_note(self):
        from wms.services.reports import QTY as RQ
        from wms.services.reports import Column, ReportResult
        gi = self.gi
        res = ReportResult(f"Goods Out {gi.issue_number}", [Column("Code"), Column("Product"), Column("Quantity", RQ),
                                                            Column("Unit"), Column("From Bin"), Column("Notes")],
                           [[l.product.internal_code, l.product.name, l.quantity, l.product.unit.code,
                             l.bin.full_code, l.notes or ""] for l in gi.lines],
                           f"Site: {gi.site.display}; Date: {fmt_date(gi.issue_date)}; Ref: {gi.reference or '-'}; "
                           f"Requested by: {gi.requested_by or '-'}; Issued by: {gi.issued_by}")
        _print(self, res)

    def reverse(self):
        if not confirm(self, "Reverse Goods Out",
                       "Use this only if the Goods Out was entered by mistake and the material never left.\n"
                       "If material came back from site, record a Return instead.\n\nReverse it?",
                       yes="Reverse", danger=True):
            return
        reason = ask_text(self, "Reason", "Why is this Goods Out being reversed?")
        if reason and run(self, self.app.services.goods_out.reverse, self.gi.id, reason):
            self.app.changed()
            self.accept()


class ReturnDialog(QDialog):
    def __init__(self, app, return_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        r = self.r = app.services.stock.get_return(return_id)
        self.setWindowTitle(f"Return {r.return_number}")
        self.resize(1000, 600)
        lay = QVBoxLayout(self)
        party = r.site.display if r.site else (r.supplier.name if r.supplier else "")
        lay.addWidget(label(f"{ReturnType(r.return_type).label} {r.return_number}  -  {party}", "PageTitle"))
        banner = _status_banner(r)
        if banner:
            lay.addWidget(banner)
        lay.addLayout(_form([("Date", fmt_date(r.return_date)), ("Reference", r.reference or ""),
                             ("Reason", r.reason or ""), ("Entered by", f"{r.user.full_name}, "
                                                                        f"{fmt_datetime(r.created_at)}")]))
        t = DataTable([Col("Code", lambda l: l.product.internal_code, width=110),
                       Col("Product", lambda l: l.product.name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("Condition", lambda l: ReturnCondition(l.condition).label, width=90),
                       Col("Bin", lambda l: l.bin.full_code, width=220), Col("Notes", lambda l: l.notes or "")])
        t.set_items(r.lines)
        lay.addWidget(t, 1)
        buttons = []
        if r.status == "POSTED" and app.can(P.RETURNS_POST) and app.can(P.STOCK_ADJUST):
            buttons.append(button("Reverse return...", self.reverse, danger=True))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def reverse(self):
        reason = ask_text(self, "Reverse return", "Why is this return being reversed?")
        if reason and run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE),
                                        self.app.services.stock.reverse_return, return_id=self.r.id, reason=reason):
            self.app.changed()
            self.accept()


class ProductionDialog(QDialog):
    def __init__(self, app, order_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        o = self.o = app.services.production.get(order_id)
        self.setWindowTitle(f"Production {o.order_number}")
        self.resize(950, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{o.order_number}  -  {o.finished_product.internal_code} {o.finished_product.name}",
                            "PageTitle"))
        banner = _status_banner(o)
        if banner:
            lay.addWidget(banner)
        from wms.core.numbers import fmt_qty
        lay.addLayout(_form([("Quantity produced", f"{fmt_qty(o.quantity)} {o.finished_product.unit.code}"),
                             ("Stored in", o.output_bin.full_code), ("Production date", fmt_date(o.production_date)),
                             ("Site / project", o.site.display if o.site else ""), ("Notes", o.notes or ""),
                             ("Entered by", f"{o.user.full_name}, {fmt_datetime(o.created_at)}")]))
        lay.addWidget(label("Materials consumed", "SectionTitle"))
        t = DataTable([Col("Code", lambda l: l.product.internal_code, width=110),
                       Col("Material", lambda l: l.product.name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("From bin", lambda l: l.bin.full_code)])
        t.set_items(o.lines)
        lay.addWidget(t, 1)
        buttons = []
        if o.status == "POSTED" and app.can(P.PRODUCTION_POST) and app.can(P.STOCK_ADJUST):
            buttons.append(button("Reverse production...", self.reverse, danger=True))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def reverse(self):
        reason = ask_text(self, "Reverse production", "Why is this production record being reversed?")
        if reason and run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE),
                                        self.app.services.production.reverse, order_id=self.o.id, reason=reason):
            self.app.changed()
            self.accept()


class BinContentsDialog(QDialog):
    def __init__(self, app, bin_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        self.bin_id = bin_id
        b = next((x for x in app.services.locations.bins(include_inactive=True) if x.id == bin_id), None)
        self.setWindowTitle(f"Bin {b.full_code if b else ''}")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"Bin {b.full_code}" if b else "Bin", "PageTitle"))
        if b and not b.is_available:
            lay.addWidget(label("Quarantine bin - stock here cannot be issued.", "Banner"))
        self.table = DataTable([Col("Code", lambda l: l.product.internal_code, width=120),
                                Col("Product", lambda l: l.product.name, width=320),
                                Col("Quantity", lambda l: l.quantity, QTY, 100),
                                Col("Unit", lambda l: l.product.unit.code)])
        self.table.activated_item.connect(lambda l: app.open_entity("PRODUCT", l.product_id, self))
        lay.addWidget(self.table, 1)
        buttons = []
        if app.can(P.STOCK_TRANSFER):
            buttons.append(button("Transfer selected...", self.transfer))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))
        self.refresh()

    def refresh(self):
        self.table.set_items(self.app.services.locations.bin_contents(self.bin_id))

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        lvl = self.table.current()
        if TransferDialog(self.app, lvl.product_id if lvl else None, self, from_bin_id=self.bin_id).exec():
            self.refresh()


__all__ = ["DeliveryNoteDialog", "GoodsIssueDialog", "ReturnDialog", "ProductionDialog", "BinContentsDialog",
           "AttachmentKind"]
