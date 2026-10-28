"""Program entry point: start-up, login, main window, restart after restore."""

from __future__ import annotations

import logging
import subprocess
import sys
import traceback
from logging.handlers import RotatingFileHandler

from wms import APP_ID, APP_NAME, __version__
from wms.config import AppConfig

log = logging.getLogger("wms")


def setup_logging(cfg: AppConfig) -> None:
    cfg.logs_path.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(cfg.logs_path / "wms.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("alembic").setLevel(logging.WARNING)
    if sys.stderr:
        root.addHandler(logging.StreamHandler())


def _install_excepthook(qt_app) -> None:
    """Log any unexpected error and tell the user instead of silently closing."""
    from PySide6.QtWidgets import QMessageBox

    def hook(exc_type, exc, tb):
        log.error("Unhandled error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            QMessageBox.critical(None, APP_NAME, f"An unexpected error occurred:\n\n{exc}\n\n"
                                                 "Details were written to the log file. Your data is safe: "
                                                 "nothing is saved unless an operation completes fully.")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def _start_database(cfg: AppConfig):
    from wms.db.engine import Database
    from wms.db.seed import seed_if_empty

    db = Database(cfg.resolved_database_url)
    db.migrate()
    with db.session() as s:
        if seed_if_empty(s):
            log.info("New database created at %s", cfg.db_path)
    return db


def self_test(cfg: AppConfig) -> int:
    """``StoreWMS.exe --self-test``: open/upgrade the database and run the data check, without the window."""
    from wms.services import Services
    from wms.services.base import AppContext

    db = _start_database(cfg)
    rep = Services(AppContext(db=db, config=cfg)).backup.integrity_check()
    from wms.exporters.export import (
        export_pdf,  # noqa: F401 - proves the PDF/Excel libraries are bundled
        export_xlsx,  # noqa: F401
    )
    db.dispose()
    print(f"{APP_NAME} {__version__} self-test: database {'OK' if rep.database_ok else 'DAMAGED'}, "
          f"{len(rep.stock_discrepancies)} stock mismatch(es), {len(rep.missing_files)} missing file(s)")
    return 0 if rep.ok else 2


def run() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    cfg = AppConfig.load()
    cfg.ensure_dirs()
    if not cfg.config_file.exists():
        cfg.save()
    setup_logging(cfg)
    log.info("%s %s starting; data folder %s", APP_NAME, __version__, cfg.data_dir)
    if "--self-test" in sys.argv:
        return self_test(cfg)

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setApplicationName(APP_NAME)
    qt_app.setOrganizationName(APP_ID)
    qt_app.setApplicationVersion(__version__)
    from wms.ui.theme import apply_theme
    apply_theme(qt_app)
    from wms.resources import icon_path
    if icon_path().exists():
        qt_app.setWindowIcon(QIcon(str(icon_path())))
    _install_excepthook(qt_app)

    try:
        db = _start_database(cfg)
    except Exception as exc:  # noqa: BLE001
        log.exception("Database start-up failed")
        QMessageBox.critical(None, APP_NAME, f"The database could not be opened:\n\n{exc}\n\n"
                                             f"Data folder: {cfg.data_dir}\nIf this persists, restore the latest "
                                             f"backup or contact support with the log file from {cfg.logs_path}.")
        return 1

    from wms.services import Services
    from wms.services.base import AppContext
    from wms.ui.app import App
    from wms.ui.login import ChangePasswordDialog, LoginDialog
    from wms.ui.main_window import MainWindow

    state = {"logout": False, "window": None, "app": None}

    def on_logout(window):
        state["logout"] = True
        window.close()

    while True:
        ctx = AppContext(db=db, config=cfg)
        services = Services(ctx)
        if LoginDialog(services).exec() != LoginDialog.DialogCode.Accepted:
            break
        if ctx.user.must_change_password:
            if ChangePasswordDialog(services, forced=True).exec() != ChangePasswordDialog.DialogCode.Accepted:
                continue
        app = App(ctx)
        window = MainWindow(app, on_logout=on_logout)
        state.update(logout=False, window=window, app=app)
        window.showMaximized()
        qt_app.exec()
        if getattr(app, "restart_required", False):
            log.info("Restarting after restore")
            _restart()
            break
        if not state["logout"]:
            break
        log.info("User %s logged out", ctx.user.username)

    db.dispose()
    log.info("%s closed", APP_NAME)
    return 0


def _restart() -> None:
    """Start a fresh copy of the program (used after restoring a backup)."""
    if getattr(sys, "frozen", False):
        args = [sys.executable, *sys.argv[1:]]
    else:
        args = [sys.executable, "-m", "wms", *sys.argv[1:]]
    subprocess.Popen(args, close_fds=True)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
