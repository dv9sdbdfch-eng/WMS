"""Application configuration and data locations.

Everything the app writes lives under one data folder:

    <data>/config.json      connection + paths (editable from Settings)
    <data>/wms.db           SQLite database
    <data>/attachments/     photos and delivery-note scans
    <data>/logs/            rotating log files

On Windows the default data folder is %LOCALAPPDATA%\\StoreWMS; backups go to
Documents\\StoreWMS Backups. ``WMS_DATA_DIR`` overrides the data folder (used
by tests and for portable installs).

To move to a shared PostgreSQL server later, set ``database_url`` in
config.json (e.g. postgresql+psycopg://wms@server/wms) and point
``attachments_dir`` at a network share. No code changes are needed.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from wms import APP_ID


def default_data_dir() -> Path:
    env = os.environ.get("WMS_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_ID
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_ID


def default_backup_dir() -> Path:
    env = os.environ.get("WMS_DATA_DIR")
    if env:
        return Path(env) / "backups"
    return Path.home() / "Documents" / "StoreWMS Backups"


@dataclass
class AppConfig:
    data_dir: Path
    database_url: str = ""
    attachments_dir: str = ""
    backup_dir: str = ""
    auto_backup_on_exit: bool = True
    backups_to_keep: int = 30
    company_name: str = "My Company Ltd"
    max_attachment_mb: int = 25
    extra: dict = field(default_factory=dict)

    # -- resolved paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "wms.db"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.db_path.as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def attachments_path(self) -> Path:
        return Path(self.attachments_dir) if self.attachments_dir else self.data_dir / "attachments"

    @property
    def backup_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else default_backup_dir()

    @property
    def logs_path(self) -> Path:
        return self.data_dir / "logs"

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.attachments_path, self.logs_path):
            p.mkdir(parents=True, exist_ok=True)

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        data = asdict(self)
        data.pop("data_dir")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.config_file)

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "AppConfig":
        data_dir = Path(data_dir) if data_dir else default_data_dir()
        cfg = cls(data_dir=data_dir)
        if cfg.config_file.exists():
            try:
                raw = json.loads(cfg.config_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            known = {f for f in cls.__dataclass_fields__ if f != "data_dir"}
            for k, v in raw.items():
                if k in known:
                    setattr(cfg, k, v)
        return cfg
