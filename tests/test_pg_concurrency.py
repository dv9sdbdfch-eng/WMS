"""Many PCs on one PostgreSQL server at the same moment (runs only with WMS_TEST_PG set).

Each "PC" here is a thread with its own Database (own connection pool) and its own login, posting
through the normal services, released together by a barrier so the postings really overlap."""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal as D

import pytest
from conftest import PG_URL

from wms.core.exceptions import DatabaseBusyError, InsufficientStockError
from wms.db.engine import Database
from wms.services import Services
from wms.services.base import AppContext
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a PostgreSQL server (WMS_TEST_PG)")

TODAY = date.today()


def pcs(cfg, n, **db_kwargs):
    out = []
    for _ in range(n):
        s = Services(AppContext(db=Database(cfg.resolved_database_url, **db_kwargs), config=cfg))
        s.auth.login("admin", "admin")
        out.append(s)
    return out


def together(workers, job):
    """Run job(pc, i) on every PC at the same moment; returns (results, errors) by index."""
    barrier = threading.Barrier(len(workers))
    results, errors = {}, {}

    def go(i, pc):
        barrier.wait()
        try:
            results[i] = job(pc, i)
        except Exception as exc:  # noqa: BLE001 - collected and checked by the test
            errors[i] = exc

    threads = [threading.Thread(target=go, args=(i, pc)) for i, pc in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    for pc in workers:
        pc.ctx.db.dispose()
    return results, errors


def test_last_sheets_never_go_below_zero(svc, sample, cfg):
    sample.receive(sample.mdf, 10, sample.bin1)
    workers = pcs(cfg, 8)

    def issue(pc, i):
        return pc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY},
                                 [IssueLine(sample.mdf.id, D(2), sample.bin1.id)])

    done, failed = together(workers, issue)
    assert len(done) == 5, failed  # 10 sheets / 2 each
    assert all(isinstance(e, (InsufficientStockError, DatabaseBusyError)) for e in failed.values()), failed
    assert svc.products.row(sample.mdf.id).on_hand == D(0)
    numbers = sorted(d.issue_number for d in done.values())
    assert len(set(numbers)) == 5  # no duplicate document numbers
    # gap-free: the failed postings rolled their number back
    assert [int(n.split("-")[-1]) for n in numbers] == list(range(int(numbers[0].split("-")[-1]),
                                                                  int(numbers[0].split("-")[-1]) + 5))
    assert svc.ledger.integrity_check() == []  # ledger and bin balances agree


def test_different_documents_on_the_same_bin(svc, sample, cfg):
    """Goods Out and transfers take numbers from different sequences, so only the bin's own
    lock keeps them apart: the stock check must be made after taking that lock."""
    sample.receive(sample.mdf, 10, sample.bin1)
    workers = pcs(cfg, 10)

    def take(pc, i):
        if i % 2:
            return pc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, D(2))
        return pc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY},
                                 [IssueLine(sample.mdf.id, D(2), sample.bin1.id)])

    done, failed = together(workers, take)
    assert len(done) == 5, failed
    assert all(isinstance(e, (InsufficientStockError, DatabaseBusyError)) for e in failed.values()), failed
    with svc.ctx.db.read_session() as s:
        from wms.services.stock_engine import StockEngine
        assert StockEngine(s, svc.ctx).qty_in_bin(sample.mdf.id, sample.bin1.id) == D(0)
    assert svc.ledger.integrity_check() == []


def test_first_stock_into_an_empty_bin_from_many_pcs(svc, sample, cfg):
    workers = pcs(cfg, 8)

    def receive(pc, i):
        return pc.goods_in.post({"dn_number": f"DN-C{i}", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                                [ReceiptLine(sample.edge.id, D(5), sample.bin2.id, None)])

    done, failed = together(workers, receive)
    assert not failed, failed
    assert svc.products.row(sample.edge.id).on_hand == D(40)


def test_a_pc_waiting_too_long_gets_a_clear_message(svc, sample, cfg, monkeypatch):
    """One PC holds the bin (a long save); another gives up after the lock timeout with a
    friendly 'save again' message instead of freezing."""
    import wms.db.engine as eng
    sample.receive(sample.mdf, 10, sample.bin1)
    monkeypatch.setitem(eng.PG_CONNECT_ARGS, "options", "-c lock_timeout=300 -c timezone=UTC")
    [other] = pcs(cfg, 1)
    from sqlalchemy import text
    with svc.ctx.db.session() as s:  # this PC is in the middle of a save on that bin
        s.execute(text("SELECT id FROM stock_levels WHERE product_id = :p AND bin_id = :b FOR UPDATE"),
                  {"p": sample.mdf.id, "b": sample.bin1.id})
        with pytest.raises(DatabaseBusyError, match="Save again"):
            other.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY},
                                 [IssueLine(sample.mdf.id, D(1), sample.bin1.id)])
    other.ctx.db.dispose()
    assert svc.products.row(sample.mdf.id).on_hand == D(10)


def test_server_guards_match_sqlite(svc, sample):
    """The protective triggers work on PostgreSQL as on SQLite: a posted line or a reversed
    document cannot be changed, stock records cannot be deleted, the ledger is append-only."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    dn = sample.receive(sample.mdf, 3, sample.bin1)
    svc.goods_in.reverse(dn.id, "test")
    for sql in ("UPDATE delivery_note_lines SET quantity = 1",
                "UPDATE delivery_notes SET status = 'POSTED'",
                "DELETE FROM delivery_notes",
                "UPDATE stock_movements SET quantity = 1",
                "DELETE FROM audit_log",
                "INSERT INTO delivery_note_lines (delivery_note_id, line_no, product_id, quantity, bin_id) "
                f"VALUES ({dn.id}, 99, {sample.mdf.id}, 0, {sample.bin1.id})"):
        with pytest.raises(DBAPIError), svc.ctx.db.session() as s:
            s.execute(text(sql))
    with pytest.raises(DBAPIError, match="mirror"), svc.ctx.db.session() as s:
        mv = s.execute(text("SELECT id FROM stock_movements ORDER BY id LIMIT 1")).scalar()
        s.execute(text("INSERT INTO stock_movements (movement_type, product_id, quantity, to_bin_id, occurred_at, "
                       "reversal_of_id, user_id) VALUES ('ADJUSTMENT', :p, 999, :b, now(), :m, 1)"),
                  {"p": sample.mdf.id, "b": sample.bin1.id, "m": mv})


def _pg_objects(db) -> dict[str, set[str]]:
    from sqlalchemy import text
    q = {"table": "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
         "index": "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'",
         "trigger": "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal",
         "column": "SELECT table_name || '.' || column_name || ':' || data_type || ':' || is_nullable "
                   "FROM information_schema.columns WHERE table_schema = 'public'"}
    with db.engine.connect() as c:
        return {k: set(c.execute(text(sql)).scalars()) for k, sql in q.items()}


def test_every_migration_runs_on_postgresql(pg_database):
    """Upgrading step by step from the first schema gives the same tables, columns, indexes and
    guards as a new server database - so later upgrades will also run on the shared server."""
    from alembic import command
    from alembic.config import Config
    from conftest import _pg_admin
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from wms.db.engine import MIGRATIONS_DIR
    name = "wms_chain_" + make_url(pg_database).database[-8:]
    with _pg_admin().connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(pg_database).set(database=name).render_as_string(hide_password=False)
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "0001")
        old = Database(url)
        old.migrate()
        new = Database(pg_database)
        a, b = _pg_objects(new), _pg_objects(old)
        old.dispose()
        new.dispose()
        for kind in ("table", "column", "index", "trigger"):
            assert a[kind] == b[kind], (kind, sorted(a[kind] ^ b[kind]))
    finally:
        with _pg_admin().connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
