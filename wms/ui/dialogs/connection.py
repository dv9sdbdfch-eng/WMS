"""Connection window: where this PC's data is - its own file, or the shared server."""

from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QDialog, QFormLayout, QLineEdit, QRadioButton, QSpinBox, QVBoxLayout

from wms.config import AppConfig, save_server_password, server_password
from wms.services.connection import check_server
from wms.ui.widgets.common import button, h, info, label


class ConnectionDialog(QDialog):
    """Choose "This PC only" (SQLite file) or "Shared server" (PostgreSQL), test it, save it.

    The password goes to Windows Credential Manager, not to config.json. Saving asks for a
    restart: the program opens its database once, at start-up."""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.saved = False
        self.setWindowTitle("Connection")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Where Store WMS keeps its data.", "SectionTitle"))
        self.local = QRadioButton("This PC only (data file on this computer)")
        self.server = QRadioButton("Shared server - all PCs see the same stock")
        grp = QButtonGroup(self)
        for b in (self.local, self.server):
            grp.addButton(b)
            lay.addWidget(b)
        form = QFormLayout()
        self.host = QLineEdit(cfg.server_host)
        self.host.setPlaceholderText("Server PC name or IP address, e.g. 192.168.1.10")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(cfg.server_port or 5432))
        self.port.setMaximumWidth(100)
        self.database = QLineEdit(cfg.server_database)
        self.user = QLineEdit(cfg.server_user)
        self.password = QLineEdit(server_password(cfg.server_host, cfg.server_user) or "" if cfg.server_host else "")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Server", self.host)
        form.addRow("Port", self.port)
        form.addRow("Database", self.database)
        form.addRow("Database user", self.user)
        form.addRow("Password", self.password)
        lay.addLayout(form)
        self.result = label("", "Muted", wrap=True)
        lay.addWidget(self.result)
        lay.addWidget(label("Your administrator gives you these details. The password is kept in Windows "
                            "Credential Manager on this PC, not in a file.", "Muted", wrap=True))
        self.test_btn = button("Test connection", self.test)
        self.save_btn = button("Save", self.save, primary=True)
        lay.addLayout(h(self.test_btn, None, button("Cancel", self.reject), self.save_btn))
        (self.server if cfg.server_host else self.local).setChecked(True)
        self.local.toggled.connect(lambda _: self._mode())
        self._mode()

    def _fields(self):
        return (self.host, self.port, self.database, self.user, self.password)

    def _mode(self) -> None:
        on = self.server.isChecked()
        for w in self._fields():
            w.setEnabled(on)
        self.test_btn.setEnabled(on)
        self.result.setText("")

    def test(self):
        check = check_server(self.host.text(), self.port.value(), self.database.text(), self.user.text(),
                             self.password.text())
        self.result.setText(("OK: " if check.ok else "Not connected: ") + check.message)
        self.result.setObjectName("InfoBanner" if check.ok else "Banner")
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        return check

    def save(self) -> None:
        cfg = self.cfg
        if self.server.isChecked():
            check = self.test()
            if not check.ok:
                return
            host, user = self.host.text().strip(), self.user.text().strip()
            if not save_server_password(host, user, self.password.text()):
                info(self, "Connection", "This PC has no password store, so the password could not be kept. "
                                         "Ask your administrator to set WMS_DB_PASSWORD on this PC.")
                return
            cfg.server_host, cfg.server_port = host, self.port.value()
            cfg.server_database, cfg.server_user = self.database.text().strip(), user
        else:
            cfg.server_host = ""
        cfg.database_url = ""
        cfg.save()
        self.saved = True
        info(self, "Connection", "Saved. Store WMS will now close; start it again to use the new connection.")
        self.accept()


class MoveToServerDialog(ConnectionDialog):
    """Settings > "Move data to the shared server...": copy this PC's data file to a new, empty
    server database, check the copy, then switch this PC to the server (restart)."""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(cfg, parent)
        self.setWindowTitle("Move data to the shared server")
        self.report = None
        self.server.setChecked(True)
        self.local.hide()
        self.server.hide()
        self.save_btn.setText("Move data")
        self.layout().insertWidget(1, label(
            "Copies everything on this PC (stock, documents, history, users, photos and scans) to the new server "
            "database, then checks the copy row by row. If anything differs, the server is left empty and this "
            "PC carries on as before. This PC's own file is never changed. Everyone else must close Store WMS "
            "first.", "InfoBanner", wrap=True))

    def save(self) -> None:
        from wms.config import server_url
        from wms.services.server_move import move_to_server
        from wms.ui.widgets.common import confirm, run
        check = self.test()
        if not check.ok:
            return
        if not check.new_database:
            self.result.setText("Not moved: the server database already has data. The move only fills a new, "
                                "empty database.")
            return
        if not confirm(self, "Move data", "Copy all data from this PC to the server now?\n\nIt can take a few "
                                          "minutes. Do not close the program.", yes="Move data"):
            return
        host, user, pwd = self.host.text().strip(), self.user.text().strip(), self.password.text()
        url = server_url(host, self.port.value(), self.database.text().strip(), user, pwd)
        self.report = run(self, move_to_server, self.cfg.resolved_database_url, url, self.cfg.attachments_path,
                          lambda m: self.result.setText(m))
        if self.report is None:
            return
        if not save_server_password(host, user, pwd):
            info(self, "Move data", "The data was moved, but this PC has no password store. Ask your "
                                    "administrator to set WMS_DB_PASSWORD on this PC.")
        self.cfg.server_host, self.cfg.server_port = host, self.port.value()
        self.cfg.server_database, self.cfg.server_user = self.database.text().strip(), user
        self.cfg.database_url = ""
        self.cfg.save()
        self.saved = True
        info(self, "Move data", f"Done. {self.report.summary()}\n\nStore WMS will now restart and work from the "
                                f"server. Then set up the other PCs with Connection... on their sign-in screen.")
        self.accept()
