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
    ask_reason,
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
        Col("Code", lambda m: m.product_code, width=110),
        Col("Qty", lambda m: m.quantity, QTY, 80),
        Col("From", lambda m: m.from_bin_code, width=200),
        Col("To", lambda m: m.to_bin_code, width=200),
        Col("User", lambda m: m.user_label, width=120),
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



def _ask_reversal_reason(dlg, prompt: str) -> tuple[str, str] | None:
    """Reason code + explanation for a reversal; stored with the reversal record."""
    return ask_reason(dlg, "Reverse", prompt, dlg.app.services.reasons.choices("REVERSAL"))

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
        self.lay.addWidget(label(f"Delivery note {dn.dn_number}  -  {dn.supplier_label}", "PageTitle"))
        banner = _status_banner(dn)
        if banner:
            self.lay.addWidget(banner)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        total = sum(((l.unit_cost or 0) * l.quantity for l in dn.lines), 0)
        ll.addLayout(_form([("Supplier", dn.supplier_label), ("Delivery date", fmt_date(dn.delivery_date)),
                            ("PO / reference", dn.po_reference or ""), ("Received by", dn.received_by.full_name),
                            ("Entered", fmt_datetime(dn.created_at)), ("Status", dn.status.title()),
                            ("Total value", fmt_money(total)), ("Notes", dn.notes or "")]))
        tabs = QTabWidget()
        lines = DataTable([Col("#", lambda l: l.line_no, width=40),
                           Col("Code", lambda l: l.product_code, width=110),
                           Col("Product", lambda l: l.product_name, width=220),
                           Col("Qty received", lambda l: l.quantity, QTY, 100),
                           Col("Unit", lambda l: l.product.unit.code, width=55),
                           Col("Bin", lambda l: l.bin_code, width=200),
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
                           [[l.product_code, l.product_name, l.quantity, l.product.unit.code,
                             l.bin_code] for l in dn.lines],
                           f"Supplier: {dn.supplier_label}; Date: {fmt_date(dn.delivery_date)}; "
                           f"Received by: {dn.received_by.full_name}")
        _print(self, res)

    def reverse(self):
        if not confirm(self, "Reverse delivery note",
                       f"Reverse delivery note {self.dn.dn_number}?\n\nAll {len(self.dn.lines)} line(s) will be taken "
                       f"back out of stock. The note stays on record marked REVERSED.", yes="Reverse", danger=True):
            return
        picked = _ask_reversal_reason(self, "Why is this delivery note being reversed?")
        if not picked:
            return
        if run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE), self.app.services.goods_in.reverse,
                             dn_id=self.dn.id, reason_code=picked[0], reason=picked[1]):
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
        lay.addWidget(label(f"Goods Out {gi.issue_number}  -  {gi.site_label}", "PageTitle"))
        banner = _status_banner(gi)
        if banner:
            lay.addWidget(banner)
        lay.addLayout(_form([("Date", fmt_date(gi.issue_date)), ("Site / project", gi.site_label),
                             ("Reference / job no.", gi.reference or ""), ("Requested by", gi.requested_by or ""),
                             ("Issued by", gi.issued_by), ("Entered by", f"{gi.user.full_name}, "
                                                                         f"{fmt_datetime(gi.created_at)}"),
                             ("Notes", gi.notes or "")]))
        t = DataTable([Col("#", lambda l: l.line_no, width=40), Col("Code", lambda l: l.product_code, width=110),
                       Col("Product", lambda l: l.product_name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("From bin", lambda l: l.bin_code, width=220),
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
                           [[l.product_code, l.product_name, l.quantity, l.product.unit.code,
                             l.bin_code, l.notes or ""] for l in gi.lines],
                           f"Site: {gi.site_label}; Date: {fmt_date(gi.issue_date)}; Ref: {gi.reference or '-'}; "
                           f"Requested by: {gi.requested_by or '-'}; Issued by: {gi.issued_by}")
        _print(self, res)

    def reverse(self):
        if not confirm(self, "Reverse Goods Out",
                       "Use this only if the Goods Out was entered by mistake and the material never left.\n"
                       "If material came back from site, record a Return instead.\n\nReverse it?",
                       yes="Reverse", danger=True):
            return
        picked = _ask_reversal_reason(self, "Why is this Goods Out being reversed?")
        if picked and run(self, self.app.services.goods_out.reverse, self.gi.id, picked[1], reason_code=picked[0]):
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
        party = r.party_label
        lay.addWidget(label(f"{ReturnType(r.return_type).label} {r.return_number}  -  {party}", "PageTitle"))
        banner = _status_banner(r)
        if banner:
            lay.addWidget(banner)
        lay.addLayout(_form([("Date", fmt_date(r.return_date)), ("Reference", r.reference or ""),
                             ("Reason", r.reason or ""), ("Entered by", f"{r.user.full_name}, "
                                                                        f"{fmt_datetime(r.created_at)}")]))
        t = DataTable([Col("Code", lambda l: l.product_code, width=110),
                       Col("Product", lambda l: l.product_name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("Condition", lambda l: ReturnCondition(l.condition).label, width=90),
                       Col("Bin", lambda l: l.bin_code, width=220), Col("Notes", lambda l: l.notes or "")])
        t.set_items(r.lines)
        lay.addWidget(t, 1)
        buttons = []
        if r.status == "POSTED" and app.can(P.RETURNS_POST) and app.can(P.STOCK_ADJUST):
            buttons.append(button("Reverse return...", self.reverse, danger=True))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def reverse(self):
        picked = _ask_reversal_reason(self, "Why is this return being reversed?")
        if picked and run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE),
                                        self.app.services.stock.reverse_return, return_id=self.r.id,
                                        reason_code=picked[0], reason=picked[1]):
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
        lay.addWidget(label(f"{o.order_number}  -  {o.product_code} {o.product_name}",
                            "PageTitle"))
        banner = _status_banner(o)
        if banner:
            lay.addWidget(banner)
        from wms.core.numbers import fmt_qty
        lay.addLayout(_form([("Quantity produced", f"{fmt_qty(o.quantity)} {o.finished_product.unit.code}"),
                             ("Stored in", o.output_bin_code), ("Production date", fmt_date(o.production_date)),
                             ("Site / project", o.site_label), ("Notes", o.notes or ""),
                             ("Entered by", f"{o.user.full_name}, {fmt_datetime(o.created_at)}")]))
        lay.addWidget(label("Materials consumed", "SectionTitle"))
        t = DataTable([Col("Code", lambda l: l.product_code, width=110),
                       Col("Material", lambda l: l.product_name, width=260), Col("Qty", lambda l: l.quantity, QTY, 90),
                       Col("Unit", lambda l: l.product.unit.code, width=55),
                       Col("From bin", lambda l: l.bin_code)])
        t.set_items(o.lines)
        lay.addWidget(t, 1)
        buttons = []
        if o.status == "POSTED" and app.can(P.PRODUCTION_POST) and app.can(P.STOCK_ADJUST):
            buttons.append(button("Reverse production...", self.reverse, danger=True))
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))

    def reverse(self):
        picked = _ask_reversal_reason(self, "Why is this production record being reversed?")
        if picked and run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE),
                                        self.app.services.production.reverse, order_id=self.o.id,
                                        reason_code=picked[0], reason=picked[1]):
            self.app.changed()
            self.accept()


class BinContentsDialog(QDialog):
    """BIN B04: where it is, what is in it, and what recently went in and out."""

    def __init__(self, app, bin_id: int, parent=None):
        super().__init__(parent)
        self.app = app
        self.bin_id = bin_id
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        self.title = label("", "PageTitle")
        self.where = label("", "SectionTitle")
        self.summary = label("", "Muted")
        lay.addWidget(self.title)
        lay.addWidget(self.where)
        lay.addWidget(self.summary)
        self.banner = label("Quarantine bin - stock here cannot be issued.", "Banner")
        self.banner.hide()
        lay.addWidget(self.banner)
        self.archived = label("", "Banner", wrap=True)
        self.archived.hide()
        lay.addWidget(self.archived)
        tabs = QTabWidget()
        self.table = DataTable([Col("Code", lambda l: l.product.internal_code, width=120),
                                Col("Product", lambda l: l.product.name, width=340),
                                Col("Quantity", lambda l: l.quantity, QTY, 100),
                                Col("Unit", lambda l: l.product.unit.code, width=60),
                                Col("Supplier", lambda l: l.product.supplier.name if l.product.supplier else "")])
        self.table.activated_item.connect(lambda l: app.open_entity("PRODUCT", l.product_id, self))
        tabs.addTab(self.table, "Contents")
        self.moved_in = DataTable(_bin_movement_columns())
        self.moved_out = DataTable(_bin_movement_columns())
        for t in (self.moved_in, self.moved_out):
            t.activated_item.connect(self._open_movement)
        tabs.addTab(self.moved_in, "Recent movements into the bin")
        tabs.addTab(self.moved_out, "Recent movements out of the bin")
        self.history = DataTable(_bin_history_columns())
        self.history.activated_item.connect(self._open_movement)
        self.history_index = tabs.addTab(self.history, "Full history")
        self.tabs = tabs
        lay.addWidget(tabs, 1)
        buttons = [button("View product", lambda: self._open_product())]
        if app.can(P.STOCK_TRANSFER):
            self.transfer_btn = button("Transfer selected...", self.transfer)
            buttons.append(self.transfer_btn)
        if app.can(P.LOCATIONS_EDIT):
            self.delete_btn = button("Delete bin", self.delete_bin, danger=True)
            self.restore_btn = button("Restore bin", self.restore_bin)
            buttons += [self.delete_btn, self.restore_btn]
        lay.addLayout(h(*buttons, None, button("Close", self.accept)))
        self.refresh()

    def refresh(self):
        v = self.app.services.search.bin_view(self.bin_id)
        b = v.bin
        self.setWindowTitle(f"Bin {b.code}")
        self.title.setText(f"BIN {b.code}" + ("" if b.is_active else "  —  Archived"))
        self.where.setText(f"Location: {v.path_text}")
        from wms.core.numbers import fmt_qty
        self.summary.setText(f"Current stock: {fmt_qty(v.current_stock)}   ·   "
                             f"{v.product_count} different product(s)   ·   "
                             f"Historical movements: {v.history_count:,}   ·   full code {b.full_code}"
                             + (f"   ·   {b.description}" if b.description else ""))
        self.banner.setVisible(b.is_active and not b.is_available)
        if v.is_archived:
            when = f" on {fmt_datetime(v.removed_on)} by {v.removed_by}" if v.removed_on else ""
            why = f" Reason: {v.removed_reason}" if v.removed_reason else ""
            self.archived.setText(f"This bin was removed from active warehouse operations{when}. It cannot "
                                  f"receive stock or be chosen for transfers. Its history is kept below.{why}")
        self.archived.setVisible(v.is_archived)
        if hasattr(self, "delete_btn"):
            self.delete_btn.setVisible(b.is_active)
            self.restore_btn.setVisible(not b.is_active)
        if hasattr(self, "transfer_btn"):
            self.transfer_btn.setVisible(b.is_active)
        self.table.set_items(v.contents)
        self.moved_in.set_items(v.recent_in)
        self.moved_out.set_items(v.recent_out)
        self.history.set_items(v.history)
        if v.is_archived:
            self.tabs.setCurrentIndex(self.history_index)

    def _open_product(self):
        lvl = self.table.current()
        if lvl is not None:
            self.app.open_entity("PRODUCT", lvl.product_id, self)

    def _open_movement(self, m):
        from wms.ui.dialogs.product_dialog import open_movement
        open_movement(self.app, m, self)

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        lvl = self.table.current()
        if TransferDialog(self.app, lvl.product_id if lvl else None, self, from_bin_id=self.bin_id).exec():
            self.app.changed()
            self.refresh()

    def delete_bin(self):
        from wms.core.exceptions import NotFoundError
        from wms.ui.dialogs.removal import delete_record
        loc = self.app.services.locations
        if delete_record(self, loc.bin_removal_plan, loc.remove_bin, self.bin_id):
            self.app.changed()
            try:
                self.refresh()  # archived: stay open on its history
            except NotFoundError:  # deleted permanently: nothing left to show
                self.accept()

    def restore_bin(self):
        from wms.ui.dialogs.removal import restore_record
        v = self.app.services.search.bin_view(self.bin_id)
        if restore_record(self, "Bin", v.bin.code, self.app.services.locations.restore_bin, self.bin_id):
            self.app.changed()
            self.refresh()


def _bin_history_columns() -> list[Col]:
    """Date | Product | Movement | Qty | From | To | Project | Reference - as recorded at the time."""
    from wms.services.search import movement_reference
    return [Col("Date", lambda m: m.occurred_at, DATETIME, 130),
            Col("Product", lambda m: f"{m.product_code} - {m.product_name}", width=240),
            Col("Movement", lambda m: m.type_label, width=150),
            Col("Qty", lambda m: m.quantity, QTY, 80),
            Col("From", lambda m: m.from_bin_code or m.supplier_label or m.site_label, width=200),
            Col("To", lambda m: m.to_bin_code or m.site_label or m.supplier_label, width=200),
            Col("Project", lambda m: m.site_label, width=180),
            Col("Reference", movement_reference, width=120),
            Col("User", lambda m: m.user_label)]


def _bin_movement_columns() -> list[Col]:
    from wms.services.search import movement_reference, movement_route
    return [Col("Date / Time", lambda m: m.occurred_at, DATETIME, 130),
            Col("Type", lambda m: MovementType(m.movement_type).label, width=150),
            Col("Code", lambda m: m.product_code, width=110),
            Col("Product", lambda m: m.product_name, width=220),
            Col("Qty", lambda m: m.quantity, QTY, 80),
            Col("From", lambda m: movement_route(m)[0], width=200),
            Col("To", lambda m: movement_route(m)[1], width=200),
            Col("Reference", movement_reference, width=120),
            Col("User", lambda m: m.user_label)]


__all__ = ["DeliveryNoteDialog", "GoodsIssueDialog", "ReturnDialog", "ProductionDialog", "BinContentsDialog",
           "AttachmentKind"]
