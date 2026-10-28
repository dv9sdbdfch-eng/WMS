"""End-to-end stock scenarios through the service layer."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from wms.core.exceptions import (
    DuplicateError,
    InsufficientStockError,
    PermissionDeniedError,
    ValidationError,
)
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.production import MaterialInput
from wms.services.stock_ops import ReturnLineInput

D = Decimal


def on_hand(svc, product):
    row = next(r for r in svc.products.list(include_inactive=True) if r.product.id == product.id)
    return row.on_hand, row.available


def assert_consistent(svc):
    rep = svc.backup.integrity_check()
    assert rep.database_ok, rep.database_messages
    assert rep.foreign_key_problems == 0
    assert rep.stock_discrepancies == []


def test_seed_and_codes_normalised(sample):
    assert sample.mdf.internal_code == "MDF-18"
    assert sample.bin1.full_code == "MAIN / A / R03 / S02 / B04"


def test_goods_in_multi_line_increases_stock(svc, sample):
    dn = svc.goods_in.post(
        {"dn_number": "44851", "supplier_id": sample.supplier.id, "delivery_date": date.today(), "po_reference": "PO77"},
        [ReceiptLine(sample.mdf.id, "40", sample.bin1.id, "23.90"),
         ReceiptLine(sample.edge.id, "250.5", None, None),
         ReceiptLine(sample.mdf.id, "5", sample.bin2.id, "23.90")],
    )
    assert len(dn.lines) == 3
    assert on_hand(svc, sample.mdf) == (D("45"), D("45"))
    assert on_hand(svc, sample.edge)[0] == D("250.5")
    assert svc.products.get(sample.mdf.id).unit_cost == D("23.90")  # last purchase price
    assert len(svc.goods_in.movements(dn.id)) == 3
    assert_consistent(svc)


def test_duplicate_delivery_note_blocked(svc, sample):
    sample.receive(sample.mdf, 10, number="DN 500")
    with pytest.raises(DuplicateError):
        sample.receive(sample.mdf, 10, number="dn  500")  # same number, different spacing/case
    assert on_hand(svc, sample.mdf)[0] == D("10")


def test_whole_units_enforced(svc, sample):
    with pytest.raises(ValidationError):
        sample.receive(sample.mdf, "2.5")
    assert on_hand(svc, sample.mdf)[0] == D("0")


def test_future_date_rejected(svc, sample):
    with pytest.raises(ValidationError):
        svc.goods_in.post({"dn_number": "X", "supplier_id": sample.supplier.id,
                           "delivery_date": date.today() + timedelta(days=1)},
                          [ReceiptLine(sample.mdf.id, "1", sample.bin1.id)])


def test_failed_line_rolls_back_whole_document(svc, sample):
    with pytest.raises(ValidationError):
        svc.goods_in.post({"dn_number": "RB1", "supplier_id": sample.supplier.id, "delivery_date": date.today()},
                          [ReceiptLine(sample.mdf.id, "5", sample.bin1.id), ReceiptLine(sample.mdf.id, "0", sample.bin1.id)])
    assert on_hand(svc, sample.mdf)[0] == D("0")
    assert svc.goods_in.search() == []


def test_goods_out_decreases_and_blocks_negative(svc, sample):
    sample.receive(sample.mdf, 20)
    gi = svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id, "requested_by": "Dave (fitter)",
                             "reference": "JOB-88"}, [IssueLine(sample.mdf.id, "8")])
    assert gi.issue_number == "GO-000001"
    assert on_hand(svc, sample.mdf)[0] == D("12")
    with pytest.raises(InsufficientStockError):
        svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "13")])
    assert on_hand(svc, sample.mdf)[0] == D("12")
    assert_consistent(svc)


def test_negative_override_requires_permission_and_is_audited(svc, sample):
    sample.receive(sample.mdf, 2)
    svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "5")],
                       override_reason="Count was wrong, sheets are physically here")
    assert on_hand(svc, sample.mdf)[0] == D("-3")
    assert any(a.action == "NEGATIVE_STOCK_OVERRIDE" for a in svc.audit.search())
    # A viewer cannot override
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op", "full_name": "Op", "role_id": roles["OPERATIVE"].id}, "secret1")
    svc.auth.login("op", "secret1")
    with pytest.raises(InsufficientStockError):
        svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "1")],
                           override_reason="please")
    assert_consistent(svc)


def test_auto_allocation_spans_bins_and_skips_quarantine(svc, sample):
    sample.receive(sample.edge, 30, sample.bin1, "A")
    sample.receive(sample.edge, 50, sample.bin2, "B")
    sample.receive(sample.edge, 100, sample.quarantine, "C")
    assert on_hand(svc, sample.edge) == (D("180"), D("80"))
    gi = svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.edge.id, "70")])
    # default bin (bin2) first, then bin1
    assert [(l.bin.code, l.quantity) for l in gi.lines] == [("B05", D("50")), ("B04", D("20"))]
    with pytest.raises(InsufficientStockError):
        svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.edge.id, "11")])


def test_closed_site_blocked(svc, sample):
    sample.receive(sample.mdf, 5)
    svc.sites.save(sample.site.id, {"project_number": "P-1001", "name": "Riverside Apartments", "status": "COMPLETED"})
    with pytest.raises(ValidationError):
        svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "1")])


def test_transfer_keeps_total(svc, sample):
    sample.receive(sample.mdf, 10)
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "4")
    assert on_hand(svc, sample.mdf)[0] == D("10")
    by_bin = {l.bin.code: l.quantity for l in svc.products.stock_by_bin(sample.mdf.id)}
    assert by_bin == {"B04": D("6"), "B05": D("4")}
    with pytest.raises(InsufficientStockError):
        svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "7")
    with pytest.raises(ValidationError):
        svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin1.id, "1")
    assert_consistent(svc)


def test_adjustment_records_previous_new_difference(svc, sample):
    sample.receive(sample.mdf, 10)
    adj = svc.stock.adjust(sample.mdf.id, sample.bin1.id, "7", "STOCK_COUNT", "Monthly count")
    assert (adj.previous_qty, adj.new_qty, adj.difference) == (D("10"), D("7"), D("-3"))
    adj2 = svc.stock.adjust(sample.mdf.id, sample.bin1.id, "9", "FOUND", "Found behind rack")
    assert adj2.difference == D("2")
    assert on_hand(svc, sample.mdf)[0] == D("9")
    with pytest.raises(ValidationError):
        svc.stock.adjust(sample.mdf.id, sample.bin1.id, "9", "OTHER", "no change")
    with pytest.raises(ValidationError):
        svc.stock.adjust(sample.mdf.id, sample.bin1.id, "5", "OTHER", "")  # reason required
    assert_consistent(svc)


def test_returns_from_site_and_to_supplier(svc, sample):
    sample.receive(sample.mdf, 10)
    svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "6")])
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": date.today(), "site_id": sample.site.id},
                          [ReturnLineInput(sample.mdf.id, "2", sample.bin1.id, "USABLE"),
                           ReturnLineInput(sample.mdf.id, "1", sample.quarantine.id, "DAMAGED")])
    assert on_hand(svc, sample.mdf) == (D("7"), D("6"))
    summary = svc.sites.material_summary(sample.site.id)
    assert (summary[0].issued, summary[0].returned, summary[0].net) == (D("6"), D("3"), D("3"))
    svc.stock.post_return({"return_type": "TO_SUPPLIER", "return_date": date.today(),
                           "supplier_id": sample.supplier.id, "reason": "Damaged on arrival"},
                          [ReturnLineInput(sample.mdf.id, "1", sample.quarantine.id, "DAMAGED")])
    assert on_hand(svc, sample.mdf) == (D("6"), D("6"))
    assert_consistent(svc)


def test_reverse_delivery_note(svc, sample):
    dn = sample.receive(sample.mdf, 10)
    svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "4")])
    with pytest.raises(InsufficientStockError):
        svc.goods_in.reverse(dn.id, "Wrong supplier")
    dn = svc.goods_in.reverse(dn.id, "Wrong supplier", override_reason="Will correct with new DN")
    assert dn.status == "REVERSED"
    assert on_hand(svc, sample.mdf)[0] == D("-4")
    with pytest.raises(ValidationError):
        svc.goods_in.reverse(dn.id, "again")
    # Same DN number can be re-entered after reversal
    sample.receive(sample.mdf, 10)
    assert on_hand(svc, sample.mdf)[0] == D("6")
    assert_consistent(svc)


def test_reverse_goods_out(svc, sample):
    sample.receive(sample.mdf, 10)
    gi = svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id}, [IssueLine(sample.mdf.id, "4")])
    svc.goods_out.reverse(gi.id, "Posted to wrong site")
    assert on_hand(svc, sample.mdf)[0] == D("10")
    assert svc.sites.material_summary(sample.site.id) == []


def test_production_consumes_and_outputs(svc, sample):
    sample.receive(sample.mdf, 10)
    sample.receive(sample.edge, 100, number="E1")
    order = svc.production.post({"finished_product_id": sample.cabinet.id, "quantity": "2",
                                 "production_date": date.today(), "site_id": sample.site.id},
                                [MaterialInput(sample.mdf.id, "3"), MaterialInput(sample.edge.id, "12.4")])
    assert order.order_number == "PRD-000001"
    assert on_hand(svc, sample.mdf)[0] == D("7")
    assert on_hand(svc, sample.edge)[0] == D("87.6")
    assert on_hand(svc, sample.cabinet)[0] == D("2")
    svc.production.reverse(order.id, "Recorded twice")
    assert on_hand(svc, sample.cabinet)[0] == D("0")
    assert on_hand(svc, sample.mdf)[0] == D("10")
    assert_consistent(svc)


def test_ledger_is_append_only(svc, sample):
    sample.receive(sample.mdf, 1)
    with pytest.raises(Exception, match="append-only"):
        with svc.ctx.db.session() as s:
            s.execute(text("DELETE FROM stock_movements"))
    with pytest.raises(Exception, match="append-only"):
        with svc.ctx.db.session() as s:
            s.execute(text("UPDATE audit_log SET username='x'"))


def test_discrepancy_detected_and_rebuilt(svc, sample):
    sample.receive(sample.mdf, 10)
    with svc.ctx.db.session() as s:
        s.execute(text("UPDATE stock_levels SET quantity = 99000"))  # simulate tampering
    rep = svc.backup.integrity_check()
    assert len(rep.stock_discrepancies) == 1
    assert svc.backup.rebuild_stock_levels() == 1
    assert_consistent(svc)
    assert on_hand(svc, sample.mdf)[0] == D("10")


def test_viewer_cannot_post(svc, sample):
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "viewer", "full_name": "V", "role_id": roles["VIEWER"].id}, "secret1")
    svc.auth.login("viewer", "secret1")
    with pytest.raises(PermissionDeniedError):
        sample.receive(sample.mdf, 1)
    with pytest.raises(PermissionDeniedError):
        svc.products.save(None, {"internal_code": "X", "name": "X", "unit_id": sample.mdf.unit_id})


def test_unit_change_blocked_after_movements(svc, sample):
    sample.receive(sample.mdf, 1)
    units = {u.code: u for u in svc.reference.units()}
    data = {"internal_code": "MDF-18", "name": "MDF", "unit_id": units["M2"].id, "item_type": "RAW_MATERIAL"}
    with pytest.raises(ValidationError):
        svc.products.save(sample.mdf.id, data)


def test_cannot_deactivate_bin_or_product_with_stock(svc, sample):
    sample.receive(sample.mdf, 1)
    with pytest.raises(ValidationError):
        svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B04", "is_active": False})
    p = svc.products.get(sample.mdf.id)
    data = {k: getattr(p, k) for k in ("internal_code", "name", "unit_id", "item_type", "default_bin_id")}
    data["is_active"] = False
    with pytest.raises(ValidationError):
        svc.products.save(p.id, data)


def test_low_stock_and_search(svc, sample):
    sample.receive(sample.mdf, 10)
    low = svc.products.low_stock()
    assert [r.product.internal_code for r in low] == ["MDF-18"]
    assert low[0].shortfall == D("0")  # at min level, no reorder qty set: target = min
    hits = svc.search.search("B04")
    assert any(h.kind == "Bin" for h in hits) and any(h.kind == "Product" for h in hits)
    assert any(h.kind == "Delivery Note" for h in svc.search.search("Egger"))
    assert [r.product.internal_code for r in svc.products.list(text="EG-MDF")] == ["MDF-18"]


def test_moving_location_updates_bin_paths(svc, sample):
    svc.locations.save_location(sample.zone.id, {"kind": "ZONE", "code": "Z1", "name": "Zone 1",
                                                 "parent_id": sample.main.id})
    codes = {b.full_code for b in svc.locations.bins()}
    assert "MAIN / Z1 / R03 / S02 / B04" in codes


def test_last_admin_protected(svc):
    admin = next(u for u in svc.users.list_users() if u.username == "admin")
    with pytest.raises(ValidationError):
        svc.users.update_user(admin.id, {"is_active": False})


def test_optimistic_locking(svc, sample):
    from wms.core.exceptions import ConcurrencyError
    p = svc.products.get(sample.mdf.id)
    data = {k: getattr(p, k) for k in ("internal_code", "name", "unit_id", "item_type")}
    svc.products.save(p.id, {**data, "name": "Changed elsewhere"}, expected_version=p.version)
    with pytest.raises(ConcurrencyError):
        svc.products.save(p.id, {**data, "name": "Stale edit"}, expected_version=p.version)
