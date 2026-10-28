"""Login and password change dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QVBoxLayout

from wms import APP_NAME, __version__
from wms.core.exceptions import WMSError
from wms.services import Services
from wms.ui.widgets.common import button, h, label, show_error


class LoginDialog(QDialog):
    def __init__(self, services: Services, parent=None):
        super().__init__(parent)
        self.services = services
        self.setWindowTitle(f"{APP_NAME} - Sign in")
        self.setFixedWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(10)
        title = label(APP_NAME, "PageTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        sub = label(f"Warehouse Management  -  v{__version__}", "Muted")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        lay.addLayout(form)
        self.message = label("", "Banner", wrap=True)
        self.message.hide()
        lay.addWidget(self.message)
        self.btn = button("Sign in", self.try_login, primary=True)
        self.btn.setDefault(True)
        lay.addLayout(h(None, button("Exit", self.reject), self.btn))
        self.username.setFocus()

    def try_login(self):
        try:
            self.services.auth.login(self.username.text(), self.password.text())
        except WMSError as e:
            self.message.setText(e.message)
            self.message.show()
            self.password.selectAll()
            self.password.setFocus()
            return
        except Exception as e:  # noqa: BLE001
            show_error(self, e)
            return
        self.accept()


class ChangePasswordDialog(QDialog):
    def __init__(self, services: Services, forced: bool = False, parent=None):
        super().__init__(parent)
        self.services = services
        self.setWindowTitle("Change password")
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        if forced:
            lay.addWidget(label("Please choose a new password before you continue.", "InfoBanner", wrap=True))
        form = QFormLayout()
        self.current = QLineEdit()
        self.new = QLineEdit()
        self.repeat = QLineEdit()
        for w in (self.current, self.new, self.repeat):
            w.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Current password", self.current)
        form.addRow("New password", self.new)
        form.addRow("Repeat new password", self.repeat)
        lay.addLayout(form)
        ok = button("Change password", self.save, primary=True)
        ok.setDefault(True)
        lay.addLayout(h(None, button("Cancel", self.reject), ok))

    def save(self):
        if self.new.text() != self.repeat.text():
            show_error(self, WMSError("The new passwords do not match."))
            return
        try:
            self.services.auth.change_own_password(self.current.text(), self.new.text())
        except Exception as e:  # noqa: BLE001
            show_error(self, e)
            return
        self.accept()
