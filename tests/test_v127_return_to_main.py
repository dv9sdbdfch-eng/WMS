"""v1.27 stage 3: return material from a project's stock to the main warehouse stock."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.core.exceptions import ValidationError
from wms.db.models import AuditLog, StockMovement
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine

D = Decimal
TODAY = date.today()


@pytest.fixture
def held(svc, sample):
    """P-1001 holds 20 MDF delivered for it + 5 reserved by hand, and 10 edging delivered for it."""
    svc.goods_in.post({"dn_number": "DN-W", "supplier_id": sample.supplier.id, "delivery_date": TODAY},
                      [ReceiptLine(sample.mdf.id, D(30), sample.bin1.id)])
    svc.goods_in.post({"dn_number": "DN-P", "supplier_id": sample.supplier.id, "delivery_date": TODAY,
                       "site_id": sample.site.id},
                      [ReceiptLine(sample.mdf.id, D(20), sample.bin1.id),
                       ReceiptLine(sample.edge.id, D(10), sample.bin2.id)])
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "5"})
    return sample


def _stock(svc, site_id):
    return {x.code: x.total for x in svc.reservations.project_stock(site_id)}


def _free(svc, product):
    return svc.products.row(product.id).available


def test_partial_return_frees_material_for_other_work(svc, held):
    before_free = _free(svc, held.mdf)
    with svc.ctx.db.read_session() as s:
        moves = s.query(StockMovement).count()
    number = svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "8")], "Left over after Level 3")
    assert number.startswith("PSR-")
    assert _stock(svc, held.site.id) == {"MDF-18": D(17), "EDGE-W22": D(10)}
    assert _free(svc, held.mdf) == before_free + 8
    with svc.ctx.db.read_session() as s:
        assert s.query(StockMovement).count() == moves  # nothing moved on the shelves
        assert s.scalar(select(AuditLog).where(AuditLog.action == "PROJECT_STOCK_RETURNED"))
    [row] = svc.reservations.project_releases(held.site.id)
    assert (row.number, row.quantity, row.reason) == (number, D(8), "Left over after Level 3")


def test_return_all_of_several_materials_in_one_document(svc, held):
    number = svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "25"), (held.edge.id, "10")],
                                             "Project cancelled")
    assert _stock(svc, held.site.id) == {}
    rows = svc.reservations.project_releases(held.site.id)
    assert {r.number for r in rows} == {number} and len(rows) == 2
    assert {r.reservation.status for r in svc.reservations.list(site_id=held.site.id)} == {"RELEASED"}


def test_issued_part_stays_issued(svc, held):
    svc.goods_out.post({"site_id": held.site.id, "issue_date": TODAY}, [IssueLine(held.mdf.id, D(22))])
    assert _stock(svc, held.site.id)["MDF-18"] == D(3)
    svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "3")], "Not needed")
    assert "MDF-18" not in _stock(svc, held.site.id)
    statuses = {r.reservation.status for r in svc.reservations.list(site_id=held.site.id, product_id=held.mdf.id)}
    assert "ACTIVE" not in statuses


def test_cannot_return_more_than_the_project_holds_or_nothing(svc, held):
    with pytest.raises(ValidationError):
        svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "26")], "Too much")
    with pytest.raises(ValidationError):
        svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "")], "Nothing")
    with pytest.raises(ValidationError):
        svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "1")], "  ")  # reason required
    assert _stock(svc, held.site.id) == {"MDF-18": D(25), "EDGE-W22": D(10)}  # all or nothing


def test_viewer_cannot_return(svc, held):
    from wms.core.exceptions import PermissionDeniedError
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "viewer", "full_name": "V", "role_id": roles["VIEWER"].id}, "secret1")
    svc.auth.login("viewer", "secret1")
    with pytest.raises(PermissionDeniedError):
        svc.reservations.return_to_main(held.site.id, [(held.mdf.id, "1")], "No rights")


def test_project_stock_dialog_returns(ui, held):  # noqa: F811
    from wms.ui.dialogs.reservations import ProjectStockDialog
    app, win, msgs, _ = ui
    dlg = ProjectStockDialog(app, held.site.id, win)
    assert dlg.table.rowCount() == 2
    row = [ln.code for ln in dlg.lines].index("EDGE-W22")
    dlg.table.item(row, dlg.C_RETURN).setText("4")
    dlg.reason.setText("Spare")
    dlg.save()
    assert _errors(msgs) == []
    assert _stock(app.services, held.site.id)["EDGE-W22"] == D(6)
    dlg.fill_all()
    dlg.reason.setText("Project finished")
    dlg.save()
    assert _stock(app.services, held.site.id) == {}
