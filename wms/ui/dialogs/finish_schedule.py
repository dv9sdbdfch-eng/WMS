"""Finish Schedule (v1.29): the unit card - the materials one unit type is made of.

Opened by double-clicking a unit in the Projects tree (or its menu / the details panel). Per
material: the norm per unit, what the units made used, what the units still to make need, the
project's stock of it, how many of the units still to make that stock covers, and the shortage.
Materials are added, changed and removed here (they are no longer rows of the tree)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout

from wms.core.numbers import fmt_qty
from wms.ui.widgets.common import QTY, Col, DataTable, button, h, label, run

COVER_LABELS = {"GREEN": "Covered", "YELLOW": "Part", "RED": "None"}


class FinishScheduleDialog(QDialog):
    def __init__(self, page, unit_id: int):
        super().__init__(page)
        self.page, self.app, self.unit_id = page, page.app, unit_id
        self.svc = page.svc
        self.setMinimumSize(1120, 480)
        self.lines: list = []
        lay = QVBoxLayout(self)
        self.title = label("", "SectionTitle")
        lay.addWidget(self.title)
        self.place = label("", "Muted", wrap=True)
        lay.addWidget(self.place)
        self.table = DataTable([
            Col("Code", lambda r: r.need.product_code, width=95),
            Col("Material", lambda r: " · ".join(x for x in (r.need.product_name, r.brand,
                                                               f"VPK {r.vpk_code}" if r.vpk_code else None) if x),
                width=210),
            Col("Per unit", lambda r: r.need.norm, QTY, 65),
            Col("Unit", lambda r: r.uom, width=60),
            Col("Total required", lambda r: r.need.total, QTY, 95),
            Col("Used (made)", lambda r: r.need.used_by_done, QTY, 85),
            Col("Still needed", lambda r: r.still_needed, QTY, 85),
            Col("Project stock", lambda r: r.project_stock, QTY, 90),
            Col("Covers units", lambda r: r.units_covered, QTY, 85),
            Col("Shortage", lambda r: r.shortage, QTY, 75),
            Col("Cover", lambda r: COVER_LABELS[r.coverage], width=65),
            Col("Notes", lambda r: r.notes or ""),
        ])
        self.table.set_row_style(lambda r: "muted" if not r.product_active else
                                 ("low" if r.coverage != "GREEN" else None))
        self.table.activated_item.connect(self._activated)
        lay.addWidget(self.table, 1)
        self.summary = label("", wrap=True)
        lay.addWidget(self.summary)
        lay.addWidget(label("Project stock = material delivered for the project or reserved for it, less what was "
                            "issued. It is shared by every unit of the project that uses the material.", "Muted",
                            wrap=True))
        self.edit_buttons = []
        if page.can_edit:
            for text, slot in (("Add Material...", self.add), ("Edit Quantity...", self.edit),
                               ("Replace...", self.replace), ("Move Up", lambda: self.move(-1)),
                               ("Move Down", lambda: self.move(1)), ("Remove", self.remove)):
                b = button(text, slot, danger=text == "Remove")
                self.edit_buttons.append(b)
        lay.addLayout(h(*self.edit_buttons, None, button("Open Material", self.open_material),
                        button("Close", self.accept, primary=True)))
        self.reload()

    # ---- data ----------------------------------------------------------------------------------
    def reload(self) -> None:
        unit = run(self, self.svc.unit, self.unit_id)
        if unit is None:
            return
        self.unit = unit
        project = run(self, self.svc.project, unit.site_id)
        self.setWindowTitle(f"Finish Schedule - {unit.label}")
        self.title.setText(f"Finish Schedule: {unit.label}")
        self.lines = run(self, self.app.services.unit_norms.finish_schedule, self.unit_id) or []
        done = self.lines[0].need.units_done if self.lines else self.app.services.unit_norms.units_done(self.unit_id)
        self.place.setText(f"{project.code} {project.name}  ·  {fmt_qty(unit.quantity)} unit(s), "
                           f"{fmt_qty(done)} made, {fmt_qty(max(unit.quantity - done, 0))} still to make"
                           if project else "")
        editable = project is not None and not project.read_only
        for b in self.edit_buttons:
            b.setEnabled(editable)
        self.table.set_items(self.lines)
        short = [r for r in self.lines if r.coverage != "GREEN"]
        if not self.lines:
            self.summary.setText("No materials yet. Add the materials one unit is made of.")
        elif not short:
            self.summary.setText(f"<b>All {len(self.lines)} material(s) covered</b> by the project's stock.")
        else:
            self.summary.setText(f"<b>{len(short)} of {len(self.lines)} material(s) short</b>: "
                                 + ", ".join(f"{r.need.product_code} {fmt_qty(r.shortage)} {r.uom}" for r in short[:8])
                                 + (" ..." if len(short) > 8 else ""))

    def _node(self):
        """The selected material line as the tree service sees it (for the shared actions)."""
        row = self.table.current()
        if row is None:
            return None
        return run(self, self.svc.material, row.id)

    def _select(self, line_id: int | None) -> None:
        if line_id is None:
            return
        for r in range(self.table.proxy.rowCount()):
            src = self.table.proxy.mapToSource(self.table.proxy.index(r, 0)).row()
            if self.table.model_.items[src].id == line_id:
                self.table.selectRow(r)
                return

    # ---- actions -------------------------------------------------------------------------------
    def _activated(self, row) -> None:
        if self.page.can_edit and self.edit_buttons and self.edit_buttons[0].isEnabled():
            self.edit()
        else:
            self.app.open_entity("PRODUCT", row.need.product_id, self)

    def add(self) -> None:
        ids = self.page.add_material(self.unit, self)
        if ids:
            self.reload()
            self._select(ids[-1])

    def edit(self) -> None:
        node = self._node()
        if node is not None and self.page.edit_material_quantity(node, self):
            self.reload()
            self._select(node.id)

    def replace(self) -> None:
        node = self._node()
        if node is not None and self.page.replace_material(node, self):
            self.reload()
            self._select(node.id)

    def move(self, delta: int) -> None:
        node = self._node()
        if node is not None and self.page.move_material(node, delta, self):
            self.reload()
            self._select(node.id)

    def remove(self) -> None:
        node = self._node()
        if node is not None and self.page.remove_material(node, self):
            self.reload()

    def open_material(self) -> None:
        row = self.table.current()
        if row is not None:
            self.app.open_entity("PRODUCT", row.need.product_id, self)
