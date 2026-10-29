"""Import products (and their photos) from an Excel workbook, step by step:
choose the file -> choose the worksheet -> match the columns -> check the preview -> import.

Nothing is written until the last step, and the last step takes a backup first."""

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
    CREATE,
    FIELDS,
    SKIP,
    UPDATE,
    ImportOptions,
    copy_template,
)
from wms.ui.widgets.common import button, confirm, h, label, run, show_error

STEPS = ["1. Workbook", "2. Worksheet", "3. Columns", "4. Preview and checks", "5. Done"]
NOT_IN_FILE = "- not in the file -"
ERROR_BG = QBrush(QColor("#FDE2E1"))
WARN_BG = QBrush(QColor("#FFF4D6"))


class ExcelImportDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.svc = app.services.excel_import
        self.info = None  # WorkbookInfo
        self.sheet_info = None  # SheetInfo of the chosen sheet / header row
        self.plan = None
        self.result = None
        self.setWindowTitle("Import products from Excel")
        self.resize(1100, 720)
        lay = QVBoxLayout(self)
        self.step_label = label("", "SectionTitle")
        lay.addWidget(self.step_label)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.stack.addWidget(self._file_page())
        self.stack.addWidget(self._sheet_page())
        self.stack.addWidget(self._columns_page())
        self.stack.addWidget(self._preview_page())
        self.stack.addWidget(self._done_page())
        self.back_btn = button("< Back", self.back)
        self.next_btn = button("Next >", self.next, primary=True)
        self.close_btn = button("Cancel", self.reject)
        lay.addLayout(h(None, self.back_btn, self.next_btn, self.close_btn))
        self._show(0)

    # -- pages ------------------------------------------------------------------------------
    def _file_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(label(
            "Choose the Excel workbook with your materials list (.xlsx). It is only read - the WMS never changes "
            "it. Photos can be pictures pasted into the sheet, pictures placed in cells, file paths, hyperlinks "
            "or just file names: for file names, also choose the folder the photos are in.", wrap=True))
        form = QFormLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("C:\\...\\Materials.xlsx")
        form.addRow("Workbook *", h(self.path, button("Browse...", self._browse_file)))
        self.photo_folder = QLineEdit()
        self.photo_folder.setPlaceholderText("Optional - the folder (and its sub-folders) with the product photos")
        form.addRow("Photo folder", h(self.photo_folder, button("Browse...", self._browse_folder)))
        lay.addLayout(form)
        lay.addWidget(label("Starting from scratch? Save a blank workbook with the right columns, fill it in, "
                            "then import it.", "Muted", wrap=True))
        lay.addLayout(h(button("Save blank template...", self._template), None))
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
        outer.addWidget(label("Check which column holds what. The guesses come from the column titles. "
                              "Code and Name are required.", wrap=True))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        self.map_combos: dict[str, QComboBox] = {}
        for f, (text, _words) in FIELDS.items():
            combo = QComboBox()
            self.map_combos[f] = combo
            form.addRow(text, combo)
        form.addRow(label("Options", "SectionTitle"))
        self.default_unit = QComboBox()
        self.default_unit.addItem("Choose... (used when the Unit cell is empty)", None)
        for u in self.app.services.reference.units():
            self.default_unit.addItem(f"{u.code} - {u.name}", u.id)
        form.addRow("Default unit", self.default_unit)
        self.item_type = QComboBox()
        for val, text in ItemType.choices():
            self.item_type.addItem(text, val)
        form.addRow("New products are", self.item_type)
        self.on_existing = QComboBox()
        self.on_existing.addItem("Skip them (leave the WMS as it is)", SKIP)
        self.on_existing.addItem("Update them from the file (only filled-in cells)", UPDATE)
        form.addRow("Codes already in the WMS", self.on_existing)
        self.create_suppliers = QCheckBox("Create suppliers that are not in the WMS yet")
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
        self.only_problems = QCheckBox("Show only rows with errors or warnings")
        self.only_problems.toggled.connect(lambda _: self._fill_preview())
        lay.addLayout(h(self.only_problems, None))
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.preview, 1)
        lay.addWidget(label("Red rows have errors and are NOT imported - fix them in Excel and import again, or "
                            "import the rest now. Yellow rows are imported, but read the note. Use the Action "
                            "column to skip a row or to update a product that already exists.", "Muted", wrap=True))
        return w

    def _done_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.done_text = label("", wrap=True)
        self.done_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.done_text)
        lay.addStretch(1)
        return w

    # -- navigation ---------------------------------------------------------------------------
    def _show(self, i: int) -> None:
        self.stack.setCurrentIndex(i)
        self.step_label.setText("   ".join(f"[{s}]" if n == i else s for n, s in enumerate(STEPS)))
        self.back_btn.setEnabled(0 < i < 4)
        self.next_btn.setVisible(i < 4)
        self.next_btn.setText("Import" if i == 3 else "Next >")
        self.close_btn.setText("Close" if i == 4 else "Cancel")

    def back(self) -> None:
        self._show(max(self.stack.currentIndex() - 1, 0))

    def next(self) -> None:
        i = self.stack.currentIndex()
        if i == 0 and self._open_workbook():
            self._show(1)
        elif i == 1 and self.sheet_info is not None:
            self._fill_mapping()
            self._show(2)
        elif i == 2 and self._prepare():
            self._show(3)
        elif i == 3 and self._import():
            self._show(4)

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
        path, _ = QFileDialog.getSaveFileName(self, "Blank import template",
                                              str(Path.home() / "Documents" / "WMS_product_import.xlsx"),
                                              "Excel workbook (*.xlsx)")
        if path and run(self, copy_template, Path(path)):
            self.path.setText(path)

    def _open_workbook(self) -> bool:
        if not self.path.text().strip():
            show_error(self, ValidationError("Choose the Excel workbook first."), "Workbook")
            return False
        folder = self.photo_folder.text().strip()
        if folder and not Path(folder).is_dir():
            show_error(self, ValidationError(f"Photo folder not found: {folder}"), "Photo folder")
            return False
        info = run(self, self.svc.inspect, self.path.text().strip())
        if info is None:
            return False
        self.info = info
        self.sheet.blockSignals(True)
        self.sheet.clear()
        for s in info.sheets:
            self.sheet.addItem(f"{s.name}   ({s.rows} rows)", s.name)
        # Start on the sheet that looks most like a product list
        best = max(range(len(info.sheets)), key=lambda n: (len(info.sheets[n].guess), info.sheets[n].rows))
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
    def _fill_mapping(self) -> None:
        s = self.sheet_info
        for f, combo in self.map_combos.items():
            combo.clear()
            combo.addItem(NOT_IN_FILE, None)
            for i, title in enumerate(s.headers):
                combo.addItem(title, i)
            if f in s.guess:
                combo.setCurrentIndex(s.guess[f] + 1)

    def options(self) -> ImportOptions:
        mapping = {f: c.currentData() for f, c in self.map_combos.items() if c.currentData() is not None}
        folder = self.photo_folder.text().strip()
        return ImportOptions(
            sheet=self.sheet_info.name, header_row=self.header_row.value(), mapping=mapping,
            photo_folder=Path(folder) if folder else None, default_unit_id=self.default_unit.currentData(),
            item_type=self.item_type.currentData(), on_existing=self.on_existing.currentData(),
            create_suppliers=self.create_suppliers.isChecked(), create_categories=self.create_categories.isChecked())

    def _prepare(self) -> bool:
        o = self.options()
        used = [c for c in o.mapping.values()]
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

    # -- step 4 -------------------------------------------------------------------------------
    def _fill_preview(self) -> None:
        if self.plan is None:
            return
        self.preview_summary.setText(self.plan.summary())
        rows = [r for r in self.plan.rows if not self.only_problems.isChecked() or r.errors or r.warnings]
        heads = ["Row", "Action", "Code", "Name", "Product Number", "Supplier", "Unit", "Photo",
                 "Existing product", "Errors / notes"]
        self.preview.clear()
        self.preview.setColumnCount(len(heads))
        self.preview.setHorizontalHeaderLabels(heads)
        self.preview.setRowCount(len(rows))
        for n, r in enumerate(rows):
            v = r.values
            cells = [str(r.row), "", r.code, v.get("name", ""), v.get("supplier_code", ""), v.get("supplier", ""),
                     v.get("unit", ""), r.photo.label if r.photo else "", r.existing_label, r.problems]
            bg = ERROR_BG if r.errors else (WARN_BG if r.warnings else None)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if bg is not None:
                    item.setBackground(bg)
                self.preview.setItem(n, c, item)
            self.preview.setCellWidget(n, 1, self._action_combo(r))
        hdr = self.preview.horizontalHeader()
        self.preview.resizeColumnsToContents()
        hdr.setSectionResizeMode(len(heads) - 1, QHeaderView.ResizeMode.Stretch)

    def _action_combo(self, r) -> QWidget:
        if r.errors:
            return label("  Not imported", "Muted")
        combo = QComboBox()
        choices = [UPDATE, SKIP] if r.existing_id is not None else [CREATE, SKIP]
        if r.existing_id is not None and r.action == SKIP and "removed from active use" in r.problems:
            choices = [SKIP]
        for a in choices:
            combo.addItem(ACTION_LABELS[a], a)
        combo.setCurrentIndex(max(combo.findData(r.action), 0))

        def changed(_=None, row=r, box=combo):
            row.action = box.currentData()
            self.preview_summary.setText(self.plan.summary())

        combo.currentIndexChanged.connect(changed)
        return combo

    # -- step 5 -------------------------------------------------------------------------------
    def _import(self) -> bool:
        c = self.plan.counts()
        if not c["create"] and not c["update"]:
            show_error(self, ValidationError("Nothing to import: every row is skipped or has errors."), "Import")
            return False
        text = (f"Import now?\n\n{c['create']} new product(s), {c['update']} update(s), {c['photos']} photo(s).\n"
                f"{c['skip']} row(s) skipped.")
        if c["errors"]:
            text += f"\n\n{c['errors']} row(s) with errors will NOT be imported."
        text += ("\n\nA backup of the WMS is taken first. If anything goes wrong, nothing is imported "
                 "(all or nothing).")
        if not confirm(self, "Import from Excel", text, yes="Import"):
            return False
        result = run(self, self.svc.run, self.plan)
        if result is None:
            return False
        self.result = result
        lines = [f"Pre-import backup successfully created: {result.backup.name}", "",
                 f"{result.created} product(s) created, {result.updated} updated, {result.skipped} skipped, "
                 f"{result.failed_rows} row(s) with errors not imported.",
                 f"{result.photos} photo(s) copied into the WMS (the workbook and photo folder are no longer "
                 f"needed)."]
        if result.suppliers_created:
            lines.append("New suppliers: " + ", ".join(result.suppliers_created))
        if result.categories_created:
            lines.append("New categories: " + ", ".join(result.categories_created))
        if result.photo_problems:
            lines += ["", "Photos that could not be copied:", *result.photo_problems]
        self.done_text.setText("\n".join(lines))
        self.app.changed()
        return True
