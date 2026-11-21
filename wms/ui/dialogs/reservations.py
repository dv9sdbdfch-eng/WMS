"""Project stock: what was set aside for a project (read-only history) and Return to main.

v1.35: stock is no longer reserved by hand. A project's stock is what was delivered for it with
Goods In (and, from older versions, what was reserved by hand). It is used up by Goods Out to the
project and can be given back with Return to main; the list here is history only.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from wms.core.enums import ReservationStatus
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_qty
from wms.core.permissions import P
from wms.ui.widgets.common import (
    DATETIME,
    QTY,
    Col,
    DataTable,
    button,
    h,
    label,
    run,
    show_error,
)


def origin(r) -> str:
    return "Delivery" if r.delivery_note_id else "Reserved by hand"


def reservation_columns() -> list[Col]:
    return [Col("No.", lambda v: v.reservation.number, width=95),
            Col("Project", lambda v: v.reservation.site_label, width=220),
            Col("Code", lambda v: v.reservation.product.internal_code, width=110),
            Col("Product", lambda v: v.reservation.product.name, width=220),
            Col("From", lambda v: origin(v.reservation), width=115),
            Col("Set aside", lambda v: v.reservation.quantity, QTY, 80),
            Col("Issued", lambda v: v.issued, QTY, 75),
            Col("Left", lambda v: v.remaining, QTY, 75),
            Col("Unit", lambda v: v.reservation.product.unit.code, width=55),
            Col("Status", lambda v: ReservationStatus(v.reservation.status).label, width=85),
            Col("Created", lambda v: v.reservation.created_at, DATETIME, 125),
            Col("Notes", lambda v: v.reservation.notes or v.reservation.release_reason or "")]


def reservation_style(v) -> str | None:
    return None if v.reservation.status == "ACTIVE" else "muted"


class ReservationsPanel(QWidget):
    """Project stock history of one product or one project (read-only)."""

    def __init__(self, app, product_id: int | None = None, site_id: int | None = None, parent=None):
        super().__init__(parent)
        self.app, self.product_id, self.site_id = app, product_id, site_id
        lay = QVBoxLayout(self)
        self.table = DataTable(reservation_columns())
        self.table.set_row_style(reservation_style)
        lay.addWidget(self.table, 1)
        self.count = label("", "Muted")
        btns = [self.count, None]
        if site_id and app.can(P.RESERVATIONS_EDIT):
            btns.append(button("Return to main...", self.return_to_main))
        lay.addLayout(h(*btns))
        self.refresh()

    def refresh(self):
        rows = self.app.services.reservations.list(product_id=self.product_id, site_id=self.site_id)
        self.table.set_items(rows)
        active = [v for v in rows if v.remaining > 0]
        self.count.setText(f"{len(active)} line(s) of project stock left" if rows else "No project stock.")

    def return_to_main(self):
        if ProjectStockDialog(self.app, self.site_id, self).exec():
            self.refresh()


class ProjectStockDialog(QDialog):
    """v1.27: a project's stock (delivered for it, plus anything reserved by hand before v1.35, less
    issued) and Return to Main Stock - give some or all of it back to the warehouse so it is free for any work again."""

    C_RETURN = 6

    def __init__(self, app, site_id: int, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QTableWidget
        self.app, self.site_id = app, site_id
        site = app.services.sites.get(site_id)
        self.setWindowTitle(f"Project Stock - {site.display}")
        self.setMinimumSize(900, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"Project stock: {site.display}", "PageTitle"))
        lay.addWidget(label("Material delivered for this project with Goods In (or reserved for it by hand in "
                            "older versions) and not issued yet. Return to Main Stock gives it back to the warehouse: nothing "
                            "moves on the shelves, the material just stops being kept for this project.",
                            "Muted", wrap=True))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Code", "Material", "Unit", "Delivered for project",
                                              "Reserved (old)", "Project stock", "Return"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        for c, w in enumerate((110, 280, 55, 140, 90, 110)):
            self.table.setColumnWidth(c, w)
        lay.addWidget(self.table, 1)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Why it goes back, e.g. 'Left over after Level 3', 'Project changed'")
        form = QFormLayout()
        form.addRow("Reason *", self.reason)
        lay.addLayout(form)
        self.history = DataTable([Col("No.", lambda r: r.number, width=95),
                                  Col("Date", lambda r: r.released_at, DATETIME, 125),
                                  Col("Code", lambda r: r.product.internal_code, width=110),
                                  Col("Material", lambda r: r.product.name, width=220),
                                  Col("Returned", lambda r: r.quantity, QTY, 85),
                                  Col("Unit", lambda r: r.product.unit.code, width=55),
                                  Col("By", lambda r: r.user_name_snapshot or "", width=120),
                                  Col("Reason", lambda r: r.reason)])
        lay.addWidget(label("Returned to main stock", "SectionTitle"))
        self.history.setMaximumHeight(160)
        lay.addWidget(self.history)
        btns = [None, button("Close", self.reject)]
        if app.can(P.RESERVATIONS_EDIT):
            btns = [button("Return All", self.fill_all), None, button("Close", self.reject),
                    button("Return to Main Stock", self.save, primary=True)]
        lay.addLayout(h(*btns))
        self.refresh()

    def refresh(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTableWidgetItem
        self.lines = self.app.services.reservations.project_stock(self.site_id)
        self.table.setRowCount(len(self.lines))
        for r, ln in enumerate(self.lines):
            vals = [ln.code, ln.name, ln.unit, fmt_qty(ln.from_deliveries), fmt_qty(ln.reserved), fmt_qty(ln.total)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c >= 3:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
            ret = QTableWidgetItem("")
            ret.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ret.setToolTip("Quantity to give back to main stock (empty = keep it all for the project)")
            self.table.setItem(r, self.C_RETURN, ret)
        self.history.set_items(self.app.services.reservations.project_releases(self.site_id))

    def fill_all(self):
        for r, ln in enumerate(self.lines):
            self.table.item(r, self.C_RETURN).setText(fmt_qty(ln.total))

    def items(self) -> list[tuple[int, str]]:
        out = []
        for r, ln in enumerate(self.lines):
            text = (self.table.item(r, self.C_RETURN).text() or "").strip()
            if text:
                out.append((ln.product_id, text))
        return out

    def save(self):
        items = self.items()
        if not items:
            show_error(self, ValidationError("Enter the quantity to return in the Return column."))
            return
        number = run(self, self.app.services.reservations.return_to_main, self.site_id, items, self.reason.text())
        if number:
            self.app.changed()
            self.reason.clear()
            self.refresh()
            from wms.ui.widgets.common import info
            info(self, "Returned to main stock", f"{number}: {len(items)} material(s) given back to main stock.")
