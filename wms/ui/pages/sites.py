"""Sites / projects that materials are issued to."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from wms.core.enums import CLOSED_SITE_STATUSES, OPEN_SITE_STATUSES, SiteStatus
from wms.core.permissions import P
from wms.ui.pages.base import ListPage
from wms.ui.widgets.common import DATE, Col, button

_OPEN = list(OPEN_SITE_STATUSES)


class SitesPage(ListPage):
    title = "Sites / Projects"
    subtitle = "Customer sites and projects. Open one to see every material issued and returned."
    search_hint = "Search project no., name, customer, address, postcode, manager..."
    date_filter = False

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.SITES_EDIT):
            self.add_action(button("New site / project", self.new, primary=True, shortcut="Ctrl+N"))
        self.status = QComboBox()
        self.status.addItem("Open (planned, active, on hold)", _OPEN)
        self.status.addItem("All (include completed / archived)", None)
        for val, text in SiteStatus.choices():
            self.status.addItem(text, [val])
        self.add_filter(self.status, self.status.currentIndexChanged)
        self.table.set_row_style(lambda s: "muted" if s.status in CLOSED_SITE_STATUSES else None)

    def columns(self):
        return [Col("Project No.", lambda s: s.project_number, width=110),
                Col("Site / Project", lambda s: s.name, width=240),
                Col("Customer", lambda s: s.customer or "", width=180),
                Col("Postcode", lambda s: s.postcode or "", width=80),
                Col("Project Manager", lambda s: s.project_manager or "", width=140),
                Col("Status", lambda s: SiteStatus(s.status).label, width=100),
                Col("Start", lambda s: s.start_date, DATE, 90),
                Col("End", lambda s: s.end_date, DATE, 90),
                Col("Address", lambda s: (s.address or "").replace("\n", ", "))]

    def fetch(self):
        return self.app.services.sites.list(self.search.text(), self.status.currentData())

    def describe(self, rows):
        return f"{len(rows)} site(s)"

    def new(self):
        from wms.ui.dialogs.site_dialog import SiteDialog
        SiteDialog(self.app, None, self).exec()

    def open_item(self, item):
        self.app.open_entity("SITE", item.id, self)
