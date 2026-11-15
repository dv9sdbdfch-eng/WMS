"""Photos and scans on the shared server: a PC in another building sees them without a network share."""

from __future__ import annotations

import pytest
from conftest import PG_URL

from wms.config import AppConfig
from wms.db.engine import Database
from wms.services import Services
from wms.services.base import AppContext

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a PostgreSQL server (WMS_TEST_PG)")


def _jpeg(path, colour=0xFF8800, w=900, h=600):
    from PySide6.QtGui import QImage
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(colour)
    assert img.save(str(path), "JPEG", 85)
    return path


def other_pc(cfg, tmp_path):
    """A second PC: same server, its own (empty) data and attachments folders."""
    c = AppConfig.load(tmp_path / "pc2")
    c.database_url = cfg.database_url
    c.ensure_dirs()
    svc = Services(AppContext(db=Database(c.resolved_database_url), config=c))
    svc.auth.login("admin", "admin")
    return svc


def test_photo_added_on_one_pc_opens_on_another(svc, sample, cfg, tmp_path, qapp):
    photo = _jpeg(tmp_path / "mdf.jpg")
    att = svc.attachments.add("PRODUCT", sample.mdf.id, photo)
    pc2 = other_pc(cfg, tmp_path)
    try:
        assert not (pc2.ctx.config.attachments_path / att.stored_name).exists()
        # the list shows a small copy first (fetched once), not the whole photo
        thumb = pc2.attachments.thumbnail_path(att.stored_name)
        assert thumb is not None and thumb.is_file() and thumb.stat().st_size < photo.stat().st_size
        assert pc2.attachments.thumbnail_path(att.stored_name) == thumb  # cached
        # opening it downloads the full file, byte for byte
        [a2] = pc2.attachments.list_for("PRODUCT", sample.mdf.id)
        path = pc2.attachments.path_of(a2)
        assert path.read_bytes() == photo.read_bytes()
        assert pc2.attachments.missing_files() == []
    finally:
        pc2.ctx.db.dispose()


def test_same_file_twice_is_stored_once_and_rollback_leaves_nothing(svc, sample, tmp_path, qapp):
    from sqlalchemy import func, select

    from wms.db.models import AttachmentFile
    scan = _jpeg(tmp_path / "dn.jpg", 0x2255AA)
    svc.attachments.add("PRODUCT", sample.mdf.id, scan)
    svc.attachments.add("PRODUCT", sample.edge.id, scan)
    with svc.ctx.db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(AttachmentFile)) == 1
    staged = svc.attachments.stage(_jpeg(tmp_path / "other.jpg", 0x00AA00))
    with pytest.raises(RuntimeError), svc.ctx.db.session() as s:
        svc.attachments.attach_staged(s, "PRODUCT", sample.mdf.id, staged)
        raise RuntimeError("the document failed to post")
    svc.attachments.discard([staged])
    with svc.ctx.db.read_session() as s:
        assert s.scalar(select(func.count()).select_from(AttachmentFile)) == 1
