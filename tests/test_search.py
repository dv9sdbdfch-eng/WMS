"""Search & filtering (Search screen, wms.services.search).

Two kinds of test:

* A small, hand-built warehouse ("the Egger / Surrey County Hall scenario") where every expected
  answer is written out, one test per search behaviour listed in the specification.
* A generated warehouse (1,200 products, ~7,000 movements) where 120 random filter combinations
  are checked against a brute-force answer computed in plain Python - so the filters are shown to
  be mathematically correct, not just "return something".
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from wms.core.exceptions import ValidationError
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.search import (
    MOVEMENT_GROUPS,
    SearchCriteria,
    movement_route,
    signed_qty,
)
from wms.services.stock_ops import ReturnLineInput

D = Decimal
TODAY = date.today()


# --- the hand-built warehouse ----------------------------------------------------------------

@dataclass
class W:
    svc: object
    egger: object
    krono: object
    surrey: object
    riverside: object
    a01: object
    a03: object
    b01: object
    b04: object
    fg02: object
    zone_a: object
    zone_b: object
    fg: object
    h3311: object
    h3303: object
    h3395: object
    k001: object
    k002: object
    cab: object
    old: object
    pct: object
    dn_74821: object
    dn_old: object


def code(rows) -> list[str]:
    return [r.product.internal_code for r in rows]


@pytest.fixture
def w(svc) -> W:
    units = {u.code: u for u in svc.reference.units()}
    main = next(loc for loc in svc.locations.tree() if loc.code == "MAIN")
    loc = svc.locations.save_location
    zone_a = loc(None, {"kind": "ZONE", "code": "A", "name": "Zone A", "parent_id": main.id})
    rack_a = loc(None, {"kind": "RACK", "code": "R01", "name": "Rack 01", "parent_id": zone_a.id})
    shelf_a = loc(None, {"kind": "SHELF", "code": "S01", "name": "Shelf 01", "parent_id": rack_a.id})
    zone_b = loc(None, {"kind": "ZONE", "code": "B", "name": "Zone B", "parent_id": main.id})
    rack_b = loc(None, {"kind": "RACK", "code": "R02", "name": "Rack 02", "parent_id": zone_b.id})
    shelf_b = loc(None, {"kind": "SHELF", "code": "S03", "name": "Shelf 03", "parent_id": rack_b.id})
    fg = loc(None, {"kind": "WAREHOUSE", "code": "FG", "name": "Finished Goods"})
    b = svc.locations.save_bin
    a01, a03 = b(None, {"location_id": shelf_a.id, "code": "A-01"}), b(None, {"location_id": shelf_a.id, "code": "A-03"})
    b01, b04 = b(None, {"location_id": shelf_b.id, "code": "B-01"}), b(None, {"location_id": shelf_b.id, "code": "B-04"})
    fg02 = b(None, {"location_id": fg.id, "code": "FG-02"})
    egger = svc.reference.save_supplier(None, {"code": "EGG", "name": "Egger"})
    krono = svc.reference.save_supplier(None, {"code": "KRO", "name": "Kronospan"})
    surrey = svc.sites.save(None, {"project_number": "P-100", "name": "Surrey County Hall", "customer": "Surrey CC",
                                   "project_manager": "Jo Smith", "status": "ACTIVE"})
    riverside = svc.sites.save(None, {"project_number": "P-200", "name": "Riverside Flats", "customer": "Acme",
                                      "status": "ACTIVE"})

    def product(c, name, **kw):
        data = {"item_type": "RAW_MATERIAL", "internal_code": c, "name": name, "unit_id": units["SHEET"].id} | kw
        return svc.products.save(None, data)

    h3311 = product("H3311", "Cleaf Oak 18mm", supplier_id=egger.id, brand="Egger", supplier_code="EGG-H3311 ST28",
                    barcode="5012345000011", description="Egger H3311 ST28 Cleaf oak natural", min_stock="10")
    h3303 = product("H3303", "Hamilton Oak 18mm", supplier_id=egger.id, brand="Egger", min_stock="50")
    h3395 = product("H3395", "Corbridge Oak 18mm", supplier_id=egger.id, brand="Egger")
    k001 = product("K001", "White melamine 18mm", supplier_id=krono.id, brand="Kronospan",
                   barcode="5099999000017", min_stock="5")
    k002 = product("K002", "ABS edging 22mm", supplier_id=krono.id, unit_id=units["M"].id,
                   description="Edging to match Egger H3303")
    cab = product("FG-CAB", "Base cabinet 600", item_type="FINISHED", unit_id=units["EA"].id)
    old = product("OLD-1", "Discontinued chipboard", supplier_id=egger.id)
    pct = product("PCT-1", "Board 100% recycled_fibre", description="O'Brien special \\ order")
    svc.products.save(old.id, {"item_type": "RAW_MATERIAL", "internal_code": "OLD-1", "name": old.name,
                               "unit_id": units["SHEET"].id, "supplier_id": egger.id, "is_active": False})

    def receive(sup, number, when, lines):
        return svc.goods_in.post({"dn_number": number, "supplier_id": sup.id, "delivery_date": when},
                                 [ReceiptLine(p.id, D(q), bin_.id) for p, q, bin_ in lines])

    dn_old = receive(egger, "DN-70000", TODAY - timedelta(days=60), [(h3395, 10, b04)])
    dn_74821 = receive(egger, "DN-74821", TODAY - timedelta(days=25), [(h3311, 50, b01), (h3303, 20, a01)])
    receive(krono, "KR-1", TODAY, [(k001, 100, b04), (k002, 300, a01)])
    receive(krono, "KR-2", TODAY, [(cab, 3, fg02)])
    svc.stock.transfer(h3311.id, b01.id, a03.id, D(20))
    svc.goods_out.post({"site_id": surrey.id, "issue_date": TODAY - timedelta(days=2), "reference": "JOB-77"},
                       [IssueLine(h3311.id, D(5), a03.id)])
    svc.goods_out.post({"site_id": riverside.id, "issue_date": TODAY}, [IssueLine(k001.id, D(30), b04.id)])
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": surrey.id},
                          [ReturnLineInput(h3311.id, "1", a03.id)])
    return W(svc, egger, krono, surrey, riverside, a01, a03, b01, b04, fg02, zone_a, zone_b, fg, h3311, h3303,
             h3395, k001, k002, cab, old, pct, dn_74821, dn_old)


def P(w, sort=None, descending=False, **kw):
    """Product results for the given filters (optionally sorted)."""
    if sort is None:
        return w.svc.search.products(SearchCriteria(**kw))
    return w.svc.search.products(SearchCriteria(**kw), sort=sort, descending=descending)


def M(w, sort=None, descending=True, **kw):
    """Transaction results for the given filters (optionally sorted)."""
    if sort is None:
        return w.svc.search.movements(SearchCriteria(**kw))
    return w.svc.search.movements(SearchCriteria(**kw), sort=sort, descending=descending)


# --- 1. exact and partial code search -----------------------------------------------------

def test_exact_code_search_puts_the_product_first_and_identifies_it(w):
    res = P(w, text="H3311")
    assert code(res.rows)[0] == "H3311"
    assert res.exact.internal_code == "H3311"


def test_barcode_scan_identifies_exactly_one_product(w):
    res = P(w, text="5012345000011")
    assert code(res.rows) == ["H3311"] and res.exact.id == w.h3311.id
    assert w.svc.search.exact_product("5099999000017").internal_code == "K001"
    assert w.svc.search.exact_product("50123") is None  # partial barcode is a search, not an identification


def test_partial_code_search(w):
    codes = set(code(P(w, text="H33").rows))
    assert {"H3311", "H3303", "H3395"} <= codes
    # K002 mentions H3303 in its description - "search every field" means it is found too
    assert codes == {"H3311", "H3303", "H3395", "K002"}
    assert P(w, text="H33").exact is None


def test_product_name_and_multi_word_search(w):
    assert code(P(w, text="cleaf oak").rows) == ["H3311"]
    assert code(P(w, text="oak 18mm").rows) == ["H3303", "H3311", "H3395"]
    assert code(P(w, text="Corbridge").rows) == ["H3395"]


# --- 2. supplier / brand / description, case, spaces --------------------------------------

def test_supplier_brand_and_description_search(w):
    # Supplier = Egger, Brand = Egger or description contains Egger (archived OLD-1 is excluded)
    assert set(code(P(w, text="egger").rows)) == {"H3311", "H3303", "H3395", "K002"}
    assert set(code(P(w, text="kronospan").rows)) == {"K001", "K002"}


def test_case_insensitive(w):
    for text in ("h3311", "H3311", "eGgEr H3311"):
        assert "H3311" in code(P(w, text=text).rows), text
    assert P(w, text="EGGER").total == P(w, text="egger").total


def test_spaces_are_trimmed_and_normalised_in_codes(w):
    for text in ("  H3311  ", "h 3311", "H-3311", "h.3311"):
        res = P(w, text=text)
        assert code(res.rows)[0] == "H3311", text
        assert res.exact is not None and res.exact.internal_code == "H3311", text
    # supplier code typed without its space
    assert code(P(w, text="EGG-H3311ST28").rows) == ["H3311"]
    assert code(P(w, supplier_code="egg h3311").rows) == ["H3311"]


def test_special_characters_are_literal_and_safe(w):
    assert code(P(w, text="100%").rows) == ["PCT-1"]
    assert code(P(w, text="recycled_fibre").rows) == ["PCT-1"]
    assert P(w, text="_").total == 1  # only PCT-1 has an underscore; "_" is not a wildcard
    assert P(w, text="%").total == 1
    assert code(P(w, text="O'Brien").rows) == ["PCT-1"]
    assert code(P(w, text="\\ order").rows) == ["PCT-1"]
    for text in ('"; DROP TABLE products; --', "[]", "*", "?", "'", "\\"):
        P(w, text=text)
        M(w, text=text)
        w.svc.search.matches(text)
    assert w.svc.products.list(include_inactive=True)  # nothing was dropped


# --- 3. bin, project, supplier and delivery note searches ------------------------------------

def test_bin_search_finds_the_bin_and_its_stock(w):
    for text in ("B04", "b-04", "B 04"):
        hits = w.svc.search.matches(text)
        assert hits[0].kind == "Bin" and hits[0].entity_id == w.b04.id, text
        assert {"H3395", "K001"} <= set(code(P(w, text=text).rows)), text
    v = w.svc.search.bin_view(w.b04.id)
    assert v.path_text == "Main Warehouse → Zone B → Rack 02 → Shelf 03"
    assert {lvl.product.internal_code: lvl.quantity for lvl in v.contents} == {"H3395": D(10), "K001": D(70)}
    assert v.product_count == 2
    assert {m.product.internal_code for m in v.recent_in} == {"H3395", "K001"}
    assert [m.product.internal_code for m in v.recent_out] == ["K001"]


def test_project_search_and_material_view(w):
    hits = [h for h in w.svc.search.matches("surrey") if h.kind == "Project"]
    assert [h.entity_id for h in hits] == [w.surrey.id]
    summary = {r.product.internal_code: r for r in w.svc.sites.material_summary(w.surrey.id)}
    row = summary["H3311"]
    assert (row.issued, row.returned, row.net) == (D(5), D(1), D(4))
    assert row.last_movement is not None
    # "How much was sent to Surrey County Hall?" through the filters
    res = M(w, site_id=w.surrey.id, movement_types=["GOODS_OUT"])
    assert res.total == 1 and sum(-signed_qty(m) for m in res.rows) == D(5)
    assert code(P(w, site_id=w.surrey.id).rows) == ["H3311"]
    assert code(P(w, project_number="p-100").rows) == ["H3311"]
    assert code(P(w, customer="surrey cc").rows) == ["H3311"]
    assert code(P(w, project_manager="smith").rows) == ["H3311"]


def test_project_active_closed_filter(w):
    w.svc.sites.save(w.riverside.id, {"project_number": "P-200", "name": "Riverside Flats", "status": "COMPLETED"})
    assert code(P(w, project_status="CLOSED").rows) == ["K001"]
    assert code(P(w, project_status="ACTIVE").rows) == ["H3311"]


def test_supplier_view(w):
    v = w.svc.search.supplier_view(w.egger.id)
    # H3311, H3303, H3395 delivered; OLD-1 has Egger as main supplier but never arrived
    assert {r.product.internal_code: r.received for r in v.products} == {
        "H3311": D(50), "H3303": D(20), "H3395": D(10), "OLD-1": D(0)}
    assert v.products_supplied == 4 and v.delivery_count == 2
    assert v.last_delivery == TODAY - timedelta(days=25)
    w.svc.goods_in.reverse(w.dn_old.id, "keyed twice")  # reversed: no longer counts as received
    v = w.svc.search.supplier_view(w.egger.id)
    assert {r.product.internal_code: r.received for r in v.products}["H3395"] == D(0)
    assert v.delivery_count == 1


def test_delivery_note_search(w):
    for text in ("DN-74821", "dn 74821", "74821", "dn74821"):
        hits = [h for h in w.svc.search.matches(text) if h.kind == "Delivery Note"]
        assert [h.entity_id for h in hits] == [w.dn_74821.id], text
        assert set(code(P(w, text=text).rows)) == {"H3311", "H3303"}, text
    res = M(w, dn_number="dn 74821")
    assert res.total == 2 and {m.movement_type for m in res.rows} == {"GOODS_IN"}
    assert {m.doc_id for m in res.rows} == {w.dn_74821.id}
    assert set(code(P(w, dn_number="74821").rows)) == {"H3311", "H3303"}


def test_reference_search(w):
    res = M(w, reference="job-77")
    assert res.total == 1 and res.rows[0].product_id == w.h3311.id
    assert code(P(w, text="JOB-77").rows) == ["H3311"]


# --- 4. combined filters, dates, stock ------------------------------------------------------

def test_combined_filters_return_only_records_matching_all(w):
    # Supplier Egger + Project Surrey + Bin A-03 + Last 30 Days (the example in the specification)
    c = dict(supplier_id=w.egger.id, site_id=w.surrey.id, bin_id=w.a03.id, date_preset="LAST_30")
    res = M(w, **c)
    assert {(m.movement_type, m.product_id) for m in res.rows} == {("GOODS_OUT", w.h3311.id),
                                                                     ("RETURN_FROM_SITE", w.h3311.id)}
    assert code(P(w, **c).rows) == ["H3311"]
    # each extra condition can only narrow the result
    assert M(w, **c, movement_types=["GOODS_OUT"]).total == 1
    assert M(w, **c, movement_types=["TRANSFER"]).total == 0
    assert M(w, **(c | {"bin_id": w.b04.id})).total == 0
    assert P(w, **(c | {"supplier_id": w.krono.id})).total == 0


def test_what_did_egger_deliver_in_the_last_30_days(w):
    res = M(w, supplier_id=w.egger.id, movement_types=["GOODS_IN"], date_preset="LAST_30")
    assert sorted((m.product.internal_code, m.quantity) for m in res.rows) == [("H3303", D(20)), ("H3311", D(50))]
    # without the date filter the 60-day-old delivery is included
    assert M(w, supplier_id=w.egger.id, movement_types=["GOODS_IN"]).total == 3


def test_location_filters(w):
    assert set(code(P(w, zone_id=w.zone_a.id).rows)) == {"H3311", "H3303", "K002"}
    assert set(code(P(w, zone_id=w.zone_b.id).rows)) == {"H3311", "H3395", "K001"}  # H3311: 30 left in B-01
    assert code(P(w, warehouse_id=w.fg.id).rows) == ["FG-CAB"]
    assert set(code(P(w, bin_id=w.b04.id).rows)) == {"H3395", "K001"}
    # zone + bin must both hold (B-04 is not in zone A)
    assert P(w, zone_id=w.zone_a.id, bin_id=w.b04.id).total == 0
    # transactions touching zone A: DN line into A-01 x2, transfer into A-03, Goods Out and return at A-03
    assert M(w, zone_id=w.zone_a.id).total == 5


def test_date_presets():
    c = SearchCriteria()
    today = date(2026, 10, 28)
    expect = {"TODAY": (today, today), "YESTERDAY": (date(2026, 10, 27), date(2026, 10, 27)),
              "LAST_7": (date(2026, 10, 22), today), "LAST_30": (date(2026, 9, 29), today),
              "THIS_MONTH": (date(2026, 10, 1), today)}
    for preset, rng in expect.items():
        c.date_preset = preset
        assert c.date_range(today) == rng, preset
    c.date_preset, c.date_from, c.date_to = "CUSTOM", date(2026, 1, 1), date(2026, 1, 31)
    assert c.date_range(today) == (date(2026, 1, 1), date(2026, 1, 31))
    c.date_from, c.date_to = date(2026, 2, 1), date(2026, 1, 1)
    with pytest.raises(ValidationError):
        c.validate()


def test_date_ranges_are_inclusive_and_exact(w):
    day = TODAY - timedelta(days=60)
    res = M(w, date_preset="CUSTOM", date_from=day, date_to=day)
    assert [m.doc_id for m in res.rows] == [w.dn_old.id]
    assert M(w, date_preset="CUSTOM", date_from=day + timedelta(days=1), date_to=day + timedelta(days=1)).total == 0
    assert M(w, date_preset="TODAY").total == M(w, date_preset="CUSTOM", date_from=TODAY, date_to=TODAY).total
    assert M(w, date_preset="YESTERDAY").total == 0
    assert code(P(w, date_preset="LAST_7", movement_types=["GOODS_OUT"]).rows) == ["H3311", "K001"]


def test_stock_filters(w):
    # on hand: H3311 30+16=46, H3303 20 (min 50 -> low), H3395 10, K001 70, K002 300, FG-CAB 3, PCT-1 0
    assert set(code(P(w, stock_status="LOW_STOCK").rows)) == {"H3303"}
    assert set(code(P(w, stock_status="OUT_OF_STOCK").rows)) == {"PCT-1"}
    assert P(w, stock_status="IN_STOCK").total == 6
    assert set(code(P(w, qty_gt=D(20)).rows)) == {"H3311", "K001", "K002"}
    assert set(code(P(w, qty_lt=D(10)).rows)) == {"FG-CAB", "PCT-1"}
    assert set(code(P(w, qty_gt=D(9.999), qty_lt=D(20.001)).rows)) == {"H3303", "H3395"}
    with pytest.raises(ValidationError):
        P(w, qty_gt=D(10), qty_lt=D(5))


def test_quick_filters(w):
    assert set(code(P(w, quick="FINISHED").rows)) == {"FG-CAB"}
    assert "FG-CAB" not in code(P(w, quick="RAW").rows)
    assert set(code(P(w, quick="RECENTLY_RECEIVED").rows)) == {"K001", "K002", "FG-CAB"}
    assert set(code(P(w, quick="RECENTLY_ISSUED").rows)) == {"H3311", "K001"}
    assert M(w, quick="RECENTLY_ISSUED").total == 2
    assert set(code(P(w, quick="LOW_STOCK").rows)) == {"H3303"}


def test_transaction_type_filter(w):
    for group, (_label, types) in MOVEMENT_GROUPS.items():
        res = M(w, movement_types=[group])
        if group == "REVERSAL":
            assert all(m.reversal_of_id for m in res.rows)
            continue
        assert all(m.movement_type in types for m in res.rows), group
    assert M(w, movement_types=["GOODS_IN"]).total == 6
    assert M(w, movement_types=["GOODS_IN", "GOODS_OUT"]).total == 8
    assert M(w, movement_types=["TRANSFER"]).total == 1
    assert M(w, movement_types=["RETURN"]).total == 1


def test_attachment_filter(w, tmp_path):
    pdf = tmp_path / "dn.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    w.svc.attachments.add("DELIVERY_NOTE", w.dn_74821.id, pdf)
    res = M(w, attachment="WITH")
    assert res.total == 2 and {m.doc_id for m in res.rows} == {w.dn_74821.id}
    assert M(w, attachment="WITHOUT").total == M(w).total - 2
    assert P(w, attachment="WITH").total == 0  # products: has a photo


# --- 5. empty search, archived records, large result sets -----------------------------------

def test_empty_search_lists_everything(w):
    assert P(w).total == 7  # active products
    assert M(w).total == 10  # every ledger row
    assert w.svc.search.matches("") == [] and w.svc.search.matches(" ") == []


def test_archived_records(w):
    assert "OLD-1" not in code(P(w, text="OLD").rows)
    assert code(P(w, text="OLD", archive="ARCHIVED").rows) == ["OLD-1"]
    assert code(P(w, archive="ARCHIVED").rows) == ["OLD-1"]
    assert P(w, archive="ALL").total == 8
    assert w.svc.search.exact_product("OLD-1") is None  # a scan never picks an archived product
    # archived products keep their history in the transaction list
    w.svc.products.save(w.k002.id, {"item_type": "RAW_MATERIAL", "internal_code": "K002", "name": "ABS edging 22mm",
                                    "unit_id": w.k002.unit_id, "is_active": True})
    assert M(w, text="K002").total == 1


def test_renamed_supplier_and_bin_keep_history_findable(w):
    w.svc.reference.save_supplier(w.egger.id, {"code": "EGG", "name": "Egger UK Ltd"})
    assert M(w, supplier_id=w.egger.id, movement_types=["GOODS_IN"]).total == 3
    assert M(w, text="Egger UK", movement_types=["GOODS_IN"]).total == 3
    # History shows the name the supplier had when the goods arrived; the new name still finds it.
    route = movement_route(M(w, dn_number="74821").rows[0])
    assert route[0] == "Egger"


def test_movement_route_and_sign_read_like_the_specification(w):
    rows = {m.movement_type: m for m in M(w, site_id=w.surrey.id).rows}
    gi = M(w, dn_number="74821", text="H3311").rows[0]
    assert (movement_route(gi), signed_qty(gi)) == (("Egger", "Bin MAIN / B / R02 / S03 / B-01"), D(50))
    go = rows["GOODS_OUT"]
    assert movement_route(go) == ("Bin MAIN / A / R01 / S01 / A-03", "P-100 - Surrey County Hall")
    assert signed_qty(go) == D(-5)
    tr = M(w, movement_types=["TRANSFER"]).rows[0]
    assert movement_route(tr) == ("Bin MAIN / B / R02 / S03 / B-01", "Bin MAIN / A / R01 / S01 / A-03")
    assert signed_qty(tr) == D(20)


def test_sorting(w):
    rows = P(w, text="", sort="qty", descending=True).rows
    qtys = [r.on_hand for r in rows]
    assert qtys == sorted(qtys, reverse=True)
    assert code(P(w, sort="code").rows) == sorted(code(P(w).rows))
    assert code(P(w, sort="code", descending=True).rows) == sorted(code(P(w).rows), reverse=True)
    names = [r.product.name for r in P(w, sort="name").rows]
    assert names == sorted(names, key=str.lower) or names == sorted(names)
    dates = [m.occurred_at for m in M(w).rows]
    assert dates == sorted(dates, reverse=True)  # newest first by default
    dates = [m.occurred_at for m in M(w, sort="date", descending=False).rows]
    assert dates == sorted(dates)


# --- 6. chips, saved and recent searches --------------------------------------------------------

def test_chips_and_removing_one_filter(w):
    c = SearchCriteria(text="oak", supplier_id=w.egger.id, site_id=w.surrey.id, bin_id=w.a03.id,
                       date_preset="LAST_30", movement_types=["GOODS_OUT"], quick="LOW_STOCK")
    chips = dict(w.svc.search.describe(c))
    assert chips["supplier_id"] == "Supplier: Egger"
    assert chips["site_id"] == "Project: Surrey County Hall"
    assert chips["bin_id"] == "Bin: MAIN / A / R01 / S01 / A-03"
    assert chips["date"] == "Date: Last 30 Days"
    assert chips["movement_types"] == "Transaction: Goods Out"
    c.clear("bin_id")
    assert "bin_id" not in dict(w.svc.search.describe(c)) and c.supplier_id == w.egger.id
    c.clear("date")
    assert c.date_preset is None and c.date_range() == (None, None)
    c.clear_filters()
    assert c.text == "oak" and not c.has_filters and c.quick == "ALL"


def test_criteria_survive_saving(w):
    c = SearchCriteria(text="H33", supplier_id=3, qty_gt=D("2.5"), date_preset="CUSTOM",
                       date_from=date(2026, 1, 2), movement_types=["GOODS_IN", "BOGUS"], quick="RAW")
    back = SearchCriteria.from_json(c.to_json())
    assert back.text == "H33" and back.supplier_id == 3 and back.qty_gt == D("2.5")
    assert back.date_from == date(2026, 1, 2) and back.quick == "RAW"
    assert back.movement_types == ["GOODS_IN"]  # unknown values from another version are dropped
    assert SearchCriteria.from_json('{"no_such_filter": 1}') == SearchCriteria()


def test_saved_searches(w):
    s = w.svc.search
    s.save_search("Low Stock - Egger", SearchCriteria(supplier_id=w.egger.id, stock_status="LOW_STOCK"))
    s.save_search("Surrey Current Materials", SearchCriteria(site_id=w.surrey.id, movement_types=["GOODS_OUT"]))
    names = [x.name for x in s.saved_searches()]
    assert names == ["Low Stock - Egger", "Surrey Current Materials"]
    low = SearchCriteria.from_json(s.saved_searches()[0].criteria)
    assert code(s.products(low).rows) == ["H3303"]
    with pytest.raises(ValidationError):
        s.save_search("low stock - EGGER", SearchCriteria())  # same name, any case
    s.save_search("low stock - EGGER", SearchCriteria(stock_status="OUT_OF_STOCK"), replace=True)
    assert len(s.saved_searches()) == 2
    s.delete_saved(s.saved_searches()[0].id)
    assert [x.name for x in s.saved_searches()] == ["Surrey Current Materials"]
    with pytest.raises(ValidationError):
        s.save_search("   ", SearchCriteria())


def test_saved_searches_are_per_user(w):
    w.svc.search.save_search("Mine", SearchCriteria(stock_status="LOW_STOCK"))
    roles = {r.code: r for r in w.svc.users.list_roles()}
    w.svc.users.create_user({"username": "jo", "full_name": "Jo Bloggs", "role_id": roles["OPERATIVE"].id},
                            "secret1")
    w.svc.auth.login("jo", "secret1")
    assert w.svc.search.saved_searches() == []
    assert w.svc.search.recent() == []


def test_recent_searches_stay_short_and_unique(w):
    s = w.svc.search
    for t in ("H3311", "Surrey County Hall", "B04", "Egger", "DN-74821"):
        s.remember(t)
    s.remember("h3311")  # same search again: moves to the top, no duplicate
    assert s.recent() == ["h3311", "DN-74821", "Egger", "B04", "Surrey County Hall"]
    for i in range(30):
        s.remember(f"search {i}")
    assert len(s.recent()) == 10 and s.recent()[0] == "search 29"
    s.remember("x")  # too short to be worth keeping
    assert "x" not in s.recent()
    s.clear_recent()
    assert s.recent() == []


# --- 7. large data: limits, counts and brute-force correctness ----------------------------------

@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """1,200 products, 30 sites, 400 bins, 250 deliveries + issues + transfers (~7,000 movements)."""
    from tools.generate_test_data import generate
    from wms.config import AppConfig
    from wms.db.engine import Database
    from wms.services import Services
    from wms.services.base import AppContext
    folder = tmp_path_factory.mktemp("bigdata")
    generate(folder, products=1200, deliveries=250, issues=200, transfers=600, sites=30, suppliers=12)
    cfg = AppConfig(data_dir=folder)
    db = Database(cfg.resolved_database_url)
    db.migrate()
    svc = Services(AppContext(db=db, config=cfg))
    svc.auth.login("admin", "admin")
    _reserve_and_set_reorder_levels(svc)
    yield svc
    db.dispose()


def _reserve_and_set_reorder_levels(svc) -> None:
    """Reorder levels on some products, reservations (some partly issued) on others, so the stock
    status filters are checked against Available = usable - reserved, not just on hand."""
    from sqlalchemy import update

    from wms.db.models import Product
    from wms.services.goods_out import IssueLine
    rnd = random.Random(7)
    rows = [r for r in svc.products.list() if r.available >= 4]
    sites = svc.sites.list(statuses=["ACTIVE"])
    with svc.ctx.db.session() as s:
        for r in rnd.sample(rows, 80):
            s.execute(update(Product).where(Product.id == r.product.id).values(
                reorder_level=r.available + D(rnd.choice([-3, 0, 2, 50]))))
    for r in rnd.sample(rows, 60):
        site_id = rnd.choice(sites).id
        qty = (r.available * D(rnd.choice(["0.3", "0.6", "1"]))).to_integral_value()
        if qty <= 0:
            continue
        res = svc.reservations.reserve({"product_id": r.product.id, "site_id": site_id, "quantity": qty})
        if rnd.random() < 0.4:
            svc.goods_out.post({"site_id": site_id, "issue_date": TODAY},
                               [IssueLine(r.product.id, max(D(1), (qty / 2).to_integral_value()))])
        assert res.reservation.number.startswith("RES-")


def test_large_result_sets_are_counted_in_full_and_shown_in_part(big):
    res = big.search.products(SearchCriteria(), limit=500)
    assert res.total == 1200 and len(res.rows) == 500 and res.truncated
    assert "1,200 products" in res.summary("product") and "first 500" in res.summary("product")
    res = big.search.movements(SearchCriteria(), limit=500)
    assert res.total > 5000 and len(res.rows) == 500
    # server-side sort: the first page really is the top of the whole list
    top = big.search.products(SearchCriteria(), limit=50, sort="qty", descending=True)
    everything = big.search.products(SearchCriteria(), limit=5000, sort="code")
    assert [r.on_hand for r in top.rows] == sorted((r.on_hand for r in everything.rows), reverse=True)[:50]


class Oracle:
    """Answers the same questions by brute force over every row, in plain Python."""

    def __init__(self, svc):
        from wms.db.models import Bin, Location, Product, StockLevel, StockMovement
        with svc.ctx.db.read_session() as s:
            self.products = {p.id: p for p in s.scalars(select(Product)).unique()}
            self.bins = {b.id: b for b in s.scalars(select(Bin)).unique()}
            self.locations = {loc.id: loc for loc in s.scalars(select(Location))}
            self.levels = [(lvl.product_id, lvl.bin_id, lvl.quantity) for lvl in s.scalars(select(StockLevel))]
            self.moves = list(s.scalars(select(StockMovement)).unique())
        self.levels_of: dict[int, list] = {}
        for pid, bid, q in self.levels:
            self.levels_of.setdefault(pid, []).append((bid, q))
        self.moves_of: dict[int, list] = {}
        for m in self.moves:
            self.moves_of.setdefault(m.product_id, []).append(m)
        self.on_hand: dict[int, Decimal] = {}
        self.available: dict[int, Decimal] = {}
        for pid, bid, q in self.levels:
            self.on_hand[pid] = self.on_hand.get(pid, D(0)) + q
            if self.bins[bid].is_available:
                self.available[pid] = self.available.get(pid, D(0)) + q
        # Reserved the Python way (row by row), to check the SQL used by search.
        from wms.services.reservations import reserved_by_product
        with svc.ctx.db.read_session() as s:
            self.reserved = reserved_by_product(s)

    def is_low(self, p) -> bool:
        from wms.services.products import ProductRow
        return ProductRow(p, self.on_hand.get(p.id, D(0)), self.available.get(p.id, D(0)),
                          self.reserved.get(p.id, D(0))).is_low

    def bin_ok(self, c, bid) -> bool:
        if c.bin_id and bid != c.bin_id:
            return False
        for attr in ("shelf_id", "rack_id", "zone_id", "warehouse_id"):
            if getattr(c, attr):
                return self.bins[bid].full_code.startswith(self.locations[getattr(c, attr)].path + " / ")
        return True

    def has_bin_filter(self, c) -> bool:
        return bool(c.bin_id or c.shelf_id or c.rack_id or c.zone_id or c.warehouse_id)

    def move_ok(self, c, m) -> bool:
        types = [t for g in c.movement_types for t in MOVEMENT_GROUPS[g][1]]
        is_rev = "REVERSAL" in c.movement_types and m.reversal_of_id is not None
        if (types or "REVERSAL" in c.movement_types) and m.movement_type not in types and not is_rev:
            return False
        lo, hi = c.date_range()
        day = m.occurred_at.astimezone().date()
        if (lo and day < lo) or (hi and day > hi):
            return False
        return not (c.site_id and m.site_id != c.site_id)

    def product_ok(self, c, p) -> bool:
        oh = self.on_hand.get(p.id, D(0))
        if c.text and c.text.upper() not in p.internal_code:
            return False
        if c.stock_status == "IN_STOCK" and not oh > 0:
            return False
        if c.stock_status == "OUT_OF_STOCK" and not oh <= 0:
            return False
        if c.stock_status == "LOW_STOCK" and not self.is_low(p):
            return False
        if c.qty_gt is not None and not oh > c.qty_gt:
            return False
        return not (c.qty_lt is not None and not oh < c.qty_lt)

    def products_result(self, c) -> set[int]:
        out = set()
        for p in self.products.values():
            if not p.is_active or not self.product_ok(c, p):
                continue
            mine = self.moves_of.get(p.id, [])
            if c.supplier_id and p.supplier_id != c.supplier_id and not any(
                    m.supplier_id == c.supplier_id and m.movement_type == "GOODS_IN" for m in mine):
                continue
            if self.has_bin_filter(c) and not any(q != 0 and self.bin_ok(c, bid)
                                                  for bid, q in self.levels_of.get(p.id, [])):
                continue
            if (c.movement_types or c.site_id or c.date_preset) and not any(self.move_ok(c, m) for m in mine):
                continue
            out.add(p.id)
        return out

    def movements_result(self, c) -> set[int]:
        out = set()
        for m in self.moves:
            p = self.products[m.product_id]
            if not self.move_ok(c, m) or not self.product_ok(c, p):
                continue
            if c.supplier_id and m.supplier_id != c.supplier_id and p.supplier_id != c.supplier_id:
                continue
            if self.has_bin_filter(c) and not any(b and self.bin_ok(c, b) for b in (m.from_bin_id, m.to_bin_id)):
                continue
            out.add(m.id)
        return out


def random_criteria(rnd: random.Random, o: Oracle) -> SearchCriteria:
    c = SearchCriteria()
    zones = [loc for loc in o.locations.values() if loc.kind == "ZONE"]
    racks = [loc for loc in o.locations.values() if loc.kind == "RACK"]
    sites = sorted({m.site_id for m in o.moves if m.site_id})
    suppliers = sorted({p.supplier_id for p in o.products.values() if p.supplier_id})
    picks = rnd.sample(["supplier", "site", "bin", "zone", "rack", "types", "date", "stock", "gt", "lt", "text"],
                       rnd.randint(1, 4))
    for k in picks:
        if k == "supplier":
            c.supplier_id = rnd.choice(suppliers)
        elif k == "site":
            c.site_id = rnd.choice(sites)
        elif k == "bin":
            c.bin_id = rnd.choice(list(o.bins))
        elif k == "zone":
            c.zone_id = rnd.choice(zones).id
        elif k == "rack":
            c.rack_id = rnd.choice(racks).id
        elif k == "types":
            c.movement_types = rnd.sample(list(MOVEMENT_GROUPS), rnd.randint(1, 2))
        elif k == "date":
            c.date_preset = rnd.choice(["LAST_7", "LAST_30", "THIS_MONTH", "CUSTOM"])
            if c.date_preset == "CUSTOM":
                start = TODAY - timedelta(days=rnd.randint(0, 700))
                c.date_from, c.date_to = start, start + timedelta(days=rnd.randint(0, 90))
        elif k == "stock":
            c.stock_status = rnd.choice(["IN_STOCK", "OUT_OF_STOCK", "LOW_STOCK"])
        elif k == "gt":
            c.qty_gt = D(rnd.choice([0, 5, 50, 200]))
        elif k == "lt":
            c.qty_lt = D(rnd.choice([1, 10, 100, 1000])) + (c.qty_gt or 0)
        elif k == "text":
            c.text = f"PRD-0{rnd.randint(0, 11)}"
    return c


def test_random_filter_combinations_match_brute_force(big):
    o = Oracle(big)
    rnd = random.Random(2026)
    checked = 0
    for _ in range(120):
        c = random_criteria(rnd, o)
        got = big.search.products(c, limit=5000)
        assert {r.product.id for r in got.rows} == o.products_result(c), c
        assert got.total == len(got.rows)
        got = big.search.movements(c, limit=100000)
        assert {m.id for m in got.rows} == o.movements_result(c), c
        assert got.total == len(got.rows)
        checked += 1
    assert checked == 120
    low = big.search.products(SearchCriteria(stock_status="LOW_STOCK"), limit=5000)
    assert {r.product.id for r in low.rows} == {r.product.id for r in big.products.low_stock()}
    assert any(r.reserved > 0 for r in low.rows)  # the reservations really are part of the check


def test_product_totals_and_bins_in_results_are_exact(big):
    o = Oracle(big)
    rows = big.search.products(SearchCriteria(), limit=5000).rows
    for r in rows:
        assert r.on_hand == o.on_hand.get(r.product.id, D(0))
        assert sum((b.quantity for b in r.bins), D(0)) == r.on_hand
