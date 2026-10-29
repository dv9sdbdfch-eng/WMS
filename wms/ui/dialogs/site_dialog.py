"""Site / project record with the materials issued to it."""

from __future__ import annotations

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

from wms.core.enums import OPEN_SITE_STATUSES, EntityType, MovementType, SiteStatus
from wms.core.numbers import fmt_money
from wms.core.permissions import P
from wms.ui.dialogs.product_dialog import _changes
from wms.ui.widgets.common import (
    DATETIME,
    MONEY,
    QTY,
    Col,
    DataTable,
    OptionalDateEdit,
    button,
    h,
    label,
    run,
)


def _signed(m):
    """Positive = sent to site, negative = came back (reversals included)."""
    out = -1 if m.to_bin_id else 1
    return m.quantity * out


class SiteDialog(QDialog):
    def __init__(self, app, site_id: int | None, parent=None, tab: str | None = None):
        super().__init__(parent)
        self.app = app
        self.site = app.services.sites.get(site_id) if site_id else None
        self.saved = False
        self.can_edit = app.can(P.SITES_EDIT)
        self.setWindowTitle(f"Site {self.site.project_number}" if self.site else "New site / project")
        self.resize(1150, 740)
        lay = QVBoxLayout(self)
        title = "New site / project"
        if self.site:
            title = self.site.display
            if self.site.status not in OPEN_SITE_STATUSES:
                title += f"  —  {SiteStatus(self.site.status).label}"
        self.header = label(title, "PageTitle")
        lay.addWidget(self.header)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._details(), "Details")
        if self.site:
            self._build_history()
            if tab in ("materials", "summary"):
                self.tabs.setCurrentWidget(self.summary_tab)
            elif tab == "history":
                self.tabs.setCurrentWidget(self.history_tab)
        self.btn_save = button("Save", self.save, primary=True, shortcut="Ctrl+S")
        self.btn_save.setVisible(self.can_edit)
        extra = []
        if self.site and self.can_edit:
            if self.site.status in OPEN_SITE_STATUSES:
                extra.append(button("Mark completed", self.complete))
            else:
                extra.append(button("Reopen / restore", self.restore))
            if self.site.status != SiteStatus.ARCHIVED.value:
                extra.append(button("Delete project", self.delete, danger=True))
        lay.addLayout(h(*extra, None, button("Close", self.reject), self.btn_save))

    def _details(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.f = {}
        for key, text in (("project_number", "Project number *"), ("name", "Site / project name *"),
                          ("customer", "Customer"), ("address", "Address"), ("postcode", "Postcode"),
                          ("project_manager", "Project manager"), ("contact_name", "Site contact"),
                          ("contact_phone", "Contact phone"), ("contact_email", "Contact email")):
            e = QLineEdit()
            self.f[key] = e
            form.addRow(text, e)
        self.status = QComboBox()
        for val, text in SiteStatus.choices():
            self.status.addItem(text, val)
        self.status.setCurrentIndex(self.status.findData(SiteStatus.ACTIVE.value))
        self.start, self.end = OptionalDateEdit(), OptionalDateEdit()
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(80)
        form.addRow("Status *", self.status)
        form.addRow("Start date", self.start)
        form.addRow("End date", self.end)
        form.addRow("Notes", self.notes)
        form.addRow("", label("Completed and cancelled sites cannot receive Goods Out until reopened.", "Muted"))
        if self.site:
            for key, e in self.f.items():
                e.setText(getattr(self.site, key) or "")
            self.status.setCurrentIndex(self.status.findData(self.site.status))
            self.start.set_value(self.site.start_date)
            self.end.set_value(self.site.end_date)
            self.notes.setPlainText(self.site.notes or "")
        for e in (*self.f.values(), self.status, self.start, self.end, self.notes):
            e.setEnabled(self.can_edit)
        return w

    def _build_history(self):
        app, sid = self.app, self.site.id
        issued_by = {g.id: g.issued_by for g in app.services.goods_out.search(site_id=sid, limit=100000)}
        self.movements = DataTable([
            Col("Date", lambda m: m.occurred_at, DATETIME, 130),
            Col("Type", lambda m: m.type_label,
                width=170),
            Col("Code", lambda m: m.product_code, width=110),
            Col("Material", lambda m: m.product_name, width=220),
            Col("Qty", _signed, QTY, 80),
            Col("Unit", lambda m: m.product.unit.code, width=50),
            Col("Document", lambda m: m.doc_number or "", width=100),
            Col("Job Ref", lambda m: m.reference or "", width=100),
            Col("Issued By", lambda m: issued_by.get(m.doc_id, m.user_label)
                if m.movement_type == MovementType.GOODS_OUT.value else m.user_label, width=130)])
        self.movements.activated_item.connect(self._open_doc)
        self.movements.set_items(app.services.sites.movements(sid))
        mw = QWidget()
        ml = QVBoxLayout(mw)
        ml.addWidget(label("Positive quantities went to site; negative quantities came back.", "Muted"))
        ml.addWidget(self.movements, 1)
        self.tabs.addTab(mw, "Movement History")
        self.history_tab = mw

        summary = app.services.sites.material_summary(sid)
        st = DataTable([Col("Code", lambda r: r.product.internal_code, width=110),
                        Col("Material", lambda r: r.product.name, width=260),
                        Col("Issued", lambda r: r.issued, QTY, 90),
                        Col("Returned", lambda r: r.returned, QTY, 90),
                        Col("Net Qty", lambda r: r.net, QTY, 90),
                        Col("Unit", lambda r: r.product.unit.code, width=50),
                        Col("Last Movement", lambda r: r.last_movement, DATETIME, 130),
                        Col("Value (at issue cost)", lambda r: r.value, MONEY, 130)])
        st.set_items(summary)
        st.activated_item.connect(lambda r: app.open_entity("PRODUCT", r.product.id, self, tab="history"))
        sw = QWidget()
        sl = QVBoxLayout(sw)
        sl.addWidget(label("What have we sent to this project? Issued minus returned, per material.", "Muted"))
        sl.addWidget(st, 1)
        sl.addLayout(h(label(f"{len(summary)} material(s)   ·   Total material value on site: "
                             f"{fmt_money(sum((r.value for r in summary), 0))}", "SectionTitle"), None,
                       button("Open complete movement history",
                              lambda: self.tabs.setCurrentWidget(self.history_tab))))
        self.tabs.insertTab(1, sw, "Material Summary")
        self.summary_tab = sw
        if app.can(P.AUDIT_VIEW):
            at = DataTable([Col("When", lambda a: a.occurred_at, DATETIME, 130),
                            Col("User", lambda a: a.username, width=100),
                            Col("Action", lambda a: a.action, width=150),
                            Col("Changes", _changes)])
            at.set_items(app.services.audit.for_entity(EntityType.SITE.value, sid))
            self.tabs.addTab(at, "Audit")

    def complete(self):
        from wms.ui.widgets.common import confirm
        if confirm(self, "Complete project",
                   f"Mark {self.site.display} as completed?\n\nIt disappears from normal project selection, so "
                   f"no new material can be issued to it by mistake. Its full history stays available "
                   f"(search with completed projects included, reports, this screen).", yes="Mark completed") \
                and run(self, self.app.services.sites.set_status, self.site.id, SiteStatus.COMPLETED.value):
            self.app.changed()
            self.accept()

    def restore(self):
        from wms.ui.dialogs.removal import restore_record
        if restore_record(self, "Project", self.site.display, self.app.services.sites.restore, self.site.id):
            self.app.changed()
            self.accept()

    def delete(self):
        from wms.ui.dialogs.removal import delete_record
        if delete_record(self, self.app.services.sites.removal_plan, self.app.services.sites.remove, self.site.id):
            self.app.changed()
            self.accept()

    def _open_doc(self, m):
        if m.doc_type and m.doc_id:
            self.app.open_entity(m.doc_type, m.doc_id, self)

    def save(self):
        try:
            start, end = self.start.value(), self.end.value()
        except Exception as e:  # noqa: BLE001
            from wms.ui.widgets.common import show_error
            show_error(self, e)
            return
        data = {k: e.text() for k, e in self.f.items()}
        data.update(status=self.status.currentData(), start_date=start, end_date=end,
                    notes=self.notes.toPlainText())
        site = run(self, self.app.services.sites.save, self.site.id if self.site else None, data,
                   self.site.version if self.site else None)
        if site:
            self.saved = True
            self.app.changed()
            self.accept()
