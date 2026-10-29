"""Phase 1, Stage 8 - screens: Product Passport, Dashboard counters, keyboard shortcuts."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.purchasing import POLineInput
from wms.services.stock_ops import ReturnLineInput

D = Decimal
TODAY = date.today()


def _png(path):
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d00000000"
        "49454e44ae426082"))
    return path


def test_passport_purchasing_projects_and_documents(svc, sample, tmp_path):
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY - timedelta(days=5),
                                    "expected_date": TODAY - timedelta(days=1)}, [POLineInput(sample.mdf.id, "50")])
    svc.purchasing.mark_ordered(po.id)
    dn = svc.goods_in.post({"purchase_order_id": po.id, "dn_number": "DN-77", "delivery_date": TODAY},
                           [ReceiptLine(sample.mdf.id, D("30"))])
    svc.attachments.add("DELIVERY_NOTE", dn.id, _png(tmp_path / "dn77.png"))
    spec = tmp_path / "spec.pdf"
    spec.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 200)
    svc.attachments.add("PRODUCT", sample.mdf.id, spec, "DOCUMENT")
    svc.attachments.add("PRODUCT", sample.mdf.id, _png(tmp_path / "photo.png"), "PHOTO")  # a photo, not a document
    second = svc.sites.save(None, {"project_number": "P-2002", "name": "Hotel", "status": "ACTIVE"})
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "8")])
    gi = svc.goods_out.post({"site_id": second.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "4")])
    svc.goods_out.reverse(gi.id, "wrong project")
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": sample.site.id},
                          [ReturnLineInput(sample.mdf.id, "3", sample.bin1.id)])

    pp = svc.products.passport(sample.mdf.id)
    last = pp.last_delivery
    assert (last.dn_number, last.supplier, last.quantity) == ("DN-77", "Egger UK Ltd", D("30"))
    assert [(o.po_number, o.ordered, o.received, o.outstanding, o.is_overdue) for o in pp.open_orders] == [
        (po.po_number, D("50"), D("30"), D("20"), True)]
    assert pp.outstanding == D("20") == svc.products.row(sample.mdf.id).incoming
    uses = {u.project: (u.issued, u.returned, u.net) for u in pp.projects}
    assert uses == {"P-1001 - Riverside Apartments": (D("8"), D("3"), D("5"))}  # reversed issue nets to nothing
    assert [(d.source, d.attachment.original_name) for d in pp.documents] == [
        ("Product", "spec.pdf"), ("Delivery Note DN-77", "dn77.png")]


def test_dashboard_counters_open_exactly_their_records(svc, sample):
    from wms.services.search import SearchCriteria
    d = svc.dashboard.load()
    assert [a.key for a in d.actions] == ["low_stock", "overdue_po", "partial_po", "stocktake", "discrepancies",
                                          "no_bin", "no_photo"]
    # MDF has a minimum of 10 and no stock: reorder, and out of stock
    low = d.action("low_stock")
    assert low.count == len(svc.products.low_stock()) and low.level == "danger"
    for key in ("no_bin", "no_photo"):
        a = d.action(key)
        assert a.count == svc.search.products(SearchCriteria.from_dict(a.preset)).total
    assert d.action("no_bin").count == 0 and d.action("no_photo").count == 3
    # TODAY counts posted documents and transfers, not reversals
    sample.receive(sample.mdf, 20)
    tr = svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "5")
    svc.ledger.reverse_movement(tr.movement_id, "DATA_ENTRY_ERROR", "wrong bin")
    d = svc.dashboard.load()
    assert d.today == {"Goods In": 1, "Goods Out": 0, "Transfers": 1, "Returns": 0}
    assert d.action("low_stock").count == 0
    assert d.recent_movements[0].reversal_of_id == tr.movement_id  # newest first


def test_dashboard_stocktake_and_partial_counters(svc, sample):
    from wms.services.stocktake import CountEntry
    sample.receive(sample.mdf, 50)
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY},
                             [POLineInput(sample.edge.id, "100")])
    svc.purchasing.mark_ordered(po.id)
    svc.goods_in.post({"purchase_order_id": po.id, "dn_number": "P1", "delivery_date": TODAY},
                      [ReceiptLine(sample.edge.id, D("60"))])
    count = svc.stocktake.create(bin_id=sample.bin1.id)
    svc.stocktake.record_counts(count.id, [CountEntry(ln.id, "48") for ln in count.lines])
    assert svc.dashboard.load().action("stocktake").count == 0  # not submitted yet
    svc.stocktake.submit(count.id)
    d = svc.dashboard.load()
    assert d.action("stocktake").count == 1 and d.action("partial_po").count == 1
    assert [r.po.id for r in svc.purchasing.list(status="PARTIALLY_RECEIVED")] == [po.id]


def test_dashboard_respects_permissions(svc, sample):
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op1", "full_name": "Op One", "role_id": roles["OPERATIVE"].id,
                           "must_change_password": False}, "secret123")
    svc.auth.login("op1", "secret123")
    from wms.core.permissions import P
    keys = {a.key for a in svc.dashboard.load().actions}
    # each counter shows only to someone allowed to open the list behind it
    assert ("overdue_po" in keys) == svc.ctx.has(P.PURCHASING_VIEW)
    assert ("stocktake" in keys) == svc.ctx.has(P.STOCKTAKE_COUNT)
    assert ("low_stock" in keys) == svc.ctx.has(P.PRODUCTS_VIEW)
    assert not svc.ctx.has(P.USERS_MANAGE)


def test_passport_and_dashboard_on_screen(qtbot, svc, sample, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    from wms.ui.app import App
    from wms.ui.dialogs.product_dialog import ProductDialog
    from wms.ui.main_window import MainWindow
    msgs = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: msgs.append(a))
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: msgs.append(a))
    monkeypatch.setattr(QDialog, "exec", lambda dlg: (dlg.show(), dlg.close(), 0)[-1])
    sample.receive(sample.mdf, 40)
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "6")])
    app = App(svc.ctx)
    win = MainWindow(app)
    qtbot.addWidget(win)
    win.show()
    assert "F2 Goods In" in win.keys_hint.text() and "F4 Transfer" in win.keys_hint.text()

    card = ProductDialog(app, sample.mdf.id)
    qtbot.addWidget(card)
    assert card.ov_fields["supplier_code"].text() == "EG-MDF18"  # Product Number
    assert "DN-1" in card.ov_last.text() and "Egger UK Ltd" in card.ov_last.text()
    assert [u.project for u in card.ov_projects.items()] == ["P-1001 - Riverside Apartments"]
    assert card.ov_states["minimum"].text() == "10 SHEET"
    assert card.tabs.tabText(card.tabs.indexOf(card.files)) == "Documents"
    # "Goods Out" on the passport opens Goods Out with this product on a line
    card._start("goods_out")
    page = win.page("goods_out")
    assert page.isVisible()
    assert sample.mdf.id in [ln.product_id for ln in page.lines.values(validate=False)]
    assert not msgs, msgs


def test_documents_on_project_supplier_and_goods_out(svc, sample, tmp_path, qtbot):
    """Spec 26: files can go on a project, a supplier and a Goods Out too; a record with files is
    archived, never deleted together with them."""
    from wms.ui.app import App
    from wms.ui.dialogs.documents import GoodsIssueDialog
    from wms.ui.dialogs.reference_dialogs import SupplierDialog
    from wms.ui.dialogs.site_dialog import SiteDialog
    sample.receive(sample.mdf, 10)
    gi = svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "2")])
    new_site = svc.sites.save(None, {"project_number": "P-3003", "name": "Unused", "status": "ACTIVE"})
    new_sup = svc.reference.save_supplier(None, {"code": "NEW", "name": "New Supplier"})
    assert svc.sites.removal_plan(new_site.id).permanent and svc.reference.supplier_removal_plan(new_sup.id).permanent
    for etype, eid in (("SITE", new_site.id), ("SUPPLIER", new_sup.id), ("GOODS_ISSUE", gi.id)):
        svc.attachments.add(etype, eid, _png(tmp_path / f"{etype}.png"))
    assert not svc.sites.removal_plan(new_site.id).permanent
    assert not svc.reference.supplier_removal_plan(new_sup.id).permanent
    app = App(svc.ctx)
    for dlg in (SiteDialog(app, new_site.id), SupplierDialog(app, new_sup.id), GoodsIssueDialog(app, gi.id)):
        qtbot.addWidget(dlg)
        assert dlg.docs.list.count() == 1
