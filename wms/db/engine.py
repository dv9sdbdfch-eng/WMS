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
from sqlalchemy.exc import DBAPIError
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


def _sqlite_document_triggers(documents=DOCUMENT_TABLES, lines=LINE_TABLES, reversible=REVERSIBLE_TABLES,
                              extra: bool = True) -> list[str]:
    def abort(name: str, when: str, table: str, event: str, msg: str) -> str:
        return (f"CREATE TRIGGER IF NOT EXISTS trg_{table}_{name} BEFORE {event} ON {table} "
                f"{('WHEN ' + when) if when else ''} BEGIN SELECT RAISE(ABORT, '{msg}'); END;")

    stmts = []
    for t in (*documents, *lines, *(("stock_count_lines",) if extra else ())):
        stmts.append(abort("no_delete", "", t, "DELETE", f"{t}: stock records cannot be deleted - reverse instead"))
    for t in lines:
        stmts.append(abort("posted_fixed", "OLD.movement_id IS NOT NULL", t,
                           "UPDATE OF product_id, quantity, bin_id, movement_id",
                           f"{t}: a posted line cannot be changed - reverse the document instead"))
        stmts.append(abort("qty_positive", "NEW.quantity <= 0", t, "INSERT", f"{t}: quantity must be above zero"))
    for t in reversible:
        stmts.append(abort("reversed_final", "OLD.status = 'REVERSED'", t, "UPDATE",
                           f"{t}: a reversed document cannot be changed or reopened"))
    if not extra:
        return stmts
    for t in ("stock_adjustments", "stock_transfers"):
        stmts.append(abort("posted_fixed", "OLD.movement_id IS NOT NULL", t, "UPDATE",
                           f"{t}: a posted record cannot be changed"))
    stmts.append(abort("closed_final", "OLD.status IN ('APPROVED', 'CANCELLED')", "stock_counts", "UPDATE",
                       "stock_counts: an approved or cancelled count cannot be changed"))
    stmts.append(abort("approved_fixed", "OLD.adjustment_id IS NOT NULL", "stock_count_lines", "UPDATE",
                       "stock_count_lines: an approved count line cannot be changed"))
    return stmts


def _postgres_document_triggers(documents=DOCUMENT_TABLES, lines=LINE_TABLES, reversible=REVERSIBLE_TABLES,
                                extra: bool = True) -> list[str]:
    """The same rules as ``_sqlite_document_triggers``, written for PostgreSQL: one shared function
    raises the message given to each trigger, and each trigger's WHEN clause is the SQLite one."""
    stmts = ["CREATE OR REPLACE FUNCTION wms_abort() RETURNS trigger AS $$ "
             "BEGIN RAISE EXCEPTION '%', TG_ARGV[0] USING ERRCODE = 'integrity_constraint_violation'; END; "
             "$$ LANGUAGE plpgsql;"]

    def abort(name: str, when: str, table: str, event: str, msg: str) -> None:
        stmts.append(f"DROP TRIGGER IF EXISTS trg_{table}_{name} ON {table};")
        stmts.append(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} FOR EACH ROW "
                     f"{('WHEN (' + when + ') ') if when else ''}EXECUTE FUNCTION wms_abort('{msg}');")

    for t in (*documents, *lines, *(("stock_count_lines",) if extra else ())):
        abort("no_delete", "", t, "DELETE", f"{t}: stock records cannot be deleted - reverse instead")
    for t in lines:
        abort("posted_fixed", "OLD.movement_id IS NOT NULL", t, "UPDATE OF product_id, quantity, bin_id, movement_id",
              f"{t}: a posted line cannot be changed - reverse the document instead")
        abort("qty_positive", "NEW.quantity <= 0", t, "INSERT", f"{t}: quantity must be above zero")
    for t in reversible:
        abort("reversed_final", "OLD.status = 'REVERSED'", t, "UPDATE",
              f"{t}: a reversed document cannot be changed or reopened")
    if not extra:
        return stmts
    for t in ("stock_adjustments", "stock_transfers"):
        abort("posted_fixed", "OLD.movement_id IS NOT NULL", t, "UPDATE", f"{t}: a posted record cannot be changed")
    abort("closed_final", "OLD.status IN ('APPROVED', 'CANCELLED')", "stock_counts", "UPDATE",
          "stock_counts: an approved or cancelled count cannot be changed")
    abort("approved_fixed", "OLD.adjustment_id IS NOT NULL", "stock_count_lines", "UPDATE",
          "stock_count_lines: an approved count line cannot be changed")
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
        # the two reversal rules of the SQLite triggers (PostgreSQL WHEN cannot hold a subquery)
        stmts.append(
            "CREATE OR REPLACE FUNCTION wms_reversal_check() RETURNS trigger AS $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM stock_movements o WHERE o.id = NEW.reversal_of_id "
            "AND o.reversal_of_id IS NOT NULL) THEN "
            "RAISE EXCEPTION 'stock_movements: a reversal cannot itself be reversed' "
            "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM stock_movements o WHERE o.id = NEW.reversal_of_id "
            "AND o.product_id = NEW.product_id AND o.quantity = NEW.quantity "
            "AND o.from_bin_id IS NOT DISTINCT FROM NEW.to_bin_id AND o.to_bin_id IS NOT DISTINCT FROM NEW.from_bin_id) THEN "
            "RAISE EXCEPTION 'stock_movements: a reversal must mirror the original movement' "
            "USING ERRCODE = 'integrity_constraint_violation'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql;")
        stmts.append("DROP TRIGGER IF EXISTS trg_stock_movements_reversal_check ON stock_movements;")
        stmts.append("CREATE TRIGGER trg_stock_movements_reversal_check BEFORE INSERT ON stock_movements "
                     "FOR EACH ROW WHEN (NEW.reversal_of_id IS NOT NULL) EXECUTE FUNCTION wms_reversal_check();")
        return stmts
    return []


# Added by migration 0014 (Dispatch / outgoing Delivery Note): the same protection as the other
# stock documents. Kept apart for the same reason as LEDGER_APPEND_ONLY_TABLES.
DISPATCH_TABLES = ("dispatches",)
DISPATCH_LINE_TABLES = ("dispatch_lines",)


def dispatch_trigger_sql(dialect_name: str) -> list[str]:
    if dialect_name == "sqlite":
        return _sqlite_document_triggers(DISPATCH_TABLES, DISPATCH_LINE_TABLES, DISPATCH_TABLES, extra=False)
    if dialect_name == "postgresql":
        return _postgres_document_triggers(DISPATCH_TABLES, DISPATCH_LINE_TABLES, DISPATCH_TABLES, extra=False)
    return []


def all_trigger_sql(dialect_name: str) -> list[str]:
    """Every database-level guard, for a newly created database."""
    return integrity_trigger_sql(dialect_name) + ledger_trigger_sql(dialect_name) + dispatch_trigger_sql(dialect_name)


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


# Shared server settings, sent by every connection (no server configuration needed for them):
#  - lock_timeout: a posting that waits more than 15 s for another PC gives up with a clear message
#    instead of freezing the screen;
#  - idle_in_transaction_session_timeout: a PC that crashes or loses the network in the middle of a
#    save cannot hold stock rows locked for everybody else for longer than 2 minutes;
#  - TCP keepalives notice a dead VPN / network link within about a minute.
PG_CONNECT_ARGS = {
    "connect_timeout": 10,
    "options": "-c lock_timeout=15000 -c idle_in_transaction_session_timeout=120000 -c timezone=UTC",
    "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
}


def postgres_engine_options() -> dict:
    import platform
    args = dict(PG_CONNECT_ARGS, application_name=f"StoreWMS {platform.node()}"[:63])
    # A desktop program needs few connections: 30 PCs x 5 stays far below the server's 100.
    return {"pool_pre_ping": True, "pool_size": 3, "max_overflow": 2, "pool_recycle": 1800,
            "pool_timeout": 20, "connect_args": args}


# PostgreSQL error codes (SQLSTATE) that mean "try again" or "server unreachable".
_BUSY_STATES = {"40001", "40P01", "55P03"}  # serialization failure, deadlock, lock not available


def translate_db_error(exc: BaseException):
    """A user-facing error for a database failure that is not a bug, else None."""
    from sqlalchemy.orm.exc import StaleDataError

    from wms.core.exceptions import ConcurrencyError, DatabaseBusyError, DatabaseUnavailableError, WMSError

    if isinstance(exc, StaleDataError):  # a record edited on another PC since this screen loaded it
        return ConcurrencyError("Someone else changed this record on another PC while you had it open, "
                                "so your changes were not saved.\n\nClose it, open it again and redo the change.")
    if isinstance(exc, WMSError) or not isinstance(exc, DBAPIError):
        return None
    state = getattr(exc.orig, "sqlstate", None) or ""
    if state in _BUSY_STATES:
        return DatabaseBusyError("Another PC was saving the same stock at the same moment, so nothing was saved.\n\n"
                                 "Press Save again - it is safe, the document cannot be posted twice.")
    if exc.connection_invalidated or state.startswith("08") or state in ("57P01", "57P03"):
        return DatabaseUnavailableError("The connection to the database server was lost, so nothing was saved.\n\n"
                                        "Check the network (or VPN) and that the server PC is on, then try again.")
    return None


MIGRATION_LOCK_KEY = 0x57_4D_53_31  # any fixed number, the same in every copy of the program


def check_schema_not_newer(conn, cfg) -> None:
    """Refuse to open a database that a newer version of the program has already upgraded
    (a PC that missed an update, on the shared server): its screens would not match the data."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from wms.core.exceptions import SchemaTooNewError

    current = MigrationContext.configure(conn).get_current_revision()
    if current is None:
        return
    script = ScriptDirectory.from_config(cfg)
    try:
        script.get_revision(current)
    except Exception:  # noqa: BLE001 - alembic raises different errors for unknown revisions
        from wms import __version__
        raise SchemaTooNewError(
            f"The database has been upgraded by a newer version of Store WMS (database version {current}).\n\n"
            f"This PC has version {__version__}. Install the latest version on this PC, then start it again.\n"
            f"Nothing has been changed.") from None


class Database:
    def __init__(self, url: str, echo: bool = False):
        self.url = url
        kwargs: dict = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 10}
        else:
            kwargs.update(postgres_engine_options())
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
        except BaseException as exc:
            try:
                s.rollback()
            except DBAPIError:  # the connection itself is gone; nothing was committed
                pass
            friendly = translate_db_error(exc)
            if friendly is not None:
                raise friendly from exc
            raise
        finally:
            s.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        """A session for queries only; never commits."""
        s = self._read_factory()
        try:
            yield s
        except DBAPIError as exc:
            friendly = translate_db_error(exc)
            if friendly is not None:
                raise friendly from exc
            raise
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
            if self.dialect == "postgresql":
                # Many PCs may start the new version at the same moment: one upgrades the shared
                # database, the others wait here and then find nothing left to do.
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
            check_schema_not_newer(conn, cfg)
            tables = set(inspect(conn).get_table_names())
            if not tables:
                log.info("Creating new database schema")
                Base.metadata.create_all(conn)
                for stmt in all_trigger_sql(self.dialect):
                    conn.execute(text(stmt))
                command.stamp(cfg, "head")
            else:
                self._copy_before_upgrade(conn, cfg)
                command.upgrade(cfg, "head")
        self.refresh_statistics()

    def _copy_before_upgrade(self, conn, cfg) -> Path | None:
        """A full copy of the database file next to it before a newer version changes its schema
        ("wms.before-0012.db"), so an upgrade can always be undone by hand. SQLite only; a
        failed copy is logged and the upgrade goes ahead (migrations are additive and transactional)."""
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        if self.dialect != "sqlite":
            return None
        try:
            current = MigrationContext.configure(conn).get_current_revision()
            head = ScriptDirectory.from_config(cfg).get_current_head()
            path = self.engine.url.database
            if current == head or not current or not path or path == ":memory:":
                return None
            src = Path(path)
            dst = src.with_name(f"{src.stem}.before-{head}{src.suffix or '.db'}")
            if dst.exists():
                return dst
            import sqlite3
            with sqlite3.connect(str(src)) as a, sqlite3.connect(str(dst)) as b:
                a.backup(b)
            a.close()
            b.close()
            log.info("Copy of the database before the upgrade to %s: %s", head, dst)
            return dst
        except Exception:  # noqa: BLE001
            log.warning("Could not copy the database before upgrading it", exc_info=True)
            return None

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
