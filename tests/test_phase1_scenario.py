"""Phase 1 specification, section 40: the required end-to-end scenario, step by step.

Everything is created from nothing (supplier EGGER, product H3311, bins B04 / B05, Project A) and
after every step the stock is reconciled against the movement ledger: calculated ledger quantity =
current stock for every product, bin and warehouse, with no critical integrity problem.
Steps 22-24 back the database up, restore the backup into a separate data folder (a second
"test environment" that shares nothing with the live one) and compare stock and history there.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from openpyxl import load_workbook

from wms.config import AppConfig
from wms.db.engine import Database
from wms.services import Services
from wms.services.backup import swap_in
from wms.services.base import AppContext
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.reports import ReportFilters
from wms.services.stock_ops import ReturnLineInput
from wms.services.stocktake import CountEntry

D = Decimal
TODAY = date.today()


def reconciled(svc) -> None:
    """Spec 2 and 40: Calculated Ledger Quantity = Current Stock Quantity, everywhere."""
    critical = [str(i) for i in svc.ledger.critical_issues()]
    assert critical == [], critical
    assert svc.backup.integrity_check().ok


def bin_qty(svc, product, bin_) -> Decimal:
    return sum((b.quantity for b in svc.ledger.product_balance(product.id).by_bin if b.bin_id == bin_.id), D("0"))


def snapshot(svc) -> tuple[list, list]:
    """Stock per product and bin, and every ledger movement as the history screen shows it."""
    with svc.ctx.db.read_session() as s:
        from sqlalchemy import select

        from wms.db.models import StockLevel
        levels = sorted((lv.product_id, lv.bin_id, lv.quantity) for lv in s.scalars(select(StockLevel)))
    history = [(m.id, m.occurred_at, m.type_label, m.product_code, m.product_name, m.quantity, m.from_bin_code,
                m.to_bin_code, m.site_label, m.supplier_label, m.doc_number, m.user_label)
               for m in svc.stock.movement_lines()]
    return levels, history


def test_required_scenario(svc, tmp_path, monkeypatch):
    units = {u.code: u for u in svc.reference.units()}
    main = next(loc for loc in svc.locations.tree() if loc.code == "MAIN")

    # 1. Create Supplier EGGER.
    egger = svc.reference.save_supplier(None, {"code": "EGGER", "name": "EGGER"})
    reconciled(svc)
    # 2. Create Product H3311.
    h3311 = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "H3311",
                                     "name": "Egger Halifax Oak H3311", "unit_id": units["SHEET"].id,
                                     "supplier_id": egger.id, "unit_cost": "41.20", "min_stock": "10"})
    reconciled(svc)
    # 3. Create Bin B04 (and B05, needed for the transfer in step 10).
    b04 = svc.locations.save_bin(None, {"location_id": main.id, "code": "B04"})
    b05 = svc.locations.save_bin(None, {"location_id": main.id, "code": "B05"})
    project_a = svc.sites.save(None, {"project_number": "PA-001", "name": "Project A", "status": "ACTIVE"})
    reconciled(svc)

    # 4. Receive 100 units into B04.
    dn = svc.goods_in.post({"dn_number": "DN-1047", "supplier_id": egger.id, "delivery_date": TODAY},
                           [ReceiptLine(h3311.id, D("100"), b04.id)])
    reconciled(svc)
    # 5. Verify On Hand = 100.
    row = svc.products.row(h3311.id)
    assert row.on_hand == D("100") and bin_qty(svc, h3311, b04) == D("100")

    # 6. Reserve 30 for Project A.
    res = svc.reservations.reserve({"product_id": h3311.id, "site_id": project_a.id, "quantity": "30"})
    reconciled(svc)
    # 7. Verify Available = 70 (a reservation is not a Goods Out: On Hand is unchanged).
    row = svc.products.row(h3311.id)
    assert (row.on_hand, row.reserved, row.available) == (D("100"), D("30"), D("70"))

    # 8. Issue 20 of the reservation.
    gi = svc.goods_out.post({"site_id": project_a.id, "issue_date": TODAY, "reference": "JOB-A1"},
                            [IssueLine(h3311.id, D("20"), b04.id)])
    assert gi.lines[0].reservation_id == res.reservation.id
    reconciled(svc)
    # 9. Verify On Hand = 80 (10 still reserved, so Available stays 70).
    row = svc.products.row(h3311.id)
    assert (row.on_hand, row.reserved, row.available) == (D("80"), D("10"), D("70"))

    # 10. Transfer 40 from B04 to B05.
    svc.stock.transfer(h3311.id, b04.id, b05.id, "40")
    reconciled(svc)
    assert (bin_qty(svc, h3311, b04), bin_qty(svc, h3311, b05)) == (D("40"), D("40"))

    # 11. Return 5 from Project A (back into B04).
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": project_a.id},
                          [ReturnLineInput(h3311.id, "5", b04.id)])
    reconciled(svc)
    assert bin_qty(svc, h3311, b04) == D("45")

    # 12. Perform stocktake (of bin B04).  13. Record a -2 discrepancy.
    count = svc.stocktake.create(bin_id=b04.id, description="Spec 40 count")
    line = next(ln for ln in count.lines if ln.product_id == h3311.id)
    assert line.system_qty == D("45")
    svc.stocktake.record_counts(count.id, [CountEntry(line.id, "43")])
    assert bin_qty(svc, h3311, b04) == D("45")  # counting alone never changes stock
    svc.stocktake.submit(count.id)
    svc.stocktake.approve(count.id, "Two sheets missing")
    reconciled(svc)
    assert bin_qty(svc, h3311, b04) == D("43")
    adj = [m for m in svc.stock.movement_lines(product_id=h3311.id) if m.movement_type == "STOCKTAKE_ADJUSTMENT"]
    assert [(m.quantity, m.from_bin_code.endswith("B04")) for m in adj] == [(D("2"), True)]

    # 14. Create a reversal of an incorrect transaction (3 sheets put into B05 by mistake).
    wrong = svc.stock.transfer(h3311.id, b04.id, b05.id, "3")
    rev = svc.ledger.reverse_movement(wrong.movement_id, "DATA_ENTRY_ERROR", "Wrong bin scanned")
    reconciled(svc)
    assert rev.reversal_of_id == wrong.movement_id
    assert svc.ledger.reversal_of(wrong.movement_id).reason == "Wrong bin scanned"
    assert bin_qty(svc, h3311, b04) == D("43")

    # 15. Move remaining stock out of B04.  16. Verify B04 = 0.
    svc.stock.transfer(h3311.id, b04.id, b05.id, "43")
    reconciled(svc)
    assert bin_qty(svc, h3311, b04) == D("0") and bin_qty(svc, h3311, b05) == D("83")
    assert svc.products.row(h3311.id).on_hand == D("83")

    # 17. Remove B04 from active use.
    plan = svc.locations.remove_bin(b04.id, "Racking taken down")
    reconciled(svc)
    assert not plan.permanent and plan.history == 8  # it has history, so it is archived, not deleted
    assert b04.id not in [b.id for b in svc.locations.bins()]  # gone from new transaction selectors

    # 18. Verify old transactions still display B04.
    history = svc.stock.movement_lines(product_id=h3311.id)
    shown = [m.from_bin_code + " " + m.to_bin_code for m in history]
    # receipt, issue, transfer, return, count, wrong transfer and its reversal, move-out
    assert sum("B04" in x for x in shown) == 8
    assert svc.locations.find_bin("B04", include_inactive=True).id == b04.id  # historical search still finds it
    report = svc.reports.run("goods_in", ReportFilters())
    assert report.rows[0][1] == "DN-1047" and report.rows[0][7].endswith("B04")

    # 19. Complete Project A.
    svc.sites.set_status(project_a.id, "COMPLETED")
    reconciled(svc)
    assert project_a.id not in [s.id for s in svc.sites.open_sites()]  # not offered for new Goods Out
    assert svc.products.row(h3311.id).reserved == D("0")  # its unissued reservation was released

    # 20. Verify Project A still appears historically.
    summary = svc.sites.material_summary(project_a.id)
    assert [(r.product.internal_code, r.issued, r.returned, r.net) for r in summary] == [
        ("H3311", D("20"), D("5"), D("15"))]
    assert {m.site_label for m in svc.stock.movement_lines(site_id=project_a.id)} == {"PA-001 - Project A"}
    assert [u.project for u in svc.products.passport(h3311.id).projects] == ["PA-001 - Project A"]

    # 21. Export Product history.
    from wms.exporters.export import export_xlsx
    from wms.ui.dialogs.product_dialog import history_report

    class _App:  # history_report only needs the services
        services = svc
    result = history_report(_App, h3311.id)
    out = export_xlsx(result, tmp_path / "H3311_history.xlsx", "Test Ltd")
    ws = load_workbook(out).active
    data_rows = [r for r in ws.iter_rows(min_row=6, values_only=True) if r[2] == "H3311"]
    assert len(data_rows) == len(history) == 8
    assert {r[1] for r in data_rows} >= {"Goods In", "Goods Out", "Bin Transfer", "Reversal of Bin Transfer"}

    # 22. Backup database.
    before = snapshot(svc)
    backup = svc.backup.create(note="Spec 40 step 22")
    assert backup.is_file()
    reconciled(svc)

    # 23. Restore backup into a test environment (its own data folder, nothing shared).
    with monkeypatch.context() as m:
        m.setenv("WMS_DATA_DIR", str(tmp_path / "test-environment"))
        cfg2 = AppConfig.load()
    cfg2.ensure_dirs()
    assert cfg2.db_path != svc.ctx.config.db_path
    swap_in(cfg2, backup)
    db2 = Database(cfg2.resolved_database_url)
    db2.migrate()
    try:
        restored = Services(AppContext(db=db2, config=cfg2))
        restored.auth.login("admin", "admin")
        # 24. Verify stock and transaction history remain identical.
        reconciled(restored)
        after = snapshot(restored)
        assert after[0] == before[0]
        assert after[1] == before[1]
        assert restored.products.row(h3311.id).on_hand == D("83")
    finally:
        db2.dispose()
    assert snapshot(svc) == before  # the live data was not touched by the restore test
    assert dn.id  # the delivery note from step 4 is still the first document
