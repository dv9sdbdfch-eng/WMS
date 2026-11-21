"""v1.34 stage 1 "stock and cost accuracy":

* B-014 material consumed by Production for a project counts in its Net Issued (Remaining).
* B-013 a reversed delivery note puts back the unit cost it set; odd prices are warned about.
* B-015 Goods Out shows the planned quantity; "Not found" flags the bin for a count, closed by
  the next approved count.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.production import MaterialInput

D = Decimal
TODAY = date.today()


@pytest.fixture
def proj(svc, sample):
    """Riverside needs 14 MDF (kitchen 4 x 3 + desk 2 x 1); 16 in stock."""
    t = svc.project_tree
    kitchen = t.save_unit(None, {"name": "Kitchen", "quantity": "4"}, site_id=sample.site.id)
    t.add_materials(kitchen.id, [(sample.mdf.id, "3")])
    desk = t.save_unit(None, {"name": "Desk", "quantity": "2"}, site_id=sample.site.id)
    t.add_materials(desk.id, [(sample.mdf.id, "1")])
    sample.receive(sample.mdf, 10, sample.bin1, number="DN-3001")
    sample.receive(sample.mdf, 6, sample.bin2, number="DN-3002")
    return sample


def mdf_row(svc, p):
    from wms.services.materials import MaterialFilter
    return next(r for r in svc.materials.page(MaterialFilter(site_id=p.site.id)) if r.id == p.mdf.id)


# --- B-014: production consumption is issued to the project ------------------------------------

def test_production_for_a_project_reduces_its_remaining(svc, proj):
    assert mdf_row(svc, proj).remaining == D(14)
    svc.goods_out.post({"site_id": proj.site.id, "issue_date": TODAY}, [IssueLine(proj.mdf.id, "10", proj.bin1.id)])
    order = svc.production.post({"finished_product_id": proj.cabinet.id, "quantity": "1", "production_date": TODAY,
                                 "site_id": proj.site.id}, [MaterialInput(proj.mdf.id, "3", proj.bin2.id)])
    row = mdf_row(svc, proj)
    assert row.issued == D(13) and row.remaining == D(1)
    svc.production.reverse(order.id, "Entered twice")
    row = mdf_row(svc, proj)
    assert row.issued == D(10) and row.remaining == D(4)


def test_production_output_and_other_projects_are_not_counted(svc, proj):
    other = svc.sites.save(None, {"project_number": "P-2002", "name": "Other", "status": "ACTIVE"})
    svc.production.post({"finished_product_id": proj.cabinet.id, "quantity": "2", "production_date": TODAY,
                         "site_id": other.id}, [MaterialInput(proj.mdf.id, "5", proj.bin1.id)])
    svc.production.post({"finished_product_id": proj.cabinet.id, "quantity": "1", "production_date": TODAY},
                        [MaterialInput(proj.mdf.id, "1", proj.bin1.id)])  # no project
    assert mdf_row(svc, proj).issued == D(0)
    from wms.services.materials import MaterialFilter
    cab = [r for r in svc.materials.page(MaterialFilter(site_id=other.id)) if r.id == proj.cabinet.id]
    assert not cab or cab[0].issued == D(0)  # the finished product made is not "issued"


def test_reserved_pick_list_leaves_out_what_production_used(svc, proj):
    svc.allocation.reserve_remaining(proj.site.id, [(proj.mdf.id, D(14))])
    svc.production.post({"finished_product_id": proj.cabinet.id, "quantity": "1", "production_date": TODAY,
                         "site_id": proj.site.id}, [MaterialInput(proj.mdf.id, "3", proj.bin2.id)])
    pick = svc.allocation.pick_list(proj.site.id, source="RESERVED")
    assert sum(r.quantity for r in pick.rows) == D(11)  # 14 reserved, 3 already used: not picked twice


# --- B-013: unit cost after a reversal, price warnings -----------------------------------------

def _receive(svc, s, cost, number, po_id=None):
    header = {"dn_number": number, "supplier_id": s.supplier.id, "delivery_date": TODAY}
    if po_id:
        header["purchase_order_id"] = po_id
    return svc.goods_in.post(header, [ReceiptLine(s.mdf.id, "5", s.bin1.id, cost)])


def test_reversal_restores_the_cost_the_note_set(svc, sample):
    assert svc.products.get(sample.mdf.id).unit_cost == D("24.50")
    dn = _receive(svc, sample, "245.00", "DN-TYPO")  # £245 keyed instead of £24.50
    assert svc.products.get(sample.mdf.id).unit_cost == D("245")
    done = svc.goods_in.reverse(dn.id, "Wrong price keyed")
    assert svc.products.get(sample.mdf.id).unit_cost == D("24.50")
    assert "restored to £24.50" in done.cost_messages[0]
    acts = [a.action for a in svc.audit.search(entity_type="PRODUCT")]
    assert "PRODUCT_COST_RESTORED" in acts


def test_reversal_leaves_a_cost_set_later_alone(svc, sample):
    dn = _receive(svc, sample, "30.00", "DN-A")
    _receive(svc, sample, "31.00", "DN-B")  # a later delivery changed the cost again
    done = svc.goods_in.reverse(dn.id, "Wrong supplier")
    assert svc.products.get(sample.mdf.id).unit_cost == D("31")
    assert "not restored" in done.cost_messages[0]


def test_reversal_leaves_a_cost_confirmed_by_a_later_note_at_the_same_price(svc, sample):
    dn = _receive(svc, sample, "30.00", "DN-A")   # supplier's new price
    _receive(svc, sample, "30.00", "DN-B")        # the same price again: no cost change is recorded
    done = svc.goods_in.reverse(dn.id, "Wrong supplier")
    assert svc.products.get(sample.mdf.id).unit_cost == D("30")
    assert "not restored" in done.cost_messages[0]


def test_reversal_leaves_a_cost_edited_by_hand_back_to_the_same_value(svc, sample):
    dn = _receive(svc, sample, "30.00", "DN-A")
    base = {"item_type": "RAW_MATERIAL", "internal_code": "MDF-18", "name": "MDF 18mm 2440x1220",
            "unit_id": sample.mdf.unit_id, "default_bin_id": sample.bin1.id}
    svc.products.save(sample.mdf.id, {**base, "unit_cost": "28.00"})
    svc.products.save(sample.mdf.id, {**base, "unit_cost": "30.00"})
    done = svc.goods_in.reverse(dn.id, "Wrong supplier")
    assert svc.products.get(sample.mdf.id).unit_cost == D("30")
    assert "not restored" in done.cost_messages[0]


def test_reversal_without_a_cost_change_says_nothing(svc, sample):
    dn = _receive(svc, sample, None, "DN-NOCOST")
    assert svc.goods_in.reverse(dn.id, "Test").cost_messages == []
    assert svc.products.get(sample.mdf.id).unit_cost == D("24.50")


def test_price_warnings_against_last_cost_and_purchase_order(svc, sample):
    def warn(cost, po_id=None):
        return svc.goods_in.price_warnings([ReceiptLine(sample.mdf.id, "5", sample.bin1.id, cost)], po_id)

    assert warn("26.00") == [] and warn("70.00") == []          # within 3x of £24.50
    assert "vs last cost £24.50" in warn("245.00")[0]           # slipped decimal
    assert warn("2.45") and warn(None) == []
    from wms.services.purchasing import POLineInput
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY},
                             [POLineInput(sample.mdf.id, "20", "20.00")])
    assert warn("24.00", po.id) == []                            # +20 %: fine
    assert "purchase order price £20.00" in warn("26.00", po.id)[0]  # +30 %


# --- B-015: planned vs picked, not found -> bin to count -----------------------------------------

def test_not_found_request_is_closed_by_the_next_count(svc, sample):
    sample.receive(sample.mdf, 8, sample.bin1)
    st = svc.stocktake
    st.request_count(sample.bin1.id, sample.mdf.id, "Not found when picking")
    st.request_count(sample.bin1.id, sample.mdf.id, "Not found again")  # one open request per bin/product
    assert len(st.open_requests()) == 1
    item = svc.dashboard.load().action("bins_to_count")
    assert item is not None and item.count == 1 and item.page == "stocktake"
    count = st.create_for_requests()
    assert {(l.product_id, l.bin_id) for l in count.lines} == {(sample.mdf.id, sample.bin1.id)}
    from wms.services.stocktake import CountEntry
    st.record_counts(count.id, [CountEntry(count.lines[0].id, D(0))])
    st.submit(count.id)
    st.approve(count.id)
    assert st.open_requests() == []
    assert svc.products.row(sample.mdf.id).on_hand == D(0)


def test_count_for_a_flagged_empty_bin_still_lists_the_product(svc, sample):
    svc.stocktake.request_count(sample.bin2.id, sample.mdf.id, "Not found")
    count = svc.stocktake.create_for_requests()
    assert [(l.product_id, l.bin_id, l.system_qty) for l in count.lines] == [(sample.mdf.id, sample.bin2.id, D(0))]


def _approve_all(st, count):
    from wms.services.stocktake import CountEntry
    st.record_counts(count.id, [CountEntry(l.id, l.system_qty) for l in count.lines])
    st.submit(count.id)
    st.approve(count.id)


def test_count_that_did_not_cover_the_flagged_product_leaves_the_flag(svc, sample):
    sample.receive(sample.edge, 50, sample.bin1)
    st = svc.stocktake
    st.request_count(sample.bin1.id, sample.mdf.id, "Not found")    # MDF not in bin B04 at all
    _approve_all(st, st.create(location_id=sample.shelf.id))         # area count: only the edging line
    assert [(r.bin_id, r.product_id) for r in st.open_requests()] == [(sample.bin1.id, sample.mdf.id)]


def test_count_started_before_the_flag_does_not_close_it(svc, sample):
    sample.receive(sample.mdf, 8, sample.bin1)
    st = svc.stocktake
    older = st.create(bin_id=sample.bin1.id)
    st.request_count(sample.bin1.id, sample.mdf.id, "Not found")
    _approve_all(st, older)
    assert len(st.open_requests()) == 1


def test_flag_on_a_bin_taken_out_of_use_drops_off(svc, sample):
    st = svc.stocktake
    st.request_count(sample.bin2.id, sample.mdf.id, "Not found")
    svc.locations.save_bin(sample.bin2.id, {"location_id": sample.shelf.id, "code": "B05", "is_active": False})
    assert st.open_requests() == []
    assert svc.dashboard.load().action("bins_to_count").count == 0


@pytest.fixture
def win(qtbot, svc):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    w = MainWindow(App(svc.ctx))
    qtbot.addWidget(w)
    return w


def test_goods_out_shows_planned_and_not_found_flags_the_bin(svc, proj, win, monkeypatch):
    import wms.ui.dialogs.allocation as dlgmod
    import wms.ui.pages.goods_out as gomod
    win.go("goods_out")
    page = win.page("goods_out")
    planned_col = page.lines._col("Planned")
    assert page.lines.table.isColumnHidden(planned_col)  # nothing loaded yet: no extra column
    page.site.set_value(proj.site.id)

    def fake_exec(self):
        self.source.setCurrentIndex(self.source.findData("REMAINING"))
        self.accept_lines()
        return 1
    monkeypatch.setattr(dlgmod.PickListDialog, "exec", fake_exec)
    assert page.load_from_project()
    t = page.lines.table
    assert not t.isColumnHidden(planned_col)
    assert t.cellWidget(0, planned_col).text() == "10" and page.lines.planned_of(0) == D(10)
    qty = t.cellWidget(0, 1)
    assert qty.styleSheet() == ""
    qty.set_value(D(7))  # short pick: the line is marked as changed
    assert "background" in qty.styleSheet() and "planned 10" in qty.toolTip()
    # second line: not in its bin
    monkeypatch.setattr(gomod, "confirm", lambda *a, **k: True)
    t.setCurrentCell(1, 0)
    page.not_found()
    assert [(v.product_id, v.quantity, v.bin_id) for v in page.lines.values()] == [(proj.mdf.id, D(7), proj.bin1.id)]
    reqs = svc.stocktake.open_requests()
    assert [(r.bin_id, r.product_id) for r in reqs] == [(proj.bin2.id, proj.mdf.id)]
    assert "flagged for a count" in page.not_found_note.text()
    page.clear()
    assert t.isColumnHidden(planned_col) and page.not_found_note.text() == ""
