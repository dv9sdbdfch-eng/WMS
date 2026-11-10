"""Dispatch: finished products out to a customer or project, with a printed Delivery Note."""

from __future__ import annotations

import tempfile
import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import ItemType
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_date, fmt_datetime
from wms.core.permissions import P
from wms.services.dispatch import DispatchLineInput
from wms.ui.pages.base import ListPage, Page
from wms.ui.widgets.common import (
    DATE,
    INT,
    QTY,
    Col,
    DataTable,
    DateEdit,
    PickCombo,
    button,
    confirm,
    h,
    info,
    label,
    run,
    show_error,
)
from wms.ui.widgets.lines import LinesEditor


def delivery_note_pdf(parent, app, dispatch_id: int, path: str | Path | None = None, open_after: bool = True):
    """Make the Delivery Note PDF (temporary folder unless ``path``) and open it for printing."""
    d = app.services.dispatch.get(dispatch_id)
    out = Path(path) if path else Path(tempfile.gettempdir()) / f"Delivery_Note_{d.dispatch_number}.pdf"
    try:
        done = app.services.dispatch.delivery_note_pdf(dispatch_id, out, app.ctx.config.company_name)
    except PermissionError:
        info(parent, "Delivery Note", f"Close the open PDF '{out.name}' first and try again.")
        return None
    if open_after:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(done)))
    return done


class DispatchHistory(ListPage):
    title = ""
    search_hint = "Search DEL number, customer, project, address, vehicle, product..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.status = QComboBox()
        for text, val in (("All statuses", None), ("Posted", "POSTED"), ("Reversed", "REVERSED")):
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        self.table.set_row_style(lambda d: "muted" if d.status == "REVERSED" else None)

    def columns(self):
        return [Col("Date", lambda d: d.dispatch_date, DATE, 95),
                Col("Delivery Note", lambda d: d.dispatch_number, width=110),
                Col("Deliver to", lambda d: d.deliver_to, width=280),
                Col("Address", lambda d: (d.delivery_address or "").replace("\n", ", "), width=220),
                Col("Items", lambda d: len(d.lines), INT, 60),
                Col("Vehicle / driver", lambda d: d.vehicle or "", width=130),
                Col("Status", lambda d: d.status.title(), width=90),
                Col("Notes", lambda d: d.notes or "")]

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.dispatch.search(self.search.text(), None, d1, d2, self.status.currentData())

    def describe(self, rows):
        return f"{len(rows)} dispatch(es)"

    def open_item(self, item):
        self.app.open_entity("DISPATCH", item.id, self)


class DispatchPage(Page):
    title = "Dispatch"
    subtitle = ("Finished products out to a customer or project. Every dispatch prints a numbered Delivery Note "
                "for the driver and the receiver's signature. Materials go to projects with Goods Out.")

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.tabs = QTabWidget()
        self.history = DispatchHistory(app)
        self.can_post = app.can(P.GOODS_OUT_POST)
        if self.can_post:
            self.tabs.addTab(self._entry_tab(), "New Dispatch")
            self.post_button = button("Dispatch + Delivery Note", self.post, primary=True, shortcut="Ctrl+S")
            self.add_action(self.post_button)
        self._token = uuid.uuid4().hex
        self._posting = False
        self.last_dispatch_id: int | None = None
        self.tabs.addTab(self.history, "History")
        self.tabs.currentChanged.connect(lambda _: self.history.refresh() if self.tabs.currentWidget()
                                         is self.history else None)
        self.body.addWidget(self.tabs, 1)

    def apply_preset(self, preset: dict) -> None:
        self.tabs.setCurrentWidget(self.history)
        self.history.apply_preset(preset)

    def _entry_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        left, right = QFormLayout(), QFormLayout()
        self.customer = QLineEdit()
        self.customer.setPlaceholderText("Customer name (filled from the project if left empty)")
        self.site = PickCombo("No project (customer only)")
        self.site.picked.connect(lambda _: self._site_picked())
        self.address = QPlainTextEdit()
        self.address.setPlaceholderText("Delivery address")
        self.address.setMaximumHeight(64)
        self.contact = QLineEdit()
        self.contact.setPlaceholderText("Who receives it: name, phone")
        self.date = DateEdit(date.today())
        self.vehicle = QLineEdit()
        self.vehicle.setPlaceholderText("Own van / driver (optional)")
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Customer order / job reference")
        self.dispatched_by = QLineEdit(self.app.user.full_name)
        self.notes = QLineEdit()
        left.addRow("Customer", self.customer)
        left.addRow("Project", self.site)
        left.addRow("Delivery address", self.address)
        left.addRow("Contact", self.contact)
        right.addRow("Date *", self.date)
        right.addRow("Order / job ref", self.reference)
        right.addRow("Vehicle / driver", self.vehicle)
        right.addRow("Dispatched by *", self.dispatched_by)
        right.addRow("Notes", self.notes)
        top = h()
        top.addLayout(left, 3)
        top.addLayout(right, 2)
        lay.addLayout(top)
        lay.addWidget(label("Finished products", "SectionTitle"))
        self.lines = LinesEditor(self.app, show_bin=True, bin_optional=True, show_stock=True,
                                 product_types=[ItemType.FINISHED.value])
        lay.addWidget(self.lines, 1)
        lay.addLayout(h(label("Only finished products are listed. Leave the bin empty to pick automatically. "
                              "Stock never goes below zero.", "Muted"), None, button("Clear form", self.clear)))
        self.lines.add_row()
        return w

    def _site_picked(self) -> None:
        sid = self.site.value()
        if not sid:
            return
        site = self.app.services.sites.get(sid)
        if not self.customer.text().strip() and site.customer:
            self.customer.setText(site.customer)
        if not self.address.toPlainText().strip() and getattr(site, "address", None):
            self.address.setPlainText(site.address)

    def refresh(self):
        if self.can_post:
            self.site.set_options(self.app.site_options())
            self.lines.reload_options()
        self.history.refresh()

    def add_product(self, product_id: int) -> None:
        if self.can_post:
            self.tabs.setCurrentIndex(0)
            self.lines.add_product(product_id)

    def clear(self):
        for wdg in (self.customer, self.contact, self.vehicle, self.reference, self.notes):
            wdg.clear()
        self.address.clear()
        self.site.set_value(None)
        self.dispatched_by.setText(self.app.user.full_name)
        self.date.set_value(date.today())
        self.lines.clear()
        self.lines.add_row()
        self._token = uuid.uuid4().hex

    def header(self) -> dict:
        return {"customer": self.customer.text(), "site_id": self.site.value(), "dispatch_date": self.date.value(),
                "delivery_address": self.address.toPlainText(), "contact": self.contact.text(),
                "vehicle": self.vehicle.text(), "reference": self.reference.text(), "notes": self.notes.text(),
                "dispatched_by": self.dispatched_by.text(), "client_token": self._token}

    def post(self, open_note: bool = True):
        if self.tabs.currentWidget() is self.history:
            self.tabs.setCurrentIndex(0)
            return None
        if self._posting:
            return None
        self._posting = True
        self.post_button.setEnabled(False)
        try:
            return self._post(open_note)
        finally:
            self._posting = False
            self.post_button.setEnabled(True)

    def _post(self, open_note: bool):
        try:
            values = self.lines.values()
            header = self.header()
        except ValidationError as e:
            show_error(self, e)
            return None
        if not values:
            info(self, "Dispatch", "Add at least one finished product.")
            return None
        if not header["site_id"] and not header["customer"].strip():
            info(self, "Dispatch", "Say who the goods are going to: a customer or a project.")
            return None
        lines = [DispatchLineInput(v.product_id, v.quantity, v.bin_id, v.notes) for v in values]
        d = run(self, self.app.services.dispatch.post, header, lines)
        if d is None:
            return None
        self.last_dispatch_id = d.id
        self.app.changed()
        self.clear()
        if open_note:
            delivery_note_pdf(self, self.app, d.id)
        return d


class DispatchDialog(QDialog):
    """One dispatch: who, where, items, Delivery Note PDF, reversal."""

    def __init__(self, app, dispatch_id: int, parent=None):
        super().__init__(parent)
        from wms.ui.dialogs.documents import _ask_reversal_reason, _form, _status_banner
        self._ask = _ask_reversal_reason
        self.app = app
        d = self.d = app.services.dispatch.get(dispatch_id)
        self.setWindowTitle(f"Delivery Note {d.dispatch_number}")
        self.resize(1000, 620)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"Delivery Note {d.dispatch_number}  -  {d.deliver_to}", "PageTitle"))
        banner = _status_banner(d)
        if banner:
            lay.addWidget(banner)
        lay.addLayout(_form([("Date", fmt_date(d.dispatch_date)), ("Customer", d.customer or ""),
                             ("Project", d.site_label), ("Delivery address", d.delivery_address or ""),
                             ("Contact", d.contact or ""), ("Order / job ref", d.reference or ""),
                             ("Vehicle / driver", d.vehicle or ""), ("Dispatched by", d.dispatched_by),
                             ("Entered by", f"{d.user.full_name}, {fmt_datetime(d.created_at)}"),
                             ("Notes", d.notes or "")]))
        t = DataTable([Col("#", lambda x: x.line_no, width=40),
                       Col("Code", lambda x: x.product_code, width=110,
                           link=lambda x: app.open_entity("PRODUCT", x.product_id, self)),
                       Col("Product", lambda x: x.product_name, width=280),
                       Col("Qty", lambda x: x.quantity, QTY, 80), Col("Unit", lambda x: x.product.unit.code, width=55),
                       Col("From bin", lambda x: x.bin_code, width=220), Col("Notes", lambda x: x.notes or "")])
        t.set_items(d.lines)
        lay.addWidget(t, 1)
        btns = [button("Print Delivery Note", lambda: delivery_note_pdf(self, app, d.id))]
        if d.status == "POSTED" and app.can(P.GOODS_OUT_REVERSE):
            btns.append(button("Reverse (posted in error)...", self.reverse, danger=True))
        lay.addLayout(h(*btns, None, button("Close", self.accept)))

    def reverse(self):
        if not confirm(self, "Reverse dispatch",
                       "Use this only if the dispatch was entered by mistake and the goods never left.\n\n"
                       "Reverse it?", yes="Reverse", danger=True):
            return
        picked = self._ask(self, "Why is this dispatch being reversed?")
        if picked and run(self, self.app.services.dispatch.reverse, self.d.id, picked[1], reason_code=picked[0]):
            self.app.changed()
            self.accept()
