"""Project Stock: material delivered for projects and not issued yet (read-only history).

v1.35: stock is no longer reserved by hand. Goods In with a project puts the delivery into that
project's stock; Goods Out to the project uses it up; Return to main (in the project window) gives
what is left back to the warehouse. Older hand reservations are shown here as history.
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import PickCombo


class ReservationsPage(ListPage):
    title = "Project Stock"
    subtitle = ("Material delivered for a project and not issued yet. It stays in its bin and counts as "
                "On Hand, but only that project can use it, until it is issued with Goods Out or "
                "returned to main stock.")
    search_hint = "Search number, product code or name, project or notes..."
    date_filter = False

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.site = PickCombo("All projects")
        self.add_filter(self.site, self.site.picked)
        self.status = QComboBox()
        for text, val in (("Left", "ACTIVE"), ("Used up", "FULFILLED"), ("Returned / closed", "RELEASED"),
                          ("All", None)):
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
                                                   text=self.search.text(), limit=None)

    def describe(self, rows):
        return f"{len(rows)} line(s)"

    def open_item(self, item):
        self.app.open_entity("RESERVATION", item.reservation.id, self)
        self.refresh()
