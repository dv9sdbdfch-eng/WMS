"""Phase 1, Stage 7 - reliability: backup health and schedule, a second backup copy, restore audit,
test restore, start-up recovery from a damaged database, failures during backup, and the program
being killed in the middle of posting stock."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from wms.core.exceptions import ValidationError
from wms.db.engine import Database
from wms.services import Services
from wms.services.backup import RESTORE_MARKER, emergency_restore, latest_backup
from wms.services.base import AppContext

D = Decimal


def reopen(cfg) -> Services:
    db = Database(cfg.resolved_database_url)
    db.migrate()
    s = Services(AppContext(db=db, config=cfg))
    s.auth.login("admin", "admin")
    return s


def actions(svc, action) -> list:
    return svc.audit.search(action=action)


# --- backup health and schedule ----------------------------------------------------------------

def test_health_from_never_to_ok_and_the_schedule(svc, cfg):
    h = svc.backup.health()
    assert h.state == "NEVER" and not h.ok and "No backup" in h.message
    assert svc.backup.is_due()
    path = svc.backup.backup_if_due()
    assert path is not None and path.name.endswith("_auto.zip")
    h = svc.backup.health()
    assert h.state == "OK" and h.ok and h.last_file == path.name
    assert h.last_success_text.startswith("Today — ")
    assert svc.backup.backup_if_due() is None  # not due again for 24 h
    # 25 h later: due (still fine), 3 days later: stale (warning)
    st = json.loads(svc.backup.status_path.read_text())
    st["last_success_at"] = (datetime.now() - timedelta(hours=25)).isoformat()
    svc.backup.status_path.write_text(json.dumps(st))
    assert svc.backup.health().state == "DUE" and svc.backup.is_due()
    st["last_success_at"] = (datetime.now() - timedelta(days=3)).isoformat()
    svc.backup.status_path.write_text(json.dumps(st))
    h = svc.backup.health()
    assert h.state == "STALE" and not h.ok and "No successful backup since" in h.message
    cfg.auto_backup_hours = 0
    assert not svc.backup.is_due()  # schedule switched off - but the Dashboard still warns
    assert svc.backup.health().state == "STALE"


def test_status_file_lost_falls_back_to_the_backup_folder(svc):
    path = svc.backup.create(note="manual")
    svc.backup.status_path.unlink()
    h = svc.backup.health()
    assert h.state == "OK" and h.last_file == path.name


def test_failed_backup_is_recorded_shown_and_audited(svc, cfg, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    cfg.backup_dir = str(blocker / "backups")  # cannot be created: a file is in the way
    with pytest.raises(OSError):
        svc.backup.create(note="manual")
    h = svc.backup.health()
    assert h.state == "FAILED" and not h.ok and "FAILED" in h.message
    assert svc.dashboard.load().alerts[0].text == h.message  # first thing on the Dashboard
    assert actions(svc, "BACKUP_FAILED")
    assert svc.backup.is_due()  # a failed attempt is retried by the schedule
    cfg.backup_dir = str(tmp_path / "ok")
    svc.backup.create()
    assert svc.backup.health().state == "OK"
    assert not any("backup" in a.text.lower() for a in svc.dashboard.load().alerts)


def test_disk_full_while_writing_leaves_no_half_backup(svc, cfg, monkeypatch):
    real = zipfile.ZipFile.writestr

    def full(self, name, data, *a, **k):
        if name == "manifest.json":
            raise OSError(28, "No space left on device")
        return real(self, name, data, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(zipfile.ZipFile, "writestr", full)
        with pytest.raises(OSError, match="No space"):
            svc.backup.create()
    assert list(cfg.backup_path.glob("*")) == []  # no .part, no broken .zip
    assert svc.backup.health().state == "FAILED"


def test_second_copy_folder(svc, cfg, tmp_path):
    cfg.backup_copy_dir = str(tmp_path / "usb")
    cfg.backups_to_keep = 3
    made = [svc.backup.create(automatic=True) for _ in range(5)]
    copies = sorted(p.name for p in (tmp_path / "usb").glob("*.zip"))
    assert copies == sorted(p.name for p in made[-3:])  # same generations as the main folder
    assert svc.backup.verify(tmp_path / "usb" / made[-1].name)
    # USB stick pulled out: the backup itself still works, the missing copy is a warning
    (tmp_path / "usb-gone").write_text("x")
    cfg.backup_copy_dir = str(tmp_path / "usb-gone" / "sub")
    svc.backup.create()
    h = svc.backup.health()
    assert h.state == "OK" and not h.ok and "second copy failed" in h.message
    cfg.backup_copy_dir = str(tmp_path / "usb")
    svc.backup.create()
    assert svc.backup.health().ok


def test_settings_for_the_schedule_are_saved_and_audited(svc, cfg, tmp_path):
    svc.settings.save_general({"auto_backup_hours": 6, "backup_copy_dir": f"  {tmp_path}  "})
    assert (cfg.auto_backup_hours, cfg.backup_copy_dir) == (6, str(tmp_path))
    assert json.loads(cfg.config_file.read_text())["auto_backup_hours"] == 6
    svc.settings.save_general({"auto_backup_hours": 9999})
    assert cfg.auto_backup_hours == 168
    assert actions(svc, "SETTINGS_CHANGED")


# --- restore -----------------------------------------------------------------------------------

def test_restore_is_audited_in_the_restored_database(svc, sample, cfg):
    sample.receive(sample.mdf, 10)
    backup = svc.backup.create(note="before mistakes")
    sample.receive(sample.mdf, 99, number="MISTAKE")
    svc.backup.restore(backup)
    assert (cfg.data_dir / RESTORE_MARKER).exists()
    s2 = reopen(cfg)
    summary = s2.backup.finish_restore()
    assert backup.name in summary and "admin" in summary and "before-restore" in summary
    assert not (cfg.data_dir / RESTORE_MARKER).exists()
    assert s2.backup.finish_restore() is None  # once only
    done = actions(s2, "RESTORE_COMPLETED")
    assert len(done) == 1 and done[0].entity_label == backup.name
    assert s2.products.row(sample.mdf.id).on_hand == D(10)
    s2.ctx.db.dispose()


def test_test_restore_checks_a_backup_without_touching_live_data(svc, sample, cfg, tmp_path):
    sample.receive(sample.mdf, 10)
    backup = svc.backup.create()
    sample.receive(sample.mdf, 5, number="AFTER")
    result = svc.backup.test_restore(backup)
    assert result.ok and result.counts["products"] == 3 and result.counts["stock movements"] == 1
    assert "OK - restorable" in result.summary
    assert svc.products.row(sample.mdf.id).on_hand == D(15)  # live data untouched
    assert actions(svc, "BACKUP_TESTED")


def test_start_up_recovery_from_a_damaged_database(svc, sample, cfg):
    sample.receive(sample.mdf, 12)
    good = svc.backup.create(note="good")
    (cfg.backup_path / "StoreWMS_2099-01-01_000000_auto.zip").write_bytes(b"broken")  # newest, unusable
    svc.ctx.db.dispose()
    cfg.db_path.write_bytes(b"this is not a database" * 100)  # disk error / bad shutdown
    with pytest.raises(Exception):  # noqa: B017 - whatever SQLite raises for a damaged file
        reopen(cfg)
    assert latest_backup(cfg) == good  # the broken newest file is skipped
    damaged = emergency_restore(cfg, good)
    assert damaged.startswith("wms.db.damaged-") and (cfg.data_dir / damaged).exists()  # kept, not deleted
    s2 = reopen(cfg)
    assert "damaged database was kept" in s2.backup.finish_restore()
    assert s2.products.row(sample.mdf.id).on_hand == D(12)
    assert s2.backup.integrity_check().ok
    s2.ctx.db.dispose()


def test_restore_of_a_damaged_backup_changes_nothing(svc, sample, cfg, tmp_path):
    sample.receive(sample.mdf, 3)
    backup = svc.backup.create()
    bad = tmp_path / "StoreWMS_bad.zip"
    with zipfile.ZipFile(backup) as zi, zipfile.ZipFile(bad, "w") as zo:
        for n in zi.namelist():
            zo.writestr(n, b"garbage" if n == "wms.db" else zi.read(n))
    with pytest.raises(ValidationError):
        svc.backup.restore(bad)
    with pytest.raises(ValidationError):
        emergency_restore(cfg, bad)
    assert svc.products.row(sample.mdf.id).on_hand == D(3)
    assert not list(cfg.data_dir.glob("*.restoring"))


# --- audit -------------------------------------------------------------------------------------

def test_login_logout_and_backup_are_in_the_audit_log(svc):
    svc.backup.create(note="Friday")
    svc.auth.logout()
    assert svc.ctx.user is None
    svc.auth.login("admin", "admin")
    trail = [a.action for a in svc.audit.search()][:4]
    assert trail[:3] == ["LOGIN", "LOGOUT", "BACKUP_CREATED"]
    out = actions(svc, "LOGOUT")[0]
    assert (out.username, out.summary) == ("admin", "Logged out")


# --- the program killed in the middle of work --------------------------------------------------

WORKER = textwrap.dedent('''
    import sys
    from datetime import date
    from decimal import Decimal
    from wms.config import AppConfig
    from wms.db.engine import Database
    from wms.services import Services
    from wms.services.base import AppContext
    from wms.services.goods_in import ReceiptLine
    from wms.services.goods_out import IssueLine
    cfg = AppConfig.load()
    svc = Services(AppContext(db=Database(cfg.resolved_database_url), config=cfg))
    svc.auth.login("admin", "admin")
    product, product2, bin_id, supplier, site = map(int, sys.argv[1:6])
    run = sys.argv[6]
    n = 0
    while True:
        n += 1
        svc.goods_in.post({"dn_number": f"K{run}-{n}", "supplier_id": supplier, "delivery_date": date.today()},
                          [ReceiptLine(product, Decimal(3), bin_id), ReceiptLine(product2, Decimal(2), bin_id)])
        svc.goods_out.post({"site_id": site, "issue_date": date.today()},
                           [IssueLine(product, Decimal(1), bin_id), IssueLine(product2, Decimal(1), bin_id)])
        print(n, flush=True)
''')


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_killed_while_posting_leaves_a_consistent_database(svc, sample, cfg, tmp_path):
    """Power cut / Task Manager 'End task' in the middle of posting: every document is either fully
    there (header, lines, movements, balances) or not there at all."""
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    env = {**os.environ, "WMS_DATA_DIR": str(cfg.data_dir),
           "PYTHONPATH": os.pathsep.join([os.getcwd(), os.environ.get("PYTHONPATH", "")])}
    args = [sys.executable, str(script), str(sample.mdf.id), str(sample.edge.id), str(sample.bin1.id),
            str(sample.supplier.id), str(sample.site.id)]
    for run, wait in enumerate((25, 60)):  # kill at two different moments
        proc = subprocess.Popen([*args, str(run)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        done = 0
        deadline = time.monotonic() + 120
        while done < wait and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            done = int(line)
        time.sleep(0.013 * (wait % 7))  # land somewhere inside the next posting
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        assert done >= wait, proc.stderr.read()
    s2 = reopen(cfg)
    rep = s2.backup.integrity_check()
    assert rep.ok, (rep.stock_discrepancies, rep.critical_ledger_issues)
    dns = s2.goods_in.search()
    assert dns and all(n_lines == 2 for _dn, n_lines, _value in dns)
    on_hand = s2.products.row(sample.mdf.id).on_hand
    issued = sum(1 for _ in s2.goods_out.search())
    assert on_hand == D(3) * len(dns) - D(issued)
    s2.ctx.db.dispose()


def test_report_rows_match_the_movement_screen(svc, sample):
    """Reports read plain rows (fast); they must say exactly what the movement screen says,
    including names recorded at the time and reversals."""
    sample.receive(sample.mdf, 10)
    from datetime import date

    from wms.services.goods_out import IssueLine
    gi = svc.goods_out.post({"site_id": sample.site.id, "issue_date": date.today()},
                            [IssueLine(sample.mdf.id, D(4), sample.bin1.id)])
    svc.goods_out.reverse(gi.id, "Wrong project")
    svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B04-NEW"})  # renamed since
    full = svc.stock.movements(limit=None)
    lines = svc.stock.movement_lines()
    assert [m.id for m in full] == [m.id for m in lines]
    for a, b in zip(full, lines):
        assert (a.type_label, a.product_code, a.product_name, a.quantity, a.product.unit.code, a.from_bin_code,
                a.to_bin_code, a.site_label, a.supplier_label, a.user_label, a.doc_number) == (
                b.type_label, b.product_code, b.product_name, b.quantity, b.unit_code, b.from_bin_code,
                b.to_bin_code, b.site_label, b.supplier_label, b.user_label, b.doc_number)
    assert any(b.type_label.startswith("Reversal of") for b in lines)
    assert all("B04-NEW" not in b.from_bin_code + b.to_bin_code for b in lines)  # as it was at the time
    assert [b.id for b in svc.stock.movement_lines(text="P-1001")] == [m.id for m in svc.stock.movements(text="P-1001")]


def test_goods_in_and_out_reports_match_the_documents(svc, sample):
    """The Goods In / Goods Out reports read plain rows; they must match what the documents show."""
    from datetime import date

    from wms.services.goods_out import IssueLine
    from wms.services.reports import ReportFilters
    sample.receive(sample.mdf, 10)
    gi = svc.goods_out.post({"site_id": sample.site.id, "issue_date": date.today(), "reference": "R1"},
                            [IssueLine(sample.mdf.id, D(4), sample.bin1.id)])
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": date.today()},
                       [IssueLine(sample.mdf.id, D(1), sample.bin1.id)])
    svc.goods_out.reverse(gi.id, "Wrong project")
    svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B04-NEW"})
    p = svc.products.get(sample.mdf.id)
    svc.products.save(p.id, {"item_type": p.item_type, "internal_code": "MDF-RENAMED", "name": "Renamed",
                             "unit_id": p.unit_id})

    gin = svc.reports.run("goods_in", ReportFilters())
    dns = [svc.goods_in.get(r[0].id) for r in svc.goods_in.search()]
    expected = [[dn.delivery_date, dn.dn_number, dn.supplier_label, ln.product_code, ln.product_name, ln.quantity,
                 ln.product.unit.code, ln.bin_code, ln.unit_cost] for dn in dns for ln in dn.lines]
    assert sorted(r[:9] for r in gin.rows) == sorted(expected)
    assert all(r[3] != "MDF-RENAMED" and "B04-NEW" not in r[7] for r in gin.rows)

    gout = svc.reports.run("goods_out", ReportFilters())
    issues = [svc.goods_out.get(g.id) for g in svc.goods_out.search()]
    expected = [[g.issue_date, g.issue_number, g.site_label, g.reference or "", ln.product_code, ln.product_name,
                 ln.quantity, ln.product.unit.code, ln.bin_code, g.status.title()]
                for g in issues for ln in g.lines]
    assert sorted([*r[:9], r[12]] for r in gout.rows) == sorted(expected)
    reversed_row = next(r for r in gout.rows if r[1] == gi.issue_number)
    assert reversed_row[9] is None  # a reversed Goods Out is worth nothing
    assert next(r for r in gout.rows if r[1] != gi.issue_number)[9] is not None
