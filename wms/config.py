"""Application configuration and data locations.

Everything the app writes lives under one data folder:

    <data>/config.json      connection + paths (editable from Settings)
    <data>/wms.db           SQLite database
    <data>/attachments/     photos and delivery-note scans
    <data>/logs/            rotating log files

On Windows the default data folder is %LOCALAPPDATA%\\StoreWMS; backups go to
Documents\\StoreWMS Backups. ``WMS_DATA_DIR`` overrides the data folder (used
by tests and for portable installs).

Shared server (10-30 PCs): ``server_host`` (+ port, database, user) in config.json points the
program at a PostgreSQL server; the database password is kept in Windows Credential Manager,
never in the file. Set them with the Connection window on the sign-in screen. ``database_url``
still overrides everything (tests, special set-ups).
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


KEYRING_SERVICE = f"{APP_ID} database"


def server_url(host: str, port: int, database: str, user: str, password: str | None) -> str:
    from sqlalchemy.engine import URL
    return URL.create("postgresql+psycopg", username=user or None, password=password or None, host=host,
                      port=int(port or 5432), database=database).render_as_string(hide_password=False)


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:  # noqa: BLE001 - no credential store on this system
        return None


def server_password(host: str, user: str) -> str | None:
    """The database password from Windows Credential Manager (or WMS_DB_PASSWORD)."""
    env = os.environ.get("WMS_DB_PASSWORD")
    if env:
        return env
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, f"{user}@{host}")
    except Exception:  # noqa: BLE001 - locked or missing store: connect without, the server says why
        return None


def save_server_password(host: str, user: str, password: str) -> bool:
    """Keep the password in the system's credential store. False if this PC has none."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        if password:
            kr.set_password(KEYRING_SERVICE, f"{user}@{host}", password)
        else:
            try:
                kr.delete_password(KEYRING_SERVICE, f"{user}@{host}")
            except Exception:  # noqa: BLE001 - nothing stored
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class AppConfig:
    data_dir: Path
    database_url: str = ""
    attachments_dir: str = ""
    backup_dir: str = ""
    auto_backup_on_exit: bool = True
    # Automatic backup while the program is open / when it starts, if the last good one is older (0 = off).
    auto_backup_hours: int = 24
    backups_to_keep: int = 30
    # Optional second folder (USB stick, network drive, OneDrive) that gets a copy of every backup.
    backup_copy_dir: str = ""
    # Shared PostgreSQL server; empty server_host = the local SQLite file on this PC.
    server_host: str = ""
    server_port: int = 5432
    server_database: str = "storewms"
    server_user: str = "storewms"
    company_name: str = "My Company Ltd"
    max_attachment_mb: int = 25
    extra: dict = field(default_factory=dict)

    # -- resolved paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "wms.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.server_host:
            return server_url(self.server_host, self.server_port, self.server_database, self.server_user,
                              server_password(self.server_host, self.server_user))
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def server_label(self) -> str:
        """Where the data is, for the sign-in screen and Settings."""
        if self.server_host:
            return f"Server {self.server_host}" + (f":{self.server_port}" if self.server_port != 5432 else "")
        return "This PC only" if self.is_sqlite else "Server (custom connection)"

    @property
    def is_sqlite(self) -> bool:
        if self.database_url:
            return self.database_url.startswith("sqlite")
        return not self.server_host

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
