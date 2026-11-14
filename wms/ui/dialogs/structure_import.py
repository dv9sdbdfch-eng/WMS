"""Import a project's structure (Project -> Block -> Level -> Unit, and unit materials) from Excel (v1.25).

One window: choose the workbook -> Check (every row is checked, nothing is written) -> Import. The
import takes a backup first and is all or nothing; nothing in the WMS is deleted or moved by it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from wms.core.exceptions import ValidationError
from wms.services.structure_import import (
    ERROR,
    NEW,
    SAME,
    UPDATE,
    StructurePlan,
    export_problems,
    write_template,
)
from wms.ui.widgets import tasks
from wms.ui.widgets.common import button, confirm, h, info, label, run, show_error

STATUS_BG = {NEW: QBrush(QColor("#ECFDF3")), UPDATE: QBrush(QColor("#EFF8FF")), SAME: QBrush(QColor("#F2F4F7")),
             ERROR: QBrush(QColor("#FDE2E1"))}
WARN_BG = QBrush(QColor("#FFF4D6"))
UNIT_COLS = ["Row", "Status", "Project No", "Block", "Level", "Unit Code", "Unit Name", "Quantity", "Changes",
             "Errors / notes"]
MAT_COLS = ["Row", "Status", "Project No", "Block", "Level", "Unit", "Material Code", "Qty per Unit", "VPK Code",
            "Changes", "Errors / notes"]
#: filter on the preview: (label, statuses or None for "with errors / notes")
FILTERS = (("All rows", (NEW, UPDATE, SAME, ERROR)), ("Errors and notes", None), ("New", (NEW,)),
           ("To update", (UPDATE,)), ("No change", (SAME,)))


class StructureImportDialog(QDialog):

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.svc = app.services.structure_import
        self.plan: StructurePlan | None = None
        self.result = None
        self.setWindowTitle("Import Project Structure from Excel")
        self.resize(1180, 720)
        lay = QVBoxLayout(self)
        lay.addWidget(label(
            "Adds projects, blocks, levels, units and (optionally) unit materials from an Excel workbook. Nothing "
            "in the WMS is deleted or moved, and stock is never changed. Rows already in the WMS are matched and only "
            "differences are written, so importing the same workbook again changes nothing.", "InfoBanner", wrap=True))
        self.path = QLineEdit()
        self.path.setPlaceholderText("C:\\...\\Project structure.xlsx")
        self.path.returnPressed.connect(self.check)
        lay.addLayout(h(label("Workbook *"), self.path, button("Browse...", self._browse),
                        button("Check", self.check, primary=True,
                               tooltip="Read the workbook and check every row. Nothing is saved.")))
        lay.addLayout(h(label("No workbook yet? Save the template (sheets Structure and Materials, with an example), "
                              "fill it in, then choose it here.", "Muted", wrap=True),
                        button("Download Excel template...", self._template)))
        self.summary = label("Choose the workbook and press Check.", "SectionTitle", wrap=True)
        lay.addWidget(self.summary)
        self.filter = QComboBox()
        for text, _st in FILTERS:
            self.filter.addItem(text)
        self.filter.currentIndexChanged.connect(lambda _i: self._fill())
        self.export_btn = button("Export problems...", self.export_problems,
                                 tooltip="Save the rows with errors and notes to Excel, to fix the workbook")
        lay.addLayout(h(label("Show"), self.filter, None, self.export_btn))
        self.tabs = QTabWidget()
        self.units_table = self._table(UNIT_COLS)
        self.mats_table = self._table(MAT_COLS)
        self.tabs.addTab(self.units_table, "Structure")
        self.tabs.addTab(self.mats_table, "Materials")
        lay.addWidget(self.tabs, 1)
        self.import_btn = button("Import", self.do_import, primary=True)
        self.close_btn = button("Close", self.reject)
        lay.addLayout(h(None, self.import_btn, self.close_btn))
        self._update_buttons()

    @staticmethod
    def _table(cols: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(False)
        t.setWordWrap(True)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(len(cols) - 1, QHeaderView.ResizeMode.Stretch)
        return t

    # -- actions ------------------------------------------------------------------------------
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Excel workbook", str(Path.home()),
                                              "Excel workbook (*.xlsx *.xlsm)")
        if path:
            self.path.setText(path)
            self.check()

    def _template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Project structure template",
                                              str(Path.home() / "Documents" / "WMS_Project_Structure_Template.xlsx"),
                                              "Excel workbook (*.xlsx)")
        if path and run(self, write_template, Path(path)):
            info(self, "Template saved", f"The template was saved:\n{path}\n\nFill in the Structure sheet (and the "
                                         f"Materials sheet if you want), save it, then choose it here.")

    def check(self, path: str | None = None) -> bool:
        if path is not None:
            self.path.setText(path)
        text = self.path.text().strip().strip('"')
        if not text:
            show_error(self, ValidationError("Choose the Excel workbook first."), "Workbook")
            return False
        plan = run(self, self.svc.prepare, text)
        if plan is None:
            return False
        self.plan = plan
        self.filter.setCurrentIndex(1 if plan.errors else 0)
        self._fill()
        return True

    def _fill(self) -> None:
        plan = self.plan
        if plan is None:
            return
        lines = [plan.summary()]
        lines += plan.problems
        new = [f"{p.number} {p.name}" for p in plan.projects.values() if p.is_new]
        if new:
            lines.append("New projects (status Planned): " + ", ".join(new))
        if plan.errors:
            lines.append("Fix the errors in the workbook, save it and press Check again - nothing is imported while "
                         "there are errors.")
        elif not plan.changes:
            lines.append("The WMS already matches this workbook: nothing to import.")
        self.summary.setText("\n".join(lines))
        want = FILTERS[self.filter.currentIndex()][1]
        def shown(rows):
            if want is None:
                return [r for r in rows if r.errors or r.warnings]
            return [r for r in rows if r.status in want]

        units, mats = shown(plan.units), shown(plan.materials)
        self._rows(self.units_table, units, lambda r: [
            str(r.row), r.status, r.get("project_no"), r.get("block"), r.get("level"), r.get("unit_code"),
            r.get("unit_name"), r.get("quantity"), "; ".join(r.changes), r.problems])
        self._rows(self.mats_table, mats, lambda r: [
            str(r.row), r.status, r.get("project_no"), r.get("block"), r.get("level"),
            r.get("unit_code") or r.get("unit_name"), r.get("material_code"), r.get("qty_per_unit"),
            r.get("vpk_code"), "; ".join(r.changes), r.problems])
        self.tabs.setTabText(0, f"Structure ({len(plan.units)})")
        self.tabs.setTabText(1, f"Materials ({len(plan.materials)})")
        if not plan.units and plan.materials:
            self.tabs.setCurrentIndex(1)
        self._update_buttons()

    @staticmethod
    def _rows(table: QTableWidget, rows, cells) -> None:
        table.setRowCount(len(rows))
        for n, r in enumerate(rows):
            for c, text in enumerate(cells(r)):
                item = QTableWidgetItem(text)
                if c == 1:
                    item.setBackground(STATUS_BG[r.status])
                elif r.errors:
                    item.setBackground(STATUS_BG[ERROR])
                elif r.warnings:
                    item.setBackground(WARN_BG)
                if c == table.columnCount() - 1 and r.problems:
                    item.setToolTip("\n".join(r.errors + r.warnings))
                table.setItem(n, c, item)
        for c in range(table.columnCount() - 1):  # the last column (errors / notes) takes the rest, wrapped
            table.resizeColumnToContents(c)
            table.setColumnWidth(c, min(table.columnWidth(c), 260))
        table.resizeRowsToContents()

    def _update_buttons(self) -> None:
        plan = self.plan
        self.import_btn.setEnabled(bool(plan and plan.can_import))
        self.export_btn.setEnabled(bool(plan and (plan.errors or any(r.warnings for r in plan.rows))))

    def export_problems(self, path: str | None = None):
        if self.plan is None:
            return None
        if path is None:
            default = Path.home() / "Documents" / f"{self.plan.source.stem} - import problems.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, "Export import problems", str(default),
                                                  "Excel workbook (*.xlsx)")
            if not path:
                return None
        return run(self, export_problems, self.plan, Path(path))

    def do_import(self, ask: bool = True) -> bool:
        plan = self.plan
        if plan is None or not plan.can_import:
            return False
        if ask and not confirm(self, "Import project structure", f"Import now?\n\n{plan.summary()}\n\nA backup of "
                               f"the WMS is taken first. If anything goes wrong, nothing is imported (all or "
                               f"nothing).", yes="Import"):
            return False
        try:
            result = tasks.with_progress(self, "Import project structure",
                                         lambda progress: self.svc.run(plan, progress), "Importing rows...")
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            if isinstance(exc, ValidationError) and "changed since the preview" in exc.message:
                self.check()
            return False
        self.result = result
        self.app.changed()
        if ask:
            info(self, "Import project structure",
                 f"Imported.\n\n{result.summary()}\n\nPre-import backup: {result.backup.name if result.backup else '-'}")
        self.check()  # the preview now shows everything as "No change"
        return True


__all__ = ["StructureImportDialog"]
