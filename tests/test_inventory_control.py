"""Phase 1, Stage 4 - inventory control: stock states, reservations, reorder, stocktake."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import hand_reserve

from wms.core.enums import StockStatus
from wms.core.exceptions import PermissionDeniedError, ValidationError
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
    res = hand_reserve(svc, {"product_id": mdf.id, "site_id": sample.site.id, "quantity": "30",
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


def test_quarantine_stock_is_not_general_stock(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    sample.receive(sample.mdf, 5, sample.quarantine, "DN-Q")
    assert states(svc, sample.mdf) == (D("15"), D("0"), D("10"), D("0"))


def test_goods_out_fulfils_the_reservation(svc, sample):
    sample.receive(sample.mdf, 100)
    res = hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "30"})
    gi1 = issue(svc, sample.site, sample.mdf, 10)
    v = svc.reservations.get(res.reservation.id)
    assert (v.issued, v.remaining, v.reservation.status) == (D("10"), D("20"), "ACTIVE")
    assert states(svc, sample.mdf)[:3] == (D("90"), D("20"), D("70"))  # available unchanged by issuing reserved stock
    gi2 = issue(svc, sample.site, sample.mdf, 25)  # 20 reserved + 5 unreserved
    v = svc.reservations.get(res.reservation.id)
    assert (v.issued, v.remaining, v.reservation.status) == (D("30"), D("0"), "FULFILLED")  # v1.26: only 20 of the 25 go to it
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


def test_another_projects_stock_needs_a_manager_and_a_reason(svc, sample, second_site):
    """v1.35 (Ihor 21.11.2026): project A's stock is not issued to project B unless a Store Manager
    allows it with a reason; the stock then moves off A with a PSR record in A's history."""
    sample.receive(sample.mdf, 40)
    a = hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "30"})
    rs = svc.reservations
    assert rs.conflicts(second_site.id, [(sample.mdf.id, D("10"))]) == []
    warn = rs.conflicts(second_site.id, [(sample.mdf.id, D("15"))])
    assert warn == ["MDF-18: issuing 15 uses 5 SHEET of the project stock of P-1001 - Riverside Apartments."]
    assert rs.conflicts(sample.site.id, [(sample.mdf.id, D("40"))]) == []  # its own stock
    with pytest.raises(ValidationError, match="belongs to other projects") as e:
        issue(svc, second_site, sample.mdf, 15)
    assert "MDF-18: 5 SHEET from other projects - you can use up to 10 SHEET without a manager" in str(e.value)
    assert states(svc, sample.mdf)[0] == D("40")  # nothing was posted
    gi = svc.goods_out.post({"site_id": second_site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "15")],
                            other_projects_reason="Urgent repair at County Hall")
    assert states(svc, sample.mdf)[:3] == (D("25"), D("25"), D("0"))  # A gave up 5: never more than is there
    assert rs.get(a.reservation.id).remaining == D("25")
    [psr] = rs.project_releases(sample.site.id)
    assert psr.quantity == D("5") and "County Hall" in psr.reason
    assert "PROJECT_STOCK_RETURNED" in {e.action for e in svc.audit.search(entity_type="SITE")}
    assert "PROJECT_STOCK_TAKEN" in {e.action for e in svc.audit.search(entity_type="GOODS_ISSUE")}
    assert "Project stock above on hand" not in {i.check for i in svc.ledger.integrity_check()}
    assert critical(svc) == []
    # reversed by mistake: the 15 sheets are back on the shelf and A gets its 5 back
    svc.goods_out.reverse(gi.id, "Wrong project")
    assert states(svc, sample.mdf)[:3] == (D("40"), D("30"), D("10"))
    assert {x.code: x.total for x in rs.project_stock(sample.site.id)} == {"MDF-18": D("30")}
    assert critical(svc) == []


def test_reversal_after_a_project_closed_leaves_its_stock_in_main(svc, sample, second_site):
    """A Goods Out reversed after the project was closed does not bring project stock back."""
    sample.receive(sample.mdf, 20)
    hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "8"})
    gi = issue(svc, second_site, sample.mdf, 3)
    svc.sites.set_status(second_site.id, "COMPLETED", "Done")
    assert svc.reservations.project_stock(second_site.id) == []
    svc.goods_out.reverse(gi.id, "Entered twice")
    assert svc.reservations.project_stock(second_site.id) == []
    assert states(svc, sample.mdf)[:3] == (D("20"), D("0"), D("20"))
    assert critical(svc) == []


def test_over_held_stock_counts_only_what_is_there(svc, sample, second_site):
    """Old hand reservations may hold more than is on the shelf: never take more than was asked."""
    sample.receive(sample.mdf, 10)
    hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "15"})
    from wms.services.reservations import from_other_projects
    with svc.reservations.db.read_session() as s:
        assert from_other_projects(s, second_site.id, {sample.mdf.id: D("4")}) == {sample.mdf.id: D("4")}


def test_only_a_manager_may_use_another_projects_stock(svc, sample, second_site):
    sample.receive(sample.mdf, 10)
    hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "10"})
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op", "full_name": "Op", "role_id": roles["OPERATIVE"].id,
                           "must_change_password": False}, "secret1")
    svc.auth.login("op", "secret1")
    with pytest.raises(PermissionDeniedError):
        svc.goods_out.post({"site_id": second_site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "2")],
                           other_projects_reason="Urgent")
    with pytest.raises(PermissionDeniedError):
        svc.reservations.return_to_main(sample.site.id, [(sample.mdf.id, "1")], "Not needed")
    assert len(svc.reservations.list()) == 1  # but can see it


def test_closing_a_project_returns_its_stock_with_a_record(svc, sample, second_site):
    sample.receive(sample.mdf, 50)
    rs = svc.reservations
    a = hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "8"}).reservation
    issue(svc, second_site, sample.mdf, 3)
    svc.sites.set_status(second_site.id, "COMPLETED", "Job finished, 5 sheets spare")
    v = rs.get(a.id)
    assert v.reservation.status == "FULFILLED" and v.remaining == 0
    assert states(svc, sample.mdf)[1] == D("0")
    [psr] = rs.project_releases(second_site.id)
    assert psr.quantity == D("5") and "Job finished" in psr.reason
    assert rs.project_stock(second_site.id) == []
    # closed again without stock left: nothing more to record
    svc.sites.set_status(second_site.id, "ACTIVE")
    svc.sites.set_status(second_site.id, "COMPLETED")
    assert len(rs.project_releases(second_site.id)) == 1
    assert [v.reservation.number for v in rs.list(text=a.number)] == [a.number]


def test_reorder_level_and_suggested_order(svc, sample):
    mdf = svc.products.save(sample.mdf.id, {**_product_data(sample.mdf), "min_stock": "10", "reorder_level": "25",
                                            "reorder_qty": "40"})
    assert mdf.reorder_level == D("25")
    sample.receive(mdf, 30)
    row = svc.products.row(mdf.id)
    assert row.status is StockStatus.OK
    hand_reserve(svc, {"product_id": mdf.id, "site_id": sample.site.id, "quantity": "6"})
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
    hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": second_site.id, "quantity": "5"})
    issue(svc, sample.site, sample.mdf, 3)
    with svc.ctx.db.session() as s:
        s.execute(text("UPDATE goods_issue_lines SET reservation_id = (SELECT MIN(id) FROM stock_reservations)"))
    assert "Reservation link" in {i.check for i in svc.ledger.critical_issues()}
