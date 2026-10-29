"""Reservations: stock set aside for projects until it is issued with Goods Out."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import PickCombo, ask_text, button, run


class ReservationsPage(ListPage):
    title = "Reservations"
    subtitle = ("Material set aside for a project. It stays in its bin and counts as On Hand, but not as "
                "Available, until it is issued to that project with Goods Out.")
    search_hint = "Search reservation number, product code or name, project or notes..."
    date_filter = False

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.RESERVATIONS_EDIT):
            self.add_action(button("Reserve stock...", self.new_reservation, primary=True))
            self.footer.addWidget(button("Change...", lambda: self._open(self.table.current())))
            self.footer.addWidget(button("Release...", self.release))
        self.site = PickCombo("All projects")
        self.add_filter(self.site, self.site.picked)
        self.status = QComboBox()
        for text, val in (("Active", "ACTIVE"), ("Fulfilled", "FULFILLED"), ("Released", "RELEASED"), ("All", None)):
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        from wms.ui.dialogs.reservations import reservation_style
        self.table.set_row_style(reservation_style)

    def columns(self):
        from wms.ui.dialogs.reservations import reservation_columns
        return reservation_columns()

    def refresh(self):
        if not self.site.count():
            self.site.set_options(self.app.site_options(open_only=False))
        super().refresh()

    def fetch(self):
        return self.app.services.reservations.list(self.status.currentData(), site_id=self.site.value(),
                                                   text=self.search.text())

    def describe(self, rows):
        return f"{len(rows)} reservation(s)"

    def new_reservation(self):
        from wms.ui.dialogs.reservations import ReservationDialog
        if ReservationDialog(self.app, self, site_id=self.site.value()).exec():
            self.refresh()

    def open_item(self, item):
        self.app.open_entity("RESERVATION", item.reservation.id, self)
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
