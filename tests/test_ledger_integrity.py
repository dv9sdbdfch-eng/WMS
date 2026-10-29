"""Phase 1, Stage 1 - data integrity: the movement ledger is the single source of truth.

Covers reversal records (who / when / why, once only, enforced by the database), reason codes,
stocktake adjustment movements, product / bin / warehouse balances, the integrity check that
reconciles every stored figure against the ledger, all-or-nothing postings and repeat-submission
protection. Each scenario ends with ``assert_reconciled``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from wms.core.exceptions import AlreadyPostedError, DuplicateError, ValidationError
from wms.db.models import DeliveryNote, GoodsIssue, MovementReversal, StockLevel, StockMovement, StockTransfer
from wms.services import audit
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.stocktake import CountEntry

D = Decimal
TODAY = date.today()


def assert_reconciled(svc):
    """Calculated ledger quantity = current stock, for every bin, product and warehouse."""
    issues = svc.ledger.integrity_check()
    critical = [str(i) for i in issues if i.severity == "CRITICAL"]
    assert critical == [], critical
    assert svc.backup.integrity_check().ok


def qty(svc, product, bin_=None) -> Decimal:
    bal = svc.ledger.product_balance(product.id)
    if bin_ is None:
        return bal.on_hand
    return sum((b.quantity for b in bal.by_bin if b.bin_id == bin_.id), D("0"))


def movements(svc, **kw):
    with svc.ctx.db.read_session() as s:
        q = select(StockMovement).order_by(StockMovement.id)
        for k, v in kw.items():
            q = q.where(getattr(StockMovement, k) == v)
        return list(s.scalars(q))


def issue(svc, sample, *lines, **header):
    return svc.goods_out.post({"issue_date": TODAY, "site_id": sample.site.id, **header}, list(lines))


# --- reversals ------------------------------------------------------------------------------

def test_goods_out_reversal_is_linked_with_reason_user_and_time(svc, sample):
    sample.receive(sample.mdf, 100, sample.bin1)
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "20", sample.bin1.id))
    assert qty(svc, sample.mdf) == D("80")
    svc.goods_out.reverse(gi.id, "Issued to the wrong project", reason_code="DATA_ENTRY_ERROR")
    assert qty(svc, sample.mdf, sample.bin1) == D("100")

    original = movements(svc, doc_type="GOODS_ISSUE", reversal_of_id=None)[0]
    rec = svc.ledger.reversal_of(original.id)
    assert rec.original_movement_id == original.id
    assert rec.reason_code == "DATA_ENTRY_ERROR" and "wrong project" in rec.reason
    assert rec.reversed_by.username == "admin" and rec.reversed_at is not None
    rev = rec.reversal
    # REVERSAL | MDF | +20 | Project -> B04: mirror image of the original, both stay in history
    assert rev.reversal_of_id == original.id and rev.quantity == original.quantity
    assert rev.to_bin_id == original.from_bin_id and rev.from_bin_id is None
    assert rev.site_id == original.site_id
    assert svc.ledger.reversal_of(rev.id).id == rec.id  # found from either side
    assert len(movements(svc, product_id=sample.mdf.id)) == 3  # receipt, issue, reversal
    with pytest.raises(ValidationError, match="already been reversed"):
        svc.goods_out.reverse(gi.id, "again", reason_code="OTHER")
    assert_reconciled(svc)


def test_transfer_and_adjustment_reverse_one_by_one(svc, sample):
    sample.receive(sample.mdf, 50, sample.bin1)
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "40")
    adj = svc.stock.adjust(sample.mdf.id, sample.bin1.id, "7", "DAMAGED", "Forklift damage")
    assert qty(svc, sample.mdf, sample.bin1) == D("7") and qty(svc, sample.mdf, sample.bin2) == D("40")

    rev = svc.ledger.reverse_movement(tr.movement_id, "DATA_ENTRY_ERROR", "Wrong bin scanned")
    assert rev.reversal_of_id == tr.movement_id and rev.from_bin_id == sample.bin2.id
    svc.ledger.reverse_movement(adj.movement_id, "DATA_ENTRY_ERROR", "Miscounted")
    assert qty(svc, sample.mdf, sample.bin1) == D("50") and qty(svc, sample.mdf, sample.bin2) == D("0")

    with pytest.raises(ValidationError, match="already been reversed"):
        svc.ledger.reverse_movement(tr.movement_id, "OTHER", "double click")
    with pytest.raises(ValidationError, match="itself a reversal"):
        svc.ledger.reverse_movement(rev.id, "OTHER", "undo the undo")
    actions = [a.action for a in svc.audit.search()]
    assert actions.count("MOVEMENT_REVERSED") == 2
    assert_reconciled(svc)


def test_document_movements_are_reversed_with_their_document(svc, sample):
    dn = sample.receive(sample.mdf, 10, sample.bin1)
    mv = movements(svc, doc_type="DELIVERY_NOTE")[0]
    with pytest.raises(ValidationError, match="reverse it there"):
        svc.ledger.reverse_movement(mv.id, "OTHER", "only this line")
    svc.goods_in.reverse(dn.id, "Wrong supplier", reason_code="INCORRECT_DELIVERY")
    assert svc.ledger.reversal_of(mv.id).reason_code == "INCORRECT_DELIVERY"
    assert_reconciled(svc)


def test_reversal_needs_a_valid_reason_code(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "5")
    for bad, msg in ((None, "Choose a reason"), ("NOPE", "Choose a reason"), ("SCRAP", "cannot be used")):
        with pytest.raises(ValidationError, match=msg):
            svc.ledger.reverse_movement(tr.movement_id, bad, "x")
    with pytest.raises(ValidationError):
        svc.ledger.reverse_movement(tr.movement_id, "OTHER", "  ")  # a written reason is required too
    assert qty(svc, sample.mdf, sample.bin2) == D("5")  # nothing changed
    assert_reconciled(svc)


def test_database_itself_refuses_bad_reversals(svc, sample):
    """Guards hold even for code (or tools) that bypass the services."""
    sample.receive(sample.mdf, 10, sample.bin1)
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "4")
    rev = svc.ledger.reverse_movement(tr.movement_id, "OTHER", "test")
    eng = svc.ctx.db.engine
    cols = ("movement_type, product_id, quantity, from_bin_id, to_bin_id, user_id, occurred_at, recorded_at, "
            "is_override, reversal_of_id")

    def insert(values: str):
        with eng.begin() as c:
            c.execute(text(f"INSERT INTO stock_movements ({cols}) VALUES ({values})"))

    now = "'2026-01-01 00:00:00.000000'"
    # a second reversal of the same movement (unique index)
    with pytest.raises(IntegrityError):
        insert(f"'TRANSFER', {sample.mdf.id}, 4000, {sample.bin2.id}, {sample.bin1.id}, 1, {now}, {now}, 0, "
               f"{tr.movement_id}")
    # reversing a reversal
    with pytest.raises(IntegrityError, match="cannot itself be reversed"):
        insert(f"'TRANSFER', {sample.mdf.id}, 4000, {sample.bin1.id}, {sample.bin2.id}, 1, {now}, {now}, 0, {rev.id}")
    # a "reversal" that does not mirror its original
    dn_mv = movements(svc, doc_type="DELIVERY_NOTE")[0]
    with pytest.raises(IntegrityError, match="must mirror"):
        insert(f"'GOODS_IN', {sample.mdf.id}, 3000, {sample.bin1.id}, NULL, 1, {now}, {now}, 0, {dn_mv.id}")
    # reversal records are append-only
    for stmt in ("UPDATE movement_reversals SET reason = 'edited'", "DELETE FROM movement_reversals"):
        with pytest.raises(IntegrityError, match="append-only"), eng.begin() as c:
            c.execute(text(stmt))
    assert_reconciled(svc)


# --- reason codes ---------------------------------------------------------------------------

def test_reason_codes_are_configurable(svc, sample):
    codes = dict(svc.reasons.choices("ADJUSTMENT"))
    for expected in ("Damaged", "Missing", "Stocktake Correction", "Scrap", "Data Entry Correction", "Other"):
        assert expected in codes.values()
    assert "Incorrect Delivery" in dict(svc.reasons.choices("REVERSAL")).values()
    assert "Returned from Project" in dict(svc.reasons.choices("RETURN")).values()

    rc = svc.reasons.save(None, {"label": "Water damage", "for_adjustment": True})
    assert rc.code == "WATER_DAMAGE" and not rc.is_system
    with pytest.raises(DuplicateError):
        svc.reasons.save(None, {"label": "Water damage", "for_adjustment": True})
    sample.receive(sample.mdf, 10, sample.bin1)
    adj = svc.stock.adjust(sample.mdf.id, sample.bin1.id, "8", "WATER_DAMAGE", "Roof leak")
    assert "Water damage" in adj.movement.notes

    svc.reasons.save(rc.id, {"label": "Water / leak damage", "for_adjustment": True, "is_active": False})
    assert "WATER_DAMAGE" not in dict(svc.reasons.choices("ADJUSTMENT"))
    with pytest.raises(ValidationError, match="retired"):
        svc.stock.adjust(sample.mdf.id, sample.bin1.id, "7", "WATER_DAMAGE", "again")
    # history keeps the code; the label follows the table
    assert svc.reasons.label("WATER_DAMAGE") == "Water / leak damage"
    system = next(r for r in svc.reasons.list("ADJUSTMENT") if r.code == "STOCK_COUNT")
    with pytest.raises(ValidationError, match="used by the system"):
        svc.reasons.save(system.id, {"label": system.label, "for_adjustment": True, "is_active": False})
    with pytest.raises(ValidationError, match="at least one"):
        svc.reasons.save(None, {"label": "Nowhere"})
    assert_reconciled(svc)


def test_stocktake_posts_stocktake_adjustment_movements(svc, sample):
    sample.receive(sample.mdf, 50, sample.bin1, "D1")
    sample.receive(sample.edge, 20, sample.bin2, "D2")
    count = svc.stocktake.create(sample.shelf.id, "Count")
    lines = {l.product.internal_code: l for l in count.lines}
    svc.stocktake.record_counts(count.id, [CountEntry(lines["MDF-18"].id, "48"),
                                           CountEntry(lines["EDGE-W22"].id, "22")])
    assert qty(svc, sample.mdf) == D("50")  # counting alone changes nothing
    svc.stocktake.submit(count.id)
    svc.stocktake.approve(count.id, "ok")
    st = movements(svc, movement_type="STOCKTAKE_ADJUSTMENT")
    assert [(m.product_id, m.quantity, bool(m.from_bin_id)) for m in st] == [
        (sample.mdf.id, D("2"), True), (sample.edge.id, D("2"), False)]
    assert qty(svc, sample.mdf) == D("48") and qty(svc, sample.edge) == D("22")
    from wms.services.search import SearchCriteria
    res = svc.search.movements(SearchCriteria(movement_types=["ADJUSTMENT"]))
    assert {m.id for m in res.rows} == {m.id for m in st}
    assert_reconciled(svc)


# --- balances -------------------------------------------------------------------------------

def test_balances_per_product_bin_and_warehouse(svc, sample):
    wh2 = svc.locations.save_location(None, {"kind": "WAREHOUSE", "code": "UNIT2", "name": "Unit 2"})
    far = svc.locations.save_bin(None, {"location_id": wh2.id, "code": "A01"})
    sample.receive(sample.mdf, 30, sample.bin1, "W1")
    sample.receive(sample.mdf, 12, far, "W2")
    sample.receive(sample.mdf, 3, sample.quarantine, "W3")
    bal = svc.ledger.product_balance(sample.mdf.id)
    assert bal.on_hand == D("45") and bal.in_unavailable_bins == D("3")
    assert bal.by_warehouse == {"MAIN": D("33"), "UNIT2": D("12")}
    assert {b.bin_code: b.quantity for b in bal.by_bin} == {
        sample.bin1.full_code: D("30"), "UNIT2 / A01": D("12"), "MAIN / QUARANTINE": D("3")}
    assert svc.ledger.warehouse_balances()[(sample.mdf.id, "UNIT2")] == D("12")
    assert_reconciled(svc)


# --- integrity check ------------------------------------------------------------------------

def test_integrity_check_reports_a_tampered_balance_as_critical(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    assert_reconciled(svc)
    with svc.ctx.db.engine.begin() as c:  # someone edits the balance table directly
        c.execute(text("UPDATE stock_levels SET quantity = quantity + 5000"))
    checks = {i.check for i in svc.ledger.critical_issues()}
    assert checks == {"Bin balance", "Product balance", "Warehouse balance"}
    msg = next(str(i) for i in svc.ledger.critical_issues() if i.check == "Bin balance")
    assert "MDF-18" in msg and "ledger 10" in msg and "current stock 15" in msg
    assert not svc.backup.integrity_check().ok
    svc.backup.rebuild_stock_levels()
    assert_reconciled(svc)


def test_integrity_check_finds_broken_reversal_and_document_links(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "4")
    svc.ledger.reverse_movement(tr.movement_id, "OTHER", "test")
    with svc.ctx.db.engine.begin() as c:  # bypass the guards to simulate damage
        c.execute(text("DROP TRIGGER trg_movement_reversals_no_delete"))
        c.execute(text("DELETE FROM movement_reversals"))
        c.execute(text("DROP TRIGGER trg_delivery_note_lines_posted_fixed"))
        c.execute(text("UPDATE delivery_note_lines SET quantity = 9000"))
    checks = sorted(i.check for i in svc.ledger.critical_issues())
    assert checks == ["Document link", "Reversal record"]


def test_integrity_check_finds_a_document_marked_reversed_without_its_reversal(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "2", sample.bin1.id))
    with svc.ctx.db.engine.begin() as c:
        c.execute(text(f"UPDATE goods_issues SET status = 'REVERSED' WHERE id = {gi.id}"))
    msgs = [i.message for i in svc.ledger.critical_issues()]
    assert len(msgs) == 1 and "marked reversed but 1 of its movements" in msgs[0]


def test_negative_stock_after_override_is_a_warning_not_critical(svc, sample):
    sample.receive(sample.mdf, 2, sample.bin1)
    svc.goods_out.post({"issue_date": TODAY, "site_id": sample.site.id},
                       [IssueLine(sample.mdf.id, "5", sample.bin1.id)], override_reason="Count was wrong")
    issues = svc.ledger.integrity_check()
    assert [i.severity for i in issues] == ["WARNING"] and "Negative stock" in issues[0].check
    assert_reconciled(svc)


# --- all or nothing -------------------------------------------------------------------------

def _fail_on(monkeypatch, action: str):
    real = audit.record

    def boom(session, ctx, act, *a, **kw):
        if act == action:
            raise RuntimeError("simulated crash")
        return real(session, ctx, act, *a, **kw)

    monkeypatch.setattr(audit, "record", boom)


def _counts(svc):
    with svc.ctx.db.read_session() as s:
        return tuple(s.scalar(text(f"SELECT count(*) FROM {t}")) for t in (
            "stock_movements", "stock_levels", "delivery_notes", "goods_issues", "stock_transfers",
            "stock_adjustments", "movement_reversals", "audit_log"))


@pytest.mark.parametrize("action", ["GOODS_IN_POSTED", "GOODS_OUT_POSTED", "STOCK_TRANSFERRED", "STOCK_ADJUSTED",
                                    "MOVEMENT_REVERSED", "GOODS_OUT_REVERSED"])
def test_a_failure_at_the_last_step_leaves_nothing_behind(svc, sample, monkeypatch, action):
    sample.receive(sample.mdf, 20, sample.bin1, "BASE")
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "3", sample.bin1.id))
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "2")
    before, stock_before = _counts(svc), svc.ledger.product_balance(sample.mdf.id).by_bin
    _fail_on(monkeypatch, action)
    ops = {
        "GOODS_IN_POSTED": lambda: sample.receive(sample.mdf, 5, sample.bin1, "CRASH"),
        "GOODS_OUT_POSTED": lambda: issue(svc, sample, IssueLine(sample.mdf.id, "1", sample.bin1.id)),
        "STOCK_TRANSFERRED": lambda: svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "1"),
        "STOCK_ADJUSTED": lambda: svc.stock.adjust(sample.mdf.id, sample.bin1.id, "1", "SCRAP", "x"),
        "MOVEMENT_REVERSED": lambda: svc.ledger.reverse_movement(tr.movement_id, "OTHER", "x"),
        "GOODS_OUT_REVERSED": lambda: svc.goods_out.reverse(gi.id, "x", reason_code="OTHER"),
    }
    with pytest.raises(RuntimeError, match="simulated crash"):
        ops[action]()
    monkeypatch.undo()
    assert _counts(svc) == before
    assert svc.ledger.product_balance(sample.mdf.id).by_bin == stock_before
    assert svc.goods_out.get(gi.id).status == "POSTED"
    assert_reconciled(svc)


# --- one physical operation, one movement ---------------------------------------------------

def test_repeated_submission_never_creates_a_second_movement(svc, sample):
    tok = str(uuid.uuid4())
    hdr = {"dn_number": "DN-77", "supplier_id": sample.supplier.id, "delivery_date": TODAY, "client_token": tok}
    svc.goods_in.post(hdr, [ReceiptLine(sample.mdf.id, D("10"), sample.bin1.id)])
    with pytest.raises(AlreadyPostedError):  # Save clicked twice / Enter pressed again
        svc.goods_in.post(hdr, [ReceiptLine(sample.mdf.id, D("10"), sample.bin1.id)])
    with pytest.raises(DuplicateError):  # same delivery note keyed again later without the token
        svc.goods_in.post({**hdr, "client_token": None}, [ReceiptLine(sample.mdf.id, D("10"), sample.bin1.id)])

    t2 = str(uuid.uuid4())
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "3", client_token=t2)
    with pytest.raises(AlreadyPostedError):
        svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "3", client_token=t2)

    t3 = str(uuid.uuid4())
    issue(svc, sample, IssueLine(sample.mdf.id, "1", sample.bin1.id), client_token=t3)
    with pytest.raises(AlreadyPostedError):
        issue(svc, sample, IssueLine(sample.mdf.id, "1", sample.bin1.id), client_token=t3)

    with svc.ctx.db.read_session() as s:
        assert s.scalar(select(text("count(*)")).select_from(DeliveryNote)) == 1
        assert s.scalar(select(text("count(*)")).select_from(StockTransfer)) == 1
        assert s.scalar(select(text("count(*)")).select_from(GoodsIssue)) == 1
    assert qty(svc, sample.mdf) == D("9")
    assert_reconciled(svc)


# --- upgrade --------------------------------------------------------------------------------

def test_upgrade_records_reversals_made_before_this_version(svc, sample):
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR
    sample.receive(sample.mdf, 10, sample.bin1)
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "4", sample.bin1.id))
    svc.goods_out.reverse(gi.id, "Posted twice", reason_code="POSTED_TWICE")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", svc.ctx.db.url)
    with svc.ctx.db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0004")  # as a 1.3 database: no reversal records
    svc.ctx.db.migrate()
    with svc.ctx.db.read_session() as s:
        rec = s.scalar(select(MovementReversal))
        assert rec.reason_code == "OTHER" and "Posted twice" in rec.reason
        assert rec.reversed_by_name_snapshot == "Administrator"
        names = {r[0] for r in s.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"trg_stock_movements_reversal_mirror", "trg_movement_reversals_no_update"} <= names
        assert s.scalar(select(text("count(*)")).select_from(StockLevel)) >= 1
    assert_reconciled(svc)
