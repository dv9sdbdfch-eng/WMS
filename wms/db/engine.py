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
from sqlalchemy.dialects.sqlite.base import SQLiteCompiler
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


# Stock documents and their lines: never deleted, and a posted line never changes. Mistakes are
# corrected by reversal, so the original record always stays readable.
DOCUMENT_TABLES = ("delivery_notes", "goods_issues", "stock_returns", "production_orders",
                   "stock_adjustments", "stock_transfers", "stock_counts")
LINE_TABLES = ("delivery_note_lines", "goods_issue_lines", "return_lines", "production_lines")
REVERSIBLE_TABLES = ("delivery_notes", "goods_issues", "stock_returns", "production_orders")


def _sqlite_document_triggers() -> list[str]:
    def abort(name: str, when: str, table: str, event: str, msg: str) -> str:
        return (f"CREATE TRIGGER IF NOT EXISTS trg_{table}_{name} BEFORE {event} ON {table} "
                f"{('WHEN ' + when) if when else ''} BEGIN SELECT RAISE(ABORT, '{msg}'); END;")

    stmts = []
    for t in (*DOCUMENT_TABLES, *LINE_TABLES, "stock_count_lines"):
        stmts.append(abort("no_delete", "", t, "DELETE", f"{t}: stock records cannot be deleted - reverse instead"))
    for t in LINE_TABLES:
        stmts.append(abort("posted_fixed", "OLD.movement_id IS NOT NULL", t,
                           "UPDATE OF product_id, quantity, bin_id, movement_id",
                           f"{t}: a posted line cannot be changed - reverse the document instead"))
        stmts.append(abort("qty_positive", "NEW.quantity <= 0", t, "INSERT", f"{t}: quantity must be above zero"))
    for t in REVERSIBLE_TABLES:
        stmts.append(abort("reversed_final", "OLD.status = 'REVERSED'", t, "UPDATE",
                           f"{t}: a reversed document cannot be changed or reopened"))
    for t in ("stock_adjustments", "stock_transfers"):
        stmts.append(abort("posted_fixed", "OLD.movement_id IS NOT NULL", t, "UPDATE",
                           f"{t}: a posted record cannot be changed"))
    stmts.append(abort("closed_final", "OLD.status IN ('APPROVED', 'CANCELLED')", "stock_counts", "UPDATE",
                       "stock_counts: an approved or cancelled count cannot be changed"))
    stmts.append(abort("approved_fixed", "OLD.adjustment_id IS NOT NULL", "stock_count_lines", "UPDATE",
                       "stock_count_lines: an approved count line cannot be changed"))
    return stmts


def _postgres_document_triggers() -> list[str]:
    stmts = [
        "CREATE OR REPLACE FUNCTION wms_no_delete() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '%: stock records cannot be deleted', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;"
    ]
    for t in (*DOCUMENT_TABLES, *LINE_TABLES, "stock_count_lines"):
        stmts.append(f"DROP TRIGGER IF EXISTS trg_{t}_no_delete ON {t};")
        stmts.append(f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} "
                     f"FOR EACH ROW EXECUTE FUNCTION wms_no_delete();")
    return stmts


def integrity_trigger_sql(dialect_name: str) -> list[str]:
    """Every database-level guard: append-only ledger/audit plus protected stock documents."""
    if dialect_name == "sqlite":
        return _sqlite_append_only_triggers() + _sqlite_document_triggers()
    if dialect_name == "postgresql":
        return _postgres_append_only_triggers() + _postgres_document_triggers()
    return []


# Added by migration 0005. Kept apart from integrity_trigger_sql(), which older migrations call
# before these tables exist; all_trigger_sql() is the complete set for a new database.
LEDGER_APPEND_ONLY_TABLES = ("movement_reversals",)


def ledger_trigger_sql(dialect_name: str) -> list[str]:
    """Reversal rules enforced by the database itself (migration 0005)."""
    if dialect_name == "sqlite":
        stmts = []
        for table in LEDGER_APPEND_ONLY_TABLES:
            for ev in ("UPDATE", "DELETE"):
                stmts.append(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{ev.lower()} BEFORE {ev} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only: records cannot be changed'); END;")
        stmts.append(
            "CREATE TRIGGER IF NOT EXISTS trg_stock_movements_reversal_of_reversal BEFORE INSERT ON stock_movements "
            "WHEN NEW.reversal_of_id IS NOT NULL AND EXISTS (SELECT 1 FROM stock_movements o "
            "WHERE o.id = NEW.reversal_of_id AND o.reversal_of_id IS NOT NULL) "
            "BEGIN SELECT RAISE(ABORT, 'stock_movements: a reversal cannot itself be reversed'); END;")
        stmts.append(
            "CREATE TRIGGER IF NOT EXISTS trg_stock_movements_reversal_mirror BEFORE INSERT ON stock_movements "
            "WHEN NEW.reversal_of_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM stock_movements o "
            "WHERE o.id = NEW.reversal_of_id AND o.product_id = NEW.product_id AND o.quantity = NEW.quantity "
            "AND o.from_bin_id IS NEW.to_bin_id AND o.to_bin_id IS NEW.from_bin_id) "
            "BEGIN SELECT RAISE(ABORT, 'stock_movements: a reversal must mirror the original movement'); END;")
        return stmts
    if dialect_name == "postgresql":
        stmts = []
        for table in LEDGER_APPEND_ONLY_TABLES:
            stmts.append(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
            stmts.append(f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                         f"FOR EACH ROW EXECUTE FUNCTION wms_append_only();")
        return stmts
    return []


def all_trigger_sql(dialect_name: str) -> list[str]:
    """Every database-level guard, for a newly created database."""
    return integrity_trigger_sql(dialect_name) + ledger_trigger_sql(dialect_name)


def append_only_trigger_sql(dialect_name: str) -> list[str]:
    """Ledger and audit triggers only (what migration 0001 installs)."""
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


def _sqlite_plain_like(self, binary, operator, **kw):
    return self.visit_like_op_binary(binary, operator, **kw)


def _sqlite_plain_not_like(self, binary, operator, **kw):
    return self.visit_not_like_op_binary(binary, operator, **kw)


# Case-insensitive search on SQLite: SQLAlchemy writes ``lower(col) LIKE lower(?)``, which calls
# lower() on every row. SQLite's own LIKE already ignores case, with exactly the same rules as its
# lower() (A-Z only), so the result is identical and searches over 100,000 movements run ~3x faster.
# PostgreSQL keeps its native ILIKE.
SQLiteCompiler.visit_ilike_op_binary = _sqlite_plain_like
SQLiteCompiler.visit_not_ilike_op_binary = _sqlite_plain_not_like


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
                for stmt in all_trigger_sql(self.dialect):
                    conn.execute(text(stmt))
                command.stamp(cfg, "head")
            else:
                command.upgrade(cfg, "head")
        self.refresh_statistics()

    def refresh_statistics(self) -> None:
        """Give SQLite's query planner current table statistics (ANALYZE, sampled).

        Measured on 100,000 movements this halves several filtered searches (e.g. "Goods In from
        one supplier in the last 30 days": 47 ms -> 16 ms) - more than any extra index did. Takes
        a few milliseconds; skipped quietly if another workstation is holding the database.
        """
        if self.dialect != "sqlite":
            return
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA analysis_limit = 1000")
                conn.exec_driver_sql("ANALYZE")
                conn.commit()
        except Exception:  # noqa: BLE001 - statistics are an optimisation, never a reason to stop
            log.warning("Could not refresh database statistics", exc_info=True)

    def dispose(self) -> None:
        self.engine.dispose()
