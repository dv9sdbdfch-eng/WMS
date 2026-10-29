"""Backup, restore and integrity checks.

A backup is a single .zip containing a consistent copy of the database (made
with SQLite's online backup API, safe while the app is running), every
attachment file and a manifest. Restoring always takes a safety backup of the
current data first.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
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

MANIFEST = "manifest.json"
DB_NAME = "wms.db"


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


class BackupService(BaseService):
    @property
    def backup_dir(self) -> Path:
        return self.ctx.config.backup_path

    def _require_sqlite(self) -> None:
        if not self.ctx.config.is_sqlite:
            raise ValidationError("Built-in backup works for the local SQLite database. "
                                  "Back up a central PostgreSQL server with pg_dump on the server.")

    def create(self, note: str = "", destination: Path | None = None, automatic: bool = False) -> Path:
        if not automatic:
            self.require(P.BACKUP_RUN)
        self._require_sqlite()
        dest_dir = Path(destination) if destination else self.backup_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        kind = "auto" if automatic else "manual"
        target = dest_dir / f"StoreWMS_{stamp}_{kind}.zip"
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
            try:
                self.verify(part)  # never leave a backup behind that could not be restored
            except Exception:
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
        autos = sorted(self.backup_dir.glob("StoreWMS_*_auto.zip"), reverse=True)
        removed = 0
        for old in autos[keep:]:
            old.unlink(missing_ok=True)
            removed += 1
        return removed

    def list_backups(self) -> list[BackupInfo]:
        out = []
        if not self.backup_dir.is_dir():
            return out
        for f in sorted(self.backup_dir.glob("StoreWMS_*.zip"), reverse=True):
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

    def verify(self, backup_file: Path) -> dict:
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
        self.verify(backup_file)
        safety = self.create(note=f"Safety copy before restoring {backup_file.name}", automatic=True)
        safety_named = safety.with_name(safety.stem.replace("_auto", "_before-restore") + ".zip")
        safety.replace(safety_named)
        with self.db.session() as s:
            audit.record(s, self.ctx, "RESTORE_STARTED", "SYSTEM", None, backup_file.name,
                         f"Safety copy: {safety_named.name}")
        self.db.dispose()
        cfg = self.ctx.config
        store = cfg.attachments_path.resolve()
        with zipfile.ZipFile(backup_file) as z:
            # Stage the database next to the live file, check it opens, then swap it in with an
            # atomic rename: a failure part-way never leaves a half-copied database behind.
            staged = cfg.db_path.with_name(cfg.db_path.name + ".restoring")
            with z.open(DB_NAME) as src, open(staged, "wb") as dst:
                shutil.copyfileobj(src, dst)
            _check_sqlite_file(staged)
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
        return safety_named

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
