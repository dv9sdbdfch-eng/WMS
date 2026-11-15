"""Update windows: "a new version is available", download with progress, backup, install.

The checks and downloads run in a background thread so the program never freezes while it waits
for the internet. See ``wms.services.updater`` for how an update is kept safe.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QMessageBox, QPlainTextEdit, QProgressDialog, QVBoxLayout

from wms import APP_NAME, __version__
from wms.services import updater
from wms.ui.widgets.common import button, h, label

log = logging.getLogger(__name__)


class CheckThread(QThread):
    """Ask the update server in the background. Emits found(UpdateInfo), up_to_date() or failed(str)."""

    found = Signal(object)
    up_to_date = Signal()
    failed = Signal(str)

    def run(self):  # noqa: D401 - Qt thread body
        try:
            info = updater.check()
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a check must never take the program down
            log.exception("Update check failed")
            self.failed.emit(f"The update check failed: {exc}")
            return
        if info is None:
            self.up_to_date.emit()
        else:
            self.found.emit(info)


class DownloadThread(QThread):
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, info: updater.UpdateInfo, dest, parent=None):
        super().__init__(parent)
        self.info = info
        self.dest = dest
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            path = updater.download(self.info, self.dest, progress=self.progress.emit,
                                    cancelled=lambda: self._cancel)
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Update download failed")
            self.failed.emit(f"The download failed: {exc}")
            return
        self.done.emit(str(path))


class _Results(QObject):
    """Lives in the window's thread: signals from a worker thread arrive here queued, so nothing on
    screen is ever touched from the background thread."""

    def __init__(self, parent=None, on_progress=None):
        super().__init__(parent)
        self.values: dict = {}
        self._on_progress = on_progress

    @Slot(object)
    def found(self, info):
        self.values["info"] = info

    @Slot(str)
    def done(self, path):
        self.values["path"] = path

    @Slot(str)
    def failed(self, error):
        self.values["error"] = error

    @Slot(int, int)
    def progress(self, done, total):
        if self._on_progress:
            self._on_progress(done, total)


def _run_check(parent=None) -> dict:
    """Run a CheckThread while the window keeps painting; returns {"info": ...} / {"error": ...} / {}."""
    from PySide6.QtCore import QEventLoop

    thread = CheckThread(parent)
    res = _Results(parent)
    loop = QEventLoop()
    thread.found.connect(res.found)
    thread.failed.connect(res.failed)
    thread.finished.connect(loop.quit)
    thread.start()
    if not thread.isFinished():
        loop.exec()
    thread.wait()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()  # deliver the queued result
    return res.values


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


class UpdateDialog(QDialog):
    """Shows the new version and installs it.

    ``main_window`` (optional): the open program window. Before installing, its screens are asked
    to save or discard unsaved work, a backup is made (SQLite) and the window closes when the
    installer has started. Without it (start-up, before sign-in) the caller closes the program
    when ``installing`` is True after the dialog."""

    def __init__(self, info: updater.UpdateInfo, cfg, services=None, main_window=None, required: bool = False,
                 parent=None):
        super().__init__(parent or main_window)
        self.info = info
        self.cfg = cfg
        self.services = services
        self.main_window = main_window
        self.installing = False
        self._thread = None
        self.setWindowTitle("Update available")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{APP_NAME} {info.version} is available", "SectionTitle"))
        lines = [f"This PC has version {__version__}."]
        if required:
            lines.append("The database has already been upgraded by a newer version on another PC, so this PC "
                         "must be updated before it can open it.")
        lines.append("Your data stays where it is: the update replaces only the program files. "
                     + ("A backup of this PC's database is made first. " if cfg.is_sqlite else
                        "The shared server keeps its own backups. ")
                     + "The program closes, installs the new version (about a minute) and opens again.")
        if cfg.server_host:
            lines.append("Shared server: once one PC has the new version, the other PCs will be asked to update "
                         "too when they next start.")
        lay.addWidget(label("\n\n".join(lines), None, wrap=True))
        if info.notes:
            lay.addWidget(label("What is new:", "Muted"))
            notes = QPlainTextEdit(info.notes)
            notes.setReadOnly(True)
            notes.setMaximumHeight(160)
            lay.addWidget(notes)
        if not updater.can_install_here():
            lay.addWidget(label("This copy is not the installed Windows program, so the update cannot install "
                                "itself here. 'Download page' opens the release in the browser.", "InfoBanner",
                                wrap=True))
            self.install_btn = button("Download page", self._open_page, primary=True)
        else:
            self.install_btn = button("Install now", self._install, primary=True)
        later = button("Later", self.reject)
        btns = [self.install_btn, later]
        if not required:
            btns.insert(1, button("Skip this version", self._skip,
                                  tooltip="Do not offer this version again (you can still install it from "
                                          "Settings > About)"))
        lay.addLayout(h(None, *btns))

    # -- actions -----------------------------------------------------------------
    def _open_page(self):
        QDesktopServices.openUrl(QUrl(self.info.page_url))
        self.accept()

    def _skip(self):
        self.cfg.skipped_update_version = self.info.version
        try:
            self.cfg.save()
        except OSError:
            log.exception("Could not save the skipped version")
        self.reject()

    def _install(self):
        if self.main_window is not None and not self.main_window.can_close_for_update():
            return  # unsaved work kept; the user can install later
        self.install_btn.setEnabled(False)
        prog = QProgressDialog(f"Downloading {APP_NAME} {self.info.version}...", "Cancel", 0, 100, self)
        prog.setWindowTitle("Update")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        def on_progress(done, total):
            if total:
                prog.setMaximum(100)
                prog.setValue(min(int(done * 100 / total), 100))
                prog.setLabelText(f"Downloading {APP_NAME} {self.info.version}... {_mb(done)} of {_mb(total)}")
            else:
                prog.setMaximum(0)

        thread = DownloadThread(self.info, updater.updates_dir(self.cfg), self)
        self._thread = thread
        res = _Results(self, on_progress)
        thread.progress.connect(res.progress)
        thread.done.connect(res.done)
        thread.failed.connect(res.failed)
        thread.finished.connect(prog.close)
        prog.canceled.connect(thread.cancel)
        thread.start()
        prog.exec()
        thread.wait()
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        result = res.values
        self.install_btn.setEnabled(True)
        if "path" not in result:
            if result.get("error") and result["error"] != "Download cancelled.":
                QMessageBox.warning(self, "Update", result["error"])
            return
        if not self._backup_first():
            return
        logs = self.cfg.logs_path
        try:
            logs.mkdir(parents=True, exist_ok=True)
            updater.launch_installer(result["path"], logs / "update-install.log")
        except updater.UpdateError as exc:
            QMessageBox.warning(self, "Update", str(exc))
            return
        log.info("Installing update %s -> %s from %s", __version__, self.info.version, result["path"])
        self.installing = True
        self.accept()
        if self.main_window is not None:
            self.main_window.close_for_update()

    def _backup_first(self) -> bool:
        """SQLite: a full backup before the program files change. A failed backup stops the update
        unless the user chooses to go on."""
        if not self.cfg.is_sqlite or self.services is None:
            return True
        try:
            path = self.services.backup.create(note=f"Before update {__version__} -> {self.info.version}",
                                               automatic=True, kind="update")
            log.info("Backup before update written to %s", path)
            return True
        except Exception as exc:  # noqa: BLE001
            box = QMessageBox(QMessageBox.Icon.Warning, "Update",
                              f"The backup before the update did not work:\n\n{getattr(exc, 'message', exc)}\n\n"
                              f"It is safer to fix the backup first (Settings > Backup and restore).", parent=self)
            box.addButton("Stop", QMessageBox.ButtonRole.RejectRole)
            go = box.addButton("Update without a backup", QMessageBox.ButtonRole.DestructiveRole)
            box.exec()
            return box.clickedButton() is go


def check_now(parent, cfg, services=None, main_window=None) -> None:
    """Settings > About: check straight away and say what was found (also when up to date)."""
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        result = _run_check(parent)
    finally:
        QGuiApplication.restoreOverrideCursor()
    if "error" in result:
        QMessageBox.warning(parent, "Updates", result["error"])
    elif "info" in result:
        UpdateDialog(result["info"], cfg, services, main_window, parent=parent).exec()
    else:
        QMessageBox.information(parent, "Updates", f"This PC has the newest version ({__version__}).")


def offer_required_update(cfg) -> bool:
    """Start-up, before sign-in: the database is newer than this program (another PC was updated).
    Check for the update and offer it. True when the installer was started (the caller closes)."""
    result = _run_check()
    if "info" not in result:
        QMessageBox.warning(None, APP_NAME, "No newer version could be downloaded automatically"
                            + (f":\n\n{result['error']}" if "error" in result else ".")
                            + f"\n\nInstall the newest version from {updater.RELEASES_PAGE}")
        return False
    dlg = UpdateDialog(result["info"], cfg, required=True)
    dlg.exec()
    return dlg.installing
