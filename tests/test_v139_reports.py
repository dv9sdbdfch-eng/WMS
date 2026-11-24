"""v1.39: valuation at a date, stock by project, and structure-import units missing from the workbook."""

from __future__ import annotations

from decimal import Decimal

from openpyxl import load_workbook
from test_v125_structure_import import UNITS, book

from wms.services.structure_import import ERROR, export_problems

D = Decimal


# ---- structure import: units in the WMS that the workbook does not name -------------------------
def test_structure_import_lists_units_missing_from_the_workbook(tmp_path, svc, sample):
    svc.structure_import.run(svc.structure_import.prepare(book(tmp_path / "a.xlsx", UNITS)))
    # the next workbook leaves out Wardrobe (A/1) and Concierge desk, and has a typo in Kitchen B's quantity
    rows = [UNITS[0], UNITS[2], UNITS[3][:7] + ("five",) + UNITS[3][8:]]
    plan = svc.structure_import.prepare(book(tmp_path / "b.xlsx", rows))
    assert plan.units[2].status == ERROR
    missing = [(m.project_no, m.block, m.level, m.unit_code, m.unit_name, m.quantity) for m in plan.missing]
    assert missing == [("P-2001", "", "", "", "Concierge desk", D(1)),
                       ("P-2001", "Block A", "Level 1", "W01", "Wardrobe", D(20))]
    assert "not in the workbook: 2 unit(s)" in plan.summary()
    wb = load_workbook(export_problems(plan, tmp_path / "p.xlsx"))
    ws = wb["Not in workbook"]
    assert [c.value for c in ws[3]][:5] == ["P-2001", None, None, None, "Concierge desk"]

    # every unit named -> nothing missing; another project in the WMS is never listed
    full = svc.structure_import.prepare(book(tmp_path / "c.xlsx", UNITS))
    assert full.missing == [] and "not in the workbook" not in full.summary()


# ---- Stock Valuation at a Date ----------------------------------------------------------------
def _gi(svc, sample, number, day, lines, site=None):
    from wms.services.goods_in import ReceiptLine
    hdr = {"dn_number": number, "supplier_id": sample.supplier.id, "delivery_date": day}
    if site is not None:
        hdr["site_id"] = site.id
    return svc.goods_in.post(hdr, [ReceiptLine(p.id, D(str(q)), b.id, None if c is None else D(c))
                                   for p, q, b, c in lines])


def _rows(svc, key, **kw):
    from wms.services.reports import ReportFilters
    res = svc.reports.run(key, ReportFilters(**kw))
    return res, {r[0]: r for r in res.rows}


def test_valuation_at_a_date_uses_the_ledger_and_the_last_goods_in_price(svc, sample):
    from datetime import date, timedelta

    from wms.core.numbers import fmt_date
    from wms.services.goods_out import IssueLine
    today = date.today()
    d1, d2, d3, d4 = (today - timedelta(days=n) for n in (20, 10, 5, 2))
    _gi(svc, sample, "DN-1", d1, [(sample.mdf, 10, sample.bin1, "20")])
    _gi(svc, sample, "DN-2", d2, [(sample.mdf, 5, sample.bin2, "30"), (sample.edge, 100, sample.bin2, None)])
    gi = svc.goods_out.post({"issue_date": d3, "site_id": sample.site.id},
                            [IssueLine(sample.mdf.id, "4", sample.bin1.id)])
    dn4 = _gi(svc, sample, "DN-4", d4, [(sample.mdf, 1, sample.bin1, "50")])
    svc.goods_in.reverse(dn4.id, "Wrong supplier")  # reversed today: its price never counts

    res, rows = _rows(svc, "valuation_at", as_at=d1 - timedelta(days=1))
    assert rows == {} and res.title == "Stock Valuation"
    assert f"As at: {fmt_date(d1 - timedelta(days=1))}" in res.filters_text
    _res, rows = _rows(svc, "valuation_at", as_at=d1)
    assert rows["MDF-18"][3:] == [D(10), "SHEET", D("20"), f"Goods In {fmt_date(d1)}", D("200.00")]
    res, rows = _rows(svc, "valuation_at", as_at=d2)
    assert rows["MDF-18"][3] == D(15) and rows["MDF-18"][5] == D("30") and rows["MDF-18"][7] == D("450.00")
    assert rows["EDGE-W22"][5:] == [D("0.18"), "Product card", D("18.00")]  # bought without a price
    assert res.totals()[7] == D("468.00")
    assert f"As at: {fmt_date(d2)}" in res.filters_text
    _res, rows = _rows(svc, "valuation_at", as_at=d3)
    assert rows["MDF-18"][3] == D(11)
    _res, rows = _rows(svc, "valuation_at", as_at=d4)
    assert rows["MDF-18"][3] == D(12) and rows["MDF-18"][6] == f"Goods In {fmt_date(d2)}"
    _res, rows = _rows(svc, "valuation_at")  # no date = today
    assert rows["MDF-18"][3] == D(11)
    # today: the issue is reversed (stock back from today, not from d3) and 3 sheets go to quarantine
    svc.goods_out.reverse(gi.id, "Posted in error")
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.quarantine.id, "3")
    _res, rows = _rows(svc, "valuation_at")
    assert rows["MDF-18"][3] == D(15)  # quarantine stock is still ours: it counts
    _res, rows = _rows(svc, "valuation_at", location_id=sample.zone.id)
    assert rows["MDF-18"][3] == D(12)  # the quarantine bin is not in Zone A
    _res, rows = _rows(svc, "valuation_at", as_at=d3)
    assert rows["MDF-18"][3] == D(11)  # the past does not change
    _res, rows = _rows(svc, "valuation_at", as_at=d2, product_id=sample.edge.id)
    assert list(rows) == ["EDGE-W22"]
    _res, rows = _rows(svc, "valuation_at", as_at=d2, item_type="FINISHED")
    assert rows == {}


# ---- Stock by Project -------------------------------------------------------------------------
def test_stock_by_project_lists_what_each_project_holds(svc, sample):
    from datetime import date

    from conftest import hand_reserve

    from wms.services.goods_out import IssueLine
    other = svc.sites.save(None, {"project_number": "P-1002", "name": "Mill Lane", "status": "ACTIVE"})
    _gi(svc, sample, "DN-P1", date.today(), [(sample.mdf, 10, sample.bin1, "20")], site=sample.site)
    _gi(svc, sample, "DN-P2", date.today(), [(sample.edge, 50, sample.bin2, "0.2")], site=other)
    svc.goods_out.post({"issue_date": date.today(), "site_id": sample.site.id},
                       [IssueLine(sample.mdf.id, "3", sample.bin1.id)])
    hand_reserve(svc, {"site_id": other.id, "product_id": sample.edge.id, "quantity": "5"})  # before v1.35

    res, _ = _rows(svc, "stock_by_project")
    got = [(r[0].split(" ")[0], r[2], r[4], r[7]) for r in res.rows]
    mdf_cost = svc.products.get(sample.mdf.id).unit_cost
    assert got == [("P-1001", "MDF-18", D(7), (D(7) * mdf_cost).quantize(D("0.01"))),
                   ("P-1002", "EDGE-W22", D(55), (D(55) * svc.products.get(sample.edge.id).unit_cost)
                    .quantize(D("0.01")))]
    res, _ = _rows(svc, "stock_by_project", site_id=other.id)
    assert [r[2] for r in res.rows] == ["EDGE-W22"]
    res, _ = _rows(svc, "stock_by_project", product_id=sample.mdf.id)
    assert [r[2] for r in res.rows] == ["MDF-18"]


# ---- the screens ------------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture


def test_screens_as_at_filter_and_not_in_workbook_tab(ui, sample, tmp_path):  # noqa: F811
    from datetime import date, timedelta

    from wms.ui.dialogs.structure_import import StructureImportDialog
    app, win, msgs, _ = ui
    _gi(app.services, sample, "DN-1", date.today() - timedelta(days=3), [(sample.mdf, 10, sample.bin1, "20")])
    win.go("reports")
    page = win.page("reports")
    page.select("valuation_at")
    assert not page.form.isRowVisible(page.dates_row) and page.form.isRowVisible(page.as_at)
    assert [r[0] for r in page.result.rows] == ["MDF-18"]
    page.as_at.set_value(date.today() - timedelta(days=4))
    page.run_report()
    assert page.result.rows == []
    page.select("stock_by_project")
    assert not page.form.isRowVisible(page.as_at) and page.form.isRowVisible(page.site)

    win.go("projects")
    dlg = StructureImportDialog(app, win.page("projects"))
    assert dlg.check(str(book(tmp_path / "a.xlsx", UNITS))) and dlg.do_import(ask=False)
    assert dlg.check(str(book(tmp_path / "b.xlsx", UNITS[:3])))
    assert dlg.tabs.tabText(2) == "Not in workbook (2)" and dlg.missing_table.rowCount() == 2
    assert dlg.missing_table.item(0, 4).text() == "Kitchen B" or dlg.missing_table.item(1, 4).text() == "Kitchen B"
    assert dlg.export_btn.isEnabled()
    assert not _errors(msgs)
