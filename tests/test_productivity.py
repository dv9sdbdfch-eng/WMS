"""Phase 1, Stage 5 - productivity: grouped global search, purchase orders in search, the extra
filters behind the ready-made views, and stock status judged on Available (reservations)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.purchasing import POLineInput
from wms.services.search import BUILTIN_VIEWS, GROUP_TITLES, SearchCriteria
from wms.services.stock_ops import ReturnLineInput

D = Decimal
TODAY = date.today()
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 200


def codes(result) -> set[str]:
    return {r.product.internal_code for r in result.rows}


def receive(svc, sample, product, qty, bin_, number):
    return svc.goods_in.post({"dn_number": number, "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                             [ReceiptLine(product.id, D(qty), bin_.id)])


def test_scan_of_a_product_code_answers_where_projects_deliveries_movements(svc, sample):
    """The brief's H3311 example: LOCATIONS B04 85 / B17 42, PROJECT HISTORY Surrey 30 / Project B 15,
    DELIVERY NOTES DN-1047 / DN-1098, MOVEMENTS count."""
    units = {u.code: u for u in svc.reference.units()}
    h = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "H3311", "name": "Egger Halifax",
                                 "unit_id": units["SHEET"].id, "supplier_id": sample.supplier.id})
    b17 = svc.locations.save_bin(None, {"location_id": sample.shelf.id, "code": "B17"})
    surrey = svc.sites.save(None, {"project_number": "P-2002", "name": "Surrey County Hall", "status": "ACTIVE"})
    receive(svc, sample, h, 115, sample.bin1, "DN-1047")
    receive(svc, sample, h, 57, b17, "DN-1098")
    svc.goods_out.post({"site_id": surrey.id, "issue_date": TODAY}, [IssueLine(h.id, D(33), sample.bin1.id)])
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(h.id, D(15), b17.id)])
    svc.goods_out.post({"site_id": surrey.id, "issue_date": TODAY}, [IssueLine(h.id, D(12), sample.bin1.id)])
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": surrey.id},
                          [ReturnLineInput(h.id, "15", sample.bin1.id)])
    groups = svc.search.product_groups(svc.search.exact_product("h-3311").id)
    by_kind: dict[str, list] = {}
    for hit in groups:
        by_kind.setdefault(GROUP_TITLES[hit.kind], []).append(hit)
    assert list(by_kind) == ["LOCATIONS", "PROJECT HISTORY", "DELIVERY NOTES", "MOVEMENTS"]
    assert [(x.title, x.detail.split(" |")[0]) for x in by_kind["LOCATIONS"]] == [
        ("B04", "85 SHEET"), ("B17", "42 SHEET")]
    assert [(x.title, x.detail.split(" net")[0]) for x in by_kind["PROJECT HISTORY"]] == [
        ("P-2002 - Surrey County Hall", "30 SHEET"), ("P-1001 - Riverside Apartments", "15 SHEET")]
    assert [x.title.split(" -")[0] for x in by_kind["DELIVERY NOTES"]] == ["DN-1098", "DN-1047"]
    moves = len(svc.products.movements(h.id))
    assert by_kind["MOVEMENTS"][0].title == f"{moves} matching transaction(s)"


def test_purchase_orders_are_searchable_and_filterable(svc, sample):
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "supplier_reference": "EGG-Q-7781"},
                             [POLineInput(sample.mdf.id, "40"), POLineInput(sample.edge.id, "100")])
    svc.purchasing.mark_ordered(po.id)
    hits = svc.search.matches(po.po_number)
    assert [(h.kind, h.entity_id) for h in hits if h.kind == "Purchase Order"] == [("Purchase Order", po.id)]
    assert any(h.entity_id == po.id for h in svc.search.matches("q-7781"))  # supplier's reference
    # Products on the order - even before anything has arrived
    assert codes(svc.search.products(SearchCriteria(po_number=po.po_number))) == {"MDF-18", "EDGE-W22"}
    # Transactions: only the deliveries received against it
    dn = svc.goods_in.post({"dn_number": "DN-55", "supplier_id": sample.supplier.id, "delivery_date": TODAY,
                            "purchase_order_id": po.id},
                           [ReceiptLine(sample.mdf.id, D(40), sample.bin1.id)])
    sample.receive(sample.mdf, 5, number="DN-OTHER")
    got = svc.search.movements(SearchCriteria(po_number=po.po_number[-4:]))
    assert {m.doc_id for m in got.rows} == {dn.id} and got.total == 1
    chips = dict(svc.search.describe(SearchCriteria(po_number="PO-1")))
    assert chips["po_number"] == "Purchase Order: PO-1"


def test_reservations_and_delivery_problems_found_by_number(svc, sample):
    sample.receive(sample.mdf, 20)
    res = svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "5"})
    d = svc.purchasing.record_discrepancy({"supplier_id": sample.supplier.id, "product_id": sample.mdf.id,
                                           "reason_code": "DAMAGED", "quantity": "1"})
    kinds = {(h.kind, h.entity_id) for h in svc.search.matches(res.reservation.number)}
    assert ("Reservation", res.reservation.id) in kinds
    kinds = {(h.kind, h.entity_id) for h in svc.search.matches(d.number)}
    assert ("Delivery Problem", d.id) in kinds


def test_low_stock_filter_counts_reservations_and_reorder_level(svc, sample):
    """Reorder Required in search = the Reorder Required list: Available (on hand - reserved)."""
    sample.receive(sample.mdf, 30)  # minimum 10
    low = SearchCriteria(stock_status="LOW_STOCK")
    assert codes(svc.search.products(low)) == set()
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "22"})
    got = svc.search.products(low)
    assert codes(got) == {"MDF-18"}
    row = got.rows[0]
    assert (row.on_hand, row.reserved, row.available) == (D(30), D(22), D(8))
    assert {r.product.id for r in got.rows} == {r.product.id for r in svc.products.low_stock()}
    # Reorder level above the minimum flags it earlier
    sample.receive(sample.edge, 40, number="DN-2")
    p = svc.products.get(sample.edge.id)
    data = {"item_type": p.item_type, "internal_code": p.internal_code, "name": p.name, "unit_id": p.unit_id,
            "reorder_level": "50"}
    svc.products.save(p.id, data, p.version)
    assert codes(svc.search.products(low)) == {"MDF-18", "EDGE-W22"}
    by_status = svc.search.products(SearchCriteria(), sort="status")
    assert {r.product.internal_code for r in by_status.rows[:2]} == {"MDF-18", "EDGE-W22"}  # flagged first


def test_ready_made_views_without_bin_and_without_photo(svc, sample, tmp_path):
    photo = tmp_path / "mdf.jpg"
    photo.write_bytes(JPEG)
    svc.attachments.add("PRODUCT", sample.mdf.id, photo)
    views = dict(BUILTIN_VIEWS)
    no_bin = SearchCriteria.from_dict(views["Products Without Bin"])
    no_photo = SearchCriteria.from_dict(views["Missing Product Photos"])
    assert codes(svc.search.products(no_bin)) == set()  # every sample product has a default bin
    units = {u.code: u for u in svc.reference.units()}
    svc.products.save(None, {"item_type": "CONSUMABLE", "internal_code": "GLUE-1", "name": "PVA glue",
                             "unit_id": units["EA"].id})
    assert codes(svc.search.products(no_bin)) == {"GLUE-1"}
    assert codes(svc.search.products(no_photo)) == {"EDGE-W22", "CAB-600", "GLUE-1"}
    with_photo = SearchCriteria(photo="WITH")
    assert codes(svc.search.products(with_photo)) == {"MDF-18"}
    assert [t for _k, t in svc.search.describe(no_photo)] == ["Missing Photo"]
    for name, view in BUILTIN_VIEWS:  # every view is a valid search or a screen
        if isinstance(view, dict):
            svc.search.products(SearchCriteria.from_dict(view))
        else:
            assert view.startswith("page:"), name
