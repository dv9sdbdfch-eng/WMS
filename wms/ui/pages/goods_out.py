"""Goods Out: issue materials from the store to a site / project."""

from __future__ import annotations

import uuid
from datetime import date

from PySide6.QtWidgets import QComboBox, QFormLayout, QLineEdit, QTabWidget, QVBoxLayout, QWidget

from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_datetime
from wms.core.permissions import P
from wms.services.goods_out import IssueLine
from wms.ui.pages.base import ListPage, Page
from wms.ui.widgets.common import (
    DATE,
    INT,
    Col,
    DateEdit,
    PickCombo,
    button,
    confirm,
    h,
    info,
    label,
    run_with_override,
    show_error,
)
from wms.ui.widgets.lines import LinesEditor


class IssueHistory(ListPage):
    title = ""
    search_hint = "Search number, site, job ref, requested/issued by, product..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.site = PickCombo("All sites")
        self.add_filter(self.site, self.site.picked)
        self.status = QComboBox()
        for text, val in (("All statuses", None), ("Posted", "POSTED"), ("Reversed", "REVERSED")):
            self.status.addItem(text, val)
        self.add_filter(self.status, self.status.currentIndexChanged)
        self.table.set_row_style(lambda g: "muted" if g.status == "REVERSED" else None)

    def columns(self):
        return [Col("Date", lambda g: g.issue_date, DATE, 95),
                Col("Number", lambda g: g.issue_number, width=100),
                Col("Site / Project", lambda g: g.site_label, width=260),
                Col("Job Ref", lambda g: g.reference or "", width=110),
                Col("Lines", lambda g: len(g.lines), INT, 60),
                Col("Requested By", lambda g: g.requested_by or "", width=130),
                Col("Issued By", lambda g: g.issued_by, width=130),
                Col("Status", lambda g: g.status.title(), width=90),
                Col("Notes", lambda g: g.notes or "")]

    def refresh(self):
        if not self.site.count():
            self.site.set_options(self.app.site_options(open_only=False))
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.goods_out.search(self.search.text(), self.site.value(), d1, d2,
                                                  self.status.currentData())

    def describe(self, rows):
        return f"{len(rows)} Goods Out record(s)"

    def open_item(self, item):
        self.app.open_entity("GOODS_ISSUE", item.id, self)


class GoodsOutPage(Page):
    title = "Goods Out"
    subtitle = "Issue materials to a site or project. Stock cannot go below zero without an authorised override."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.tabs = QTabWidget()
        self.history = IssueHistory(app)
        self.can_post = app.can(P.GOODS_OUT_POST)
        if self.can_post:
            self.tabs.addTab(self._entry_tab(), "New Goods Out")
            self.post_button = button("Post Goods Out", self.post, primary=True, shortcut="Ctrl+S")
            self.add_action(self.post_button)
        self._token = uuid.uuid4().hex  # one per filled-in form: the server refuses a second posting
        self._posting = False
        self.tabs.addTab(self.history, "History")
        self.tabs.currentChanged.connect(lambda _: self.history.refresh() if self.tabs.currentWidget()
                                         is self.history else None)
        self.body.addWidget(self.tabs, 1)

    def _entry_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.site = PickCombo("Choose site / project...")
        self.date = DateEdit(date.today())
        self.requested_by = QLineEdit()
        self.requested_by.setPlaceholderText("Who asked for the material (fitter, site manager...)")
        self.issued_by = QLineEdit(self.app.user.full_name)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Job / work order reference")
        self.notes = QLineEdit()
        form.addRow("Site / project *", self.site)
        form.addRow("Date *", self.date)
        form.addRow("Requested by", self.requested_by)
        form.addRow("Issued by *", self.issued_by)
        form.addRow("Job reference", self.reference)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        lay.addWidget(label("Materials", "SectionTitle"))
        self.lines = LinesEditor(self.app, show_bin=True, bin_optional=True, show_stock=True)
        lay.addWidget(self.lines, 1)
        lay.addLayout(h(label("Leave the bin empty to pick automatically (default bin first, then the bins with most "
                              "stock). Quarantine bins are never used.", "Muted"), None,
                        button("Clear form", self.clear)))
        self.lines.add_row()
        # Scan / type the project number + Enter -> straight to the first empty product line.
        self.site.entered.connect(lambda v: v is not None and self.lines.focus_next_empty())
        return w

    def refresh(self):
        if self.can_post:
            self.site.set_options(self.app.site_options())
            self.lines.reload_options()
        self.history.refresh()

    def add_product(self, product_id: int, site_id: int | None = None) -> None:
        """Start issuing a product found in search."""
        if not self.can_post:
            return
        self.tabs.setCurrentIndex(0)
        if site_id and self.site.value() is None:
            self.site.set_value(site_id)
        self.lines.add_product(product_id)

    def clear(self):
        self.requested_by.clear()
        self.reference.clear()
        self.notes.clear()
        self.issued_by.setText(self.app.user.full_name)
        self.date.set_value(date.today())
        self.lines.clear()
        self.lines.add_row()
        self._token = uuid.uuid4().hex

    def post(self):
        if self.tabs.currentWidget() is self.history:
            self.tabs.setCurrentIndex(0)
            return
        if self._posting:  # second click / Ctrl+S while the first posting is still running
            return
        self._posting = True
        self.post_button.setEnabled(False)
        try:
            self._post()
        finally:
            self._posting = False
            self.post_button.setEnabled(True)

    def _post(self):
        try:
            values = self.lines.values()
        except ValidationError as e:
            show_error(self, e)
            return
        if not self.site.value():
            info(self, "Goods Out", "Choose the site / project the material is going to.")
            return
        if not values:
            info(self, "Goods Out", "Add at least one product line.")
            return
        dups = self.lines.duplicate_products()
        if dups and not confirm(self, "Repeated products", "These products appear on more than one line: "
                                + ", ".join(dups) + "\n\nContinue?"):
            return
        header = {"site_id": self.site.value(), "issue_date": self.date.value(),
                  "requested_by": self.requested_by.text(), "issued_by": self.issued_by.text(),
                  "reference": self.reference.text(), "notes": self.notes.text(), "client_token": self._token}
        lines = [IssueLine(v.product_id, v.quantity, v.bin_id, v.notes) for v in values]
        same = self.app.services.goods_out.recent_similar(self.site.value(), lines)
        if same and not confirm(self, "Possible duplicate",
                                f"{same.issue_number} was posted to this site at {fmt_datetime(same.created_at)} "
                                f"with exactly the same materials and quantities.\n\nIs this a separate, second "
                                f"issue? Choose Cancel if it was already entered.", yes="Post again", danger=True):
            return
        # Stock set aside for other projects: warn before it is taken (it has physically left anyway).
        clash = self.app.services.reservations.conflicts(self.site.value(),
                                                         [(v.product_id, v.quantity) for v in values])
        if clash and not confirm(self, "Reserved for another project",
                                 "\n".join(clash) + "\n\nThat project will be short. Post anyway?",
                                 yes="Post anyway", danger=True):
            return
        issue = run_with_override(self, self.app.can(P.STOCK_OVERRIDE_NEGATIVE), self.app.services.goods_out.post,
                                  header=header, lines=lines)
        if issue:
            self.app.changed()
            if confirm(self, "Goods Out posted", f"{issue.issue_number} has been posted to {issue.site_label}.\n\n"
                       "Open it to print the issue note?", yes="Open"):
                self.app.open_entity("GOODS_ISSUE", issue.id, self)
            self.clear()
