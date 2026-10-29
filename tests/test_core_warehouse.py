"""Phase 1, Stage 2 - core warehouse: Goods In, Goods Out, transfers, returns, adjustments,
projects and bins, as one day in the warehouse. Stock is reconciled with the ledger after every step.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from wms.core.exceptions import DuplicateError, ReturnExceedsIssuedError, ValidationError
from wms.services.goods_out import IssueLine
from wms.services.stock_ops import ReturnLineInput
from wms.services.stocktake import CountEntry

D = Decimal
TODAY = date.today()


def reconciled(svc):
    critical = [str(i) for i in svc.ledger.critical_issues()]
    assert critical == [], critical


def on_hand(svc, product, bin_=None) -> Decimal:
    bal = svc.ledger.product_balance(product.id)
    if bin_ is None:
        return bal.on_hand
    return sum((b.quantity for b in bal.by_bin if b.bin_id == bin_.id), D("0"))


def test_a_day_in_the_warehouse(svc, sample):
    mdf, b04, b05, site = sample.mdf, sample.bin1, sample.bin2, sample.site

    # Goods In: 100 sheets onto B04
    sample.receive(mdf, 100, b04, "DN-1001")
    assert on_hand(svc, mdf, b04) == D("100")
    reconciled(svc)

    # Goods Out: 30 to the project
    gi = svc.goods_out.post({"issue_date": TODAY, "site_id": site.id, "reference": "Ground floor"},
                            [IssueLine(mdf.id, "30", b04.id)])
    assert on_hand(svc, mdf) == D("70")
    reconciled(svc)

    # Bin transfer: 40 from B04 to B05 - total unchanged
    svc.stock.transfer(mdf.id, b04.id, b05.id, "40")
    assert (on_hand(svc, mdf, b04), on_hand(svc, mdf, b05), on_hand(svc, mdf)) == (D("30"), D("40"), D("70"))
    reconciled(svc)

    # Return from the project: 5 back (more than issued is refused)
    with pytest.raises(ReturnExceedsIssuedError):
        svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": site.id},
                              [ReturnLineInput(mdf.id, "31", b04.id)])
    ret = svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": site.id,
                                 "reason": "Over-ordered"}, [ReturnLineInput(mdf.id, "5", b04.id)])
    assert ret.reason_code == "RETURNED_FROM_PROJECT"  # default reason code for a site return
    assert on_hand(svc, mdf) == D("75")
    reconciled(svc)

    # Supplier return of 2 damaged sheets with an explicit reason
    sret = svc.stock.post_return({"return_type": "TO_SUPPLIER", "return_date": TODAY,
                                  "supplier_id": sample.supplier.id, "reason_code": "DAMAGED"},
                                 [ReturnLineInput(mdf.id, "2", b05.id)])
    assert sret.reason_code == "DAMAGED" and on_hand(svc, mdf, b05) == D("38")
    reconciled(svc)

    # Adjustment with a reason code: 38 counted as 37 in B05
    svc.stock.adjust(mdf.id, b05.id, "37", "SCRAP", "Cut wrong")
    assert on_hand(svc, mdf) == D("72")
    reconciled(svc)

    # Project page: issued, returned, net, history with references
    summary = {r.product.internal_code: r for r in svc.sites.material_summary(site.id)}["MDF-18"]
    assert (summary.issued, summary.returned, summary.net) == (D("30"), D("5"), D("25"))
    refs = {m.doc_number for m in svc.sites.movements(site.id)}
    assert gi.issue_number in refs and ret.return_number in refs

    # Completed project: not offered for new Goods Out, still in history
    svc.sites.set_status(site.id, "COMPLETED")
    assert site.id not in {s.id for s in svc.sites.open_sites()}
    with pytest.raises(ValidationError, match="reopen it"):
        svc.goods_out.post({"issue_date": TODAY, "site_id": site.id}, [IssueLine(mdf.id, "1", b04.id)])
    assert len(svc.sites.material_summary(site.id)) == 1
    reconciled(svc)


def test_return_reason_must_suit_returns(svc, sample):
    sample.receive(sample.mdf, 10, sample.bin1)
    with pytest.raises(ValidationError, match="cannot be used"):
        svc.stock.post_return({"return_type": "TO_SUPPLIER", "return_date": TODAY, "supplier_id": sample.supplier.id,
                               "reason_code": "SCRAP"}, [ReturnLineInput(sample.mdf.id, "1", sample.bin1.id)])
    assert on_hand(svc, sample.mdf) == D("10")


# --- bins -----------------------------------------------------------------------------------

def test_bin_barcode_and_scan_lookup(svc, sample):
    b04 = svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B04",
                                                  "barcode": "BIN-0004"})
    assert b04.barcode == "BIN-0004" and b04.scan_code == "BIN-0004"
    assert sample.bin2.scan_code == sample.bin2.full_code  # no barcode: the label carries the full code
    loc = svc.locations
    assert loc.find_bin("BIN-0004").id == b04.id  # scanned label
    assert loc.find_bin(" main / a / r03 / s02 / b05 ").id == sample.bin2.id  # typed full code, any case
    assert loc.find_bin("b05").id == sample.bin2.id  # short code, unique
    assert loc.find_bin("nope") is None
    with pytest.raises(DuplicateError, match="already used"):
        loc.save_bin(sample.bin2.id, {"location_id": sample.shelf.id, "code": "B05", "barcode": "BIN-0004"})
    with pytest.raises(DuplicateError, match="already used"):  # a barcode may not be another bin's full code
        loc.save_bin(sample.bin2.id, {"location_id": sample.shelf.id, "code": "B05", "barcode": b04.full_code})
    # saving without the barcode field (older screens) keeps it
    again = loc.save_bin(b04.id, {"location_id": sample.shelf.id, "code": "B04", "description": "Top shelf"})
    assert again.barcode == "BIN-0004"


def test_short_code_used_twice_is_not_guessed(svc, sample):
    other = svc.locations.save_location(None, {"kind": "SHELF", "code": "S03", "parent_id": sample.rack.id})
    svc.locations.save_bin(None, {"location_id": other.id, "code": "B04"})
    assert svc.locations.find_bin("B04") is None  # two bins called B04: scan the label instead


def test_bin_labels_pdf(svc, sample, tmp_path):
    out = svc.locations.bin_labels([sample.bin1.id, sample.bin2.id], tmp_path / "labels.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 1500
    thermal = svc.locations.bin_labels([sample.bin1.id], tmp_path / "one.pdf", "LABEL_100x50")
    assert thermal.read_bytes().startswith(b"%PDF")
    many = svc.locations.bulk_create_bins(sample.shelf.id, "C", 1, 20)
    assert many == 20
    ids = [b.id for b in svc.locations.bins(sample.shelf.id)]
    assert svc.locations.bin_labels(ids, tmp_path / "sheet.pdf").read_bytes().count(b"/Type /Page") >= 2
    with pytest.raises(ValidationError):
        svc.locations.bin_labels([], tmp_path / "none.pdf")


def test_stocktake_of_one_bin(svc, sample):
    sample.receive(sample.mdf, 12, sample.bin1, "S1")
    sample.receive(sample.edge, 30, sample.bin2, "S2")
    count = svc.stocktake.create(None, "Bin B04", bin_id=sample.bin1.id)
    assert [(l.product_id, l.bin_id) for l in count.lines] == [(sample.mdf.id, sample.bin1.id)]
    svc.stocktake.record_counts(count.id, [CountEntry(count.lines[0].id, "11")])
    svc.stocktake.submit(count.id)
    svc.stocktake.approve(count.id, "ok")
    assert on_hand(svc, sample.mdf) == D("11") and on_hand(svc, sample.edge) == D("30")
    reconciled(svc)
