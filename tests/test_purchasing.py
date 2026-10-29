"""Phase 1, Stage 3 - purchasing: purchase orders, partial deliveries, discrepancies, incoming stock."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from wms.core.exceptions import AlreadyPostedError, PermissionDeniedError, ValidationError
from wms.services.goods_in import ReceiptLine
from wms.services.purchasing import POLineInput

D = Decimal
TODAY = date.today()


def reconciled(svc):
    critical = [str(i) for i in svc.ledger.critical_issues()]
    assert critical == [], critical
    warnings = [str(i) for i in svc.ledger.integrity_check() if i.check == "Purchase order status"]
    assert warnings == [], warnings


def order(svc, sample, lines, **header):
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY, **header}, lines)
    return svc.purchasing.mark_ordered(po.id)


def deliver(svc, po, number, lines, **header):
    return svc.goods_in.post({"purchase_order_id": po.id, "dn_number": number, "delivery_date": TODAY, **header},
                             lines)


def view(svc, po):
    v = svc.purchasing.get(po.id)
    return v.po.status, [(lv.ordered, lv.received, lv.outstanding) for lv in v.lines]


def test_order_delivered_in_two_parts(svc, sample):
    mdf, edge = sample.mdf, sample.edge
    draft = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY,
                                       "expected_date": TODAY + timedelta(days=5), "site_id": sample.site.id,
                                       "supplier_reference": "Quote 551"},
                                [POLineInput(mdf.id, "100", "24.50"), POLineInput(edge.id, "500", "0.18")])
    assert draft.po_number == "PO-000001" and draft.status == "DRAFT"
    assert draft.site_label.startswith("P-1001")
    assert svc.purchasing.incoming() == {}  # a draft is not on order yet
    with pytest.raises(ValidationError, match="cannot be received"):
        deliver(svc, draft, "DN-1", [ReceiptLine(mdf.id, "10")])

    po = svc.purchasing.mark_ordered(draft.id)
    assert po.status == "ORDERED"
    assert svc.purchasing.incoming() == {mdf.id: D("100"), edge.id: D("500")}

    # First delivery: 60 sheets only, own delivery note and own stock movements
    dn1 = deliver(svc, po, "DN-1", [ReceiptLine(mdf.id, "60")])
    assert dn1.purchase_order_id == po.id and dn1.po_reference == "PO-000001"
    assert dn1.supplier_id == sample.supplier.id  # taken from the order
    assert view(svc, po) == ("PARTIALLY_RECEIVED", [(D("100"), D("60"), D("40")), (D("500"), D("0"), D("500"))])
    assert svc.purchasing.incoming() == {mdf.id: D("40"), edge.id: D("500")}
    reconciled(svc)

    # Second delivery: the rest
    dn2 = deliver(svc, po, "DN-2", [ReceiptLine(mdf.id, "40"), ReceiptLine(edge.id, "500")])
    assert view(svc, po)[0] == "RECEIVED"
    assert svc.purchasing.incoming() == {}
    assert svc.ledger.product_balance(mdf.id).on_hand == D("100")
    v = svc.purchasing.get(po.id)
    assert [d.dn_number for d in v.deliveries] == ["DN-1", "DN-2"] and v.discrepancies == []
    with pytest.raises(ValidationError, match="cannot be received"):
        deliver(svc, po, "DN-3", [ReceiptLine(mdf.id, "1")])
    reconciled(svc)

    # Reversing a delivery puts it back on order
    svc.goods_in.reverse(dn2.id, "Entered against the wrong order", reason_code="DATA_ENTRY_ERROR")
    assert view(svc, po) == ("PARTIALLY_RECEIVED", [(D("100"), D("60"), D("40")), (D("500"), D("0"), D("500"))])
    assert svc.purchasing.incoming() == {mdf.id: D("40"), edge.id: D("500")}
    assert dn1.id in {d.id for d in svc.purchasing.get(po.id).deliveries}
    reconciled(svc)


def test_over_delivery_and_wrong_product_are_recorded_automatically(svc, sample):
    po = order(svc, sample, [POLineInput(sample.mdf.id, "20")])
    dn = deliver(svc, po, "DN-9", [ReceiptLine(sample.mdf.id, "25"), ReceiptLine(sample.edge.id, "50")])
    assert svc.ledger.product_balance(sample.mdf.id).on_hand == D("25")  # everything that arrived is in stock
    problems = {d.reason_code: d for d in svc.purchasing.discrepancies(delivery_note_id=dn.id)}
    over, wrong = problems["OVER_DELIVERED"], problems["INCORRECT_DELIVERY"]
    assert (over.expected_qty, over.received_qty, over.quantity) == (D("20"), D("25"), D("5"))
    assert wrong.product_id == sample.edge.id and wrong.quantity == D("50") and wrong.is_automatic
    assert {over.status, wrong.status} == {"OPEN"} and over.purchase_order_id == po.id
    assert view(svc, po)[0] == "RECEIVED"
    alerts = [a.text for a in svc.dashboard.load().alerts]
    assert any("2 delivery problem(s)" in a for a in alerts)
    # Reversing the delivery closes the problems recorded for it
    svc.goods_in.reverse(dn.id, "wrong supplier paperwork")
    assert {d.status for d in svc.purchasing.discrepancies(delivery_note_id=dn.id)} == {"RESOLVED"}
    assert view(svc, po)[0] == "ORDERED"
    reconciled(svc)


def test_same_product_on_two_lines_fills_them_in_order(svc, sample):
    po = order(svc, sample, [POLineInput(sample.mdf.id, "10", notes="kitchen"),
                             POLineInput(sample.mdf.id, "5", notes="utility")])
    deliver(svc, po, "A", [ReceiptLine(sample.mdf.id, "8")])
    deliver(svc, po, "B", [ReceiptLine(sample.mdf.id, "4")])  # goes to the line it fits on
    assert view(svc, po) == ("PARTIALLY_RECEIVED", [(D("10"), D("8"), D("2")), (D("5"), D("4"), D("1"))])
    assert svc.purchasing.incoming() == {sample.mdf.id: D("3")}
    line2 = svc.purchasing.get(po.id).lines[1].line.id
    deliver(svc, po, "C", [ReceiptLine(sample.mdf.id, "3", po_line_id=line2)])  # booked on line 2 by hand
    assert view(svc, po)[0] == "RECEIVED"  # 15 of 15 sheets: complete, whichever line they were booked on
    assert svc.purchasing.incoming() == {} and svc.purchasing.discrepancies() == []
    with pytest.raises(ValidationError):
        deliver(svc, order(svc, sample, [POLineInput(sample.edge.id, "1")]), "D",
                [ReceiptLine(sample.mdf.id, "1", po_line_id=line2)])
    reconciled(svc)


def test_wrong_supplier_is_refused(svc, sample):
    other = svc.reference.save_supplier(None, {"code": "KRO", "name": "Kronospan"})
    po = order(svc, sample, [POLineInput(sample.mdf.id, "10")])
    with pytest.raises(ValidationError, match="Egger"):
        deliver(svc, po, "X", [ReceiptLine(sample.mdf.id, "1")], supplier_id=other.id)
    assert svc.ledger.product_balance(sample.mdf.id).on_hand == D("0")


def test_editing_an_order(svc, sample):
    po = order(svc, sample, [POLineInput(sample.mdf.id, "10"), POLineInput(sample.edge.id, "100")])
    deliver(svc, po, "DN-1", [ReceiptLine(sample.mdf.id, "6")])
    pur = svc.purchasing
    with pytest.raises(ValidationError, match="cannot be less"):
        pur.save(po.id, {}, [POLineInput(sample.mdf.id, "5"), POLineInput(sample.edge.id, "100")])
    with pytest.raises(ValidationError, match="cannot be removed"):
        pur.save(po.id, {}, [POLineInput(sample.edge.id, "100")])
    other = svc.reference.save_supplier(None, {"code": "KRO", "name": "Kronospan"})
    with pytest.raises(ValidationError, match="supplier cannot be changed"):
        pur.save(po.id, {"supplier_id": other.id}, [POLineInput(sample.mdf.id, "10")])
    # supplier sends only 6: reduce the order, drop the edging, add hinges
    hinge = svc.products.save(None, {"item_type": "COMPONENT", "internal_code": "HNG-35", "name": "Hinge 35mm",
                                     "unit_id": sample.mdf.unit_id})
    pur.save(po.id, {"expected_date": TODAY + timedelta(days=2)},
             [POLineInput(sample.mdf.id, "6"), POLineInput(hinge.id, "40")])
    assert view(svc, po) == ("PARTIALLY_RECEIVED", [(D("6"), D("6"), D("0")), (D("40"), D("0"), D("40"))])
    pur.save(po.id, {}, [POLineInput(sample.mdf.id, "6")])
    assert view(svc, po) == ("RECEIVED", [(D("6"), D("6"), D("0"))])
    with pytest.raises(ValidationError, match="can no longer be changed"):
        pur.save(po.id, {}, [POLineInput(sample.mdf.id, "7")])
    actions = {e.action for e in svc.audit.search(entity_type="PURCHASE_ORDER")}
    assert {"PO_CREATED", "PO_ORDERED", "PO_EDITED", "PO_STATUS_CHANGED"} <= actions
    reconciled(svc)


def test_validation(svc, sample):
    pur = svc.purchasing
    with pytest.raises(ValidationError, match="Supplier is required"):
        pur.save(None, {"order_date": TODAY}, [])
    with pytest.raises(ValidationError, match="before the order date"):
        pur.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY,
                        "expected_date": TODAY - timedelta(days=1)}, [])
    with pytest.raises(ValidationError, match="whole numbers"):
        pur.save(None, {"supplier_id": sample.supplier.id}, [POLineInput(sample.mdf.id, "1.5")])
    with pytest.raises(ValidationError, match="greater than zero"):
        pur.save(None, {"supplier_id": sample.supplier.id}, [POLineInput(sample.mdf.id, "0")])
    empty = pur.save(None, {"supplier_id": sample.supplier.id}, [])
    with pytest.raises(ValidationError, match="at least one"):
        pur.mark_ordered(empty.id)
    svc.sites.set_status(sample.site.id, "COMPLETED")
    with pytest.raises(ValidationError, match="closed"):
        pur.save(None, {"supplier_id": sample.supplier.id, "site_id": sample.site.id}, [])


def test_cancel_and_close_short(svc, sample):
    pur = svc.purchasing
    po = order(svc, sample, [POLineInput(sample.mdf.id, "10")])
    cancelled = pur.cancel(po.id, "Ordered from another supplier")
    assert cancelled.status == "CANCELLED" and cancelled.close_reason.startswith("Ordered")
    assert pur.incoming() == {}

    po = order(svc, sample, [POLineInput(sample.mdf.id, "10"), POLineInput(sample.edge.id, "30")])
    deliver(svc, po, "DN-1", [ReceiptLine(sample.mdf.id, "7")])
    with pytest.raises(ValidationError, match="Close short"):
        pur.cancel(po.id, "no")
    closed = pur.close_short(po.id, "Discontinued by the supplier")
    assert closed.status == "RECEIVED" and closed.closed_short
    missing = [(d.product_id, d.quantity, d.reason_code) for d in pur.discrepancies(purchase_order_id=po.id)]
    assert sorted(missing) == sorted([(sample.mdf.id, D("3"), "MISSING"), (sample.edge.id, D("30"), "MISSING")])
    assert pur.incoming() == {}
    reconciled(svc)
    reopened = pur.reopen(po.id)
    assert reopened.status == "PARTIALLY_RECEIVED" and pur.incoming()[sample.edge.id] == D("30")
    reconciled(svc)


def test_manual_discrepancy_with_photo_and_resolution(svc, sample, tmp_path):
    po = order(svc, sample, [POLineInput(sample.mdf.id, "20")])
    dn = deliver(svc, po, "DN-1", [ReceiptLine(sample.mdf.id, "20")])
    photo = tmp_path / "corner.png"
    from PySide6.QtGui import QImage
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(0x884422)
    assert img.save(str(photo))
    d = svc.purchasing.record_discrepancy({"delivery_note_id": dn.id, "product_id": sample.mdf.id,
                                           "reason_code": "DAMAGED", "expected_qty": "20", "received_qty": "20",
                                           "quantity": "2", "notes": "Two boards with crushed corners",
                                           "client_token": "tok-1"}, photos=[photo])
    assert d.number == "DSC-000001" and d.supplier_id == sample.supplier.id and d.purchase_order_id == po.id
    assert d.purchase_order_line_id == svc.purchasing.get(po.id).lines[0].line.id
    assert len(svc.attachments.list_for("DISCREPANCY", d.id)) == 1
    with pytest.raises(AlreadyPostedError):
        svc.purchasing.record_discrepancy({"delivery_note_id": dn.id, "reason_code": "DAMAGED", "quantity": "2",
                                           "client_token": "tok-1"})
    with pytest.raises(ValidationError, match="cannot be used"):
        svc.purchasing.record_discrepancy({"delivery_note_id": dn.id, "reason_code": "SCRAP", "quantity": "1"})
    assert [x.number for x in svc.purchasing.discrepancies("crushed")] == ["DSC-000001"]
    assert [x.number for x in svc.purchasing.discrepancies(status="OPEN")] == ["DSC-000001"]
    svc.purchasing.resolve_discrepancy(d.id, "Credit note CN-77 received")
    with pytest.raises(ValidationError, match="already been resolved"):
        svc.purchasing.resolve_discrepancy(d.id, "again")
    assert svc.purchasing.discrepancies(status="OPEN") == []
    assert not any("delivery problem" in a.text for a in svc.dashboard.load().alerts)


def test_overdue_orders_on_dashboard(svc, sample):
    po = svc.purchasing.save(None, {"supplier_id": sample.supplier.id, "order_date": TODAY - timedelta(days=10),
                                    "expected_date": TODAY - timedelta(days=3)}, [POLineInput(sample.mdf.id, "5")])
    assert svc.purchasing.overdue() == []  # draft: not sent yet
    svc.purchasing.mark_ordered(po.id)
    assert [r.po.po_number for r in svc.purchasing.overdue()] == [po.po_number]
    assert any("past their expected delivery date" in a.text for a in svc.dashboard.load().alerts)
    rows = svc.purchasing.list("MDF-18")
    assert [(r.po.id, r.lines_outstanding, r.value) for r in rows] == [(po.id, 1, D("122.50"))]


def test_duplicate_submission_and_permissions(svc, sample):
    header = {"supplier_id": sample.supplier.id, "client_token": "po-form-1"}
    svc.purchasing.save(None, header, [POLineInput(sample.mdf.id, "1")])
    with pytest.raises(AlreadyPostedError):
        svc.purchasing.save(None, header, [POLineInput(sample.mdf.id, "1")])
    assert len(svc.purchasing.list()) == 1
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op", "full_name": "Op", "role_id": roles["OPERATIVE"].id,
                           "must_change_password": False}, "secret1")
    svc.auth.login("op", "secret1")
    with pytest.raises(PermissionDeniedError):
        svc.purchasing.save(None, {"supplier_id": sample.supplier.id}, [])
    assert len(svc.purchasing.list()) == 1  # operatives can see orders
    assert svc.ctx.has("purchasing.view") and svc.ctx.has("goods_in.post")


def test_tampered_order_link_is_critical(svc, sample):
    from sqlalchemy import text
    po = order(svc, sample, [POLineInput(sample.mdf.id, "10"), POLineInput(sample.edge.id, "10")])
    deliver(svc, po, "DN-1", [ReceiptLine(sample.mdf.id, "10")])
    edge_line = svc.purchasing.get(po.id).lines[1].line.id
    with svc.ctx.db.session() as s:
        s.execute(text("UPDATE delivery_note_lines SET purchase_order_line_id = :l"), {"l": edge_line})
    checks = {i.check for i in svc.ledger.critical_issues()}
    assert "Purchase order link" in checks
    with svc.ctx.db.session() as s:
        s.execute(text("UPDATE purchase_orders SET status = 'ORDERED'"))
    assert any(i.check == "Purchase order status" for i in svc.ledger.integrity_check())
