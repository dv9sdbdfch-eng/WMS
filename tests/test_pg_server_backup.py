"""Every PC shows the shared server's backups (written by server/backup.ps1 into server_backups)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import PG_URL

from wms.core.numbers import utcnow
from wms.db.models import ServerBackup

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a PostgreSQL server (WMS_TEST_PG)")


def add(svc, hours_ago, ok=True, msg=None):
    with svc.ctx.db.session() as s:
        s.add(ServerBackup(finished_at=utcnow() - timedelta(hours=hours_ago), kind="HOURLY", ok=ok,
                           file_name=f"storewms_{hours_ago}.backup", size_bytes=1000, message=msg))


def test_server_backup_health_and_dashboard_alert(svc):
    h = svc.backup.health()
    assert h.server and h.state == "NEVER" and "server PC" in h.message
    assert any(a.key == "backup" for a in svc.dashboard.load().alerts)
    add(svc, 30)
    assert svc.backup.health().state == "STALE"
    add(svc, 1)
    h = svc.backup.health()
    assert h.ok and h.last_file == "storewms_1.backup"
    add(svc, 0, ok=False, msg="disk full")
    h = svc.backup.health()
    assert h.state == "FAILED" and "disk full" in h.message
    assert svc.backup.create_safety("before import") is None  # the PC never copies the server itself
