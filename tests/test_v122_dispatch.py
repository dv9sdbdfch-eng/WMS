"""v1.22: finished products leave by Dispatch, which makes a numbered Delivery Note."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal as D

import pytest

from wms.core.exceptions import AlreadyPostedError, InsufficientStockError, ValidationError
from wms.services.dispatch import DispatchLineInput as L
from wms.services.goods_out import IssueLine

TODAY = date.today()


@pytest.fixture
def fg(svc, sample):
    sample.receive(sample.cabinet, 5, sample.bin1, number="DN-FG1")
    return sample


def stock(svc, p):
    return svc.products.row(p.id).on_hand


def test_dispatch_posts_delivery_note_and_reversal(svc, fg, tmp_path):
    token = uuid.uuid4().hex
    d = svc.dispatch.post({"customer": "Acme Homes", "site_id": fg.site.id, "dispatch_date": TODAY,
                           "delivery_address": "1 Riverside Way\nLeeds LS1 1AA", "contact": "Jo, 07700 900000",
                           "vehicle": "Van 2 / Sam", "reference": "PO-778", "client_token": token},
                          [L(fg.cabinet.id, "2")])
    assert d.dispatch_number == "DEL-000001" and d.status == "POSTED"
    assert stock(svc, fg.cabinet) == D(3)
    assert [(x.quantity, x.bin_id) for x in d.lines] == [(D(2), fg.bin1.id)]
    with pytest.raises(AlreadyPostedError):  # the same form twice
        svc.dispatch.post({"customer": "Acme", "dispatch_date": TODAY, "client_token": token}, [L(fg.cabinet.id, 1)])
    pdf = svc.dispatch.delivery_note_pdf(d.id, tmp_path / "dn.pdf", "Store Ltd")
    assert pdf.read_bytes()[:4] == b"%PDF" and pdf.stat().st_size > 1500
    # a dispatch to a project is not material issued to it
    from wms.services.materials import MaterialFilter
    rows = [r for r in svc.materials.page(MaterialFilter(site_id=fg.site.id)) if r.id == fg.cabinet.id]
    assert not rows or rows[0].issued == 0
    assert svc.dispatch.search("PO-778")[0].id == d.id
    assert svc.dispatch.search("Riverside Way")[0].id == d.id
    svc.dispatch.reverse(d.id, "entered twice")
    assert stock(svc, fg.cabinet) == D(5) and svc.dispatch.get(d.id).status == "REVERSED"
    with pytest.raises(ValidationError):
        svc.dispatch.reverse(d.id, "again")


def test_dispatch_rules(svc, fg):
    with pytest.raises(ValidationError, match="customer or a project"):
        svc.dispatch.post({"dispatch_date": TODAY}, [L(fg.cabinet.id, 1)])
    with pytest.raises(ValidationError, match="material"):
        svc.dispatch.post({"customer": "X", "dispatch_date": TODAY}, [L(fg.mdf.id, 1)])
    with pytest.raises(InsufficientStockError):  # never below zero
        svc.dispatch.post({"customer": "X", "dispatch_date": TODAY}, [L(fg.cabinet.id, 6)])
    with pytest.raises(ValidationError, match="whole"):
        svc.dispatch.post({"customer": "X", "dispatch_date": TODAY}, [L(fg.cabinet.id, "1.5")])
    assert stock(svc, fg.cabinet) == D(5)  # nothing posted


def test_goods_out_refuses_finished_products(svc, fg):
    with pytest.raises(ValidationError, match="Dispatch"):
        svc.goods_out.post({"site_id": fg.site.id, "issue_date": TODAY}, [IssueLine(fg.cabinet.id, "1")])


def test_status_dispatched_when_none_left_and_ready_after_reversal(svc, fg):
    d = svc.dispatch.post({"customer": "X", "dispatch_date": TODAY}, [L(fg.cabinet.id, 5)])
    assert svc.products.get(fg.cabinet.id).finished_info.status == "DISPATCHED"
    svc.dispatch.reverse(d.id, "mistake")
    assert svc.products.get(fg.cabinet.id).finished_info.status == "READY"


def test_stock_options_list_finished_in_stock_only(svc, fg):
    opts = svc.dispatch.stock_options()
    assert [pid for _, pid in opts] == [fg.cabinet.id]
    assert "in stock 5" in opts[0][0]


# --- screens ----------------------------------------------------------------------------------------

@pytest.fixture
def win(qtbot, svc):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    w = MainWindow(App(svc.ctx))
    qtbot.addWidget(w)
    return w


def test_dispatch_screen_posts_and_dialog_prints(svc, fg, win, tmp_path, monkeypatch):
    win.go("dispatch")
    page = win.page("dispatch")
    assert fg.cabinet.id in page.lines._product_ids and fg.mdf.id not in page.lines._product_ids
    page.site.set_value(fg.site.id)
    page._site_picked()
    assert page.customer.text() == "Acme Homes"  # from the project
    page.lines.clear()
    page.lines.add_row(fg.cabinet.id, D(1))
    d = page.post(open_note=False)
    assert d is not None and d.dispatch_number.startswith("DEL-")
    assert stock(svc, fg.cabinet) == D(4)
    from wms.ui.pages.dispatch import DispatchDialog, delivery_note_pdf
    dlg = DispatchDialog(win.app, d.id, win)
    assert "Delivery Note" in dlg.windowTitle()
    out = delivery_note_pdf(dlg, win.app, d.id, tmp_path / "x.pdf", open_after=False)
    assert out.exists()
    # Goods Out no longer offers finished products
    win.go("goods_out")
    gout = win.page("goods_out")
    assert fg.cabinet.id not in gout.lines._product_ids and fg.mdf.id in gout.lines._product_ids
