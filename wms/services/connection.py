"""Checking a connection to the shared PostgreSQL server before saving it (Connection window)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServerCheck:
    ok: bool
    message: str
    server_version: str = ""
    new_database: bool = False  # reachable but still empty: the first PC will create the tables


def check_server(host: str, port: int, database: str, user: str, password: str | None,
                 timeout: int = 8) -> ServerCheck:
    """Connect once and say, in plain words, whether this PC can use that server."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.pool import NullPool

    from wms.config import server_url
    from wms.db.engine import MIGRATIONS_DIR, PG_CONNECT_ARGS

    host = (host or "").strip()
    if not host:
        return ServerCheck(False, "Enter the server's name or IP address.")
    url = server_url(host, port, database.strip(), user.strip(), password)
    args = dict(PG_CONNECT_ARGS, connect_timeout=timeout)
    engine = create_engine(url, connect_args=args, poolclass=NullPool)
    try:
        with engine.connect() as c:
            version = (c.execute(text("SHOW server_version")).scalar() or "").split(" ")[0]
            tables = inspect(c).get_table_names()
            current = MigrationContext.configure(c).get_current_revision()
    except DBAPIError as exc:
        return ServerCheck(False, _explain(exc, host, port, database, user))
    finally:
        engine.dispose()
    major = int(version.split(".")[0]) if version[:2].isdigit() else 0
    if major and major < 13:
        return ServerCheck(False, f"The server runs PostgreSQL {version}; version 13 or newer is needed.", version)
    if not tables:
        return ServerCheck(True, f"Connected (PostgreSQL {version}). The database is empty: Store WMS will "
                                 f"create it when this PC starts.", version, new_database=True)
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    try:
        ScriptDirectory.from_config(cfg).get_revision(current)
    except Exception:  # noqa: BLE001
        return ServerCheck(False, "Connected, but the database belongs to a newer version of Store WMS. "
                                  "Install the latest version on this PC first.", version)
    return ServerCheck(True, f"Connected to PostgreSQL {version}. The Store WMS database is ready.", version)


def _explain(exc, host, port, database, user) -> str:
    text = str(getattr(exc, "orig", exc)).lower()
    state = getattr(getattr(exc, "orig", None), "sqlstate", "") or ""
    if state == "28P01" or "password authentication failed" in text or ("role" in text and "does not exist" in text):
        return f"The server refused user '{user}' with this password. Check both."
    if state == "3D000" or ("database" in text and "does not exist" in text):
        return f"The server has no database called '{database}'. Check the name, or create it on the server."
    if "no pg_hba.conf entry" in text:
        return ("The server does not allow connections from this PC. Add this PC's network to pg_hba.conf on "
                "the server (see the server set-up guide).")
    if "timeout" in text or "timed out" in text:
        return (f"No answer from {host}:{port}. Check that the server PC is on, the VPN between buildings is "
                f"up and the firewall on the server allows port {port}.")
    if "could not translate host name" in text or "name or service not known" in text:
        return f"The name '{host}' is not known on this network. Try the server's IP address instead."
    if "connection refused" in text:
        return (f"{host} answered but nothing is listening on port {port}. Check that the PostgreSQL service "
                f"is running on the server.")
    return f"Could not connect: {getattr(exc, 'orig', exc)}"
