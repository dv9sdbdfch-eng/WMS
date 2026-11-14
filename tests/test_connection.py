"""Shared server connection: settings, password store, version guard and the Connection window."""

from __future__ import annotations

import pytest
from conftest import PG_URL
from sqlalchemy.engine import make_url

from wms.config import AppConfig, server_url
from wms.core.exceptions import SchemaTooNewError
from wms.db.engine import Database


def test_server_settings_build_the_url_and_keep_the_password_out_of_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WMS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WMS_DB_PASSWORD", "p@ss:/w")
    c = AppConfig.load()
    assert c.is_sqlite and c.server_label == "This PC only"
    c.server_host = "192.168.1.10"
    assert not c.is_sqlite and c.server_label == "Server 192.168.1.10"
    url = make_url(c.resolved_database_url)
    assert (url.drivername, url.host, url.port, url.database, url.username, url.password) == \
        ("postgresql+psycopg", "192.168.1.10", 5432, "storewms", "storewms", "p@ss:/w")
    c.save()
    assert "p@ss" not in c.config_file.read_text(encoding="utf-8")
    assert AppConfig.load().server_host == "192.168.1.10"
    assert "%40" in server_url("h", 5432, "d", "u", "p@x")  # special characters are escaped


def test_older_program_refuses_a_database_upgraded_by_a_newer_one(cfg):
    from sqlalchemy import text
    db = Database(cfg.resolved_database_url)
    db.migrate()
    with db.engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    with pytest.raises(SchemaTooNewError, match="Install the latest version"):
        db.migrate()
    with db.engine.connect() as c:  # nothing was changed
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "9999"
    db.dispose()


def test_friendly_messages_for_busy_and_lost_server():
    from wms.core.exceptions import ConcurrencyError, DatabaseBusyError, DatabaseUnavailableError
    from wms.db.engine import translate_db_error

    class Orig(Exception):
        def __init__(self, state):
            super().__init__(state)
            self.sqlstate = state

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm.exc import StaleDataError
    assert isinstance(translate_db_error(OperationalError("x", {}, Orig("40P01"))), DatabaseBusyError)
    assert isinstance(translate_db_error(OperationalError("x", {}, Orig("55P03"))), DatabaseBusyError)
    assert isinstance(translate_db_error(OperationalError("x", {}, Orig("08006"))), DatabaseUnavailableError)
    assert isinstance(translate_db_error(StaleDataError("x")), ConcurrencyError)
    assert translate_db_error(OperationalError("x", {}, Orig("23505"))) is None  # a real bug stays visible
    assert translate_db_error(ValueError("x")) is None


@pytest.mark.skipif(not PG_URL, reason="needs a PostgreSQL server (WMS_TEST_PG)")
def test_check_server_says_what_is_wrong(pg_database):
    from wms.services.connection import check_server
    u = make_url(pg_database)
    host = u.host or "127.0.0.1"
    ok = check_server(host, u.port or 5432, u.database, u.username, u.password)
    assert ok.ok and "ready" in ok.message, ok
    bad = check_server(host, u.port or 5432, "no_such_db", u.username, u.password)
    assert not bad.ok and "no database called 'no_such_db'" in bad.message
    assert not check_server("", 5432, "d", "u", "p").ok


def test_connection_window_saves_server_and_password(qtbot, cfg, monkeypatch):
    import wms.ui.dialogs.connection as mod
    from wms.services.connection import ServerCheck
    stored = {}
    monkeypatch.setattr(mod, "check_server", lambda *a, **k: ServerCheck(True, "Connected."))
    monkeypatch.setattr(mod, "save_server_password", lambda h, u, p: stored.update({(h, u): p}) or True)
    monkeypatch.setattr(mod, "info", lambda *a, **k: None)
    dlg = mod.ConnectionDialog(cfg)
    qtbot.addWidget(dlg)
    assert dlg.local.isChecked() and not dlg.host.isEnabled()
    dlg.server.setChecked(True)
    dlg.host.setText(" 10.0.5.2 ")
    dlg.password.setText("secret")
    dlg.save()
    assert dlg.saved and stored == {("10.0.5.2", "storewms"): "secret"}
    again = AppConfig.load(cfg.data_dir)
    assert again.server_host == "10.0.5.2" and "secret" not in cfg.config_file.read_text(encoding="utf-8")
    # a failed test keeps the window open and saves nothing
    monkeypatch.setattr(mod, "check_server", lambda *a, **k: ServerCheck(False, "No answer."))
    dlg2 = mod.ConnectionDialog(again)
    qtbot.addWidget(dlg2)
    dlg2.host.setText("10.0.5.3")
    dlg2.save()
    assert not dlg2.saved and "No answer" in dlg2.result.text()
    assert AppConfig.load(cfg.data_dir).server_host == "10.0.5.2"


def test_sign_in_shows_where_the_data_is(qtbot, svc):
    from PySide6.QtWidgets import QLabel, QPushButton

    from wms.ui.login import LoginDialog
    dlg = LoginDialog(svc)
    qtbot.addWidget(dlg)
    assert "Connection..." in [b.text() for b in dlg.findChildren(QPushButton)]
    expected = "This PC only" if svc.ctx.config.is_sqlite else "Server"
    assert any(expected in lab.text() for lab in dlg.findChildren(QLabel))
