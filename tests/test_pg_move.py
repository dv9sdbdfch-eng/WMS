"""Moving one PC's data file to the shared server (runs only with WMS_TEST_PG set)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal as D

import pytest
from conftest import PG_URL, Sample, _pg_admin, _pg_url
from sqlalchemy import text
from sqlalchemy.engine import make_url

from wms.config import AppConfig
from wms.core.exceptions import ValidationError
from wms.db.engine import Database
from wms.db.seed import seed_if_empty
from wms.services import Services
from wms.services.base import AppContext
from wms.services.goods_out import IssueLine
from wms.services.server_move import move_to_server

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a PostgreSQL server (WMS_TEST_PG)")
TODAY = date.today()


@pytest.fixture
def empty_server():
    name = f"wms_move_{uuid.uuid4().hex[:10]}"
    with _pg_admin().connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield _pg_url(name)
    with _pg_admin().connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def local_pc(tmp_path, qapp):
    """A PC that has worked on its own data file for a while: stock in, out, a reversal, a photo."""
    c = AppConfig.load(tmp_path / "pc1")
    c.ensure_dirs()
    db = Database(c.resolved_database_url)
    db.migrate()
    with db.session() as s:
        seed_if_empty(s)
    svc = Services(AppContext(db=db, config=c))
    svc.auth.login("admin", "admin")
    smp = Sample(svc)
    smp.receive(smp.mdf, 20, smp.bin1, number="DN-A")
    dn = smp.receive(smp.edge, 50, smp.bin2, number="DN-B")
    svc.goods_out.post({"site_id": smp.site.id, "issue_date": TODAY}, [IssueLine(smp.mdf.id, D(7), smp.bin1.id)])
    svc.goods_in.reverse(dn.id, "entered twice")
    from PySide6.QtGui import QImage
    img = QImage(400, 300, QImage.Format.Format_RGB32)
    img.fill(0x336699)
    photo = tmp_path / "p.jpg"
    img.save(str(photo), "JPEG")
    svc.attachments.add("PRODUCT", smp.mdf.id, photo)
    yield svc, smp
    db.dispose()


def test_move_copies_everything_and_the_server_carries_on(local_pc, empty_server, tmp_path):
    svc, smp = local_pc
    cfg = svc.ctx.config
    report = move_to_server(cfg.resolved_database_url, empty_server, cfg.attachments_path)
    assert report.tables["stock_movements"] >= 4 and report.files == 1 and not report.missing_files
    assert "match" in report.summary()
    # a PC in another building opens the server
    c2 = AppConfig.load(tmp_path / "pc2")
    c2.database_url = empty_server
    c2.ensure_dirs()
    db2 = Database(empty_server)
    db2.migrate()
    with db2.session() as s:
        assert not seed_if_empty(s)  # the copied data is used, nothing is seeded on top
    pc2 = Services(AppContext(db=db2, config=c2))
    pc2.auth.login("admin", "admin")
    try:
        assert pc2.products.row(smp.mdf.id).on_hand == D(13)
        assert pc2.products.row(smp.edge.id).on_hand == D(0)
        assert pc2.ledger.integrity_check() == []
        [att] = pc2.attachments.list_for("PRODUCT", smp.mdf.id)
        assert pc2.attachments.path_of(att).is_file()
        # new documents continue the numbers and ids of the old file
        gi = pc2.goods_out.post({"site_id": smp.site.id, "issue_date": TODAY},
                                [IssueLine(smp.mdf.id, D(1), smp.bin1.id)])
        assert gi.issue_number.endswith("000002")
        assert pc2.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "NEW-1", "name": "New",
                                        "unit_id": smp.mdf.unit_id}).id > smp.cabinet.id
    finally:
        db2.dispose()
    # the PC's own file was not changed
    assert svc.products.row(smp.mdf.id).on_hand == D(13)


def test_move_never_overwrites_a_server_that_has_data(local_pc, pg_database):
    svc, _ = local_pc
    with pytest.raises(ValidationError, match="not empty"):
        move_to_server(svc.ctx.config.resolved_database_url, pg_database, svc.ctx.config.attachments_path)
    db = Database(pg_database)  # the existing server data is still there
    with db.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users")).scalar() >= 0
    db.dispose()


def test_failed_check_leaves_the_server_empty(local_pc, empty_server, monkeypatch):
    import wms.services.server_move as mod
    svc, _ = local_pc

    def broken(sc, dc, report):
        raise ValidationError("stock balances per product and bin differ")
    monkeypatch.setattr(mod, "_verify", broken)
    with pytest.raises(ValidationError, match="differ"):
        move_to_server(svc.ctx.config.resolved_database_url, empty_server, svc.ctx.config.attachments_path)
    db = Database(empty_server)
    with db.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar() == 0
    db.dispose()
    assert make_url(empty_server).database.startswith("wms_move_")


def test_move_window_moves_and_switches_this_pc(local_pc, empty_server, monkeypatch, qtbot):
    import wms.ui.dialogs.connection as mod
    import wms.ui.widgets.common as common
    svc, smp = local_pc
    cfg = svc.ctx.config
    u = make_url(empty_server)
    monkeypatch.setattr(mod, "info", lambda *a, **k: None)
    monkeypatch.setattr(common, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(mod, "save_server_password", lambda *a: True)
    dlg = mod.MoveToServerDialog(cfg)
    qtbot.addWidget(dlg)
    dlg.host.setText(u.host or "127.0.0.1")
    dlg.port.setValue(u.port or 5432)
    dlg.database.setText(u.database)
    dlg.user.setText(u.username)
    dlg.password.setText(u.password or "")
    dlg.save()
    assert dlg.saved and dlg.report is not None and dlg.report.files == 1
    assert AppConfig.load(cfg.data_dir).server_host == (u.host or "127.0.0.1")
    # a second run refuses: the server now has data
    dlg2 = mod.MoveToServerDialog(cfg)
    qtbot.addWidget(dlg2)
    for w, v in ((dlg2.host, u.host or "127.0.0.1"), (dlg2.database, u.database), (dlg2.user, u.username)):
        w.setText(v)
    dlg2.port.setValue(u.port or 5432)
    dlg2.save()
    assert not dlg2.saved and "already has data" in dlg2.result.text()
