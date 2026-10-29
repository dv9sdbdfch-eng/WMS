"""Backup, restore and integrity checks.

A backup is a single .zip containing a consistent copy of the database (made
with SQLite's online backup API, safe while the app is running), every
attachment file and a manifest. Restoring always takes a safety backup of the
current data first.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from wms import __version__
from wms.core.exceptions import ValidationError
from wms.core.permissions import P
from wms.db.models import StockLevel
from wms.services import audit
from wms.services.attachments import AttachmentService
from wms.services.base import BaseService
from wms.services.integrity import run_checks
from wms.services.stock_engine import Discrepancy, StockEngine

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
DB_NAME = "wms.db"
STATUS_FILE = "backup_status.json"  # in the data folder: last good backup, last failure
RESTORE_MARKER = "restore_done.json"  # written by a restore, read (and audited) at the next start
STALE_AFTER = timedelta(hours=48)  # warn when the last good backup is older than this


@dataclass
class BackupHealth:
    """What the Dashboard and Settings show: when the last good backup was made and whether to worry.

    state: OK, DUE (older than the automatic interval - one will be made), STALE (none for 48 h+ or
    twice the interval), FAILED (the latest attempt failed), NEVER (no backup at all)."""

    state: str
    last_success: datetime | None = None
    last_file: str = ""
    last_failure: datetime | None = None
    last_error: str = ""
    copy_problem: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("OK", "DUE") and not self.copy_problem

    @property
    def last_success_text(self) -> str:
        if self.last_success is None:
            return "Never"
        d = self.last_success
        today = datetime.now().date()
        day = ("Today" if d.date() == today else "Yesterday" if d.date() == today - timedelta(days=1)
               else d.strftime("%d/%m/%Y"))
        return f"{day} \u2014 {d:%H:%M}"

    @property
    def message(self) -> str:
        if self.state == "NEVER":
            return "No backup has been made yet. Go to Settings > Backup and restore > Back up now."
        if self.state == "FAILED":
            return (f"The last backup FAILED ({self.last_failure:%d/%m/%Y %H:%M}): {self.last_error}. "
                    f"Last good backup: {self.last_success_text}.")
        if self.state == "STALE":
            return f"No successful backup since {self.last_success_text}. Back up now."
        if self.copy_problem:
            return f"Backup made, but the second copy failed: {self.copy_problem}"
        return f"Last successful backup: {self.last_success_text}"


@dataclass
class BackupInfo:
    path: Path
    created: datetime
    size_bytes: int
    app_version: str = ""
    note: str = ""


@dataclass
class IntegrityReport:
    database_ok: bool = True
    database_messages: list[str] = field(default_factory=list)
    foreign_key_problems: int = 0
    stock_discrepancies: list[Discrepancy] = field(default_factory=list)
    negative_levels: list[StockLevel] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    # Ledger reconciliation: bin / product / warehouse balances, reversal links, document links.
    ledger_issues: list = field(default_factory=list)

    @property
    def critical_ledger_issues(self) -> list:
        return [i for i in self.ledger_issues if i.severity == "CRITICAL"]

    @property
    def ok(self) -> bool:
        return (self.database_ok and not self.foreign_key_problems and not self.stock_discrepancies
                and not self.critical_ledger_issues and not self.missing_files)


def _check_sqlite_file(path: Path) -> list[str]:
    """Raise unless ``path`` is a healthy Store WMS database; returns its attachment file names."""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValidationError(f"The database inside the backup is damaged: {result}")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"products", "stock_movements", "stock_levels"} <= tables:
                raise ValidationError("The backup does not contain a Store WMS database.")
            return [r[0] for r in con.execute("SELECT stored_name FROM attachments WHERE is_deleted = 0")]
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise ValidationError(f"The database inside the backup cannot be read: {exc}") from None


@dataclass
class RestoreTest:
    backup: str
    created: str
    ok: bool
    counts: dict[str, int]
    report: IntegrityReport

    @property
    def summary(self) -> str:
        what = ", ".join(f"{n} {k}" for k, n in self.counts.items())
        return (f"Test restore of {self.backup}: {'OK - restorable' if self.ok else 'PROBLEMS FOUND'}. {what}. "
                f"{len(self.report.stock_discrepancies)} stock mismatch(es), "
                f"{len(self.report.critical_ledger_issues)} critical ledger problem(s), "
                f"{len(self.report.missing_files)} missing file(s).")


def swap_in(cfg, backup_file: Path) -> None:
    """Put the backup's database and files in place of the data in ``cfg.data_dir``.

    The database is staged next to the live file, checked, then swapped in with an atomic rename:
    a failure part-way (disk full, power cut) never leaves a half-copied database behind."""
    store = cfg.attachments_path.resolve()
    store.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup_file) as z:
        staged = cfg.db_path.with_name(cfg.db_path.name + ".restoring")
        try:
            with z.open(DB_NAME) as src, open(staged, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            _check_sqlite_file(staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        for suffix in ("-wal", "-shm"):
            Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)
        os.replace(staged, cfg.db_path)
        for name in z.namelist():
            if not name.startswith("attachments/") or name.endswith("/"):
                continue
            target = (store / Path(name).relative_to("attachments")).resolve()
            if not target.is_relative_to(store):
                continue  # "attachments/../.." entries would write outside the store - never follow them
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def write_restore_marker(cfg, backup_file: Path, manifest: dict, safety: str | None, user: str,
                         damaged: str | None = None) -> None:
    info = {"backup": Path(backup_file).name, "backup_created": manifest.get("created", ""),
            "backup_version": manifest.get("app_version", ""), "restored_by": user,
            "restored_at": datetime.now().isoformat(timespec="seconds"), "safety": safety,
            "damaged_database": damaged}
    (cfg.data_dir / RESTORE_MARKER).write_text(json.dumps(info, indent=2), encoding="utf-8")


def emergency_restore(cfg, backup_file: Path) -> str:
    """Restore when the program cannot open its database at all (damaged file, failed disk): no login
    is possible, so nothing is required. The damaged database is kept (renamed), never deleted.
    Returns the name it was kept under ('' if there was none)."""
    backup_file = Path(backup_file)
    manifest = BackupService.verify(backup_file)
    damaged = ""
    if cfg.db_path.exists():
        damaged = f"{cfg.db_path.name}.damaged-{datetime.now():%Y%m%d-%H%M%S}"
        os.replace(cfg.db_path, cfg.db_path.with_name(damaged))
        for suffix in ("-wal", "-shm"):
            p = Path(str(cfg.db_path) + suffix)
            if p.exists():
                os.replace(p, p.with_name(damaged + suffix))
    swap_in(cfg, backup_file)
    write_restore_marker(cfg, backup_file, manifest, None, "(start-up recovery)", damaged or None)
    log.warning("Emergency restore from %s; damaged database kept as %s", backup_file, damaged or "-")
    return damaged


def _newest_first(p: Path):
    """Order backups by when they were written (two can share a second; names alone do not sort them)."""
    return -p.stat().st_mtime_ns, p.name


def latest_backup(cfg) -> Path | None:
    """The newest backup that opens and checks out (for start-up recovery)."""
    folder = cfg.backup_path
    for f in sorted(folder.glob("StoreWMS_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if folder.is_dir() else []:
        try:
            BackupService.verify(f)
            return f
        except Exception:  # noqa: BLE001 - try the next one
            continue
    return None


class BackupService(BaseService):
    @property
    def backup_dir(self) -> Path:
        return self.ctx.config.backup_path

    def _require_sqlite(self) -> None:
        if not self.ctx.config.is_sqlite:
            raise ValidationError("Built-in backup works for the local SQLite database. "
                                  "Back up a central PostgreSQL server with pg_dump on the server.")

    def create_safety(self, note: str) -> Path:
        """A backup the program takes itself before a bulk change (Excel import). Anyone allowed to
        make that change gets one - it only protects their own work - and it is never pruned."""
        return self.create(note, automatic=True, kind="safety")

    # -- backup status (Dashboard / Settings: "Last successful backup") ---------------------------
    @property
    def status_path(self) -> Path:
        return self.ctx.config.data_dir / STATUS_FILE

    def _status(self) -> dict:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_status(self, **changes) -> None:
        st = self._status()
        st.update(changes)
        try:
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
            tmp.replace(self.status_path)
        except OSError:
            log.exception("Could not write the backup status file")

    def health(self) -> BackupHealth:
        st = self._status()

        def when(key):
            try:
                return datetime.fromisoformat(st[key]) if st.get(key) else None
            except ValueError:
                return None

        last, failed = when("last_success_at"), when("last_failure_at")
        if last is None:  # status file lost / older version: the newest backup in the folder counts
            found = self.list_backups()
            good = [b for b in found if b.note != "(unreadable)"]
            if good:
                last = good[0].created
                st.setdefault("last_success_file", good[0].path.name)
        h = BackupHealth("OK", last, st.get("last_success_file", ""), failed, st.get("last_failure_error", ""),
                         st.get("copy_problem", ""))
        hours = int(self.ctx.config.auto_backup_hours or 0)
        age = datetime.now() - last if last else None
        if failed is not None and (last is None or failed > last):
            h.state = "FAILED"
        elif last is None:
            h.state = "NEVER"
        elif age > max(STALE_AFTER, timedelta(hours=2 * hours)):
            h.state = "STALE"
        elif hours and age >= timedelta(hours=hours):
            h.state = "DUE"
        return h

    def is_due(self) -> bool:
        hours = int(self.ctx.config.auto_backup_hours or 0)
        if not hours or not self.ctx.config.is_sqlite:
            return False
        h = self.health()
        return h.last_success is None or datetime.now() - h.last_success >= timedelta(hours=hours) \
            or h.state == "FAILED"

    def backup_if_due(self) -> Path | None:
        """The scheduled automatic backup (at start-up and every hour while the program is open)."""
        if not self.is_due():
            return None
        return self.create(note=f"Scheduled automatic backup (every {self.ctx.config.auto_backup_hours} h)",
                           automatic=True)

    def create(self, note: str = "", destination: Path | None = None, automatic: bool = False,
               kind: str | None = None) -> Path:
        """Make a backup. A failure is recorded (Dashboard warning, audit log) and raised - never silent."""
        if not automatic:
            self.require(P.BACKUP_RUN)
        self._require_sqlite()
        try:
            target = self._create(note, destination, automatic, kind)
        except Exception as exc:
            error = exc.message if isinstance(exc, ValidationError) else f"{type(exc).__name__}: {exc}"
            log.exception("Backup failed")
            self._write_status(last_failure_at=datetime.now().isoformat(timespec="seconds"),
                               last_failure_error=error[:500])
            try:
                with self.db.session() as s:
                    audit.record(s, self.ctx, "BACKUP_FAILED", "SYSTEM", None, note[:200] or None, error[:1000])
            except Exception:  # noqa: BLE001 - the database may be the reason the backup failed
                log.exception("Could not write BACKUP_FAILED to the audit log")
            raise
        self._write_status(last_success_at=datetime.now().isoformat(timespec="seconds"),
                           last_success_file=target.name, copy_problem="")
        if destination is None:
            self._second_copy(target)
        return target

    def _second_copy(self, target: Path) -> None:
        """Copy the new backup to the second backup folder, if one is set (a failed PC or disk must not
        take the backups with it). A failed copy keeps the backup but shows a warning."""
        copy_dir = (self.ctx.config.backup_copy_dir or "").strip()
        if not copy_dir:
            return
        try:
            dest = Path(copy_dir)
            dest.mkdir(parents=True, exist_ok=True)
            part = dest / (target.name + ".part")
            shutil.copy2(target, part)
            part.replace(dest / target.name)
            keep = max(int(self.ctx.config.backups_to_keep or 30), 3)
            for old in sorted(dest.glob("StoreWMS_*_auto.zip"), key=_newest_first)[keep:]:
                old.unlink(missing_ok=True)
        except OSError as exc:
            log.exception("Second backup copy failed")
            self._write_status(copy_problem=f"{copy_dir}: {exc.strerror or exc}")

    def _create(self, note: str, destination: Path | None, automatic: bool, kind: str | None) -> Path:
        dest_dir = Path(destination) if destination else self.backup_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        kind = kind or ("auto" if automatic else "manual")
        target = dest_dir / f"StoreWMS_{stamp}_{kind}.zip"
        n = 1
        while target.exists():  # two backups in the same second (import straight after a manual backup)
            n += 1
            target = dest_dir / f"StoreWMS_{stamp}-{n}_{kind}.zip"
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / DB_NAME
            src = sqlite3.connect(str(self.ctx.config.db_path))
            dst = sqlite3.connect(str(snap))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            manifest = {"app": "Store WMS", "app_version": __version__, "created": datetime.now().isoformat(),
                        "note": note, "automatic": automatic,
                        "created_by": self.ctx.user.username if self.ctx.user else "system"}
            part = target.with_suffix(".part")
            try:
                with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    z.write(snap, DB_NAME)
                    store = self.ctx.config.attachments_path
                    if store.is_dir():
                        for f in store.rglob("*"):
                            if f.is_file():
                                z.write(f, Path("attachments") / f.relative_to(store))
                    missing = [a.stored_name for a in AttachmentService(self.ctx).missing_files()]
                    if missing:
                        manifest["attachments_missing_on_disk"] = missing
                    z.writestr(MANIFEST, json.dumps(manifest, indent=2))
                self.verify(part)  # never leave a backup behind that could not be restored
            except BaseException:  # disk full, USB stick pulled out, damaged copy: no half-written file
                part.unlink(missing_ok=True)
                raise
            part.replace(target)
        with self.db.session() as s:
            audit.record(s, self.ctx, "BACKUP_CREATED", "SYSTEM", None, target.name, note or None)
        self.prune()
        return target

    def prune(self) -> int:
        """Keep only the newest N automatic backups. Manual backups are never deleted."""
        keep = max(int(self.ctx.config.backups_to_keep or 30), 3)
        autos = sorted(self.backup_dir.glob("StoreWMS_*_auto.zip"), key=_newest_first)
        removed = 0
        for old in autos[keep:]:
            old.unlink(missing_ok=True)
            removed += 1
        return removed

    def list_backups(self) -> list[BackupInfo]:
        out = []
        if not self.backup_dir.is_dir():
            return out
        for f in sorted(self.backup_dir.glob("StoreWMS_*.zip"), key=_newest_first):
            info = BackupInfo(f, datetime.fromtimestamp(f.stat().st_mtime), f.stat().st_size)
            try:
                with zipfile.ZipFile(f) as z:
                    m = json.loads(z.read(MANIFEST))
                    info.app_version = m.get("app_version", "")
                    info.note = m.get("note", "")
                    info.created = datetime.fromisoformat(m["created"])
            except (zipfile.BadZipFile, KeyError, ValueError, json.JSONDecodeError):
                info.note = "(unreadable)"
            out.append(info)
        return out

    @staticmethod
    def verify(backup_file: Path) -> dict:
        """Check a backup file is complete and its database is not corrupt."""
        try:
            with zipfile.ZipFile(backup_file) as z:
                names = set(z.namelist())
                if DB_NAME not in names or MANIFEST not in names:
                    raise ValidationError("This file is not a Store WMS backup.")
                bad = z.testzip()
                if bad:
                    raise ValidationError(f"The backup is damaged (file {bad}).")
                manifest = json.loads(z.read(MANIFEST))
                with tempfile.TemporaryDirectory() as tmp:
                    z.extract(DB_NAME, tmp)
                    stored = _check_sqlite_file(Path(tmp) / DB_NAME)
                missing = sorted(n for n in stored if f"attachments/{n}" not in names)
                manifest["attachments_in_database"] = len(stored)
                manifest["attachments_missing_from_backup"] = missing
                return manifest
        except zipfile.BadZipFile:
            raise ValidationError("This file is not a valid backup (.zip) file.") from None

    def restore(self, backup_file: Path) -> Path:
        """Replace the current data with the backup. Returns the safety backup path.

        The caller must restart the application afterwards.
        """
        self.require(P.BACKUP_RESTORE)
        self._require_sqlite()
        backup_file = Path(backup_file)
        manifest = self.verify(backup_file)
        safety = self.create(note=f"Safety copy before restoring {backup_file.name}", automatic=True)
        safety_named = safety.with_name(safety.stem.replace("_auto", "_before-restore") + ".zip")
        safety.replace(safety_named)
        with self.db.session() as s:
            audit.record(s, self.ctx, "RESTORE_STARTED", "SYSTEM", None, backup_file.name,
                         f"Safety copy: {safety_named.name}")
        self.db.dispose()
        swap_in(self.ctx.config, backup_file)
        write_restore_marker(self.ctx.config, backup_file, manifest, safety_named.name,
                             self.ctx.user.username if self.ctx.user else "system")
        return safety_named

    def finish_restore(self) -> str | None:
        """At start-up after a restore (database already upgraded): write RESTORE_COMPLETED into the
        RESTORED database's audit log - the one written before the restore was replaced with it."""
        marker = self.ctx.config.data_dir / RESTORE_MARKER
        if not marker.exists():
            return None
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        summary = (f"Restored from {info.get('backup', '?')} (made {info.get('backup_created', '?')[:16]}) by "
                   f"{info.get('restored_by', '?')} at {info.get('restored_at', '?')[:16]}.")
        if info.get("safety"):
            summary += f" Data before the restore: {info['safety']}."
        elif info.get("damaged_database"):
            summary += f" The damaged database was kept as {info['damaged_database']}."
        with self.db.session() as s:
            audit.record(s, self.ctx, "RESTORE_COMPLETED", "SYSTEM", None, info.get("backup"), summary,
                         new=info)
        marker.unlink(missing_ok=True)
        return summary

    def test_restore(self, backup_file: Path) -> RestoreTest:
        """Restore a backup into a temporary folder (the live data is not touched), open it, upgrade it
        and run the full data check - proof the backup can really be restored."""
        self.require(P.BACKUP_RUN)
        backup_file = Path(backup_file)
        manifest = self.verify(backup_file)
        from wms.config import AppConfig
        from wms.db.engine import Database
        from wms.services.base import AppContext
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(data_dir=Path(tmp))
            cfg.ensure_dirs()
            swap_in(cfg, backup_file)
            db = Database(f"sqlite:///{cfg.db_path.as_posix()}")
            try:
                db.migrate()
                svc = BackupService(AppContext(db=db, config=cfg, user=self.ctx.user))
                rep = svc.integrity_check()
                with db.read_session() as s:
                    counts = {name: s.scalar(text(f"SELECT COUNT(*) FROM {table}")) for name, table in (
                        ("products", "products"), ("stock movements", "stock_movements"),
                        ("bins with stock", "stock_levels WHERE quantity <> 0"),
                        ("delivery notes", "delivery_notes"), ("attachments", "attachments WHERE is_deleted = 0"))}
            finally:
                db.dispose()
        result = RestoreTest(backup_file.name, manifest.get("created", ""), rep.ok, counts, rep)
        with self.db.session() as s:
            audit.record(s, self.ctx, "BACKUP_TESTED", "SYSTEM", None, backup_file.name, result.summary)
        return result

    # -- integrity ----------------------------------------------------------
    def integrity_check(self) -> IntegrityReport:
        rep = IntegrityReport()
        with self.db.read_session() as s:
            if self.db.dialect == "sqlite":
                msgs = [r[0] for r in s.execute(text("PRAGMA integrity_check"))]
                rep.database_ok = msgs == ["ok"]
                rep.database_messages = msgs
                rep.foreign_key_problems = len(list(s.execute(text("PRAGMA foreign_key_check"))))
            rep.stock_discrepancies = StockEngine(s, self.ctx).find_discrepancies()
            rep.negative_levels = list(s.scalars(select(StockLevel).where(StockLevel.quantity < 0)))
            rep.ledger_issues = run_checks(s)
        rep.missing_files = [a.original_name for a in AttachmentService(self.ctx).missing_files()]
        return rep

    def rebuild_stock_levels(self) -> int:
        self.require(P.SETTINGS_MANAGE)
        with self.db.session() as s:
            return StockEngine(s, self.ctx).rebuild_levels()
