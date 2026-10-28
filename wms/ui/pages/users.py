"""User accounts, roles / permissions and the audit trail."""

from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import EntityType
from wms.core.permissions import ALL_PERMISSIONS, PERMISSION_LABELS
from wms.core.security import MIN_PASSWORD_LENGTH
from wms.ui.pages.base import ListPage, Page
from wms.ui.widgets.common import DATETIME, Col, DataTable, button, h, info, label, run


class UserDialog(QDialog):
    def __init__(self, app, user=None, parent=None):
        super().__init__(parent)
        self.app, self.user = app, user
        self.setWindowTitle(f"User {user.username}" if user else "New user")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit()
        self.full_name = QLineEdit()
        self.email = QLineEdit()
        self.role = QComboBox()
        for r in app.services.users.list_roles():
            self.role.addItem(r.name, r.id)
        self.active = QCheckBox("Active (can log in)")
        self.active.setChecked(True)
        form.addRow("Username *", self.username)
        form.addRow("Full name *", self.full_name)
        form.addRow("Email", self.email)
        form.addRow("Role *", self.role)
        if user:
            self.username.setText(user.username)
            self.username.setEnabled(False)
            self.full_name.setText(user.full_name)
            self.email.setText(user.email or "")
            self.role.setCurrentIndex(self.role.findData(user.role_id))
            self.active.setChecked(user.is_active)
            form.addRow("", self.active)
        else:
            self.password = QLineEdit()
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password.setPlaceholderText(f"At least {MIN_PASSWORD_LENGTH} characters")
            self.must_change = QCheckBox("Must change password at first login")
            self.must_change.setChecked(True)
            form.addRow("Password *", self.password)
            form.addRow("", self.must_change)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))

    def save(self):
        data = {"username": self.username.text(), "full_name": self.full_name.text(), "email": self.email.text(),
                "role_id": self.role.currentData()}
        if self.user:
            data["is_active"] = self.active.isChecked()
            ok = run(self, self.app.services.users.update_user, self.user.id, data)
        else:
            data["must_change_password"] = self.must_change.isChecked()
            ok = run(self, self.app.services.users.create_user, data, self.password.text())
        if ok:
            self.accept()


class RoleDialog(QDialog):
    def __init__(self, app, role=None, parent=None):
        super().__init__(parent)
        self.app, self.role = app, role
        self.setWindowTitle(f"Role {role.name}" if role else "New role")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.code = QLineEdit(role.code if role else "")
        self.code.setEnabled(role is None)
        self.name = QLineEdit(role.name if role else "")
        self.description = QLineEdit((role.description or "") if role else "")
        form.addRow("Code *", self.code)
        form.addRow("Name *", self.name)
        form.addRow("Description", self.description)
        lay.addLayout(form)
        lay.addWidget(label("Permissions", "SectionTitle"))
        grid = QGridLayout()
        self.checks = {}
        have = role.permission_set if role else set()
        locked = bool(role and role.code == "ADMIN")
        for i, perm in enumerate(ALL_PERMISSIONS):
            cb = QCheckBox(PERMISSION_LABELS.get(perm, perm))
            cb.setChecked(perm in have)
            cb.setEnabled(not locked)
            self.checks[perm] = cb
            grid.addWidget(cb, i // 2, i % 2)
        lay.addLayout(grid)
        if locked:
            lay.addWidget(label("The Administrator role always has full access.", "Muted"))
        save = button("Save", self.save, primary=True)
        save.setEnabled(not locked)
        lay.addLayout(h(None, button("Cancel", self.reject), save))

    def save(self):
        perms = {p for p, cb in self.checks.items() if cb.isChecked()}
        if run(self, self.app.services.users.save_role, self.role.id if self.role else None, self.code.text(),
               self.name.text(), self.description.text(), perms):
            self.accept()


class UsersPage(Page):
    title = "Users"
    subtitle = "Who can log in and what each role is allowed to do"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        tabs = QTabWidget()
        uw = QWidget()
        ul = QVBoxLayout(uw)
        self.users = DataTable([Col("Username", lambda u: u.username, width=120),
                                Col("Full name", lambda u: u.full_name, width=200),
                                Col("Role", lambda u: u.role.name, width=160),
                                Col("Email", lambda u: u.email or "", width=200),
                                Col("Active", lambda u: "Yes" if u.is_active else "No", width=60),
                                Col("Last login", lambda u: u.last_login_at, DATETIME, 130)])
        self.users.set_row_style(lambda u: None if u.is_active else "muted")
        self.users.activated_item.connect(self.edit_user)
        ul.addWidget(self.users, 1)
        ul.addLayout(h(button("New user", self.new_user, primary=True),
                       button("Edit", lambda: self.edit_user(self.users.current())),
                       button("Reset password", self.reset_password), None))
        tabs.addTab(uw, "Users")
        rw = QWidget()
        rl = QVBoxLayout(rw)
        self.roles = DataTable([Col("Code", lambda r: r.code, width=120),
                                Col("Role", lambda r: r.name, width=180),
                                Col("Permissions", lambda r: str(len(r.permission_set)), width=100),
                                Col("Description", lambda r: r.description or "")])
        self.roles.activated_item.connect(self.edit_role)
        rl.addWidget(self.roles, 1)
        rl.addLayout(h(button("New role", self.new_role),
                       button("Edit", lambda: self.edit_role(self.roles.current())), None))
        tabs.addTab(rw, "Roles and permissions")
        self.body.addWidget(tabs, 1)

    def refresh(self):
        self.users.set_items(self.app.services.users.list_users())
        self.roles.set_items(self.app.services.users.list_roles())

    def new_user(self):
        if UserDialog(self.app, None, self).exec():
            self.refresh()

    def edit_user(self, u):
        if u is not None and UserDialog(self.app, u, self).exec():
            self.refresh()

    def reset_password(self):
        u = self.users.current()
        if u is None:
            return
        pw, ok = QInputDialog.getText(self, "Reset password", f"New temporary password for {u.username}\n"
                                      f"(they must change it at next login):", QLineEdit.EchoMode.Password)
        if not ok or not pw:
            return
        if run(self, lambda: self.app.services.users.reset_password(u.id, pw) or True):
            info(self, "Password reset", f"Password for {u.username} has been reset.")

    def new_role(self):
        if RoleDialog(self.app, None, self).exec():
            self.refresh()

    def edit_role(self, r):
        if r is not None and RoleDialog(self.app, r, self).exec():
            self.refresh()


def _pretty(js: str | None) -> str:
    if not js:
        return ""
    try:
        return json.dumps(json.loads(js), indent=2, ensure_ascii=False)
    except ValueError:
        return js


class AuditDetailDialog(QDialog):
    def __init__(self, entry, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Audit entry #{entry.id}")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        from wms.ui.dialogs.documents import _form
        lay.addLayout(_form([("When", entry.occurred_at.astimezone().strftime("%d/%m/%Y %H:%M:%S")),
                             ("User", entry.username), ("Workstation", entry.workstation or ""),
                             ("Action", entry.action), ("Record", f"{entry.entity_type} {entry.entity_label or ''}"),
                             ("Summary", entry.summary or "")]))
        grid = QGridLayout()
        for col, (title, js) in enumerate((("Before", entry.old_values), ("After", entry.new_values))):
            grid.addWidget(label(title, "SectionTitle"), 0, col)
            t = QPlainTextEdit(_pretty(js))
            t.setReadOnly(True)
            grid.addWidget(t, 1, col)
        lay.addLayout(grid, 1)
        lay.addLayout(h(None, button("Close", self.accept)))


class AuditPage(ListPage):
    title = "Audit Trail"
    subtitle = "Every change made in the system: who, when, from which computer, and what changed"
    search_hint = "Search record, user, summary or changed values..."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.entity = QComboBox()
        self.entity.addItem("All records", None)
        for val, text in EntityType.choices():
            self.entity.addItem(text, val)
        self.add_filter(self.entity, self.entity.currentIndexChanged)
        self.action = QComboBox()
        self.action.addItem("All actions", None)
        self.add_filter(self.action, self.action.currentIndexChanged)

    def columns(self):
        return [Col("When", lambda a: a.occurred_at, DATETIME, 130),
                Col("User", lambda a: a.username, width=100),
                Col("Action", lambda a: a.action, width=180),
                Col("Record", lambda a: a.entity_type.replace("_", " ").title(), width=110),
                Col("Name", lambda a: a.entity_label or "", width=200),
                Col("Summary", lambda a: a.summary or "", width=300),
                Col("Computer", lambda a: a.workstation or "")]

    def refresh(self):
        if self.action.count() == 1:
            self.action.blockSignals(True)
            for a in self.app.services.audit.actions():
                self.action.addItem(a.replace("_", " ").title(), a)
            self.action.blockSignals(False)
        super().refresh()

    def fetch(self):
        d1, d2 = self.dates()
        return self.app.services.audit.search(self.search.text() or None, self.entity.currentData(),
                                              self.action.currentData(), d1, d2)

    def describe(self, rows):
        return f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}" + ("  (latest 2,000)" if len(rows) >= 2000 else "")

    def open_item(self, item):
        AuditDetailDialog(item, self).exec()

    def _open(self, item):  # viewing an entry changes nothing - skip the global refresh
        if item is not None:
            self.open_item(item)
