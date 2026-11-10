"""Reserve remaining and the picking list (also "Load from project" in Goods Out)."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QFormLayout, QLineEdit, QVBoxLayout

from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_qty
from wms.services.allocation import SOURCES
from wms.ui.widgets.common import QTY, Col, DataTable, OptionalDateEdit, button, h, info, label, run, show_error

AREA_NOTE = ("Reservations and Goods Out are kept per project. For a block, level or unit the quantity is that "
             "part's own requirement, never more than the whole project still needs.")


def _area_combo(app, site_id: int) -> QComboBox:
    c = QComboBox()
    c.addItem("Whole project", None)
    for text, key in app.services.allocation.areas(site_id):
        c.addItem(text, key)
    c.setToolTip("Limit the list to one block, level or unit of the project.")
    return c


class ReserveRemainingDialog(QDialog):
    """Preview what the project still has To Allocate and reserve it all in one go."""

    def __init__(self, app, site_id: int, parent=None):
        super().__init__(parent)
        self.app, self.site_id = app, site_id
        self._token = uuid.uuid4().hex
        self.numbers: list[str] = []
        site = app.services.sites.get(site_id)
        self.setWindowTitle(f"Reserve remaining - {site.display}")
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Sets aside everything this project still has To Allocate (Remaining - Reserved), "
                            "as far as stock is available. Nothing moves: Goods Out issues it later.",
                            "Muted", wrap=True))
        form = QFormLayout()
        self.area = _area_combo(app, site_id)
        self.area.currentIndexChanged.connect(lambda _: self.refresh())
        self.required = OptionalDateEdit()
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("Reserve remaining")
        form.addRow("Part of project", self.area)
        form.addRow("Required by", self.required)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        self.table = DataTable([
            Col("Code", lambda x: x.code, width=110), Col("Material", lambda x: x.name, width=240),
            Col("To Allocate", lambda x: x.need, QTY, 90), Col("Available", lambda x: x.available, QTY, 85),
            Col("Will reserve", lambda x: x.quantity, QTY, 95), Col("Short", lambda x: x.short or None, QTY, 70),
            Col("Unit", lambda x: x.uom, width=55)], settings_key="ReserveRemaining")
        self.table.set_row_style(lambda x: "low" if x.short else None)
        lay.addWidget(self.table, 1)
        self.summary = label("", "Muted", wrap=True)
        lay.addWidget(self.summary)
        lay.addWidget(label(AREA_NOTE, "Muted", wrap=True))
        self.ok = button("Reserve", self.save, primary=True)
        lay.addLayout(h(None, button("Cancel", self.reject), self.ok))
        self.refresh()

    def refresh(self) -> None:
        self.plan = run(self, self.app.services.allocation.reserve_plan, self.site_id, self.area.currentData()) or []
        self.table.set_items(self.plan)
        lines = [x for x in self.plan if x.quantity > 0]
        short = [x for x in self.plan if x.short > 0]
        if not self.plan:
            text = "Nothing to reserve: every material on the list is already reserved, issued or not required."
        else:
            text = f"{len(lines)} material(s) will be reserved."
            if short:
                text += (f" {len(short)} cannot be fully reserved because there is not enough available stock "
                         f"(highlighted) - order or receive them, then run this again.")
        self.summary.setText(text)
        self.ok.setEnabled(bool(lines))

    def save(self) -> None:
        try:
            required = self.required.value()
        except ValidationError as e:
            show_error(self, e)
            return
        lines = [(x.product_id, x.quantity) for x in self.plan if x.quantity > 0]
        done = run(self, self.app.services.allocation.reserve_remaining, self.site_id, lines, required,
                   self.notes.text() or None, self._token)
        if done:
            self.numbers = done
            self.app.changed()
            info(self, "Reserved", f"{len(done)} reservation(s) created: {', '.join(done)}")
            self.accept()


class PickListDialog(QDialog):
    """What to pick for a project, bin by bin in walking order. Save as PDF, or (from Goods Out)
    load the lines onto the Goods Out form."""

    def __init__(self, app, site_id: int, parent=None, for_goods_out: bool = False):
        super().__init__(parent)
        self.app, self.site_id = app, site_id
        self.picked_rows: list = []
        site = app.services.sites.get(site_id)
        self.setWindowTitle(("Load from project - " if for_goods_out else "Picking list - ") + site.display)
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.source = QComboBox()
        for key, text in SOURCES:
            self.source.addItem(text, key)
        self.source.setToolTip("Reserved: what is set aside for this project.\n"
                               "Remaining: everything still to issue (Required - Net Issued), from stock not "
                               "reserved for other projects.")
        self.area = _area_combo(app, site_id)
        for c in (self.source, self.area):
            c.currentIndexChanged.connect(lambda _: self.refresh())
        form.addRow("Pick", self.source)
        form.addRow("Part of project", self.area)
        lay.addLayout(form)
        self.table = DataTable([
            Col("Bin", lambda r: r.bin_full, width=230), Col("Code", lambda r: r.code, width=110),
            Col("Material", lambda r: r.name, width=240), Col("Quantity", lambda r: r.quantity, QTY, 85),
            Col("Unit", lambda r: r.uom, width=55)], settings_key="PickList")
        lay.addWidget(self.table, 1)
        self.summary = label("", "Muted", wrap=True)
        lay.addWidget(self.summary)
        lay.addWidget(label(AREA_NOTE, "Muted", wrap=True))
        self.pdf = button("Save PDF...", lambda: self.save_pdf())
        btns = [None, button("Close" if not for_goods_out else "Cancel", self.reject), self.pdf]
        if for_goods_out:
            self.load = button("Load into Goods Out", self.accept_lines, primary=True)
            btns.append(self.load)
        lay.addLayout(h(*btns))
        self.for_goods_out = for_goods_out
        self.refresh()

    def refresh(self) -> None:
        self.pick = run(self, self.app.services.allocation.pick_list, self.site_id, self.area.currentData(),
                        self.source.currentData())
        rows = self.pick.rows if self.pick else []
        self.table.set_items(rows)
        stops = len({r.bin_id for r in rows})
        text = f"{len(rows)} line(s) from {stops} bin(s), in walking order." if rows else "Nothing to pick."
        if self.pick and self.pick.short:
            text += "  Short (not in stock): " + "; ".join(
                f"{x.code} {fmt_qty(x.missing)} {x.uom}" for x in self.pick.short)
        self.summary.setText(text)
        self.pdf.setEnabled(bool(self.pick and (rows or self.pick.short)))
        if hasattr(self, "load"):
            self.load.setEnabled(bool(rows))

    def save_pdf(self, path: str | None = None):
        from wms.exporters.export import export_pdf
        if not self.pick:
            return None
        open_after = path is None
        if path is None:
            default = Path.home() / "Documents" / f"Picking_list_{self.pick.site_label.split()[0]}_{date.today():%Y-%m-%d}.pdf"
            path, _ = QFileDialog.getSaveFileName(self, "Save picking list", str(default), "PDF (*.pdf)")
            if not path:
                return None
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        out = run(self, export_pdf, self.app.services.allocation.pick_report(self.pick), Path(path),
                  self.app.ctx.config.company_name)
        if out and open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
        return out

    def accept_lines(self) -> None:
        self.picked_rows = list(self.pick.rows) if self.pick else []
        if self.picked_rows:
            self.accept()
