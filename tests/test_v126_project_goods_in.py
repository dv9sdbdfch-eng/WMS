"""v1.26 stage 2: a delivery note received for a project becomes that project's stock."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from wms.core.exceptions import ValidationError
from wms.db.models import AuditLog
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine

D = Decimal
TODAY = date.today()


def _receive(svc, sample, number, lines, site_id=None):
    header = {"dn_number": number, "supplier_id": sample.supplier.id, "delivery_date": TODAY}
    if site_id:
        header["site_id"] = site_id
    return svc.goods_in.post(header, [ReceiptLine(p.id, D(q), sample.bin1.id) for p, q in lines])


def _stock(svc, site_id):
    return {x.code: (x.from_deliveries, x.reserved) for x in svc.reservations.project_stock(site_id)}


def test_delivery_for_a_project_becomes_its_stock(svc, sample):
    dn = _receive(svc, sample, "DN-P1", [(sample.mdf, "20"), (sample.edge, "50"), (sample.mdf, "5")],
                  sample.site.id)
    assert dn.site_id == sample.site.id and dn.site_label == "P-1001 - Riverside Apartments"
    assert _stock(svc, sample.site.id) == {"MDF-18": (D(25), 0), "EDGE-W22": (D(50), 0)}
    # set aside: nothing of it is available to other work
    assert svc.reservations.summary_by_product([sample.mdf.id])[sample.mdf.id] == D(25)
    rows = svc.reservations.list(site_id=sample.site.id)
    assert {r.reservation.delivery_note_id for r in rows} == {dn.id}
    # listed under the project
    assert [r[0].id for r in svc.goods_in.search(site_id=sample.site.id)] == [dn.id]


def test_delivery_without_project_is_warehouse_stock(svc, sample):
    _receive(svc, sample, "DN-W1", [(sample.mdf, "20")])
    assert _stock(svc, sample.site.id) == {}
    assert svc.reservations.summary_by_product([sample.mdf.id]).get(sample.mdf.id, 0) == 0


def test_project_stock_adds_hand_reservations_and_goes_down_when_issued(svc, sample):
    _receive(svc, sample, "DN-W1", [(sample.mdf, "30")])
    _receive(svc, sample, "DN-P1", [(sample.mdf, "10")], sample.site.id)
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "4"})
    assert _stock(svc, sample.site.id) == {"MDF-18": (D(10), D(4))}
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, D(12))])
    left = _stock(svc, sample.site.id)["MDF-18"]
    assert sum(left) == D(2)


def test_closed_project_cannot_receive(svc, sample):
    svc.sites.set_status(sample.site.id, "COMPLETED", "Handed over")
    with pytest.raises(ValidationError):
        _receive(svc, sample, "DN-X", [(sample.mdf, "1")], sample.site.id)


def test_reversing_the_delivery_frees_what_is_left(svc, sample):
    _receive(svc, sample, "DN-W1", [(sample.mdf, "30")])  # spare stock so the reversal can take 20 back
    dn = _receive(svc, sample, "DN-P1", [(sample.mdf, "20"), (sample.edge, "10")], sample.site.id)
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": TODAY}, [IssueLine(sample.mdf.id, D(5))])
    svc.goods_in.reverse(dn.id, "Wrong project", reason_code="DATA_ENTRY_ERROR")
    assert _stock(svc, sample.site.id) == {}
    by = {r.reservation.product_id: r.reservation for r in svc.reservations.list(site_id=sample.site.id)}
    assert by[sample.mdf.id].status == "FULFILLED" and by[sample.mdf.id].quantity == D(5)  # issued part stays
    assert by[sample.edge.id].status == "RELEASED"
    with svc.ctx.db.read_session() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "RESERVATION_RELEASED"))


def test_upgrade_adds_the_columns(svc, sample):
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR
    _receive(svc, sample, "DN-OLD", [(sample.mdf, "3")])
    db = svc.ctx.db
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0016")
    db.dispose()
    db.migrate()
    dn = _receive(svc, sample, "DN-NEW", [(sample.mdf, "2")], sample.site.id)
    assert dn.site_id == sample.site.id
    assert _stock(svc, sample.site.id) == {"MDF-18": (D(2), 0)}
