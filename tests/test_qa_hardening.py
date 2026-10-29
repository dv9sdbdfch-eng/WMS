"""QA cycle (v1.1): stock integrity sequences, atomicity, duplicate protection, attachments,
backup/restore safety, database guards, reports against the ledger, stocktake and edge cases.

Every stock scenario ends with ``assert_consistent``: the cached balances must equal what the
immutable movement ledger says, and the database must pass its own integrity check.
"""

from __future__ import annotations

import os
import shutil
import uuid
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from wms.core.enums import StockStatus
from wms.core.exceptions import (
    AlreadyPostedError,
    DuplicateError,
    InsufficientStockError,
    PermissionDeniedError,
    ReturnExceedsIssuedError,
    ValidationError,
)
from wms.core.numbers import to_qty
from wms.db.models import Attachment, DeliveryNote, GoodsIssue, NumberSequence, StockLevel, StockMovement
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.reports import ReportFilters
from wms.services.stock_ops import ReturnLineInput
from wms.services.stocktake import CountEntry

D = Decimal
TODAY = date.today()
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 200


# --- helpers -------------------------------------------------------------------------

def bins_of(svc, product) -> dict[str, Decimal]:
    return {l.bin.code: l.quantity for l in svc.products.stock_by_bin(product.id)}


def total(svc, product) -> Decimal:
    return sum(bins_of(svc, product).values(), D("0"))


def ledger_total(svc, product) -> Decimal:
    with svc.ctx.db.read_session() as s:
        inn = s.scalar(select(func.sum(StockMovement.quantity)).where(
            StockMovement.product_id == product.id, StockMovement.to_bin_id.is_not(None))) or D("0")
        out = s.scalar(select(func.sum(StockMovement.quantity)).where(
            StockMovement.product_id == product.id, StockMovement.from_bin_id.is_not(None))) or D("0")
    return inn - out


def assert_consistent(svc):
    rep = svc.backup.integrity_check()
    assert rep.database_ok, rep.database_messages
    assert rep.foreign_key_problems == 0
    assert rep.stock_discrepancies == []


def count_rows(svc, model) -> int:
    with svc.ctx.db.read_session() as s:
        return s.scalar(select(func.count()).select_from(model))


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def issue(svc, sample, *lines, override_reason=None, **header):
    return svc.goods_out.post({"issue_date": TODAY, "site_id": sample.site.id, **header}, list(lines),
                              override_reason=override_reason)


def login_as(svc, role_code: str, username: str):
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": username, "full_name": username.title(), "role_id": roles[role_code].id,
                           "must_change_password": False}, "secret1")
    svc.auth.login(username, "secret1")


# --- 2. stock integrity: the full sequence from the brief ---------------------------

def test_full_transaction_sequence_matches_expected_after_every_step(svc, sample):
    p = sample.edge  # metres, decimals allowed; default bin B05 (bin2)
    assert total(svc, p) == 0
    sample.receive(p, 100, sample.bin2, "SEQ-100")
    assert (total(svc, p), bins_of(svc, p)) == (D("100"), {"B05": D("100")})
    issue(svc, sample, IssueLine(p.id, "30"))
    assert (total(svc, p), bins_of(svc, p)) == (D("70"), {"B05": D("70")})
    svc.stock.transfer(p.id, sample.bin2.id, sample.bin1.id, "20")
    assert (total(svc, p), bins_of(svc, p)) == (D("70"), {"B04": D("20"), "B05": D("50")})  # total unchanged
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": sample.site.id},
                          [ReturnLineInput(p.id, "5", sample.bin1.id)])
    assert (total(svc, p), bins_of(svc, p)) == (D("75"), {"B04": D("25"), "B05": D("50")})
    svc.stock.post_return({"return_type": "TO_SUPPLIER", "return_date": TODAY, "supplier_id": sample.supplier.id},
                          [ReturnLineInput(p.id, "10", sample.bin2.id)])
    assert (total(svc, p), bins_of(svc, p)) == (D("65"), {"B04": D("25"), "B05": D("40")})
    adj = svc.stock.adjust(p.id, sample.bin2.id, "38", "LOST", "2 m damaged and binned")
    assert (adj.previous_qty, adj.new_qty, adj.difference) == (D("40"), D("38"), D("-2"))
    assert (total(svc, p), bins_of(svc, p)) == (D("63"), {"B04": D("25"), "B05": D("38")})
    assert ledger_total(svc, p) == D("63")
    # every step is traceable: one movement per operation, in order
    types = [m.movement_type for m in reversed(svc.stock.movements(product_id=p.id))]
    assert types == ["GOODS_IN", "GOODS_OUT", "TRANSFER", "RETURN_FROM_SITE", "RETURN_TO_SUPPLIER",
                     "ADJUSTMENT_OUT"]
    summary = svc.sites.material_summary(sample.site.id)
    assert (summary[0].issued, summary[0].returned, summary[0].net) == (D("30"), D("5"), D("25"))
    assert_consistent(svc)


def test_decimal_quantities_kept_exactly(svc, sample):
    sample.receive(sample.edge, "0.001", number="D1")
    sample.receive(sample.edge, "12.345", number="D2")
    issue(svc, sample, IssueLine(sample.edge.id, "0.346"))
    assert total(svc, sample.edge) == D("12.000")
    assert_consistent(svc)


@pytest.mark.parametrize("qty", ["0", "-5", "abc", "NaN", "inf", "1e400", "999999999999999999", "0.0004"])
def test_invalid_quantities_rejected_with_a_clear_message(svc, sample, qty):
    with pytest.raises(ValidationError):
        svc.goods_in.post({"dn_number": "BAD", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                          [ReceiptLine(sample.edge.id, qty, sample.bin2.id)])
    assert total(svc, sample.edge) == 0
    assert count_rows(svc, DeliveryNote) == 0


def test_goods_out_reversal_restores_bins_and_keeps_history(svc, sample):
    sample.receive(sample.edge, 30, sample.bin1, "A")
    sample.receive(sample.edge, 50, sample.bin2, "B")
    gi = issue(svc, sample, IssueLine(sample.edge.id, "70"))
    svc.goods_out.reverse(gi.id, "Posted twice by mistake")
    assert bins_of(svc, sample.edge) == {"B04": D("30"), "B05": D("50")}
    assert svc.goods_out.get(gi.id).status == "REVERSED"
    mv = svc.stock.movements(product_id=sample.edge.id)
    assert sum(1 for m in mv if m.reversal_of_id) == 2 and len(mv) == 6
    assert svc.sites.material_summary(sample.site.id) == []
    assert_consistent(svc)


# --- 3. atomicity --------------------------------------------------------------------

def test_failure_on_last_line_leaves_nothing_behind(svc, sample, monkeypatch):
    sample.receive(sample.mdf, 10)
    before = (count_rows(svc, StockMovement), count_rows(svc, GoodsIssue), bins_of(svc, sample.mdf))
    from wms.services import stock_engine
    real_post = stock_engine.StockEngine.post
    calls = {"n": 0}

    def flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk failure half-way through the document")
        return real_post(self, **kw)

    monkeypatch.setattr(stock_engine.StockEngine, "post", flaky)
    with pytest.raises(OSError):
        issue(svc, sample, IssueLine(sample.mdf.id, "2"), IssueLine(sample.mdf.id, "3"))
    monkeypatch.setattr(stock_engine.StockEngine, "post", real_post)
    assert (count_rows(svc, StockMovement), count_rows(svc, GoodsIssue), bins_of(svc, sample.mdf)) == before
    # the document number was not used up either (numbers stay gap-free)
    assert issue(svc, sample, IssueLine(sample.mdf.id, "1")).issue_number == "GO-000001"
    assert_consistent(svc)


def test_failure_writing_audit_rolls_back_stock(svc, sample, monkeypatch):
    from wms.services import goods_in
    real = goods_in.audit.record

    def boom(session, ctx, action, *a, **k):
        if action == "GOODS_IN_POSTED":
            raise RuntimeError("audit write failed")
        return real(session, ctx, action, *a, **k)

    monkeypatch.setattr(goods_in.audit, "record", boom)
    with pytest.raises(RuntimeError):
        sample.receive(sample.mdf, 10, number="ATOM")
    assert total(svc, sample.mdf) == 0 and count_rows(svc, StockMovement) == 0
    assert_consistent(svc)


# --- 4. duplicate protection -----------------------------------------------------------

def test_same_goods_out_form_cannot_post_twice(svc, sample):
    sample.receive(sample.mdf, 10)
    token = uuid.uuid4().hex
    first = issue(svc, sample, IssueLine(sample.mdf.id, "3"), client_token=token)
    with pytest.raises(AlreadyPostedError) as e:
        issue(svc, sample, IssueLine(sample.mdf.id, "3"), client_token=token)
    assert first.issue_number in e.value.message and e.value.entity_id == first.id
    assert total(svc, sample.mdf) == D("7")


def test_same_goods_in_form_cannot_post_twice_even_with_new_number(svc, sample):
    token = uuid.uuid4().hex
    header = {"dn_number": "T-1", "supplier_id": sample.supplier.id, "delivery_date": TODAY, "client_token": token}
    svc.goods_in.post(header, [ReceiptLine(sample.mdf.id, "5", sample.bin1.id)])
    with pytest.raises(AlreadyPostedError):
        svc.goods_in.post({**header, "dn_number": "T-1 (typo fixed)"}, [ReceiptLine(sample.mdf.id, "5", sample.bin1.id)])
    with pytest.raises(DuplicateError):  # different form, same supplier + DN number
        svc.goods_in.post({**header, "client_token": None}, [ReceiptLine(sample.mdf.id, "5", sample.bin1.id)])
    assert total(svc, sample.mdf) == D("5")


def test_transfer_adjustment_and_return_tokens(svc, sample):
    sample.receive(sample.mdf, 10)
    issue(svc, sample, IssueLine(sample.mdf.id, "4"))
    t = uuid.uuid4().hex
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "2", client_token=t)
    with pytest.raises(AlreadyPostedError):
        svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "2", client_token=t)
    a = uuid.uuid4().hex
    svc.stock.adjust(sample.mdf.id, sample.bin1.id, "3", "LOST", "x", client_token=a)
    with pytest.raises(AlreadyPostedError):
        svc.stock.adjust(sample.mdf.id, sample.bin1.id, "2", "LOST", "x", client_token=a)
    r = uuid.uuid4().hex
    hdr = {"return_type": "FROM_SITE", "return_date": TODAY, "site_id": sample.site.id, "client_token": r}
    svc.stock.post_return(hdr, [ReturnLineInput(sample.mdf.id, "1", sample.bin1.id)])
    with pytest.raises(AlreadyPostedError):
        svc.stock.post_return(hdr, [ReturnLineInput(sample.mdf.id, "1", sample.bin1.id)])
    assert bins_of(svc, sample.mdf) == {"B04": D("4"), "B05": D("2")}
    assert_consistent(svc)


def test_recent_identical_goods_out_is_detected(svc, sample):
    sample.receive(sample.mdf, 10)
    sample.receive(sample.edge, 10, number="E")
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "2"), IssueLine(sample.edge.id, "1.5"))
    same = [IssueLine(sample.edge.id, "1.5"), IssueLine(sample.mdf.id, "2")]
    assert svc.goods_out.recent_similar(sample.site.id, same).id == gi.id
    assert svc.goods_out.recent_similar(sample.site.id, [IssueLine(sample.mdf.id, "2")]) is None
    svc.goods_out.reverse(gi.id, "mistake")
    assert svc.goods_out.recent_similar(sample.site.id, same) is None


# --- 5/6. delivery notes and attachments ------------------------------------------------

def test_delivery_note_with_jpg_png_pdf_and_search(svc, sample, tmp_path):
    files = [write(tmp_path, "scan.jpg", JPEG), write(tmp_path, "page2.png", PNG), write(tmp_path, "dn.pdf", PDF)]
    dn = svc.goods_in.post({"dn_number": "DN 7781", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                           [ReceiptLine(sample.mdf.id, "4", sample.bin1.id), ReceiptLine(sample.edge.id, "2.5")],
                           files)
    atts = svc.attachments.list_for("DELIVERY_NOTE", dn.id)
    assert sorted(a.original_name for a in atts) == ["dn.pdf", "page2.png", "scan.jpg"]
    for f in files:  # originals moved/deleted from the Desktop - the WMS copies are unaffected
        f.unlink()
    assert all(svc.attachments.path_of(a).is_file() for a in atts)
    assert len({a.stored_name for a in atts}) == 3 and all(".." not in a.stored_name for a in atts)
    for q in ("dn7781", "DN-7781", " 7781 ", "Egger"):
        assert [r[0].id for r in svc.goods_in.search(q)] == [dn.id], q
    assert any(h.entity_id == dn.id for h in svc.search.search("dn7781"))
    assert svc.goods_in.search(supplier_id=sample.supplier.id, date_from=TODAY, date_to=TODAY)[0][1] == 2


def test_two_files_with_the_same_name_do_not_overwrite_each_other(svc, sample, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write(tmp_path / "a", "scan.jpg", JPEG + b"A")
    b = write(tmp_path / "b", "scan.jpg", JPEG + b"B")
    x = svc.attachments.add("PRODUCT", sample.mdf.id, a)
    y = svc.attachments.add("PRODUCT", sample.mdf.id, b)
    assert x.stored_name != y.stored_name
    assert svc.attachments.path_of(x).read_bytes().endswith(b"A")
    assert svc.attachments.path_of(y).read_bytes().endswith(b"B")


@pytest.mark.parametrize("name,data", [("fake.pdf", b"MZ\x90\x00 an exe renamed"), ("broken.jpg", b"not an image"),
                                       ("empty.png", b""), ("macro.docm", b"PK\x03\x04")])
def test_invalid_or_corrupted_attachments_rejected(svc, sample, tmp_path, name, data):
    with pytest.raises(ValidationError):
        svc.attachments.add("PRODUCT", sample.mdf.id, write(tmp_path, name, data))
    assert count_rows(svc, Attachment) == 0


def test_bad_attachment_stops_the_delivery_before_any_stock_moves(svc, sample, tmp_path):
    good, bad = write(tmp_path, "ok.jpg", JPEG), write(tmp_path, "bad.pdf", b"garbage")
    with pytest.raises(ValidationError):
        svc.goods_in.post({"dn_number": "X1", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                          [ReceiptLine(sample.mdf.id, "4", sample.bin1.id)], [good, bad])
    assert total(svc, sample.mdf) == 0 and count_rows(svc, DeliveryNote) == 0
    with pytest.raises(ValidationError):  # missing file
        svc.goods_in.post({"dn_number": "X2", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                          [ReceiptLine(sample.mdf.id, "4", sample.bin1.id)], [tmp_path / "gone.pdf"])
    assert count_rows(svc, DeliveryNote) == 0


def test_attachment_failure_rolls_back_delivery_and_removes_copied_files(svc, sample, tmp_path, monkeypatch):
    from wms.services.attachments import AttachmentService

    def boom(*a, **k):
        raise OSError("disk full while recording the scan")

    monkeypatch.setattr(AttachmentService, "attach_staged", boom)
    with pytest.raises(OSError):
        svc.goods_in.post({"dn_number": "ATT1", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                          [ReceiptLine(sample.mdf.id, "4", sample.bin1.id)], [write(tmp_path, "s.jpg", JPEG)])
    assert count_rows(svc, DeliveryNote) == 0 and total(svc, sample.mdf) == 0
    store = svc.ctx.config.attachments_path
    assert not [f for f in store.rglob("*") if f.is_file()]  # the staged copy was removed
    assert svc.goods_in.find_existing(sample.supplier.id, "ATT1") is None  # the user can simply post again


def test_missing_attachment_file_is_reported(svc, sample, tmp_path):
    att = svc.attachments.add("PRODUCT", sample.mdf.id, write(tmp_path, "p.png", PNG))
    svc.attachments.path_of(att).unlink()
    rep = svc.backup.integrity_check()
    assert rep.missing_files == ["p.png"] and not rep.ok


# --- 7. products --------------------------------------------------------------------------

def test_product_codes_lengths_and_special_characters(svc, sample):
    unit = sample.edge.unit_id
    with pytest.raises(ValidationError):
        svc.products.save(None, {"internal_code": "X" * 51, "name": "n", "unit_id": unit})
    with pytest.raises(ValidationError):
        svc.products.save(None, {"internal_code": "  ", "name": "n", "unit_id": unit})
    with pytest.raises(ValidationError):
        svc.products.save(None, {"internal_code": "OK1", "name": "n" * 201, "unit_id": unit})
    p = svc.products.save(None, {"internal_code": "o'brien-50%_off", "name": "Ünïcödé \"quote\" ; DROP TABLE x;--",
                                 "description": "d" * 5000, "unit_id": unit})
    assert p.internal_code == "O'BRIEN-50%_OFF"
    assert [r.product.id for r in svc.products.list(text="50%_")] == [p.id]
    # '%' and '_' are literal characters, not "match everything"
    assert [r.product.id for r in svc.products.list(text="%")] == [p.id]
    assert [r.product.id for r in svc.products.list(text="_")] == [p.id]
    assert [r.product.id for r in svc.products.list(text="drop table")] == [p.id]


def test_barcode_must_identify_one_product(svc, sample):
    unit = sample.edge.unit_id
    svc.products.save(None, {"internal_code": "HINGE-1", "name": "Hinge", "unit_id": unit, "barcode": "5012345678900"})
    with pytest.raises(DuplicateError):  # same barcode, different case/spacing
        svc.products.save(None, {"internal_code": "HINGE-2", "name": "Hinge", "unit_id": unit,
                                 "barcode": " 5012345678900 "})
    with pytest.raises(DuplicateError):  # barcode equal to another product's internal code
        svc.products.save(None, {"internal_code": "HINGE-3", "name": "Hinge", "unit_id": unit, "barcode": "mdf-18"})
    with pytest.raises(DuplicateError):  # internal code equal to another product's barcode
        svc.products.save(None, {"internal_code": "5012345678900", "name": "Hinge", "unit_id": unit})
    assert svc.products.find_by_code(" 5012345678900\n").internal_code == "HINGE-1"
    assert svc.products.find_by_code("mdf-18").internal_code == "MDF-18"
    assert svc.products.find_by_code("eg-mdf18").internal_code == "MDF-18"  # supplier code
    svc.products.save(None, {"internal_code": "MDF-18-B", "name": "MDF other", "unit_id": sample.mdf.unit_id,
                             "supplier_code": "EG-MDF18"})
    assert svc.products.find_by_code("EG-MDF18") is None  # ambiguous supplier code: no random pick


def test_archive_blocked_while_any_bin_holds_stock_even_if_total_is_zero(svc, sample):
    svc.stock.adjust(sample.cabinet.id, sample.bin1.id, "5", "FOUND", "found")
    issue(svc, sample, IssueLine(sample.cabinet.id, "5", sample.bin2.id), override_reason="count wrong")
    assert total(svc, sample.cabinet) == 0
    p = svc.products.get(sample.cabinet.id)
    data = {"item_type": "FINISHED", "internal_code": p.internal_code, "name": p.name, "unit_id": p.unit_id,
            "is_active": False}
    with pytest.raises(ValidationError, match="2 bin"):
        svc.products.save(p.id, data)
    svc.stock.transfer(sample.cabinet.id, sample.bin1.id, sample.bin2.id, "5")
    assert svc.products.save(p.id, data).is_active is False  # archived, history kept
    assert len(svc.products.movements(p.id)) == 3
    with pytest.raises(ValidationError, match="inactive"):
        svc.stock.adjust(p.id, sample.bin1.id, "1", "FOUND", "x")
    assert_consistent(svc)


# --- 8. locations ---------------------------------------------------------------------

def test_where_is_product_multiple_bins(svc, sample):
    sample.receive(sample.mdf, 7, sample.bin1, "W1")
    sample.receive(sample.mdf, 3, sample.bin2, "W2")
    sample.receive(sample.mdf, 1, sample.quarantine, "W3")
    where = [(l.bin.full_code, l.quantity, l.bin.is_available) for l in svc.products.stock_by_bin(sample.mdf.id)]
    assert where == [("MAIN / A / R03 / S02 / B04", D("7"), True), ("MAIN / A / R03 / S02 / B05", D("3"), True),
                     ("MAIN / QUARANTINE", D("1"), False)]


def test_archiving_location_archives_its_bins_and_blocks_new_stock(svc, sample):
    rack2 = svc.locations.save_location(None, {"kind": "RACK", "code": "R09", "parent_id": sample.zone.id})
    shelf = svc.locations.save_location(None, {"kind": "SHELF", "code": "S1", "parent_id": rack2.id})
    b = svc.locations.save_bin(None, {"location_id": shelf.id, "code": "B1"})
    sample.receive(sample.mdf, 2, b, "L1")
    with pytest.raises(ValidationError, match="holds stock"):
        svc.locations.save_location(rack2.id, {"kind": "RACK", "code": "R09", "parent_id": sample.zone.id,
                                               "is_active": False})
    svc.stock.transfer(sample.mdf.id, b.id, sample.bin1.id, "2")
    svc.locations.save_location(rack2.id, {"kind": "RACK", "code": "R09", "parent_id": sample.zone.id,
                                           "is_active": False})
    assert b.id not in {x.id for x in svc.locations.bins()}
    assert b.id in {x.id for x in svc.locations.bins(include_inactive=True)}
    with pytest.raises(ValidationError, match="inactive"):
        sample.receive(sample.mdf, 1, svc.locations.reload(type(b), b.id), "L2")
    with pytest.raises(ValidationError, match="archived"):
        svc.locations.save_bin(None, {"location_id": shelf.id, "code": "B2"})
    # history still shows the archived bin
    assert any(m.from_bin and m.from_bin.id == b.id for m in svc.stock.movements(product_id=sample.mdf.id))


def test_location_codes_with_wildcards_do_not_leak_into_other_locations(svc, sample):
    z1 = svc.locations.save_location(None, {"kind": "ZONE", "code": "A_1", "parent_id": sample.main.id})
    z2 = svc.locations.save_location(None, {"kind": "ZONE", "code": "AX1", "parent_id": sample.main.id})
    b1 = svc.locations.save_bin(None, {"location_id": z1.id, "code": "B"})
    b2 = svc.locations.save_bin(None, {"location_id": z2.id, "code": "B"})
    assert [b.id for b in svc.locations.bins(z1.id)] == [b1.id]
    assert [b.id for b in svc.locations.bins(z2.id)] == [b2.id]


# --- 9. sites / projects ------------------------------------------------------------------

def test_return_from_site_more_than_issued_needs_confirmation(svc, sample):
    sample.receive(sample.mdf, 10)
    issue(svc, sample, IssueLine(sample.mdf.id, "3"))
    hdr = {"return_type": "FROM_SITE", "return_date": TODAY, "site_id": sample.site.id}
    with pytest.raises(ReturnExceedsIssuedError):
        svc.stock.post_return(hdr, [ReturnLineInput(sample.mdf.id, "2", sample.bin1.id),
                                    ReturnLineInput(sample.mdf.id, "2", sample.bin1.id)])  # 4 > 3 across lines
    assert total(svc, sample.mdf) == D("7")
    svc.stock.post_return({**hdr, "allow_more_than_issued": True},
                          [ReturnLineInput(sample.mdf.id, "4", sample.bin1.id)])
    assert total(svc, sample.mdf) == D("11")
    assert_consistent(svc)


def test_project_material_history_has_everything_needed(svc, sample):
    sample.receive(sample.mdf, 10)
    issue(svc, sample, IssueLine(sample.mdf.id, "6"), reference="JOB-77", requested_by="Dave")
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": sample.site.id,
                           "reference": "RET-1"}, [ReturnLineInput(sample.mdf.id, "2", sample.bin1.id)])
    rows = svc.reports.run("project_history", ReportFilters(site_id=sample.site.id))
    titles = [c.title for c in rows.columns]
    for needed in ("Date", "Code", "Material", "Issued", "Returned", "Document", "Reference", "User"):
        assert needed in titles
    by = {r[titles.index("Document")]: r for r in rows.rows}
    assert len(by) == 2
    assert sum(r[titles.index("Issued")] or 0 for r in rows.rows) == D("6")
    assert sum(r[titles.index("Returned")] or 0 for r in rows.rows) == D("2")
    assert any(r[titles.index("Reference")] == "JOB-77" for r in rows.rows)


# --- 13. database-level guards --------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM goods_issue_lines",
    "DELETE FROM goods_issues",
    "DELETE FROM delivery_notes",
    "UPDATE goods_issue_lines SET quantity = 1",
    "UPDATE delivery_note_lines SET product_id = product_id + 1",
    "INSERT INTO goods_issue_lines (issue_id, line_no, product_id, quantity, bin_id) SELECT id, 9, 1, 0, 1 "
    "FROM goods_issues",
])
def test_stock_documents_cannot_be_deleted_or_edited_outside_the_app(svc, sample, sql):
    sample.receive(sample.mdf, 10)
    issue(svc, sample, IssueLine(sample.mdf.id, "2"))
    with pytest.raises(Exception, match="cannot|must be above zero"):
        with svc.ctx.db.session() as s:
            s.execute(text(sql))
    assert_consistent(svc)


def test_reversed_document_cannot_be_reopened(svc, sample):
    sample.receive(sample.mdf, 10)
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "2"))
    svc.goods_out.reverse(gi.id, "wrong")
    with pytest.raises(Exception, match="reversed"):
        with svc.ctx.db.session() as s:
            s.execute(text("UPDATE goods_issues SET status = 'POSTED'"))


def test_upgrade_from_version_1_0_database(tmp_path):
    """A database created by v1.0 (schema 0001) upgrades in place and gains the new guards."""
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR, Database
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0001")
    db = Database(url)
    db.migrate()
    with db.engine.connect() as c:
        from alembic.script import ScriptDirectory
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"trg_goods_issue_lines_no_delete", "trg_stock_movements_no_update"} <= names
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(goods_issues)"))}
        assert "client_token" in cols
    db.dispose()


# --- 14. backup / restore ------------------------------------------------------------------

def test_backup_contains_attachments_and_restore_brings_back_data(svc, sample, cfg, tmp_path):
    dn = svc.goods_in.post({"dn_number": "BK1", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                           [ReceiptLine(sample.mdf.id, "10", sample.bin1.id)], [write(tmp_path, "dn.pdf", PDF)])
    att = svc.attachments.list_for("DELIVERY_NOTE", dn.id)[0]
    backup = svc.backup.create(note="before mistakes")
    with zipfile.ZipFile(backup) as z:
        assert f"attachments/{att.stored_name}" in z.namelist()
    assert svc.backup.verify(backup)["attachments_missing_from_backup"] == []
    issue(svc, sample, IssueLine(sample.mdf.id, "4"))  # done after the backup
    svc.attachments.path_of(att).unlink()  # scan lost from disk
    safety = svc.backup.restore(backup)
    assert safety.exists() and "before-restore" in safety.name
    from wms.db.engine import Database
    from wms.services import Services
    from wms.services.base import AppContext
    db2 = Database(cfg.resolved_database_url)
    db2.migrate()
    s2 = Services(AppContext(db=db2, config=cfg))
    s2.auth.login("admin", "admin")
    assert sum(l.quantity for l in s2.products.stock_by_bin(sample.mdf.id)) == D("10")
    assert s2.goods_out.search() == []
    assert s2.attachments.path_of(att).read_bytes() == PDF  # scan restored too
    assert s2.backup.integrity_check().ok
    db2.dispose()


def test_restore_never_writes_outside_the_attachment_folder(svc, sample, cfg, tmp_path):
    backup = svc.backup.create()
    evil = tmp_path / "StoreWMS_evil.zip"
    victim = cfg.data_dir.parent / "zipslip_victim.txt"
    with zipfile.ZipFile(backup) as zi, zipfile.ZipFile(evil, "w") as zo:
        for n in zi.namelist():
            zo.writestr(n, zi.read(n))
        zo.writestr("attachments/../../zipslip_victim.txt", "pwned")
    svc.backup.restore(evil)
    assert not victim.exists()
    assert not (cfg.data_dir / "zipslip_victim.txt").exists()


def test_restore_rejects_damaged_or_foreign_files(svc, tmp_path):
    bad = tmp_path / "StoreWMS_bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("wms.db", b"this is not sqlite")
        z.writestr("manifest.json", "{}")
    with pytest.raises(ValidationError):
        svc.backup.restore(bad)
    other = tmp_path / "other.db"
    import sqlite3
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE x (a)")
    con.commit()
    con.close()
    foreign = tmp_path / "StoreWMS_foreign.zip"
    with zipfile.ZipFile(foreign, "w") as z:
        z.write(other, "wms.db")
        z.writestr("manifest.json", "{}")
    with pytest.raises(ValidationError, match="Store WMS"):
        svc.backup.restore(foreign)
    assert svc.backup.integrity_check().database_ok  # live data untouched


def test_backup_requires_permission(svc, sample):
    login_as(svc, "OPERATIVE", "op")
    with pytest.raises(PermissionDeniedError):
        svc.backup.create()
    with pytest.raises(PermissionDeniedError):
        svc.backup.restore(Path("x.zip"))


# --- 19. low stock --------------------------------------------------------------------------

def test_stock_statuses(svc, sample):
    rows = {r.product.internal_code: r for r in svc.products.list()}
    assert rows["MDF-18"].status is StockStatus.OUT  # min 10, nothing in stock
    assert rows["EDGE-W22"].status is StockStatus.NO_MINIMUM
    sample.receive(sample.mdf, 8)
    assert {r.product.internal_code: r for r in svc.products.list()}["MDF-18"].status is StockStatus.LOW
    sample.receive(sample.mdf, 50, sample.quarantine, "Q")  # quarantine stock cannot be issued
    row = {r.product.internal_code: r for r in svc.products.list()}["MDF-18"]
    assert row.status is StockStatus.LOW and row.shortfall == D("2")
    sample.receive(sample.mdf, 5, number="MORE")
    assert {r.product.internal_code: r for r in svc.products.list()}["MDF-18"].status is StockStatus.OK
    assert [r.product.internal_code for r in svc.products.low_stock()] == []


# --- 20. reports against the ledger -------------------------------------------------------

def test_location_and_bin_reports_match_stock_when_bins_are_shared_defaults(svc, sample):
    """v1.0 bug: every level was repeated once per product whose default bin was the same bin."""
    units = {u.code: u for u in svc.reference.units()}
    for i in range(5):
        svc.products.save(None, {"internal_code": f"DEF-{i}", "name": "Uses B04 as default",
                                 "unit_id": units["EA"].id, "default_bin_id": sample.bin1.id})
    sample.receive(sample.mdf, 10, sample.bin1, "R1")
    sample.receive(sample.edge, 4, sample.bin1, "R2")
    true_total = D("14")
    by_bin = svc.reports.run("stock_by_bin", ReportFilters())
    by_loc = svc.reports.run("stock_by_location", ReportFilters())
    current = svc.reports.run("current_stock", ReportFilters())
    assert len(by_bin.rows) == 2
    assert sum(r[4] for r in by_bin.rows) == true_total
    assert sum(r[3] for r in by_loc.rows) == true_total
    assert sum(r[5] for r in current.rows) == true_total
    assert by_bin.totals()[-1] == by_loc.totals()[-1] == current.totals()[9]


def test_reversed_documents_do_not_count_in_report_totals(svc, sample):
    dn = sample.receive(sample.mdf, 10, cost="10")
    sample.receive(sample.mdf, 5, cost="10", number="KEEP")
    svc.goods_in.reverse(dn.id, "wrong supplier")
    gi_report = svc.reports.run("goods_in", ReportFilters())
    assert len(gi_report.rows) == 2 and gi_report.totals()[9] == D("50.00")
    gi = issue(svc, sample, IssueLine(sample.mdf.id, "2"))
    issue(svc, sample, IssueLine(sample.mdf.id, "1"))
    svc.goods_out.reverse(gi.id, "mistake")
    go_report = svc.reports.run("goods_out", ReportFilters())
    assert go_report.totals()[9] == D("10.00")
    sup = svc.reports.run("supplier_deliveries", ReportFilters())
    assert sup.rows[0][1:4] == [1, 1, D("50.00")]


def test_reports_are_not_truncated(svc, sample, monkeypatch):
    from wms.services import stock_ops
    seen = {}
    real = stock_ops.StockOpsService.movements

    def spy(self, *a, **k):
        seen.setdefault("limits", []).append(k.get("limit"))
        return real(self, *a, **k)

    monkeypatch.setattr(stock_ops.StockOpsService, "movements", spy)
    svc.reports.run("movements", ReportFilters())
    svc.reports.run("stock_by_site", ReportFilters())
    assert seen["limits"] == [None, None]


# --- 18. stocktake --------------------------------------------------------------------------

def test_stocktake_full_workflow(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1, "C1")
    sample.receive(sample.edge, 25.5, sample.bin2, "C2")
    login_as(svc, "OPERATIVE", "counter")
    count = svc.stocktake.create(sample.shelf.id, "Shelf S02 monthly count")
    assert count.status == "OPEN" and len(count.lines) == 2
    line = {l.product.internal_code: l for l in count.lines}
    with pytest.raises(ValidationError, match="not been counted"):
        svc.stocktake.submit(count.id)
    svc.stocktake.record_counts(count.id, [CountEntry(line["MDF-18"].id, "9", "1 sheet chipped, binned"),
                                           CountEntry(line["EDGE-W22"].id, "25.5")])
    assert total(svc, sample.mdf) == D("10")  # counting never changes stock
    svc.stocktake.submit(count.id)
    with pytest.raises(PermissionDeniedError):  # operatives count, managers approve
        svc.stocktake.approve(count.id)
    svc.auth.login("admin", "admin")
    approved = svc.stocktake.approve(count.id, "Checked")
    assert approved.status == "APPROVED"
    assert total(svc, sample.mdf) == D("9") and total(svc, sample.edge) == D("25.5")
    adj = svc.stock.adjustments()
    assert len(adj) == 1 and adj[0].reason_code == "STOCK_COUNT" and count.count_number in adj[0].reason
    lines = {l.product.internal_code: l for l in svc.stocktake.get(count.id).lines}
    assert lines["MDF-18"].adjustment_id == adj[0].id and lines["EDGE-W22"].adjustment_id is None
    with pytest.raises(ValidationError):
        svc.stocktake.record_counts(count.id, [CountEntry(lines["MDF-18"].id, "1")])
    actions = {a.action for a in svc.audit.search()}
    assert {"STOCK_COUNT_STARTED", "STOCK_COUNT_SUBMITTED", "STOCK_COUNT_APPROVED", "STOCK_ADJUSTED"} <= actions
    assert_consistent(svc)


def test_stocktake_blocks_approval_when_stock_moved_during_count(svc, sample):
    sample.receive(sample.mdf, 10)
    count = svc.stocktake.create(None)
    line = count.lines[0]
    svc.stocktake.record_counts(count.id, [CountEntry(line.id, "10")])
    svc.stocktake.submit(count.id)
    issue(svc, sample, IssueLine(sample.mdf.id, "3"))  # someone issues while the count waits for approval
    with pytest.raises(ValidationError, match="moved"):
        svc.stocktake.approve(count.id)
    assert total(svc, sample.mdf) == D("7")  # the Goods Out was not undone
    assert [l.id for l in svc.stocktake.changed_since_count(count.id)] == [line.id]
    assert svc.stocktake.refresh_system_qty(count.id) == 1
    c = svc.stocktake.get(count.id)
    assert c.status == "OPEN" and c.lines[0].system_qty == D("7") and c.lines[0].counted_qty is None
    svc.stocktake.record_counts(count.id, [CountEntry(line.id, "6")])
    svc.stocktake.submit(count.id)
    svc.stocktake.approve(count.id)
    assert total(svc, sample.mdf) == D("6")
    assert_consistent(svc)


def test_stocktake_cancel_add_line_and_sheet(svc, sample):
    sample.receive(sample.mdf, 4)
    count = svc.stocktake.create(None, include_zero_bins=True)
    codes = {(l.product.internal_code, l.bin.code) for l in count.lines}
    assert ("MDF-18", "B04") in codes and ("EDGE-W22", "B05") in codes  # empty default bin listed to confirm
    count = svc.stocktake.add_line(count.id, sample.cabinet.id, sample.bin2.id)  # found, not expected
    with pytest.raises(ValidationError):
        svc.stocktake.add_line(count.id, sample.cabinet.id, sample.bin2.id)
    sheet = svc.stocktake.count_sheet(count.id)
    assert "System Qty" not in [c.title for c in sheet.columns]  # blind count sheet
    svc.stocktake.cancel(count.id, "Started by mistake")
    with pytest.raises(ValidationError):
        svc.stocktake.submit(count.id)
    with pytest.raises(Exception, match="cannot be changed"):
        with svc.ctx.db.session() as s:
            s.execute(text("UPDATE stock_counts SET status = 'OPEN'"))
    assert total(svc, sample.mdf) == D("4")


# --- edge cases -----------------------------------------------------------------------------

def test_override_uses_available_stock_first(svc, sample):
    sample.receive(sample.edge, 5, sample.bin1, "O1")  # not the default bin
    gi = issue(svc, sample, IssueLine(sample.edge.id, "8"), override_reason="sheets are on site already")
    assert sorted((l.bin.code, l.quantity) for l in gi.lines) == [("B04", D("5")), ("B05", D("3"))]
    assert bins_of(svc, sample.edge) == {"B05": D("-3")}  # only the true shortfall goes negative
    assert_consistent(svc)


def test_archived_or_closed_targets_rejected(svc, sample):
    sample.receive(sample.mdf, 5)
    svc.sites.save(sample.site.id, {"project_number": "P-1001", "name": "Riverside", "status": "CANCELLED"})
    with pytest.raises(ValidationError):
        issue(svc, sample, IssueLine(sample.mdf.id, "1"))
    with pytest.raises(ValidationError):
        svc.goods_out.post({"issue_date": TODAY, "site_id": 999999}, [IssueLine(sample.mdf.id, "1")])
    with pytest.raises(ValidationError):
        svc.goods_in.post({"dn_number": "N", "supplier_id": None, "delivery_date": TODAY},
                          [ReceiptLine(sample.mdf.id, "1", sample.bin1.id)])
    with pytest.raises(ValidationError):  # issuing from a quarantine bin
        svc.goods_out.post({"issue_date": TODAY, "site_id": sample.site.id},
                           [IssueLine(sample.mdf.id, "1", sample.quarantine.id)])


def test_number_parsing_limits():
    assert to_qty("1,250.5") == D("1250.500")
    assert to_qty("£12.50") == D("12.500")
    for bad in ("1e400", "1000000000", "-1e12", "1e-400x"):
        with pytest.raises(ValidationError):
            to_qty(bad)


def test_settings_changes_are_audited_and_need_permission(svc):
    svc.settings.save_general({"company_name": "  Oak & Co Ltd  ", "backups_to_keep": 1})
    assert svc.ctx.config.company_name == "Oak & Co Ltd" and svc.ctx.config.backups_to_keep == 3
    assert any(a.action == "SETTINGS_CHANGED" for a in svc.audit.search())
    login_as(svc, "MANAGER", "boss")
    with pytest.raises(PermissionDeniedError):
        svc.settings.save_general({"company_name": "x"})


def test_audit_trail_records_old_and_new_values(svc, sample):
    p = svc.products.get(sample.mdf.id)
    svc.products.save(p.id, {"item_type": "RAW_MATERIAL", "internal_code": "MDF-18", "name": "MDF 18mm FSC",
                             "unit_id": p.unit_id, "min_stock": "12"})
    entry = next(a for a in svc.audit.search() if a.action == "PRODUCT_UPDATED")
    assert '"min_stock": "10"' in entry.old_values and '"min_stock": "12"' in entry.new_values
    assert entry.username == "admin" and entry.entity_id == p.id and entry.workstation


def test_sequence_numbers_survive_failed_posts(svc, sample):
    sample.receive(sample.mdf, 1)
    with pytest.raises(InsufficientStockError):
        issue(svc, sample, IssueLine(sample.mdf.id, "5"))
    with svc.ctx.db.read_session() as s:
        assert s.get(NumberSequence, "GOODS_ISSUE").next_value == 1


def test_levels_never_written_without_a_movement(svc, sample):
    sample.receive(sample.mdf, 10)
    issue(svc, sample, IssueLine(sample.mdf.id, "4"))
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "1")
    with svc.ctx.db.read_session() as s:
        levels = {(l.product_id, l.bin_id): l.quantity for l in s.scalars(select(StockLevel))}
    assert levels == {(sample.mdf.id, sample.bin1.id): D("5"), (sample.mdf.id, sample.bin2.id): D("1")}
    assert ledger_total(svc, sample.mdf) == D("6")


@pytest.mark.skipif(os.environ.get("WMS_PERF") != "1", reason="set WMS_PERF=1 to run the volume test")
def test_volume_10k_products_100k_movements(tmp_path):
    from tools.generate_test_data import generate
    from tools.perf_check import run
    info = generate(tmp_path / "perf")
    assert info["movements"] >= 100_000
    res = run(tmp_path / "perf")
    assert res["product list (all active)"] < 2000
    assert res["report: Stock by Bin"] < 10000
    shutil.rmtree(tmp_path / "perf", ignore_errors=True)


def test_delivery_note_header_edit_is_audited_and_lines_stay_fixed(svc, sample):
    dn = sample.receive(sample.mdf, 4, number="H1")
    svc.goods_in.update_header(dn.id, "PO-991", "Driver left 2 pallets")
    entry = next(a for a in svc.audit.search() if a.action == "DELIVERY_NOTE_EDITED")
    assert "PO-991" in entry.new_values
    assert svc.goods_in.get(dn.id).lines[0].quantity == D("4")
    svc.goods_in.reverse(dn.id, "wrong")
    with pytest.raises(ValidationError):
        svc.goods_in.update_header(dn.id, "x", "y")
