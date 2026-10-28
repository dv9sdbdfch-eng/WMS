"""Database connection, schema migration and transactions.

``Database`` is the only object that knows which database engine is in use.
Services ask it for a transactional session; everything else is
SQLAlchemy and therefore portable between SQLite and PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wms.db import models  # noqa: F401  (registers tables)
from wms.db.base import Base

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Tables whose rows may never be changed or removed once written.
APPEND_ONLY_TABLES = ("stock_movements", "audit_log")


def _sqlite_append_only_triggers() -> list[str]:
    stmts = []
    for table in APPEND_ONLY_TABLES:
        stmts.append(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only: records cannot be changed'); END;"
        )
        stmts.append(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only: records cannot be deleted'); END;"
        )
    return stmts


def _postgres_append_only_triggers() -> list[str]:
    stmts = [
        "CREATE OR REPLACE FUNCTION wms_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;"
    ]
    for table in APPEND_ONLY_TABLES:
        stmts.append(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
        stmts.append(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION wms_append_only();"
        )
    return stmts


def append_only_trigger_sql(dialect_name: str) -> list[str]:
    if dialect_name == "sqlite":
        return _sqlite_append_only_triggers()
    if dialect_name == "postgresql":
        return _postgres_append_only_triggers()
    return []


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # Let SQLAlchemy control transactions (see the "begin" hook below).
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA busy_timeout = 10000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        # IMMEDIATE takes the write lock up front, so two copies of the app
        # can never interleave stock postings.
        if conn.get_execution_options().get("wms_readonly"):
            conn.exec_driver_sql("BEGIN")
        else:
            conn.exec_driver_sql("BEGIN IMMEDIATE")


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = url
        kwargs: dict = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        else:
            kwargs["pool_pre_ping"] = True
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            _configure_sqlite(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=True)
        self._read_factory = sessionmaker(
            bind=self.engine.execution_options(wms_readonly=True), expire_on_commit=False
        )

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A unit of work: commits on success, rolls back on any exception."""
        s = self._factory()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        """A session for queries only; never commits."""
        s = self._read_factory()
        try:
            yield s
        finally:
            s.close()  # close (not rollback) keeps loaded objects usable after the session ends

    # -- schema -------------------------------------------------------------
    def migrate(self) -> None:
        """Create a new database or upgrade an existing one to the latest schema."""
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            tables = set(inspect(conn).get_table_names())
            if not tables:
                log.info("Creating new database schema")
                Base.metadata.create_all(conn)
                for stmt in append_only_trigger_sql(self.dialect):
                    conn.execute(text(stmt))
                command.stamp(cfg, "head")
            else:
                command.upgrade(cfg, "head")

    def dispose(self) -> None:
        self.engine.dispose()
