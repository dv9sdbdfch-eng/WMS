"""v1.38 Stock quantities from Excel: the file becomes a submitted stock count; approval posts in bulk."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal as D

import pytest
from openpyxl import Workbook

from wms.core.exceptions import ValidationError
from wms.services.reports import ReportFilters


def book(path, rows, header=("Code", "Bin", "Quantity"), title_rows=0, exported=None):
    wb = Workbook()
    ws = wb.active
    for _ in range(title_rows):
        ws.append(["Stock list 21/11/2026"])
    if exported is not None:
        ws.append([f"Test Ltd - Generated {exported:%d/%m/%Y %H:%M}"])
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def bins(svc):
    return {b.full_code: b for b in svc.locations.bins()}


def test_file_becomes_a_submitted_count_and_approval_sets_the_quantities(svc, sample, tmp_path):
    sample.receive(sample.mdf, 10)
    b1, b2 = sample.bin1.full_code, sample.bin2.full_code
    path = book(tmp_path / "stock.xlsx", [
        ["MDF-18", b1, 7],                 # code case does not matter: 10 -> 7
        ["EDGE-W22", "", "125.5"],         # blank bin -> default bin (B05): 0 -> 125.5
        ["mdf-18", b2, 3],                 # same material, another bin
        ["NOPE-1", b1, 1],                 # unknown code
        ["CAB-600", b1, "2.5"],            # EA is whole units
        ["EDGE-W22", b2, 1],               # duplicate of row 3
        ["MDF-18", "Z-NOWHERE", 1],        # unknown bin
        ["MDF-18", b1, "lots"],            # not a number
        ["MDF-18", b1, -1],                # below zero
        [None, None, None],                # empty row is skipped
    ], title_rows=2)
    plan = svc.stock_import.read(path)
    assert plan.header_row == 3 and len(plan.rows) == 9
    assert [(r.row, r.problem) for r in plan.problems] == [
        (7, "Unknown code"), (8, "Counted in whole EA"), (9, "Same material and bin as row 5"),
        (10, "Unknown bin Z-NOWHERE"), (11, "Quantity is not a number"), (12, "Quantity below zero")]
    assert [(r.code, r.bin_full, r.current, r.qty) for r in plan.good] == [
        ("MDF-18", b1, D("10"), D("7")), ("EDGE-W22", b2, D("0"), D("125.5")), ("mdf-18", b2, D("0"), D("3"))]
    assert "3 will change" in plan.summary() and "6 with a problem" in plan.summary()

    count = svc.stock_import.create_count(plan)
    assert count.status == "SUBMITTED" and len(count.lines) == 3
    svc.stocktake.approve(count.id)
    assert _qty(svc, sample.mdf.id, sample.bin1.id) == D("7")
    assert _qty(svc, sample.mdf.id, sample.bin2.id) == D("3")
    assert _qty(svc, sample.edge.id, sample.bin2.id) == D("125.5")
    adj = svc.stock.adjustments()
    assert len(adj) == 3 and all(a.movement_id for a in adj)
    assert {a.reason_code for a in adj} == {"STOCK_COUNT"}
    assert "STOCK_COUNT_IMPORTED" in [r.action for r in svc.audit.search()]
    # the same file again: nothing to change
    again = svc.stock_import.read(path)
    assert not again.changes and len(again.good) == 3
    c2 = svc.stock_import.create_count(again)
    svc.stocktake.approve(c2.id)
    assert len(svc.stock.adjustments()) == 3


def test_safety_checks_stale_rows_other_bins_cost_and_project_stock(svc, sample, tmp_path):
    """Warehouse review: an old export must not put back stock that has gone out since; a blank bin
    must not double-count other bins; no unit cost and project stock are flagged."""
    from datetime import datetime, timedelta

    from wms.services.goods_in import ReceiptLine
    an_hour_ago = datetime.now() - timedelta(hours=1)
    sample.receive(sample.mdf, 10)                                    # moved after the "export"
    sample.receive(sample.mdf, 4, sample.bin2, number="DN-2")         # MDF now in two bins
    svc.goods_in.post({"dn_number": "DN-P", "supplier_id": sample.supplier.id, "delivery_date": date.today(),
                       "site_id": sample.site.id}, [ReceiptLine(sample.edge.id, D("30"), sample.bin2.id)])
    rows = [["MDF-18", sample.bin1.full_code, 7],      # 1: stale row (bin moved after export)
            ["MDF-18", "", 9],                         # 2: blank bin, but MDF is in 2 bins
            ["CAB-600", sample.bin1.full_code, 2],     # 3: no unit cost
            ["EDGE-W22", sample.bin2.full_code, 30]]   # 4: unchanged
    plan = svc.stock_import.read(book(tmp_path / "s.xlsx", rows, exported=an_hour_ago))
    assert plan.exported_at is not None and "Exported from WMS" in plan.export_note
    by = {r.row: r for r in plan.rows}
    assert by[3].problem.startswith("Stock moved since the file was exported")
    assert by[4].problem == "No bin given, and the material is in 2 bin(s) - give the bin"
    assert by[5].warning == "No unit cost: the added stock is valued at £0" and not by[5].problem
    assert by[6].status == "SAME"
    # project stock: EDGE has 30 for the project; setting the bin to 10 is allowed but flagged
    plan = svc.stock_import.read(book(tmp_path / "e.xlsx", [["EDGE-W22", sample.bin2.full_code, 10]]))
    [r] = plan.rows
    assert r.warning == "On hand would be 10, below project stock 30" and not r.problem
    assert "no export time" in plan.export_note and "1 to check" in plan.summary()


def _qty(svc, product_id, bin_id):
    from wms.services.stock_engine import StockEngine
    with svc.ctx.db.read_session() as s:
        return StockEngine(s, svc.ctx).qty_in_bin(product_id, bin_id)


def test_stock_by_bin_export_reads_back_unchanged(svc, sample, tmp_path):
    from wms.exporters.export import export
    sample.receive(sample.mdf, 12)
    sample.receive(sample.edge, 40, number="DN-2")
    res = svc.reports.run("stock_by_bin", ReportFilters())
    path = export(res, tmp_path / "stock.xlsx", "Test Ltd")
    plan = svc.stock_import.read(path)
    assert not plan.problems and len(plan.good) == 2 and not plan.changes


def test_stock_moved_after_import_blocks_approval(svc, sample, tmp_path):
    sample.receive(sample.mdf, 10)
    plan = svc.stock_import.read(book(tmp_path / "s.xlsx", [["MDF-18", sample.bin1.full_code, 8]]))
    count = svc.stock_import.create_count(plan)
    sample.receive(sample.mdf, 1, number="DN-2")
    with pytest.raises(ValidationError, match="Stock moved"):
        svc.stocktake.approve(count.id)
    assert _qty(svc, sample.mdf.id, sample.bin1.id) == D("11")


def test_files_without_the_columns_or_usable_rows_are_refused(svc, sample, tmp_path):
    with pytest.raises(ValidationError, match="'Code' and 'Quantity'"):
        svc.stock_import.read(book(tmp_path / "a.xlsx", [["x", 1]], header=("Name", "Amount")))
    plan = svc.stock_import.read(book(tmp_path / "b.xlsx", [["NOPE", "", 1]]))
    with pytest.raises(ValidationError, match="No row"):
        svc.stock_import.create_count(plan)
    (tmp_path / "c.xls").write_bytes(b"x")
    with pytest.raises(ValidationError, match="Save As"):
        svc.stock_import.read(tmp_path / "c.xls")


def test_ten_thousand_lines_import_and_approve_quickly(svc, sample, tmp_path):
    """Bulk path: the approval of a 10 000-line count is a few statements, not ~12 per line."""
    from sqlalchemy import event
    units = {u.code: u for u in svc.reference.units()}
    n = 10_000
    with svc.ctx.db.session() as s:
        from wms.db.models import Product
        s.add_all([Product(item_type="RAW_MATERIAL", internal_code=f"BULK-{i:05d}", name=f"Bulk {i}",
                           unit_id=units["EA"].id, default_bin_id=sample.bin1.id) for i in range(n)])
    path = book(tmp_path / "big.xlsx", [[f"BULK-{i:05d}", "", i % 7 + 1] for i in range(n)])
    t0 = time.perf_counter()
    plan = svc.stock_import.read(path)
    assert len(plan.changes) == n
    count = svc.stock_import.create_count(plan)
    statements = []
    engine = svc.ctx.db.engine
    listener = lambda conn, cur, stmt, *a: statements.append(" ".join(stmt.split()[:3])[:60])  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        svc.stocktake.approve(count.id)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    took = time.perf_counter() - t0
    assert len(svc.stock.adjustments(limit=20_000)) == n
    from collections import Counter
    if engine.dialect.name == "postgresql":    # the server over VPN: round trips are what count
        assert len(statements) < 400, Counter(statements).most_common(8)   # was ~12 per line = 120 000
    assert took < 120, took


# ---- the screen -------------------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture


def test_import_from_the_stocktake_screen_and_approve(ui, sample, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QDialog

    from wms.ui.dialogs import stock_import as si
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 10)
    path = book(tmp_path / "s.xlsx", [["MDF-18", sample.bin1.full_code, 6], ["NOPE", "", 1]])
    win.go("stocktake")
    page = win.page("stocktake")
    shown = {}

    def run_dialog(dlg):
        dlg._load(str(path))
        shown["summary"] = dlg.summary.text()
        shown["rows"] = dlg.table.model().rowCount()
        dlg.show_filter.setCurrentIndex(2)          # problems only
        shown["problems"] = [r.problem for r in dlg.table.items()]
        dlg.create()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(si.StockImportDialog, "exec", run_dialog)
    page.import_excel()
    assert "1 will change" in shown["summary"] and shown["rows"] == 2 and shown["problems"] == ["Unknown code"]
    c = page.current
    assert c.status == "SUBMITTED" and len(c.lines) == 1
    assert page.lines.item(0, 5).text() == "6"            # read-only plain cells
    assert page.counts.items()[0].line_stats == (1, 1, 1)
    page.approve()
    assert {r.product.id: r for r in app.services.products.list()}[sample.mdf.id].on_hand == 6
    assert not _errors(msgs), msgs
