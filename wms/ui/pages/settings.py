"""Settings: company details, master data, backup / restore and data checks."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms import __version__
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DATETIME, Col, DataTable, SearchBox, button, confirm, h, info, label, run


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


class SettingsPage(Page):
    title = "Settings"
    subtitle = "Company details, suppliers, categories, units, backups and data checks"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.tabs = QTabWidget()
        self._tabs = []
        if app.can(P.SETTINGS_MANAGE):
            self._add(self._general(), "General", None)
        if app.can(P.PRODUCTS_VIEW):
            self._add(self._suppliers(), "Suppliers", self._load_suppliers)
            self._add(self._categories(), "Categories", self._load_categories)
            self._add(self._units(), "Units of measure", self._load_units)
            self._add(self._reasons(), "Reason codes", self._load_reasons)
        if app.can(P.BACKUP_RUN) or app.can(P.BACKUP_RESTORE):
            self._add(self._backups(), "Backup and restore", self._load_backups)
        if app.can(P.SETTINGS_MANAGE) or app.can(P.AUDIT_VIEW):
            self._add(self._integrity(), "Data check", None)
        self._add(self._about(), "About", None)
        self.body.addWidget(self.tabs, 1)

    def _add(self, w, title, loader):
        self.tabs.addTab(w, title)
        self._tabs.append(loader)

    def refresh(self):
        for loader in self._tabs:
            if loader:
                loader()

    # -- general -------------------------------------------------------------
    def _general(self) -> QWidget:
        cfg = self.app.ctx.config
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.company = QLineEdit(cfg.company_name)
        self.auto_backup = QCheckBox("Make a backup automatically every time the program is closed")
        self.auto_backup.setChecked(cfg.auto_backup_on_exit)
        self.keep = QSpinBox()
        self.keep.setRange(3, 365)
        self.keep.setValue(int(cfg.backups_to_keep))
        self.keep.setMaximumWidth(100)
        self.backup_dir = QLineEdit(str(cfg.backup_path))
        self.max_mb = QSpinBox()
        self.max_mb.setRange(1, 200)
        self.max_mb.setValue(int(cfg.max_attachment_mb))
        self.max_mb.setMaximumWidth(100)
        form.addRow("Company name (shown on reports)", self.company)
        form.addRow("", self.auto_backup)
        form.addRow("Automatic backups to keep", self.keep)
        form.addRow("Backup folder", h(self.backup_dir, button("Browse...", self._browse_backup)))
        form.addRow("Largest attachment (MB)", self.max_mb)
        form.addRow("Data folder", label(str(cfg.data_dir), "Muted"))
        form.addRow("Database", label(cfg.resolved_database_url if not cfg.is_sqlite else str(cfg.db_path), "Muted"))
        form.addRow("Attachments folder", label(str(cfg.attachments_path), "Muted"))
        lay.addLayout(form)
        lay.addLayout(h(button("Open data folder", lambda: _open_folder(cfg.data_dir)), None,
                        button("Save settings", self._save_general, primary=True)))
        lay.addWidget(label("Tip: keep the backup folder on a different drive, a USB stick or a synced cloud "
                            "folder (OneDrive), so a failed PC does not take the backups with it.", "InfoBanner",
                            wrap=True))
        lay.addStretch(1)
        return w

    def _browse_backup(self):
        d = QFileDialog.getExistingDirectory(self, "Backup folder", self.backup_dir.text())
        if d:
            self.backup_dir.setText(d)

    def _save_general(self):
        changes = {"company_name": self.company.text(), "auto_backup_on_exit": self.auto_backup.isChecked(),
                   "backups_to_keep": self.keep.value(), "backup_dir": self.backup_dir.text().strip(),
                   "max_attachment_mb": self.max_mb.value()}
        if run(self, self.app.services.settings.save_general, changes) is not None:
            info(self, "Settings", "Settings saved.")

    # -- suppliers / categories / units -------------------------------------
    def _suppliers(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.sup_search = SearchBox("Search supplier name, code or contact...")
        self.sup_search.search.connect(lambda _: self._load_suppliers())
        self.sup_inactive = QCheckBox("Show inactive")
        self.sup_inactive.toggled.connect(lambda _: self._load_suppliers())
        lay.addLayout(h(self.sup_search, self.sup_inactive))
        self.sup_table = DataTable([Col("Code", lambda s: s.code, width=90), Col("Name", lambda s: s.name, width=220),
                                    Col("Contact", lambda s: s.contact_name or "", width=140),
                                    Col("Phone", lambda s: s.phone or "", width=120),
                                    Col("Email", lambda s: s.email or "", width=200),
                                    Col("Account no.", lambda s: s.account_number or "", width=100),
                                    Col("Active", lambda s: "Yes" if s.is_active else "No")])
        self.sup_table.set_row_style(lambda s: None if s.is_active else "muted")
        self.sup_table.activated_item.connect(self._edit_supplier)
        lay.addWidget(self.sup_table, 1)
        btns = [None]
        if self.app.can(P.SUPPLIERS_EDIT):
            btns = [button("New supplier", lambda: self._edit_supplier(None), primary=True), None]
        lay.addLayout(h(*btns, button("Open", lambda: self._edit_supplier(self.sup_table.current()))))
        return w

    def _load_suppliers(self):
        self.sup_table.set_items(self.app.services.reference.suppliers(self.sup_search.text() or None,
                                                                       self.sup_inactive.isChecked()))

    def _edit_supplier(self, sup):
        from wms.ui.dialogs.reference_dialogs import SupplierDialog
        if sup is None and not self.app.can(P.SUPPLIERS_EDIT):
            return
        SupplierDialog(self.app, sup.id if sup else None, self).exec()
        self._load_suppliers()

    def _categories(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.cat_table = DataTable([Col("Category", lambda c: c.full_name, width=320),
                                    Col("Active", lambda c: "Yes" if c.is_active else "No")])
        self.cat_table.activated_item.connect(self._edit_category)
        lay.addWidget(self.cat_table, 1)
        if self.app.can(P.SUPPLIERS_EDIT):
            lay.addLayout(h(button("New category", lambda: self._edit_category(None, True), primary=True),
                            button("Edit", lambda: self._edit_category(self.cat_table.current())), None))
        return w

    def _load_categories(self):
        self.cat_table.set_items(self.app.services.reference.categories(include_inactive=True))

    def _edit_category(self, cat, new=False):
        from wms.ui.dialogs.reference_dialogs import CategoryDialog
        if (cat or new) and self.app.can(P.SUPPLIERS_EDIT) and CategoryDialog(self.app, cat, self).exec():
            self._load_categories()
            self.app.changed()

    def _units(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.unit_table = DataTable([Col("Code", lambda u: u.code, width=90), Col("Name", lambda u: u.name, width=200),
                                     Col("Part quantities", lambda u: "Yes" if u.allow_decimal else "Whole only",
                                         width=120),
                                     Col("Active", lambda u: "Yes" if u.is_active else "No")])
        self.unit_table.activated_item.connect(self._edit_unit)
        lay.addWidget(self.unit_table, 1)
        if self.app.can(P.SUPPLIERS_EDIT):
            lay.addLayout(h(button("New unit", lambda: self._edit_unit(None, True), primary=True),
                            button("Edit", lambda: self._edit_unit(self.unit_table.current())), None))
        return w

    def _load_units(self):
        self.unit_table.set_items(self.app.services.reference.units(include_inactive=True))

    def _edit_unit(self, unit, new=False):
        from wms.ui.dialogs.reference_dialogs import UnitDialog
        if (unit or new) and self.app.can(P.SUPPLIERS_EDIT) and UnitDialog(self.app, unit, self).exec():
            self._load_units()
            self.app.changed()

    # -- reason codes -------------------------------------------------------
    def _reasons(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Reasons offered when adjusting stock, reversing a transaction, recording a return or "
                            "a delivery discrepancy. Retired reasons stay on the records that used them.",
                            "Muted", wrap=True))
        yes = lambda v: "Yes" if v else ""  # noqa: E731
        self.reason_table = DataTable([
            Col("Code", lambda r: r.code, width=170), Col("Name", lambda r: r.label, width=200),
            Col("Adjustments", lambda r: yes(r.for_adjustment), width=95),
            Col("Reversals", lambda r: yes(r.for_reversal), width=85),
            Col("Returns", lambda r: yes(r.for_return), width=75),
            Col("Discrepancies", lambda r: yes(r.for_discrepancy), width=100),
            Col("System", lambda r: yes(r.is_system), width=65),
            Col("Active", lambda r: "Yes" if r.is_active else "Retired")])
        self.reason_table.activated_item.connect(self._edit_reason)
        lay.addWidget(self.reason_table, 1)
        if self.app.can(P.SETTINGS_MANAGE):
            lay.addLayout(h(button("New reason", lambda: self._edit_reason(None, True), primary=True),
                            button("Edit", lambda: self._edit_reason(self.reason_table.current())), None))
        return w

    def _load_reasons(self):
        self.reason_table.set_items(self.app.services.reasons.list(include_inactive=True))

    def _edit_reason(self, rc, new=False):
        from wms.ui.dialogs.reference_dialogs import ReasonCodeDialog
        if (rc or new) and self.app.can(P.SETTINGS_MANAGE) and ReasonCodeDialog(self.app, rc, self).exec():
            self._load_reasons()

    # -- backup / restore ---------------------------------------------------
    def _backups(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("A backup is one .zip file holding the database and every photo and scan. Automatic "
                            "backups are made when the program closes; manual backups are never deleted.",
                            "InfoBanner", wrap=True))
        self.bk_table = DataTable([Col("Created", lambda b: b.created, DATETIME, 140),
                                   Col("File", lambda b: b.path.name, width=330),
                                   Col("Size", lambda b: _size(b.size_bytes), width=80),
                                   Col("Version", lambda b: b.app_version, width=70),
                                   Col("Note", lambda b: b.note)])
        lay.addWidget(self.bk_table, 1)
        btns = []
        if self.app.can(P.BACKUP_RUN):
            btns += [button("Back up now", self._backup_now, primary=True),
                     button("Back up to another folder...", self._backup_to)]
        btns.append(button("Open backup folder", lambda: _open_folder(self.app.ctx.config.backup_path)))
        btns.append(None)
        if self.app.can(P.BACKUP_RESTORE):
            btns += [button("Restore selected...", self._restore_selected, danger=True),
                     button("Restore from file...", self._restore_file, danger=True)]
        lay.addLayout(h(*btns))
        return w

    def _load_backups(self):
        self.bk_table.set_items(self.app.services.backup.list_backups())

    def _backup_now(self, destination=None):
        from wms.ui.widgets.common import ask_text
        note = ask_text(self, "Backup", "Optional note for this backup (e.g. 'Before stock take'):", required=False)
        if note is None:
            return
        path = run(self, self.app.services.backup.create, note, destination)
        if path:
            self._load_backups()
            info(self, "Backup complete", f"Backup saved:\n{path}")

    def _backup_to(self):
        d = QFileDialog.getExistingDirectory(self, "Save backup to")
        if d:
            self._backup_now(Path(d))

    def _restore_selected(self):
        b = self.bk_table.current()
        if b is None:
            info(self, "Restore", "Select a backup in the list first.")
            return
        self._restore(b.path)

    def _restore_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Restore backup", str(self.app.ctx.config.backup_path),
                                              "Store WMS backup (*.zip)")
        if path:
            self._restore(Path(path))

    def _restore(self, path: Path):
        manifest = run(self, self.app.services.backup.verify, path)
        if manifest is None:
            return
        if not confirm(self, "Restore backup",
                       f"Replace ALL current data with the backup:\n\n{path.name}\nCreated: "
                       f"{manifest.get('created', '')[:16].replace('T', ' ')}\nNote: {manifest.get('note') or '-'}\n\n"
                       "Everything entered after this backup will be removed from the live data. A safety copy of "
                       "the current data is made first. Other users must close the program.\n\nRestore now?",
                       yes="Restore", danger=True):
            return
        safety = run(self, self.app.services.backup.restore, path)
        if safety:
            self.app.restart_required = True
            info(self, "Restore complete", f"The backup has been restored. A safety copy of the previous data was "
                                           f"saved as:\n{safety.name}\n\nThe program will now restart.")
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()

    # -- data check -----------------------------------------------------------
    def _integrity(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("Checks the database file, links between records, that every bin, product and warehouse "
                            "quantity matches the movement history, that every document and reversal is linked to "
                            "its movements, and that every attached file is present.", "Muted", wrap=True))
        self.check_out = QPlainTextEdit()
        self.check_out.setReadOnly(True)
        lay.addWidget(self.check_out, 1)
        btns = [button("Run data check", self._run_check, primary=True), None]
        if self.app.can(P.SETTINGS_MANAGE):
            btns.append(button("Rebuild stock levels from history...", self._rebuild, danger=True))
        lay.addLayout(h(*btns))
        return w

    def _run_check(self):
        rep = run(self, self.app.services.backup.integrity_check)
        if rep is None:
            return
        lines = [f"Database file: {'OK' if rep.database_ok else 'PROBLEM'}"]
        if not rep.database_ok:
            lines += [f"   {m}" for m in rep.database_messages[:20]]
        lines.append(f"Broken links between records: {rep.foreign_key_problems}")
        lines.append(f"Bin quantities not matching movement history: {len(rep.stock_discrepancies)}")
        for d in rep.stock_discrepancies[:50]:
            lines.append(f"   {d.product_code} in {d.bin_code}: history {fmt_qty(d.ledger_qty)}, "
                         f"shown {fmt_qty(d.level_qty)}")
        lines.append(f"Negative stock (after overrides): {len(rep.negative_levels)}")
        for lv in rep.negative_levels[:50]:
            lines.append(f"   {lv.product.internal_code} in {lv.bin.full_code}: {fmt_qty(lv.quantity)}")
        crit = rep.critical_ledger_issues
        lines.append(f"CRITICAL ledger problems (stock not explained by movement history): {len(crit)}")
        lines += [f"   {i.check}: {i.message}" for i in crit[:50]]
        lines.append(f"Missing attachment files: {len(rep.missing_files)}")
        lines += [f"   {n}" for n in rep.missing_files[:50]]
        lines.append("")
        lines.append("RESULT: everything is consistent." if rep.ok else
                     "RESULT: problems found - see above. Restore a backup or use 'Rebuild stock levels' "
                     "if only bin quantities are wrong.")
        self.check_out.setPlainText("\n".join(lines))

    def _rebuild(self):
        if not confirm(self, "Rebuild stock levels",
                       "Recalculate every bin quantity from the full movement history? Use this only if the data "
                       "check reports mismatches. Movement history itself is not changed.", yes="Rebuild"):
            return
        n = run(self, self.app.services.backup.rebuild_stock_levels)
        if n is not None:
            self.app.changed()
            info(self, "Rebuild complete", f"Stock levels recalculated ({n} bin quantities corrected).")
            self._run_check()

    # -- about ------------------------------------------------------------------
    def _about(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label(f"Store WMS  v{__version__}", "PageTitle"))
        lay.addWidget(label("Warehouse management for furniture manufacturing: materials, deliveries, issues to "
                            "site, finished products, full movement history and audit trail.", "Muted", wrap=True))
        lay.addWidget(label("Keyboard: Ctrl+K search everything · F5 refresh · Ctrl+N new · Ctrl+S post / save",
                            "Muted", wrap=True))
        lay.addStretch(1)
        return w


def _open_folder(path: Path):
    Path(path).mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
