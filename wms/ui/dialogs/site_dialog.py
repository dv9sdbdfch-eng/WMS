"""Site / project record with the materials issued to it."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
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
    QTY,
    Card,
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


class _ManagerBox(QComboBox):
    """Project manager: pick one of the managers (Project Tree) or type a new name, which creates
    that manager when the project is saved."""

    def __init__(self, app):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItem("")
        for name, _mid in app.services.project_tree.manager_choices():
            self.addItem(name)
        self.lineEdit().setPlaceholderText("Choose or type a manager")

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:  # noqa: N802 - same call as QLineEdit
        self.setEditText(text)


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
        if self.site:
            # spec 26: status, number and manager at a glance; spec 42: the next actions for this project
            s = self.site
            facts = [s.project_number, SiteStatus(s.status).label]
            if s.project_manager:
                facts.append(f"Project manager: {s.project_manager}")
            if s.customer:
                facts.append(f"Customer: {s.customer}")
            self.facts = label("   ·   ".join(facts), "Muted")
            quick = []
            open_site = s.status in OPEN_SITE_STATUSES
            if open_site and app.can(P.GOODS_OUT_POST) and getattr(app, "main_window", None) is not None:
                quick.append(button("−  Goods Out to this Project", self.goods_out))
            if open_site and app.can(P.RESERVATIONS_EDIT):
                quick.append(button("Reserve Stock...", self.reserve))
            lay.addLayout(h(self.facts, None, *quick))
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._overview(), "Overview" if self.site else "Details")
        if self.site:
            self._build_history()
            if tab in ("materials", "summary"):
                self.tabs.setCurrentWidget(self.summary_tab)  # loads the list (_tab_changed)
            elif tab == "history":
                self.tabs.setCurrentWidget(self.history_tab)
            elif tab == "reservations":
                self.tabs.setCurrentWidget(self.reservations)
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

    def _overview(self) -> QWidget:
        """Project figures on top (Required / Reserved / Issued / Returned / Net), details below."""
        details = self._details()
        if not self.site:
            return details
        w = QWidget()
        lay = QVBoxLayout(w)
        self.cards = {k: Card(t) for k, t in (("materials", "Materials"), ("required", "Required"),
                                              ("reserved", "Reserved"), ("issued", "Issued"),
                                              ("returned", "Returned"), ("net", "Net Issued"),
                                              ("remaining", "Remaining"), ("value", "Material value on site"))}
        lay.addLayout(h(*self.cards.values()))
        lay.addWidget(label("Required = the project's material list (a plan, never stock).  Reserved = held in the "
                            "warehouse for it.  Issued / Returned = posted Goods Out and returns.  Net Issued = "
                            "Issued - Returned.  Remaining = Required - Net Issued - Reserved (still to reserve or "
                            "issue; a return makes that quantity needed again).", "Muted", wrap=True))
        lay.addWidget(details, 1)
        self._fill_cards()
        return w

    def _fill_cards(self) -> None:
        from wms.core.numbers import fmt_qty
        summ = run(self, self.app.services.materials.project_summary, self.site.id)
        if summ is None:
            return
        value = sum((r.value for r in self.app.services.sites.material_summary(self.site.id)), 0)
        self.cards["materials"].set_value(f"{summ.materials:,}")
        for k in ("required", "reserved", "issued", "returned", "net", "remaining"):
            self.cards[k].set_value(fmt_qty(getattr(summ, k)))
        self.cards["value"].set_value(fmt_money(value))

    def _details(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.f = {}
        for key, text in (("project_number", "Project number *"), ("name", "Site / project name *"),
                          ("customer", "Customer"), ("address", "Address"), ("postcode", "Postcode"),
                          ("project_manager", "Project manager"), ("contact_name", "Site contact"),
                          ("contact_phone", "Contact phone"), ("contact_email", "Contact email")):
            e = _ManagerBox(self.app) if key == "project_manager" else QLineEdit()
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
        self.history_tab = mw

        # Materials: the same list, filters and export as the Materials screen, with the project fixed
        from wms.ui.widgets.materials_view import MaterialsView
        self.materials = MaterialsView(app, self, fixed_site_id=sid)
        self.materials.changed.connect(self._fill_cards)
        sw = QWidget()
        sl = QVBoxLayout(sw)
        open_site = self.site.status in OPEN_SITE_STATUSES
        acts = []
        # spec 27: the actions of THIS project's material list
        if self.can_edit and open_site:
            acts += [button("+  Add Material", self.add_materials, primary=True,
                            tooltip="Add existing materials to this project's list with a Required quantity"),
                     button("Import Excel...", self.import_list,
                            tooltip=f"Import / update the {self.site.name} material requirements from Excel. "
                                    f"Stock is never changed.")]
        if app.can(P.REPORTS_EXPORT):
            exp = QPushButton("Export Excel")
            exp.setAutoDefault(False)
            m = QMenu(exp)
            m.addAction("Export Project Material List...", lambda: self.materials.export_project())
            m.addAction("Export Current View...", lambda: self.materials.export("CURRENT"))
            m.addAction("Export Selected...", lambda: self.materials.export("SELECTED"))
            exp.setMenu(m)
            self.export_btn = exp
            acts.append(exp)
        sl.addLayout(h(label("What this project needs (Required) and what it has had (Reserved / Issued / "
                             "Returned). Tick rows to set quantities or take them off the list.", "Muted"),
                       None, *acts))
        sl.addWidget(self.materials, 1)
        # spec 26: Overview, Materials, Movements, Documents; then Reservations and Audit
        self.tabs.addTab(sw, "Materials")
        self.summary_tab = sw
        self._materials_loaded = False
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.addTab(mw, "Movements")
        from wms.ui.widgets.attachments import AttachmentPanel
        self.docs = AttachmentPanel(app, EntityType.SITE.value, None, thumbs=False)  # drawings, site photos
        self.docs.set_entity(sid)
        self.tabs.addTab(self.docs, "Documents")
        from wms.ui.dialogs.reservations import ReservationsPanel
        self.reservations = ReservationsPanel(app, site_id=sid)
        self.tabs.addTab(self.reservations, "Reservations")
        if app.can(P.AUDIT_VIEW):
            at = DataTable([Col("When", lambda a: a.occurred_at, DATETIME, 130),
                            Col("User", lambda a: a.username, width=100),
                            Col("Action", lambda a: a.action, width=150),
                            Col("Changes", _changes)])
            at.set_items(app.services.audit.for_entity(EntityType.SITE.value, sid))
            self.tabs.addTab(at, "Audit")

    def _tab_changed(self, _i: int) -> None:
        if self.tabs.currentWidget() is self.summary_tab and not self._materials_loaded:
            self._materials_loaded = True
            self.materials.refresh()

    def goods_out(self) -> None:
        """Close the project and start a Goods Out to it (the project already chosen)."""
        mw = self.app.main_window
        self.accept()
        mw.go("goods_out")
        page = mw.page("goods_out")
        if page is not None and hasattr(page, "site") and page.can_post:
            page.tabs.setCurrentIndex(0)
            page.site.set_value(self.site.id)

    def reserve(self) -> None:
        self.tabs.setCurrentWidget(self.reservations)
        self.reservations.new()
        self._fill_cards()

    def add_materials(self) -> bool:
        """Choose existing materials, then their Required quantity (spec section 7). No stock moves."""
        from wms.ui.widgets.materials_view import MaterialPickerDialog
        pick = MaterialPickerDialog(self.app, self, "Add materials to the project list")
        if pick.exec() != QDialog.DialogCode.Accepted:
            return False
        rows = pick.rows()
        return bool(rows) and self.materials.add_to_project(self.site.id, rows)

    def import_list(self) -> None:
        from wms.ui.dialogs.excel_import import ExcelImportDialog
        dlg = ExcelImportDialog(self.app, self, project_id=self.site.id)
        dlg.exec()
        if dlg.result is not None:
            self.materials.refresh()
            self._fill_cards()

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
