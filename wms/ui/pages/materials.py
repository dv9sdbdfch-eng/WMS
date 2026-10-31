"""Materials: the one place to find, filter, export and import materials (spec sections 1-45)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMenu, QPushButton

from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.widgets.common import button, info
from wms.ui.widgets.materials_view import MaterialsView

#: the old Products screen's export scopes -> the Materials export scopes
SCOPES = {"shown": "CURRENT", "current": "CURRENT", "selected": "SELECTED", "all": "ALL"}


class MaterialsPage(Page):
    title = "Materials"
    subtitle = "Find any material: search, filter by project, supplier, bin and stock, then act on the result"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.PRODUCTS_EDIT):
            imp = QPushButton("Import")
            imp.setAutoDefault(False)
            m = QMenu(imp)
            m.addAction("Import Material Master Data...", lambda: self.import_excel())
            if app.can(P.SITES_EDIT):
                m.addAction("Import Project Material Requirements...", lambda: self.import_project_list())
            m.addSeparator()
            m.addAction("Download Excel Template...", lambda: self.download_template())
            imp.setMenu(m)
            self.import_btn = imp
            self.add_action(imp)
        if app.can(P.REPORTS_EXPORT):
            exp = QPushButton("Export")
            exp.setAutoDefault(False)
            m = QMenu(exp)
            m.addAction("Export Current View...", lambda: self.export_excel("CURRENT"))
            m.addAction("Export Selected...", lambda: self.export_excel("SELECTED"))
            m.addAction("Export All Materials...", lambda: self.export_excel("ALL"))
            exp.setMenu(m)
            self.export_btn = exp
            self.add_action(exp)
        if app.can(P.PRODUCTS_EDIT):
            self.add_action(button("Add Material", self.new, primary=True, shortcut="Ctrl+N"))
        # Low Stock is a state of Materials (spec 3); reordering it is one click away while it is shown
        self.reorder_btn = self.add_link("Reorder Required...", "low_stock",
                                         tooltip="Suggested quantities and draft purchase orders per supplier")
        self.reorder_btn.setVisible(False)
        self.actions.insertWidget(0, self.reorder_btn)
        self.view = MaterialsView(app, self)
        self.view.filter_changed.connect(self._filter_changed)
        self.body.addWidget(self.view, 1)
        # the old Products screen's names, used by shortcuts and the Search screen
        self.search = self.view.search

    def refresh(self) -> None:
        self.view.refresh()

    def _filter_changed(self) -> None:
        self.reorder_btn.setVisible(self.view.filter.stock in ("LOW_STOCK", "OUT_OF_STOCK")
                                    and self.app.can(P.PRODUCTS_VIEW))

    def apply_preset(self, preset: dict) -> None:
        """Dashboard counters: {"stock": "LOW_STOCK"} etc. - any MaterialFilter field."""
        from wms.services.materials import MaterialFilter
        self.view.set_filter(MaterialFilter.from_dict(preset))

    def new(self) -> None:
        from wms.ui.dialogs.product_dialog import ProductDialog
        dlg = ProductDialog(self.app, None, self)
        dlg.exec()
        if dlg.saved:
            self.view._options_loaded = False
            self.app.changed()

    def export_excel(self, scope: str, path: str | None = None):
        return self.view.export(SCOPES.get(scope, scope), path)

    def import_excel(self, project_id: int | None = None) -> None:
        from wms.ui.dialogs.excel_import import ExcelImportDialog
        dlg = ExcelImportDialog(self.app, self, project_id=project_id)
        dlg.exec()
        if dlg.result is not None:
            self.view._options_loaded = False
            self.app.changed()

    def import_project_list(self) -> None:
        self.import_excel(project_id=self.view.filter.site_id or 0)

    def download_template(self, path: str | None = None):
        from wms.services.excel_import import copy_template
        if path is None:
            default = Path.home() / "Documents" / "WMS_Materials_Template.xlsx"
            path, _ = QFileDialog.getSaveFileName(self, "Save Excel template", str(default), "Excel workbook (*.xlsx)")
            if not path:
                return None
        out = copy_template(Path(path))
        info(self, "Excel template", f"Template saved:\n{out}\n\nFill one row per material and import it with "
                                     f"Import → Import Material Master Data.")
        return out
