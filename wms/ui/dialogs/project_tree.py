"""Small forms used by the Project Tree screen: manager, project, unit, material picker."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
)

from wms.core.enums import SiteStatus
from wms.core.exceptions import ValidationError
from wms.ui.widgets.common import QTY, Col, DataTable, DecimalEdit, SearchBox, button, h, label, run, show_error


def manager_combo(app, current: int | None = None, none_text: str = "(No manager - unassigned)",
                  exclude: int | None = None) -> QComboBox:
    combo = QComboBox()
    combo.addItem(none_text, None)
    for name, mid in app.services.project_tree.manager_choices(include_inactive=current is not None):
        if mid != exclude:
            combo.addItem(name, mid)
    i = combo.findData(current)
    combo.setCurrentIndex(i if i >= 0 else 0)
    return combo


def notes_edit(text: str | None = None, height: int = 64) -> QPlainTextEdit:
    e = QPlainTextEdit()
    e.setFixedHeight(height)
    e.setPlainText(text or "")
    return e


class _Form(QDialog):
    """Title, a form and Cancel / Save. ``save()`` returns the saved record or None (error shown)."""

    def __init__(self, title: str, parent=None, width: int = 460):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        self.result_value = None
        self.lay = QVBoxLayout(self)
        self.form = QFormLayout()
        self.lay.addLayout(self.form)

    def finish(self, save_text: str = "Save") -> None:
        self.lay.addLayout(h(None, button("Cancel", self.reject), button(save_text, self._save, primary=True)))

    def _save(self) -> None:
        value = self.save()
        if value is not None:
            self.result_value = value
            self.accept()

    def save(self):  # pragma: no cover - overridden
        return None


class ManagerDialog(_Form):
    def __init__(self, app, node=None, parent=None):
        super().__init__("Edit manager" if node else "New manager", parent)
        self.app, self.node = app, node
        self.name = QLineEdit(node.name if node else "")
        self.email = QLineEdit(node.email or "" if node else "")
        self.phone = QLineEdit(node.phone or "" if node else "")
        self.active = QCheckBox("Active (shown when choosing a manager)")
        self.active.setChecked(node.is_active if node else True)
        self.notes = notes_edit(node.notes if node else None)
        for text, w in (("Name *", self.name), ("E-mail", self.email), ("Phone", self.phone), ("", self.active),
                        ("Notes", self.notes)):
            self.form.addRow(text, w)
        self.finish()

    def data(self) -> dict:
        return {"name": self.name.text(), "email": self.email.text(), "phone": self.phone.text(),
                "is_active": self.active.isChecked(), "notes": self.notes.toPlainText()}

    def save(self):
        return run(self, self.app.services.project_tree.save_manager, self.node.id if self.node else None,
                   self.data())


class ProjectDialog(_Form):
    """Create a project or change its main fields. Everything else is on the full project record."""

    def __init__(self, app, node=None, parent=None, manager_id: int | None = None):
        super().__init__(f"Edit project {node.code}" if node else "New project", parent)
        self.app, self.node = app, node
        self.code = QLineEdit(node.code if node else "")
        self.name = QLineEdit(node.name if node else "")
        self.manager = manager_combo(app, node.manager_id if node else manager_id)
        self.status = QComboBox()
        for val, text in SiteStatus.choices():
            self.status.addItem(text, val)
        self.status.setCurrentIndex(self.status.findData(node.status if node else SiteStatus.ACTIVE.value))
        self.notes = notes_edit(node.notes if node else None)
        for text, w in (("Project code / ref *", self.code), ("Project name *", self.name),
                        ("Manager", self.manager), ("Status *", self.status), ("Notes", self.notes)):
            self.form.addRow(text, w)
        self.finish()

    def data(self) -> dict:
        return {"project_number": self.code.text(), "name": self.name.text(), "manager_id": self.manager.currentData(),
                "status": self.status.currentData(), "notes": self.notes.toPlainText()}

    def save(self):
        return run(self, self.app.services.project_tree.save_project, self.node.id if self.node else None,
                   self.data(), self.node.version if self.node else None)


class DuplicateProjectDialog(_Form):
    def __init__(self, app, node, parent=None):
        super().__init__(f"Duplicate project {node.code}", parent)
        self.app, self.node = app, node
        self.lay.insertWidget(0, label(f"A new project with the same manager, details, {node.units} unit(s) and their "
                                       f"materials. It starts as Planned. Reservations, issued stock, documents and the "
                                       f"material list are not copied.", "Muted", wrap=True))
        self.code = QLineEdit(f"{node.code}-COPY")
        self.name = QLineEdit(f"{node.name} (copy)")
        self.form.addRow("New project code *", self.code)
        self.form.addRow("New project name *", self.name)
        self.finish("Duplicate")

    def save(self):
        return run(self, self.app.services.project_tree.duplicate_project, self.node.id, self.code.text(),
                   self.name.text())


class UnitDialog(_Form):
    def __init__(self, app, site_id: int | None, node=None, parent=None):
        super().__init__(f"Edit unit {node.name}" if node else "New unit", parent)
        self.app, self.site_id, self.node = app, site_id, node
        self.name = QLineEdit(node.name if node else "")
        self.name.setPlaceholderText("Kitchen, Wardrobe, Vanity Unit, Reception Desk...")
        self.code = QLineEdit(node.code or "" if node else "")
        self.code.setPlaceholderText("K01")
        self.qty = DecimalEdit()
        self.qty.set_value(node.quantity if node else Decimal(1))
        self.notes = notes_edit(node.notes if node else None)
        for text, w in (("Unit name *", self.name), ("Unit code / ref", self.code),
                        ("Quantity *", self.qty), ("Description / notes", self.notes)):
            self.form.addRow(text, w)
        self.form.addRow("", label("Quantity = how many of this unit the project needs (Kitchen K01 x 24).",
                                   "Muted", wrap=True))
        self.finish()

    def data(self) -> dict:
        return {"name": self.name.text(), "unit_code": self.code.text(), "quantity": self.qty.text(),
                "notes": self.notes.toPlainText()}

    def save(self):
        return run(self, self.app.services.project_tree.save_unit, self.node.id if self.node else None, self.data(),
                   site_id=self.site_id)


class MaterialPickerDialog(QDialog):
    """Search the existing materials (Stock / Product database) and pick one or several.

    ``ask_quantity``: also ask the quantity per unit, VPK code and notes (Add material)."""

    def __init__(self, app, title: str, parent=None, multi: bool = True, ask_quantity: bool = True,
                 unit_text: str | None = None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(title)
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        if unit_text:
            lay.addWidget(label(unit_text, "Muted", wrap=True))
        self.search = SearchBox("Search code, product number, barcode, name, description, brand or supplier...")
        self.search.search.connect(self.refresh)
        lay.addWidget(self.search)
        self.table = DataTable([Col("Code", lambda p: p.internal_code, width=120),
                                Col("Material", lambda p: p.name, width=260),
                                Col("Product No.", lambda p: p.supplier_code or "", width=120),
                                Col("Brand", lambda p: p.brand or "", width=100),
                                Col("Unit", lambda p: p.unit.code, width=60),
                                Col("Description", lambda p: (p.description or "").replace("\n", " "))])
        if multi:
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.activated_item.connect(lambda _p: self._ok())
        lay.addWidget(self.table, 1)
        self.count = label("", "Muted")
        lay.addWidget(self.count)
        self.qty = DecimalEdit()
        self.vpk = QLineEdit()
        self.notes = QLineEdit()
        if ask_quantity:
            form = QFormLayout()
            form.addRow("Quantity per unit *", self.qty)
            form.addRow("VPK code", self.vpk)
            form.addRow("Notes", self.notes)
            lay.addLayout(form)
            self.table.selection_changed_item.connect(self._offer_vpk)
        self.ask_quantity = ask_quantity
        lay.addLayout(h(None, button("Cancel", self.reject), button("Add" if ask_quantity else "Choose", self._ok,
                                                                    primary=True)))
        self.chosen: list[int] = []
        self.refresh("")

    def refresh(self, text: str | None = None) -> None:
        rows = run(self, self.app.services.project_tree.find_products, self.search.text() if text is None else text)
        rows = rows or []
        self.table.set_items(rows)
        self.count.setText(f"{len(rows)} material(s) shown" + (" - showing the first 200, type more to narrow it down"
                                                               if len(rows) >= 200 else ""))

    def _offer_vpk(self, product) -> None:
        if product is not None and not self.vpk.text().strip():
            code = self.app.services.project_tree.last_vpk_code(product.id)
            if code:
                self.vpk.setText(code)

    def _ok(self) -> None:
        picked = self.table.selected_items() or ([self.table.current()] if self.table.current() else [])
        if not picked:
            show_error(self, ValidationError("Choose a material from the list."))
            return
        if self.ask_quantity:
            try:
                q = self.qty.value()
            except Exception as e:  # noqa: BLE001
                show_error(self, e)
                return
            if q is None:
                show_error(self, ValidationError("Enter the quantity one unit needs."))
                return
        self.chosen = [p.id for p in picked]
        self.accept()


def ask_quantity(parent, title: str, prompt: str, value: Decimal | None) -> str | None:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(380)
    lay = QVBoxLayout(dlg)
    lay.addWidget(label(prompt, wrap=True))
    edit = DecimalEdit()
    edit.set_value(value)
    lay.addWidget(edit)
    lay.addLayout(h(None, button("Cancel", dlg.reject), button("OK", dlg.accept, primary=True)))
    edit.selectAll()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return edit.text()


class DeleteManagerDialog(QDialog):
    """Deleting a manager never deletes projects: leave them unassigned or hand them to someone."""

    def __init__(self, app, node, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Delete manager {node.name}")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        n = node.projects
        if n:
            lay.addWidget(label(f"{node.name} manages {n} project(s). Projects are never deleted with a manager. "
                                f"What should happen to them?", wrap=True))
        else:
            lay.addWidget(label(f"{node.name} has no projects. The manager will be deleted.", wrap=True))
        self.leave = QRadioButton(f"Leave the {n} project(s) without a manager (unassigned)")
        self.move = QRadioButton("Move them to this manager:")
        self.target = manager_combo(app, None, none_text="Choose a manager...", exclude=node.id)
        self.target.setEnabled(False)
        group = QButtonGroup(self)
        for b in (self.leave, self.move):
            group.addButton(b)
        self.leave.setChecked(True)
        self.move.toggled.connect(self.target.setEnabled)
        if n:
            lay.addWidget(self.leave)
            lay.addLayout(h(self.move, self.target, None))
        lay.addLayout(h(None, button("Cancel", self.reject), button("Delete manager", self._ok, danger=True)))

    def target_id(self) -> int | None:
        return self.target.currentData() if self.move.isChecked() else None

    def _ok(self) -> None:
        if self.move.isChecked() and self.target.currentData() is None:
            show_error(self, ValidationError("Choose the manager who takes over the projects."))
            return
        self.accept()


class DemandDialog(QDialog):
    """Material demand of every open project (planned, active, on hold)."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Material demand - all open projects")
        self.resize(860, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Required = sum of (unit quantity x quantity per unit) over every unit of every open "
                            "project (planned, active, on hold). Calculated from the project structure; stock is "
                            "not changed.", "Muted", wrap=True))
        table = DataTable([Col("Code", lambda d: d.code, width=120), Col("Material", lambda d: d.name, width=260),
                           Col("Brand", lambda d: d.brand or "", width=100),
                           Col("Required", lambda d: d.required, QTY, 90),
                           Col("Unit", lambda d: d.uom, width=60),
                           Col("Projects", lambda d: d.projects, width=70),
                           Col("Lines", lambda d: d.unit_lines)])
        rows = run(self, app.services.project_demand.for_active_projects) or []
        table.set_items(rows)
        lay.addWidget(table, 1)
        lay.addLayout(h(label(f"{len(rows)} material(s)", "Muted"), None, button("Copy all", table.copy_all),
                        button("Close", self.accept)))
