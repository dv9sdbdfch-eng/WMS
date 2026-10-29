"""Reserve stock for a project, change or release a reservation."""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from wms.core.enums import ReservationStatus
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.ui.widgets.common import (
    DATE,
    DATETIME,
    QTY,
    Col,
    DataTable,
    DecimalEdit,
    OptionalDateEdit,
    PickCombo,
    ask_text,
    button,
    h,
    label,
    run,
    show_error,
)


def reservation_columns() -> list[Col]:
    return [Col("No.", lambda v: v.reservation.number, width=95),
            Col("Project", lambda v: v.reservation.site_label, width=220),
            Col("Code", lambda v: v.reservation.product.internal_code, width=110),
            Col("Product", lambda v: v.reservation.product.name, width=220),
            Col("Reserved", lambda v: v.reservation.quantity, QTY, 80),
            Col("Issued", lambda v: v.issued, QTY, 75),
            Col("Still reserved", lambda v: v.remaining, QTY, 100),
            Col("Unit", lambda v: v.reservation.product.unit.code, width=55),
            Col("Required by", lambda v: v.reservation.required_by, DATE, 95),
            Col("Status", lambda v: ReservationStatus(v.reservation.status).label, width=85),
            Col("Created", lambda v: v.reservation.created_at, DATETIME, 125),
            Col("Notes", lambda v: v.reservation.notes or v.reservation.release_reason or "")]


def reservation_style(v) -> str | None:
    return None if v.reservation.status == "ACTIVE" else "muted"


class ReservationDialog(QDialog):
    """New reservation (product and/or project can be fixed by the caller) or change an active one."""

    def __init__(self, app, parent=None, product_id: int | None = None, site_id: int | None = None,
                 reservation_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self._token = uuid.uuid4().hex
        self.view = app.services.reservations.get(reservation_id) if reservation_id else None
        self.setWindowTitle(f"Reservation {self.view.reservation.number}" if self.view else "Reserve stock")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.product = PickCombo("Type code or name...")
        self.product.set_options(app.product_options())
        self.site = PickCombo("Choose project...")
        self.site.set_options(app.site_options())
        self.qty = DecimalEdit()
        self.required = OptionalDateEdit()
        self.notes = QLineEdit()
        self.states = label("", "Muted", wrap=True)
        if self.view:
            r = self.view.reservation
            self.product.set_value(r.product_id)
            self.site.set_options(app.site_options(open_only=False))
            self.site.set_value(r.site_id)
            self.qty.set_value(r.quantity)
            self.required.set_value(r.required_by)
            self.notes.setText(r.notes or "")
            self.product.setEnabled(False)
            self.site.setEnabled(False)
        else:
            if product_id:
                self.product.set_value(product_id)
            if site_id:
                self.site.set_value(site_id)
        self.product.picked.connect(lambda _: self._show_states())
        form.addRow("Product *", self.product)
        form.addRow("Project *", self.site)
        form.addRow("Quantity *", self.qty)
        form.addRow("Required by", self.required)
        form.addRow("Notes", self.notes)
        form.addRow("", self.states)
        lay.addLayout(form)
        lay.addWidget(label("A reservation does not move stock. It sets it aside for the project until it is "
                            "issued with Goods Out to that project.", "Muted", wrap=True))
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))
        self._show_states()

    def _show_states(self):
        pid = self.product.value()
        if not pid:
            self.states.setText("")
            return
        r = self.app.services.products.row(pid)
        u = r.product.unit.code
        self.states.setText(f"On hand {fmt_qty(r.on_hand)} {u}   ·   reserved {fmt_qty(r.reserved)}   ·   "
                            f"available {fmt_qty(r.available)}   ·   on order {fmt_qty(r.incoming)}")

    def save(self):
        try:
            qty, required = self.qty.value(), self.required.value()
        except ValidationError as e:
            show_error(self, e)
            return
        rs = self.app.services.reservations
        if self.view:
            done = run(self, rs.update, self.view.reservation.id, qty, required, self.notes.text())
        else:
            done = run(self, rs.reserve, {"product_id": self.product.value(), "site_id": self.site.value(),
                                          "quantity": qty, "required_by": required, "notes": self.notes.text(),
                                          "client_token": self._token})
        if done:
            self.saved = done
            self.app.changed()
            self.accept()


class ReservationsPanel(QWidget):
    """Reservations of one product or one project, with Reserve / Change / Release."""

    def __init__(self, app, product_id: int | None = None, site_id: int | None = None, parent=None):
        super().__init__(parent)
        self.app, self.product_id, self.site_id = app, product_id, site_id
        lay = QVBoxLayout(self)
        self.table = DataTable(reservation_columns())
        self.table.set_row_style(reservation_style)
        self.table.activated_item.connect(lambda v: self.change())
        lay.addWidget(self.table, 1)
        self.count = label("", "Muted")
        btns = [self.count, None]
        if app.can(P.RESERVATIONS_EDIT):
            btns += [button("Reserve...", self.new), button("Change...", self.change),
                     button("Release...", self.release)]
        lay.addLayout(h(*btns))
        self.refresh()

    def refresh(self):
        rows = self.app.services.reservations.list(product_id=self.product_id, site_id=self.site_id)
        self.table.set_items(rows)
        active = [v for v in rows if v.remaining > 0]
        self.count.setText(f"{len(active)} active reservation(s)" if rows else "Nothing reserved.")

    def new(self):
        if ReservationDialog(self.app, self, product_id=self.product_id, site_id=self.site_id).exec():
            self.refresh()

    def change(self):
        v = self.table.current()
        if v is None or v.reservation.status != "ACTIVE" or not self.app.can(P.RESERVATIONS_EDIT):
            return
        if ReservationDialog(self.app, self, reservation_id=v.reservation.id).exec():
            self.refresh()

    def release(self):
        v = self.table.current()
        if v is None or v.reservation.status != "ACTIVE":
            return
        reason = ask_text(self, "Release reservation",
                          f"Release what is still reserved on {v.reservation.number} "
                          f"({fmt_qty(v.remaining)} {v.reservation.product.unit.code})? Why?")
        if reason and run(self, self.app.services.reservations.release, v.reservation.id, reason):
            self.app.changed()
            self.refresh()
