"""v1.35 (Ihor 21.11.2026): no reservations - only stock split by project, and issue.

* Project stock comes only from Goods In for the project; nothing is reserved by hand.
* Goods Out and production for the project use its own stock first.
* Old hand reservations stay as project stock.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import hand_reserve

from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.production import MaterialInput

D = Decimal
TODAY = date.today()


def _for_project(svc, s, qty, number="DN-P1"):
    return svc.goods_in.post({"dn_number": number, "supplier_id": s.supplier.id, "delivery_date": TODAY,
                              "site_id": s.site.id}, [ReceiptLine(s.mdf.id, D(qty), s.bin1.id)])


def _held(svc, s):
    return {x.code: x.total for x in svc.reservations.project_stock(s.site.id)}


def test_production_for_a_project_uses_its_stock_and_reversal_puts_it_back(svc, sample):
    _for_project(svc, sample, "10")
    sample.receive(sample.mdf, 5, sample.bin1, number="DN-GEN")  # general stock
    order = svc.production.post({"finished_product_id": sample.cabinet.id, "quantity": "1", "production_date": TODAY,
                                 "site_id": sample.site.id}, [MaterialInput(sample.mdf.id, "12", sample.bin1.id)])
    assert _held(svc, sample) == {}  # 10 from the project's stock, 2 from general stock
    used = sorted((ln.quantity, ln.reservation_id is not None) for ln in order.lines)
    assert used == [(D(2), False), (D(10), True)]
    row = svc.products.row(sample.mdf.id)
    assert (row.on_hand, row.reserved, row.available) == (D(3), D(0), D(3))
    svc.production.reverse(order.id, "Entered twice")
    assert _held(svc, sample) == {"MDF-18": D(10)}
    assert svc.ledger.critical_issues() == []
    assert "Reservation status" not in {i.check for i in svc.ledger.integrity_check()}


def test_production_without_a_project_leaves_project_stock_alone(svc, sample):
    _for_project(svc, sample, "10")
    sample.receive(sample.mdf, 5, sample.bin1, number="DN-GEN")
    order = svc.production.post({"finished_product_id": sample.cabinet.id, "quantity": "1", "production_date": TODAY},
                                [MaterialInput(sample.mdf.id, "4", sample.bin1.id)])
    assert all(ln.reservation_id is None for ln in order.lines)
    assert _held(svc, sample) == {"MDF-18": D(10)}


def test_old_hand_reservation_is_project_stock(svc, sample):
    sample.receive(sample.mdf, 20)
    old = hand_reserve(svc, {"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "8"})
    assert _held(svc, sample) == {"MDF-18": D(8)}
    gi = svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "5")])
    assert {ln.reservation_id for ln in gi.lines} == {old.reservation.id}
    assert svc.reservations.return_to_main(sample.site.id, [(sample.mdf.id, "3")], "Spare").startswith("PSR-")
    assert _held(svc, sample) == {}


def test_goods_out_general_stock_to_a_project_needs_no_reason(svc, sample):
    sample.receive(sample.mdf, 20)
    gi = svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, "20")])
    assert gi.lines and svc.products.row(sample.mdf.id).on_hand == 0


@pytest.fixture
def win(qtbot, svc):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    w = MainWindow(App(svc.ctx))
    qtbot.addWidget(w)
    return w


def test_no_reserve_buttons_anywhere(svc, sample, win):
    from PySide6.QtWidgets import QPushButton

    from wms.ui.dialogs.product_dialog import ProductDialog
    from wms.ui.dialogs.site_dialog import SiteDialog
    for dlg in (SiteDialog(win.app, sample.site.id), ProductDialog(win.app, sample.mdf.id)):
        texts = [b.text() for b in dlg.findChildren(QPushButton)]
        assert not [t for t in texts if t.startswith("Reserve")], texts
    win.go("reservations")
    page = win.page("reservations")
    assert page.title == "Project Stock"
    assert not [b.text() for b in page.findChildren(QPushButton) if b.text().startswith("Reserve")]


def test_completing_a_project_with_stock_left_asks_why(svc, sample, win, monkeypatch):
    import wms.ui.widgets.common as common
    from wms.ui.dialogs.site_dialog import SiteDialog
    _for_project(svc, sample, "6")
    asked = []
    monkeypatch.setattr(common, "ask_text", lambda parent, title, text, *a, **k: asked.append(text) or "Spare boards")
    dlg = SiteDialog(win.app, sample.site.id)
    dlg.complete()
    assert "MDF-18" in asked[0] and "still has project stock" in asked[0]
    assert svc.sites.get(sample.site.id).status == "COMPLETED"
    [psr] = svc.reservations.project_releases(sample.site.id)
    assert psr.quantity == D(6) and "Spare boards" in psr.reason


def _other_site(svc):
    return svc.sites.save(None, {"project_number": "P-2002", "name": "Surrey County Hall", "status": "ACTIVE"})


def test_production_may_not_use_another_projects_stock_without_a_manager(svc, sample):
    from wms.core.exceptions import ValidationError
    _for_project(svc, sample, "10")  # all of it belongs to P-1001
    other = _other_site(svc)
    for site_id in (other.id, None):  # for another project, or for no project
        hdr = {"finished_product_id": sample.cabinet.id, "quantity": "1", "production_date": TODAY,
               "site_id": site_id}
        with pytest.raises(ValidationError, match="belongs to other projects"):
            svc.production.post(hdr, [MaterialInput(sample.mdf.id, "4", sample.bin1.id)])
    assert _held(svc, sample) == {"MDF-18": D(10)}
    order = svc.production.post({"finished_product_id": sample.cabinet.id, "quantity": "1",
                                 "production_date": TODAY, "site_id": other.id},
                                [MaterialInput(sample.mdf.id, "4", sample.bin1.id)],
                                other_projects_reason="Sample unit for the client")
    assert _held(svc, sample) == {"MDF-18": D(6)}
    [psr] = svc.reservations.project_releases(sample.site.id)
    assert psr.quantity == D(4) and "Sample unit" in psr.reason
    svc.production.reverse(order.id, "Wrong product")
    assert _held(svc, sample) == {"MDF-18": D(10)}
    assert svc.ledger.critical_issues() == []


def test_screens_ask_a_manager_why_and_stop_everyone_else(svc, sample, win, monkeypatch):
    import wms.ui.widgets.common as common
    _for_project(svc, sample, "10")
    other = _other_site(svc)
    lines = [(sample.mdf.id, "4")]
    asked = []
    monkeypatch.setattr(common, "ask_text", lambda parent, title, text, *a, **k: asked.append(text) or "Urgent")
    assert common.ask_other_projects(win, win.app, other.id, lines) == (True, "Urgent")
    assert "P-1001" in asked[0] and "history" in asked[0]
    assert common.ask_other_projects(win, win.app, sample.site.id, lines) == (True, None)  # its own stock
    warned = []
    monkeypatch.setattr(common.QMessageBox, "warning", lambda parent, title, text: warned.append(text))
    monkeypatch.setattr(win.app, "can", lambda perm: False)
    assert common.ask_other_projects(win, win.app, None, lines) == (False, None)
    assert "Nothing was posted" in warned[0]
