"""Import materials (and their photos, and optionally a project material list) from Excel, step by step:

file -> worksheet -> columns -> preview and checks -> resolve suppliers / projects -> import -> report.

Nothing is written until the person presses Import, and the import takes a backup first and runs
as one transaction on a worker thread (the window stays responsive; Cancel rolls everything back).
Importing NEVER changes stock: a project's Required Quantity is a plan, not a movement.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import ItemType
from wms.core.exceptions import ValidationError
from wms.services.excel_import import (
    ACTION_LABELS,
    COMPARE_LABELS,
    CREATE,
    DUPLICATE,
    ERROR,
    FIELDS,
    MATCH,
    MATCHED,
    NEW,
    PROJECT_ACTIONS,
    REMOVED,
    SKIP,
    SKIPPED,
    SUPPLIER_ACTIONS,
    UPDATE,
    ImportCancelled,
    ImportOptions,
    copy_template,
    export_error_report,
    guess_mapping,
)
from wms.ui.widgets import tasks
from wms.ui.widgets.common import PickCombo, button, confirm, h, info, label, run, show_error

FILE, SHEET, COLUMNS, PREVIEW, RESOLVE, COMPARE, REPORT = range(7)
STEPS = ["1. File", "2. Sheet", "3. Columns", "4. Preview and matching", "5. Resolve", "6. Compare", "7. Report"]
NOT_IN_FILE = "- not in the file -"
IGNORE_COLUMN = "- ignore -"
ERROR_BG = QBrush(QColor("#FDE2E1"))
WARN_BG = QBrush(QColor("#FFF4D6"))
STATUS_BG = {NEW: QBrush(QColor("#ECFDF3")), UPDATE: QBrush(QColor("#EFF8FF")), DUPLICATE: WARN_BG,
             ERROR: ERROR_BG, SKIPPED: QBrush(QColor("#F2F4F7")), MATCHED: QBrush(QColor("#EFF8FF"))}
CHANGE_BG = {"ADDED": QBrush(QColor("#ECFDF3")), "REMOVED": ERROR_BG, "QTY": WARN_BG, "DETAIL": WARN_BG,
             "UNCHANGED": QBrush()}
PROJECT_FIELDS = ("project", "required_qty")


class ExcelImportDialog(QDialog):
    """``project_id``: None = Import Materials; 0 = Import Project Material List (choose the project);
    a project id = import that project's material list."""

    def __init__(self, app, parent=None, project_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self.svc = app.services.excel_import
        self.project_mode = project_id is not None
        self.info = None  # WorkbookInfo
        self.sheet_info = None  # SheetInfo of the chosen sheet / header row
        self.plan = None
        self.result = None
        # spec 28: say exactly what this import changes - two different operations
        site = app.services.sites.get(project_id) if project_id else None
        self.project_name = site.name if site else ""
        if self.project_mode:
            self.setWindowTitle(f"Import {site.name} Project Material Requirements" if site
                                else "Import Project Material Requirements")
        else:
            self.setWindowTitle("Import Material Master Data")
        self.resize(1180, 760)
        lay = QVBoxLayout(self)
        self.step_label = label("", "SectionTitle")
        lay.addWidget(self.step_label)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.stack.addWidget(self._file_page(project_id))
        self.stack.addWidget(self._sheet_page())
        self.stack.addWidget(self._columns_page())
        self.stack.addWidget(self._preview_page())
        self.stack.addWidget(self._resolve_page())
        self.stack.addWidget(self._compare_page())
        self.stack.addWidget(self._report_page())
        self.back_btn = button("< Back", self.back)
        self.next_btn = button("Next >", self.next, primary=True)
        self.close_btn = button("Cancel", self.reject)
        lay.addLayout(h(None, self.back_btn, self.next_btn, self.close_btn))
        self._show(FILE)

    # -- pages ------------------------------------------------------------------------------
    def _file_page(self, project_id: int | None) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        if self.project_mode:
            what = f"the {self.project_name} " if self.project_name else "a project's "
            self.purpose = label(f"Updates {what}material requirements (Required quantities on the project's "
                                 f"material list). It does NOT change stock, does NOT reserve and does NOT create "
                                 f"Goods Out - physical stock moves only through warehouse transactions.",
                                 "InfoBanner", wrap=True)
        else:
            self.purpose = label("Creates or updates material master data (code, name, product number, supplier, "
                                 "category, photo...). It does NOT change stock and does NOT touch any project.",
                                 "InfoBanner", wrap=True)
        lay.addWidget(self.purpose)
        lay.addWidget(label(
            "Choose the Excel workbook (.xlsx). It is only read - the WMS never changes it. Photos can be pictures "
            "pasted into the sheet, pictures placed in cells, file paths, hyperlinks or just file names: for file "
            "names, also choose the folder the photos are in. Importing never changes stock.", wrap=True))
        form = QFormLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("C:\\...\\Materials.xlsx")
        form.addRow("Workbook *", h(self.path, button("Browse...", self._browse_file)))
        self.photo_folder = QLineEdit()
        self.photo_folder.setPlaceholderText("Optional - the folder (and its sub-folders) with the product photos")
        form.addRow("Photo folder", h(self.photo_folder, button("Browse...", self._browse_folder)))
        self.project = PickCombo("Choose the project...")
        self.project.set_options(self.app.site_options(open_only=True))
        if project_id:
            self.project.set_value(project_id)
        if self.project_mode:
            form.addRow("Import into project *", self.project)
        lay.addLayout(form)
        if self.project_mode:
            lay.addWidget(label("Every row goes on this project's material list with its Required Quantity. "
                                "Materials that are not in the WMS yet are created first. Before anything is saved "
                                "you see the file compared with the current list (added, removed, changed).",
                                "Muted", wrap=True))
        lay.addWidget(label("Starting from scratch? Save a blank template with the right columns, fill it in, "
                            "then import it.", "Muted", wrap=True))
        lay.addLayout(h(button("Download Excel template...", self._template), None))
        lay.addStretch(1)
        return w

    def _sheet_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.sheet = QComboBox()
        self.sheet.currentIndexChanged.connect(lambda _: self._sheet_changed())
        self.header_row = QSpinBox()
        self.header_row.setRange(1, 1000)
        self.header_row.valueChanged.connect(lambda _: self._header_changed())
        form = QFormLayout()
        form.addRow("Worksheet", self.sheet)
        form.addRow("Column titles are on row", self.header_row)
        lay.addLayout(form)
        self.sheet_summary = label("", "Muted", wrap=True)
        lay.addWidget(self.sheet_summary)
        self.sample = QTableWidget()
        self.sample.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(label("First rows under the titles:"))
        lay.addWidget(self.sample, 1)
        return w

    def _columns_page(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.addWidget(label("Check which column holds what. The guesses come from the column titles (or from a "
                              "saved profile). Code and Material Name are required; choose '- not in the file -' "
                              "for anything the workbook does not have.", wrap=True))
        self.profile = QComboBox()
        self.profile.activated.connect(lambda _: self._apply_profile())
        self.profile_note = label("", "Muted")
        outer.addLayout(h(label("Mapping profile"), self.profile, button("Save as profile...", self.save_profile),
                          self.profile_note, None))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        self.map_combos: dict[str, QComboBox] = {}
        for f, (text, _words) in FIELDS.items():
            combo = QComboBox()
            self.map_combos[f] = combo
            title = text
            if f == "required_qty" and self.project_mode:
                title += " *"
            if f in PROJECT_FIELDS and f == "project":
                form.addRow(label("Project material list (optional - never changes stock)", "SectionTitle"))
            if f == "project" and self.project_mode:
                continue  # the project is chosen on the first page
            form.addRow(title, combo)
        form.addRow(label("Options", "SectionTitle"))
        self.default_unit = QComboBox()
        self.default_unit.addItem("Choose... (used when the Unit cell is empty)", None)
        for u in self.app.services.reference.units():
            self.default_unit.addItem(f"{u.code} - {u.name}", u.id)
        form.addRow("Default unit", self.default_unit)
        self.item_type = QComboBox()
        for val, text in ItemType.choices():
            self.item_type.addItem(text, val)
        form.addRow("New materials are", self.item_type)
        self.on_existing = QComboBox()
        self.on_existing.addItem("Skip them (show the differences, change nothing)", SKIP)
        self.on_existing.addItem("Update them from the file (only filled-in cells)", UPDATE)
        form.addRow("Codes already in the WMS", self.on_existing)
        self.create_suppliers = QCheckBox("Suggest creating suppliers that are not in the WMS (you confirm each one)")
        self.create_suppliers.setChecked(True)
        self.create_categories = QCheckBox("Create categories that are not in the WMS yet")
        self.create_categories.setChecked(True)
        form.addRow("", self.create_suppliers)
        form.addRow("", self.create_categories)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return w

    def _preview_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.preview_summary = label("", wrap=True)
        lay.addWidget(self.preview_summary)
        self.only_problems = QCheckBox("Show only rows with errors, duplicates or notes")
        self.only_problems.toggled.connect(lambda _: self._fill_preview())
        self.status_filter = QComboBox()
        self.status_filter.addItem("All statuses", None)
        for s in (NEW, UPDATE, MATCHED, DUPLICATE, ERROR, SKIPPED):
            self.status_filter.addItem(s, s)
        self.status_filter.currentIndexChanged.connect(lambda _: self._fill_preview())
        lay.addLayout(h(self.only_problems, self.status_filter, None,
                        button("Export problems to Excel...", self.export_errors)))
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview.setWordWrap(False)
        lay.addWidget(self.preview, 1)
        lay.addWidget(label("NEW = a material the WMS does not have.  UPDATE = an existing material changes as shown "
                            "in 'Changes'.  DUPLICATE = already in the WMS (same code), or a POSSIBLE match (same "
                            "product number or a similar code) - nothing is merged unless you choose Match, Update "
                            "or New.  MATCHED = you chose to use the existing material.  ERROR = not imported; fix "
                            "it in Excel.  Use 'Apply to similar' to give every row like it the same choice.",
                            "Muted", wrap=True))
        return w

    def _resolve_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label("These names in the file are not exactly in the WMS. Decide what each one is - nothing "
                            "is created without your choice here.", wrap=True))
        lay.addWidget(label("Suppliers", "SectionTitle"))
        self.sup_table = QTableWidget()
        lay.addWidget(self.sup_table, 1)
        lay.addWidget(label("Projects", "SectionTitle"))
        self.proj_table = QTableWidget()
        lay.addWidget(self.proj_table, 1)
        return w

    def _compare_page(self) -> QWidget:
        """Compare with Excel (spec 33): the project's list now against the file, before anything is saved."""
        w = QWidget()
        lay = QVBoxLayout(w)
        self.compare_summary = label("", wrap=True)
        lay.addWidget(self.compare_summary)
        self.compare_filter = QComboBox()
        self.compare_filter.addItem("All changes", None)
        for k, text in COMPARE_LABELS.items():
            self.compare_filter.addItem(text, k)
        self.compare_filter.currentIndexChanged.connect(lambda _: self._fill_compare_table())
        lay.addLayout(h(label("Show", "Muted"), self.compare_filter, None))
        self.compare_table = QTableWidget()
        self.compare_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.compare_table.setWordWrap(False)
        lay.addWidget(self.compare_table, 1)
        lay.addWidget(label("Ticked lines are applied when you press Import. Removed lines are NOT taken off the "
                            "project unless you tick them - a material missing from a revised spreadsheet may "
                            "simply be a mistake. Nothing here changes stock.", "Muted", wrap=True))
        self.compare_lines = []
        self.compare_ticks: dict[int, QCheckBox] = {}
        return w

    def _report_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.done_text = label("", wrap=True)
        self.done_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.done_text)
        self.report_errors = button("View errors", self._view_errors)
        self.report_export = button("Export error report...", self.export_errors)
        lay.addLayout(h(self.report_errors, self.report_export, None))
        lay.addStretch(1)
        return w

    # -- navigation ---------------------------------------------------------------------------
    def _needs_resolve(self) -> bool:
        p = self.plan
        return p is not None and bool(p.suppliers or p.projects)

    def _needs_compare(self) -> bool:
        return self.project_mode and (self.plan is None or bool(self.plan.options.project_id))

    def _show(self, i: int) -> None:
        self.stack.setCurrentIndex(i)
        visible = [n for n in range(len(STEPS)) if (n != RESOLVE or self._needs_resolve() or i == RESOLVE)
                   and (n != COMPARE or self._needs_compare())]
        self.step_label.setText("   ".join(f"[{STEPS[n]}]" if n == i else STEPS[n] for n in visible))
        self.back_btn.setEnabled(0 < i < REPORT)
        self.next_btn.setVisible(i < REPORT)
        last = i == COMPARE or (not self._needs_compare()
                                and (i == RESOLVE or (i == PREVIEW and not self._needs_resolve())))
        self.next_btn.setText("Import" if last else "Next >")
        self.close_btn.setText("Done" if i == REPORT else "Cancel")

    def back(self) -> None:
        i = self.stack.currentIndex()
        if i == RESOLVE:
            self._reprepare()
        if i == COMPARE:
            self._apply_compare_ticks()
            self._show(RESOLVE if self._needs_resolve() else PREVIEW)
            return
        self._show(max(i - 1, 0))

    def next(self) -> None:
        i = self.stack.currentIndex()
        if i == FILE and self._open_workbook():
            self._show(SHEET)
        elif i == SHEET and self.sheet_info is not None:
            self._fill_mapping()
            self._show(COLUMNS)
        elif i == COLUMNS and self._prepare():
            self._show(PREVIEW)
        elif i == PREVIEW:
            if self._needs_resolve():
                self._fill_resolve()
                self._show(RESOLVE)
            elif self._needs_compare():
                if self.fill_compare():
                    self._show(COMPARE)
            elif self._import():
                self._show(REPORT)
        elif i == RESOLVE:
            self._reprepare()
            if self._needs_compare():
                if self.fill_compare():
                    self._show(COMPARE)
            elif self._import():
                self._show(REPORT)
        elif i == COMPARE:
            self._apply_compare_ticks()
            if self._import():
                self._show(REPORT)

    # -- step 1 -------------------------------------------------------------------------------
    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Excel workbook", str(Path.home()),
                                              "Excel workbook (*.xlsx *.xlsm)")
        if path:
            self.path.setText(path)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with the product photos", str(Path.home()))
        if folder:
            self.photo_folder.setText(folder)

    def _template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Excel import template",
                                              str(Path.home() / "Documents" / "WMS_Materials_Template.xlsx"),
                                              "Excel workbook (*.xlsx)")
        if path and run(self, copy_template, Path(path)):
            self.path.setText(path)

    def _open_workbook(self) -> bool:
        if not self.path.text().strip():
            show_error(self, ValidationError("Choose the Excel workbook first."), "Workbook")
            return False
        if self.project_mode and not self.project.value():
            show_error(self, ValidationError("Choose the project to import the material list into."), "Project")
            return False
        folder = self.photo_folder.text().strip()
        if folder and not Path(folder).is_dir():
            show_error(self, ValidationError(f"Photo folder not found: {folder}"), "Photo folder")
            return False
        info_ = run(self, self.svc.inspect, self.path.text().strip())
        if info_ is None:
            return False
        self.info = info_
        self.sheet.blockSignals(True)
        self.sheet.clear()
        for s in info_.sheets:
            self.sheet.addItem(f"{s.name}   ({s.rows} rows)", s.name)
        # Start on the sheet that looks most like a materials list
        best = max(range(len(info_.sheets)), key=lambda n: (len(info_.sheets[n].guess), info_.sheets[n].rows))
        self.sheet.setCurrentIndex(best)
        self.sheet.blockSignals(False)
        self._sheet_changed()
        return True

    # -- step 2 -------------------------------------------------------------------------------
    def _sheet_changed(self) -> None:
        if self.info is None or self.sheet.currentData() is None:
            return
        s = self.info.sheet(self.sheet.currentData())
        self.sheet_info = s
        self.header_row.blockSignals(True)
        self.header_row.setValue(s.header_row)
        self.header_row.blockSignals(False)
        self.sheet_summary.setText(f"About {s.rows} row(s) under the titles.   Photos: {s.photo_summary}.")
        self._fill_sample()

    def _header_changed(self) -> None:
        if self.sheet_info is None:
            return
        photos = self.sheet_info
        s = run(self, self.svc.read_sheet, self.info.path, photos.name, self.header_row.value())
        if s is None:
            return
        for attr in ("floating_images", "cell_images", "hyperlinks", "paths", "filenames", "web_links"):
            setattr(s, attr, getattr(photos, attr))
        prof = self.svc.match_profile(s.headers)
        if prof is not None:
            s.guess, s.profile = prof[1], prof[0]
        self.sheet_info = s
        self._fill_sample()

    def _fill_sample(self) -> None:
        s = self.sheet_info
        self.sample.clear()
        self.sample.setColumnCount(len(s.headers))
        self.sample.setHorizontalHeaderLabels(s.headers)
        self.sample.setRowCount(len(s.samples))
        for r, row in enumerate(s.samples):
            for c, v in enumerate(row[:len(s.headers)]):
                self.sample.setItem(r, c, QTableWidgetItem(v))
        self.sample.resizeColumnsToContents()

    # -- step 3 -------------------------------------------------------------------------------
    def _fill_profiles(self) -> None:
        self.profile.blockSignals(True)
        self.profile.clear()
        self.profile.addItem("Recognised from the column titles", None)
        for pid, name, _m in self.svc.profiles():
            self.profile.addItem(name, pid)
        if self.sheet_info is not None and self.sheet_info.profile:
            i = self.profile.findText(self.sheet_info.profile)
            if i >= 0:
                self.profile.setCurrentIndex(i)
        self.profile.blockSignals(False)
        self.profile_note.setText(f"Profile '{self.sheet_info.profile}' recognised for these columns."
                                  if self.sheet_info is not None and self.sheet_info.profile else "")

    def _fill_mapping(self, mapping: dict[str, int] | None = None) -> None:
        s = self.sheet_info
        guess = s.guess if mapping is None else mapping
        for f, combo in self.map_combos.items():
            combo.clear()
            combo.addItem(NOT_IN_FILE, None)
            for i, title in enumerate(s.headers):
                combo.addItem(title, i)
            if f in guess:
                combo.setCurrentIndex(guess[f] + 1)
        if mapping is None:
            self._fill_profiles()

    def _apply_profile(self) -> None:
        pid = self.profile.currentData()
        if pid is None:
            self._fill_mapping(guess_mapping(self.sheet_info.headers))
            self.profile_note.setText("")
            return
        mapping = run(self, self.svc.apply_profile, pid, self.sheet_info.headers)
        if mapping is not None:
            self._fill_mapping(mapping)
            self.profile_note.setText(f"Profile '{self.profile.currentText()}' applied.")

    def save_profile(self, name: str | None = None) -> bool:
        if name is None:
            name, ok = QInputDialog.getText(self, "Save mapping profile",
                                            "Name for this column mapping (e.g. Egger price list):")
            if not ok or not name.strip():
                return False
        mapping = {f: c.currentData() for f, c in self.map_combos.items() if c.currentData() is not None}
        if run(self, self.svc.save_profile, name, self.sheet_info.headers, mapping) is None:
            return False
        self._fill_profiles()
        i = self.profile.findText(name.strip())
        if i >= 0:
            self.profile.setCurrentIndex(i)
        self.profile_note.setText(f"Saved. Files with these column titles will use '{name.strip()}' automatically.")
        return True

    def options(self) -> ImportOptions:
        mapping = {f: c.currentData() for f, c in self.map_combos.items() if c.currentData() is not None}
        if self.project_mode:
            mapping.pop("project", None)
        folder = self.photo_folder.text().strip()
        return ImportOptions(
            sheet=self.sheet_info.name, header_row=self.header_row.value(), mapping=mapping,
            photo_folder=Path(folder) if folder else None, default_unit_id=self.default_unit.currentData(),
            item_type=self.item_type.currentData(), on_existing=self.on_existing.currentData(),
            create_suppliers=self.create_suppliers.isChecked(), create_categories=self.create_categories.isChecked(),
            project_id=self.project.value() if self.project_mode else None)

    def _prepare(self) -> bool:
        o = self.options()
        used = list(o.mapping.values())
        if len(used) != len(set(used)):
            show_error(self, ValidationError("The same column is chosen for two different things. Each column can "
                                             "be used once."), "Columns")
            return False
        plan = run(self, self.svc.prepare, self.info.path, o)
        if plan is None:
            return False
        self.plan = plan
        self.only_problems.setChecked(False)
        self._fill_preview()
        return True

    def _reprepare(self) -> None:
        """Check the rows again with the person's supplier / project choices (and row choices kept)."""
        if self.plan is None:
            return
        plan = run(self, self.svc.prepare, self.plan.source, self.plan.options, self.plan)
        if plan is not None:
            self.plan = plan
            self._fill_preview()

    # -- step 4 -------------------------------------------------------------------------------
    def shown_rows(self) -> list:
        want = self.status_filter.currentData()
        return [r for r in self.plan.rows
                if (not self.only_problems.isChecked() or r.errors or r.warnings or r.status == DUPLICATE)
                and (want is None or r.status == want)]

    def _fill_preview(self) -> None:
        if self.plan is None:
            return
        self.preview_summary.setText(self.plan.summary()
                                     + (f"\nInto project {self.plan.project_label}." if self.plan.project_label else ""))
        rows = self.shown_rows()
        heads = ["Row", "Status", "Action", "", "Code", "Material", "Product Number", "Supplier", "Unit", "Photo",
                 "Required", "Existing material", "Changes", "Errors / notes"]
        self.preview.clear()
        self.preview.setColumnCount(len(heads))
        self.preview.setHorizontalHeaderLabels(heads)
        self.preview.setRowCount(len(rows))
        for n, r in enumerate(rows):
            v = r.values
            req = "" if r.required is None else format(r.required.normalize(), "f")
            cells = [str(r.row), r.status, "", "", r.code, v.get("name", ""), v.get("supplier_code", ""),
                     v.get("supplier", ""), v.get("unit", ""), r.photo.label if r.photo else "", req,
                     r.existing_label, r.changes_text, r.problems]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setBackground(STATUS_BG[r.status] if c == 1 else
                                   (ERROR_BG if r.errors else (WARN_BG if r.warnings else QBrush())))
                if c == 12 and r.changes:
                    item.setToolTip("\n".join(f"{f}: {old or '(empty)'} → {new}" for f, old, new in r.changes))
                self.preview.setItem(n, c, item)
            self.preview.setCellWidget(n, 2, self._action_combo(r))
            if len(self.plan.choices(r)) > 1:
                self.preview.setCellWidget(n, 3, button("Apply to similar", lambda row=r: self.apply_similar(row)))
        self.preview.resizeColumnsToContents()
        for c in (5, 12, 13):
            self.preview.setColumnWidth(c, min(max(self.preview.columnWidth(c), 120), 320))
        self.preview.horizontalHeader().setSectionResizeMode(len(heads) - 1, QHeaderView.ResizeMode.Stretch)

    def _action_combo(self, r) -> QWidget:
        if not r.ok:
            return label("  Not imported", "Muted")
        choices = self.plan.choices(r)
        if choices == [SKIP]:
            return label("  Skipped", "Muted")
        combo = QComboBox()
        for a in choices:
            text = ACTION_LABELS[a]
            if a == CREATE and r.match_kind in ("PRODUCT_NO", "SIMILAR_CODE"):
                text = "New anyway"
            combo.addItem(text, a)
        combo.setCurrentIndex(max(combo.findData(r.action), 0))

        def changed(_=None, row=r, box=combo):
            row.action = box.currentData()
            self.preview_summary.setText(self.plan.summary())
            item = self.preview.item(self.shown_rows().index(row), 1) if row in self.shown_rows() else None
            if item is not None:
                item.setText(row.status)
                item.setBackground(STATUS_BG[row.status])

        combo.currentIndexChanged.connect(changed)
        return combo

    def set_action(self, excel_row: int, action: str) -> None:
        """Choose the action for one row (as the person would in the Action column)."""
        r = next(x for x in self.plan.rows if x.row == excel_row)
        if action not in self.plan.choices(r):
            raise ValidationError(f"Row {excel_row} cannot be set to {action}.")
        r.action = action
        self._fill_preview()

    def apply_similar(self, r) -> int:
        n = self.plan.apply_to_similar(r, r.action)
        self._fill_preview()
        return n

    def export_errors(self, path: str | None = None):
        if self.plan is None:
            return None
        if path is None:
            default = Path.home() / "Documents" / f"Import_problems_{self.plan.source.stem}.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, "Export import problems", str(default),
                                                  "Excel workbook (*.xlsx)")
            if not path:
                return None
        return run(self, export_error_report, self.plan, Path(path))

    # -- step 5 -------------------------------------------------------------------------------
    def _fill_resolve(self) -> None:
        p = self.plan
        sups = self.app.supplier_options()
        self.sup_table.clear()
        self.sup_table.setColumnCount(4)
        self.sup_table.setHorizontalHeaderLabels(["In the file", "Rows", "What to do", "WMS supplier"])
        self.sup_table.setRowCount(len(p.suppliers))
        self.sup_widgets = {}
        for n, (key, c) in enumerate(sorted(p.suppliers.items(), key=lambda kv: kv[1].text.lower())):
            self.sup_table.setItem(n, 0, QTableWidgetItem(c.text))
            self.sup_table.setItem(n, 1, QTableWidgetItem(str(c.rows)))
            act = QComboBox()
            for a, text in SUPPLIER_ACTIONS.items():
                act.addItem(text, a)
            act.setCurrentIndex(act.findData(c.action))
            pick = PickCombo("Choose supplier...")
            pick.set_options(sups)
            pick.set_value(c.match_id)
            pick.setEnabled(c.action == MATCH)
            act.currentIndexChanged.connect(lambda _=0, k=key: self._supplier_choice(k))
            pick.picked.connect(lambda _=0, k=key: self._supplier_choice(k))
            self.sup_table.setCellWidget(n, 2, act)
            self.sup_table.setCellWidget(n, 3, pick)
            self.sup_widgets[key] = (act, pick)
        self.sup_table.resizeColumnsToContents()
        self.sup_table.horizontalHeader().setStretchLastSection(True)
        sites = self.app.site_options(open_only=True)
        self.proj_table.clear()
        self.proj_table.setColumnCount(4)
        self.proj_table.setHorizontalHeaderLabels(["In the file", "Rows", "What to do", "WMS project"])
        self.proj_table.setRowCount(len(p.projects))
        self.proj_widgets = {}
        for n, (key, c) in enumerate(sorted(p.projects.items(), key=lambda kv: kv[1].text.lower())):
            self.proj_table.setItem(n, 0, QTableWidgetItem(c.text + ("  (closed project)" if c.closed else "")))
            self.proj_table.setItem(n, 1, QTableWidgetItem(str(c.rows)))
            act = QComboBox()
            for a, text in PROJECT_ACTIONS.items():
                act.addItem(text, a)
            act.setCurrentIndex(act.findData(c.action))
            pick = PickCombo("Choose project...")
            pick.set_options(sites)
            pick.set_value(c.match_id if not c.closed else None)
            pick.setEnabled(c.action == MATCH)
            act.currentIndexChanged.connect(lambda _=0, k=key: self._project_choice(k))
            pick.picked.connect(lambda _=0, k=key: self._project_choice(k))
            self.proj_table.setCellWidget(n, 2, act)
            self.proj_table.setCellWidget(n, 3, pick)
            self.proj_widgets[key] = (act, pick)
        self.proj_table.resizeColumnsToContents()
        self.proj_table.horizontalHeader().setStretchLastSection(True)

    def _supplier_choice(self, key: str) -> None:
        act, pick = self.sup_widgets[key]
        c = self.plan.suppliers[key]
        c.action = act.currentData()
        pick.setEnabled(c.action == MATCH)
        c.match_id = pick.value() if c.action == MATCH else None
        c.match_name = pick.currentText().split(" (")[0] if c.match_id else ""
        c.suggested = False

    def _project_choice(self, key: str) -> None:
        act, pick = self.proj_widgets[key]
        c = self.plan.projects[key]
        c.action = act.currentData()
        pick.setEnabled(c.action == MATCH)
        c.match_id = pick.value() if c.action == MATCH else None
        c.match_label = pick.currentText() if c.match_id else ""
        c.closed = False if c.match_id else c.closed

    def set_supplier(self, text: str, action: str, match_id: int | None = None) -> None:
        """Resolve one supplier name (as the person would on the Resolve step)."""
        from wms.services.excel_import import supplier_key
        act, pick = self.sup_widgets[supplier_key(text)]
        pick.set_value(match_id)
        act.setCurrentIndex(act.findData(action))
        self._supplier_choice(supplier_key(text))

    def set_project(self, text: str, action: str, match_id: int | None = None) -> None:
        act, pick = self.proj_widgets[text.strip().upper()]
        pick.set_value(match_id)
        act.setCurrentIndex(act.findData(action))
        self._project_choice(text.strip().upper())

    def _check_choices(self) -> bool:
        for c in self.plan.suppliers.values():
            if c.action == MATCH and not c.match_id:
                show_error(self, ValidationError(f"Choose the WMS supplier for '{c.text}', or Create / Ignore it."),
                           "Suppliers")
                return False
        for c in self.plan.projects.values():
            if c.action == MATCH and not c.match_id:
                show_error(self, ValidationError(f"Choose the WMS project for '{c.text}', or pick another option."),
                           "Projects")
                return False
        return True

    # -- step 6: compare with the current project list -----------------------------------------
    def fill_compare(self) -> bool:
        self.plan.excluded = set()
        self.plan.remove_ids = []
        lines = run(self, self.svc.compare_project, self.plan)
        if lines is None:
            return False
        self.compare_lines = lines
        self.compare_ticks = {}
        for n, c in enumerate(lines):
            box = QCheckBox()
            # Added / changed lines apply unless unticked; removals only when ticked (spec 33)
            box.setChecked(c.change not in (REMOVED, "UNCHANGED"))
            box.setEnabled(c.change != "UNCHANGED")
            box.toggled.connect(lambda _=False: self._compare_counts())
            self.compare_ticks[n] = box
        self._fill_compare_table()
        return True

    def _fill_compare_table(self) -> None:
        want = self.compare_filter.currentData()
        shown = [(n, c) for n, c in enumerate(self.compare_lines) if want is None or c.change == want]
        heads = ["Apply", "Change", "Code", "Material", "Current", "New file", "Difference", "Details"]
        t = self.compare_table
        for r in range(t.rowCount()):  # keep the check boxes alive: they are reused
            w = t.cellWidget(r, 0)
            if w is not None:
                w.setParent(None)
        t.clear()
        t.setColumnCount(len(heads))
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(len(shown))

        def qty(v):
            return "" if v is None else format(v.normalize(), "f")

        for row, (n, c) in enumerate(shown):
            diff = c.difference
            cells = ["", c.label, c.code, c.name, qty(c.current), qty(c.new),
                     "" if diff is None or diff == 0 else (f"+{qty(diff)}" if diff > 0 else qty(diff)), c.details]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setBackground(CHANGE_BG[c.change])
                if text and len(text) > 20:
                    item.setToolTip(text)
                t.setItem(row, col, item)
            t.setCellWidget(row, 0, self.compare_ticks[n])
        t.resizeColumnsToContents()
        t.setColumnWidth(3, min(max(t.columnWidth(3), 160), 320))
        t.horizontalHeader().setSectionResizeMode(len(heads) - 1, QHeaderView.ResizeMode.Stretch)
        self._compare_counts()

    def _compare_counts(self) -> None:
        counts = {k: 0 for k in COMPARE_LABELS}
        for c in self.compare_lines:
            counts[c.change] += 1
        ticked = sum(1 for n, c in enumerate(self.compare_lines)
                     if c.change != "UNCHANGED" and self.compare_ticks[n].isChecked())
        self.compare_summary.setText(
            f"Project {self.plan.project_label}: " + ",  ".join(f"{COMPARE_LABELS[k]} {v}" for k, v in counts.items())
            + f".   {ticked} change(s) ticked to apply. Stock is not changed.")

    def set_compare(self, change_code: str, apply: bool) -> None:
        """Tick / untick the compare line for a material code (as the person would)."""
        n = next(i for i, c in enumerate(self.compare_lines) if c.code.upper() == change_code.upper())
        self.compare_ticks[n].setChecked(apply)

    def _apply_compare_ticks(self) -> None:
        if self.plan is None or not self.compare_lines:
            return
        self.plan.excluded = {c.excel_row for n, c in enumerate(self.compare_lines)
                              if c.excel_row is not None and c.change not in (REMOVED, "UNCHANGED")
                              and not self.compare_ticks[n].isChecked()}
        self.plan.remove_ids = [c.product_id for n, c in enumerate(self.compare_lines)
                                if c.change == REMOVED and self.compare_ticks[n].isChecked()]

    # -- import + report ----------------------------------------------------------------------
    def _import(self) -> bool:
        if not self._check_choices():
            return False
        c = self.plan.counts()
        if not c["create"] and not c["update"] and not c["project_lines"] and not c["remove"]:
            show_error(self, ValidationError("Nothing to import: every row is skipped, a duplicate or has errors."),
                       "Import")
            return False
        new_sup = [x.text for x in self.plan.suppliers.values() if x.action == CREATE]
        new_proj = [x.text for x in self.plan.projects.values() if x.action == CREATE]
        text = (f"Import now?\n\n{c['create']} new material(s), {c['update']} update(s), {c['photos']} photo(s).\n"
                f"{c['duplicate']} duplicate(s) and {c['skip']} row(s) skipped - nothing is overwritten for them.")
        if c["project_lines"]:
            text += f"\n{c['project_lines']} project material line(s) (Required Quantity - stock is not changed)."
        if c["remove"]:
            text += (f"\n{c['remove']} material(s) taken OFF the project list (not in the revised file). "
                     f"Reservations and issued history stay.")
        if new_sup:
            text += "\n\nNew suppliers: " + ", ".join(sorted(new_sup))
        if new_proj:
            text += "\nNew projects: " + ", ".join(sorted(new_proj))
        if c["errors"]:
            text += f"\n\n{c['errors']} row(s) with errors will NOT be imported."
        text += ("\n\nA backup of the WMS is taken first. If anything goes wrong, nothing is imported "
                 "(all or nothing).")
        if not confirm(self, "Import from Excel", text, yes="Import"):
            return False
        plan = self.plan
        try:
            result = tasks.with_progress(self, "Import from Excel",
                                         lambda progress: self.svc.run(plan, progress), "Importing rows...")
        except ImportCancelled as exc:
            info(self, "Import from Excel", exc.message)
            return False
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return False
        self.result = result
        lines = [f"Pre-import backup successfully created: {result.backup.name}", "",
                 f"Total rows: {result.total_rows}",
                 f"New materials: {result.created}",
                 f"Updated: {result.updated}",
                 f"Duplicates (not imported): {result.duplicates}",
                 f"Skipped: {result.skipped}",
                 f"Errors (not imported): {result.failed_rows}",
                 f"Photos imported: {result.photos}   ·   Photos missing: {result.photos_missing}"]
        if result.project_lines:
            lines.append(f"Project material lines: {result.project_lines} (stock not changed)")
        if result.project_lines_removed:
            lines.append(f"Taken off the project list: {result.project_lines_removed}")
        if result.suppliers_created:
            lines.append("New suppliers: " + ", ".join(result.suppliers_created))
        if result.projects_created:
            lines.append("New projects: " + ", ".join(result.projects_created))
        if result.categories_created:
            lines.append("New categories: " + ", ".join(result.categories_created))
        if result.photo_problems:
            lines += ["", "Photos that could not be copied:", *result.photo_problems]
        lines += ["", "The workbook and the photo folder are no longer needed: photos were copied into the WMS."]
        self.done_text.setText("\n".join(lines))
        has_problems = bool(result.failed_rows or result.duplicates or result.photos_missing)
        self.report_errors.setVisible(has_problems)
        self.report_export.setVisible(has_problems)
        self.app.changed()
        return True

    def _view_errors(self) -> None:
        self.only_problems.setChecked(True)
        self.stack.setCurrentIndex(PREVIEW)
        self._show(PREVIEW)
        self.next_btn.setVisible(False)
        self.back_btn.setEnabled(False)
