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

from wms.core.enums import EntityType, MovementType, SiteStatus
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
    def __init__(self, app, site_id: int | None, parent=None):
        super().__init__(parent)
        self.app = app
        self.site = app.services.sites.get(site_id) if site_id else None
        self.saved = False
        self.can_edit = app.can(P.SITES_EDIT)
        self.setWindowTitle(f"Site {self.site.project_number}" if self.site else "New site / project")
        self.resize(1150, 740)
        lay = QVBoxLayout(self)
        self.header = label(self.site.display if self.site else "New site / project", "PageTitle")
        lay.addWidget(self.header)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._details(), "Details")
        if self.site:
            self._build_history()
        self.btn_save = button("Save", self.save, primary=True, shortcut="Ctrl+S")
        self.btn_save.setVisible(self.can_edit)
        lay.addLayout(h(None, button("Close", self.reject), self.btn_save))

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
            Col("Type", lambda m: MovementType(m.movement_type).label + (" (reversal)" if m.reversal_of_id else ""),
                width=170),
            Col("Code", lambda m: m.product.internal_code, width=110),
            Col("Material", lambda m: m.product.name, width=220),
            Col("Qty", _signed, QTY, 80),
            Col("Unit", lambda m: m.product.unit.code, width=50),
            Col("Document", lambda m: m.doc_number or "", width=100),
            Col("Job Ref", lambda m: m.reference or "", width=100),
            Col("Issued By", lambda m: issued_by.get(m.doc_id, m.user.full_name)
                if m.movement_type == MovementType.GOODS_OUT.value else m.user.full_name, width=130)])
        self.movements.activated_item.connect(self._open_doc)
        self.movements.set_items(app.services.sites.movements(sid))
        mw = QWidget()
        ml = QVBoxLayout(mw)
        ml.addWidget(label("Positive quantities went to site; negative quantities came back.", "Muted"))
        ml.addWidget(self.movements, 1)
        self.tabs.addTab(mw, "Materials Issued")

        summary = app.services.sites.material_summary(sid)
        st = DataTable([Col("Code", lambda r: r.product.internal_code, width=110),
                        Col("Material", lambda r: r.product.name, width=260),
                        Col("Issued", lambda r: r.issued, QTY, 90),
                        Col("Returned", lambda r: r.returned, QTY, 90),
                        Col("Net used", lambda r: r.net, QTY, 90),
                        Col("Unit", lambda r: r.product.unit.code, width=50),
                        Col("Value (at issue cost)", lambda r: r.value, MONEY, 130)])
        st.set_items(summary)
        sw = QWidget()
        sl = QVBoxLayout(sw)
        sl.addWidget(st, 1)
        sl.addWidget(label(f"Total material value on site: {fmt_money(sum((r.value for r in summary), 0))}",
                           "SectionTitle"))
        self.tabs.addTab(sw, "Summary by Material")
        if app.can(P.AUDIT_VIEW):
            at = DataTable([Col("When", lambda a: a.occurred_at, DATETIME, 130),
                            Col("User", lambda a: a.username, width=100),
                            Col("Action", lambda a: a.action, width=150),
                            Col("Changes", _changes)])
            at.set_items(app.services.audit.for_entity(EntityType.SITE.value, sid))
            self.tabs.addTab(at, "Audit")

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
