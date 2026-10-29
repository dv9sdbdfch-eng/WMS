"""Phase 1, Stage 4 - inventory control: stock states, reservations, reorder, stocktake."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from wms.core.enums import StockStatus
from wms.core.exceptions import AlreadyPostedError, PermissionDeniedError, ValidationError
from wms.services.goods_out import IssueLine
from wms.services.purchasing import POLineInput
from wms.services.stocktake import CountEntry

D = Decimal
TODAY = date.today()


def states(svc, product):
    r = svc.products.row(product.id)
    return r.on_hand, r.reserved, r.available, r.incoming


def issue(svc, site, product, qty):
    return svc.goods_out.post({"site_id": site.id, "issue_date": TODAY}, [IssueLine(product.id, str(qty))])


def critical(svc):
    return [str(i) for i in svc.ledger.critical_issues()]


@pytest.fixture
def second_site(svc):
    return svc.sites.save(None, {"project_number": "P-2002", "name": "Surrey County Hall", "status": "ACTIVE"})


def test_stock_states_example_from_the_brief(svc, sample):
    """On Hand 120, Reserved 30, Available 90, Incoming 50."""
    mdf = sample.mdf
    sample.receive(mdf, 120)
    res = svc.reservations.reserve({"product_id": mdf.id, "site_id": sample.site.id, "quantity": "30",
                                    "required_by": TODAY + timedelta(days=7)})
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id}, [POLineInput(mdf.id, "50")])
    svc.purchasing.mark_ordered(po.id)
    assert states(svc, mdf) == (D("120"), D("30"), D("90"), D("50"))
    assert res.reservation.number == "RES-000001" and res.remaining == D("30")
    # a reservation is not a movement: the ledger and the bins are unchanged
    assert svc.ledger.product_balance(mdf.id).on_hand == D("120")
    assert not [m for m in svc.products.movements(mdf.id) if m.movement_type == "GOODS_OUT"]
    listed = {r.product.id: r for r in svc.products.list()}[mdf.id]
    assert (listed.reserved, listed.available, listed.incoming) == (D("30"), D("90"), D("50"))
    assert critical(svc) == []


def test_over_reservation_is_refused(svc, sample, second_site):
    sample.receive(sample.mdf, 100)
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "70"})
    with pytest.raises(ValidationError, match="Only 30 SHEET .* available to reserve"):
        svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "31"})
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "30"})
    assert states(svc, sample.mdf)[1:3] == (D("100"), D("0"))
    with pytest.raises(ValidationError, match="Only 0"):
        svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "1"})


def test_quarantine_stock_cannot_be_reserved(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    sample.receive(sample.mdf, 5, sample.quarantine, "DN-Q")
    assert states(svc, sample.mdf) == (D("15"), D("0"), D("10"), D("0"))
    with pytest.raises(ValidationError, match="Only 10"):
        svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "11"})


def test_goods_out_fulfils_the_reservation(svc, sample):
    sample.receive(sample.mdf, 100)
    res = svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "30"})
    gi1 = issue(svc, sample.site, sample.mdf, 10)
    v = svc.reservations.get(res.reservation.id)
    assert (v.issued, v.remaining, v.reservation.status) == (D("10"), D("20"), "ACTIVE")
    assert states(svc, sample.mdf)[:3] == (D("90"), D("20"), D("70"))  # available unchanged by issuing reserved stock
    gi2 = issue(svc, sample.site, sample.mdf, 25)  # 20 reserved + 5 unreserved
    v = svc.reservations.get(res.reservation.id)
    assert (v.issued, v.remaining, v.reservation.status) == (D("35"), D("0"), "FULFILLED")
    assert states(svc, sample.mdf)[:3] == (D("65"), D("0"), D("65"))
    assert {l.reservation_id for l in gi1.lines} == {res.reservation.id}
    # reversing a Goods Out puts the quantity back on reserve
    svc.goods_out.reverse(gi2.id, "Posted to the wrong project")
    v = svc.reservations.get(res.reservation.id)
    assert (v.remaining, v.reservation.status) == (D("20"), "ACTIVE")
    assert states(svc, sample.mdf)[:3] == (D("90"), D("20"), D("70"))
    assert critical(svc) == []
    warnings = [i.check for i in svc.ledger.integrity_check()]
    assert "Reservation status" not in warnings


def test_issuing_reserved_stock_to_another_project_warns(svc, sample, second_site):
    sample.receive(sample.mdf, 40)
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "30"})
    rs = svc.reservations
    assert rs.conflicts(second_site.id, [(sample.mdf.id, D("10"))]) == []
    warn = rs.conflicts(second_site.id, [(sample.mdf.id, D("15"))])
    assert warn == ["MDF-18: issuing 15 uses 5 SHEET reserved for P-1001 - Riverside Apartments."]
    assert rs.conflicts(sample.site.id, [(sample.mdf.id, D("40"))]) == []  # its own reservation
    issue(svc, second_site, sample.mdf, 15)  # allowed after the warning: the material physically left
    assert states(svc, sample.mdf)[:3] == (D("25"), D("30"), D("-5"))
    assert "Over-reserved" in {i.check for i in svc.ledger.integrity_check()}
    assert any("more reserved for projects than is in stock" in a.text for a in svc.dashboard.load().alerts)


def test_change_release_and_project_closing(svc, sample, second_site):
    sample.receive(sample.mdf, 50)
    rs = svc.reservations
    a = rs.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "20"}).reservation
    issue(svc, sample.site, sample.mdf, 5)
    with pytest.raises(ValidationError, match="already been issued"):
        rs.update(a.id, "4")
    with pytest.raises(ValidationError, match="available to reserve"):
        rs.update(a.id, "51")
    rs.update(a.id, "10", TODAY, "Phase 1 only")
    assert rs.get(a.id).remaining == D("5")
    released = rs.release(a.id, "Customer changed the board")
    assert released.reservation.status == "RELEASED" and released.remaining == D("0")
    with pytest.raises(ValidationError, match="can no longer be changed"):
        rs.release(a.id, "again")
    b = rs.reserve({"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "8"}).reservation
    svc.sites.set_status(second_site.id, "COMPLETED")
    assert rs.get(b.id).reservation.status == "RELEASED"
    assert states(svc, sample.mdf)[1] == D("0")
    with pytest.raises(ValidationError, match="closed"):
        rs.reserve({"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "1"})
    actions = {e.action for e in svc.audit.search(entity_type="RESERVATION")}
    assert {"RESERVATION_CREATED", "RESERVATION_CHANGED", "RESERVATION_RELEASED"} <= actions
    assert [v.reservation.number for v in rs.list(text="RES-000002")] == ["RES-000002"]


def test_duplicate_submission_and_permissions(svc, sample):
    sample.receive(sample.mdf, 10)
    data = {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "2", "client_token": "r-1"}
    svc.reservations.reserve(data)
    with pytest.raises(AlreadyPostedError):
        svc.reservations.reserve(data)
    assert states(svc, sample.mdf)[1] == D("2")
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op", "full_name": "Op", "role_id": roles["OPERATIVE"].id,
                           "must_change_password": False}, "secret1")
    svc.auth.login("op", "secret1")
    with pytest.raises(PermissionDeniedError):
        svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "1"})
    assert len(svc.reservations.list()) == 1  # but can see them


def test_reorder_level_and_suggested_order(svc, sample):
    mdf = svc.products.save(sample.mdf.id, {**_product_data(sample.mdf), "min_stock": "10", "reorder_level": "25",
                                            "reorder_qty": "40"})
    assert mdf.reorder_level == D("25")
    sample.receive(mdf, 30)
    row = svc.products.row(mdf.id)
    assert row.status is StockStatus.OK
    svc.reservations.reserve({"product_id": mdf.id, "site_id": sample.site.id, "quantity": "6"})
    row = svc.products.row(mdf.id)
    assert row.available == D("24") and row.status is StockStatus.REORDER
    assert row.shortfall == D("41")  # 25 + 40 - 24 available
    assert [r.product.id for r in svc.products.low_stock()] == [mdf.id]
    # draft orders, grouped by preferred supplier; never placed automatically
    edge = svc.products.save(sample.edge.id, {**_product_data(sample.edge), "min_stock": "100"})  # no supplier
    drafts, skipped = svc.purchasing.draft_from_reorder([mdf.id, edge.id])
    assert [(d.status, d.supplier_id, [(l.product_id, l.quantity) for l in d.lines]) for d in drafts] == [
        ("DRAFT", sample.supplier.id, [(mdf.id, D("41"))])]
    assert skipped == ["EDGE-W22: no supplier set on the product"]
    again, skipped = svc.purchasing.draft_from_reorder([mdf.id])
    assert again == [] and skipped == [f"MDF-18: already on draft order {drafts[0].po_number}"]
    svc.purchasing.mark_ordered(drafts[0].id)
    row = svc.products.row(mdf.id)
    assert row.incoming == D("41") and row.shortfall == D("0") and row.is_low  # still low, but on order
    # saving the product from an older screen (no reorder_level field) keeps the level
    data = _product_data(svc.products.get(mdf.id))
    data.pop("reorder_level")
    assert svc.products.save(mdf.id, data).reorder_level == D("25")
    row = svc.products.row(mdf.id)
    svc.products.save(mdf.id, {**_product_data(svc.products.get(mdf.id)), "min_stock": "30"})
    assert svc.products.row(mdf.id).status is StockStatus.LOW


def _product_data(p) -> dict:
    return {"item_type": p.item_type, "internal_code": p.internal_code, "name": p.name, "unit_id": p.unit_id,
            "supplier_id": p.supplier_id, "default_bin_id": p.default_bin_id, "min_stock": p.min_stock,
            "reorder_qty": p.reorder_qty, "reorder_level": p.reorder_level, "unit_cost": p.unit_cost}


def test_stocktake_example_from_the_brief(svc, sample):
    """H3311 50 -> 48 (-2), U708 20 -> 20 (0), W100 5 -> 7 (+2); stock changes only on approval."""
    units = {u.code: u for u in svc.reference.units()}
    prods = {code: svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": code, "name": code,
                                            "unit_id": units["SHEET"].id, "default_bin_id": sample.bin1.id})
             for code in ("H3311", "U708", "W100")}
    for code, qty in (("H3311", 50), ("U708", 20), ("W100", 5)):
        sample.receive(prods[code], qty, sample.bin1, f"DN-{code}")
    count = svc.stocktake.create(sample.zone.id, "Zone A")  # select warehouse / zone / bin
    lines = {l.product.internal_code: l for l in count.lines}
    assert set(lines) == {"H3311", "U708", "W100"}
    counted = {"H3311": "48", "U708": "20", "W100": "7"}
    svc.stocktake.record_counts(count.id, [CountEntry(lines[c].id, q) for c, q in counted.items()])
    assert svc.ledger.product_balance(prods["H3311"].id).on_hand == D("50")  # entering a count changes nothing
    svc.stocktake.submit(count.id)
    review = {l.product.internal_code: (l.system_qty, l.counted_qty, l.difference)
              for l in svc.stocktake.get(count.id).lines}
    assert review == {"H3311": (D("50"), D("48"), D("-2")), "U708": (D("20"), D("20"), D("0")),
                      "W100": (D("5"), D("7"), D("2"))}
    svc.stocktake.approve(count.id, "Checked twice")
    on_hand = {c: svc.ledger.product_balance(p.id).on_hand for c, p in prods.items()}
    assert on_hand == {"H3311": D("48"), "U708": D("20"), "W100": D("7")}
    moves = [m for p in prods.values() for m in svc.products.movements(p.id)
             if m.movement_type == "STOCKTAKE_ADJUSTMENT"]
    assert sorted(m.quantity for m in moves) == [D("2"), D("2")]  # only the differences
    kept = svc.stocktake.get(count.id)  # the original count record is preserved
    assert kept.status == "APPROVED" and len(kept.lines) == 3
    assert {l.product.internal_code: l.counted_qty for l in kept.lines} == {k: D(v) for k, v in counted.items()}
    assert critical(svc) == []


def test_tampered_reservation_link_is_critical(svc, sample, second_site):
    from sqlalchemy import text
    sample.receive(sample.mdf, 20)
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "5"})
    issue(svc, sample.site, sample.mdf, 3)
    with svc.ctx.db.session() as s:
        s.execute(text("UPDATE goods_issue_lines SET reservation_id = (SELECT MIN(id) FROM stock_reservations)"))
    assert "Reservation link" in {i.check for i in svc.ledger.critical_issues()}
