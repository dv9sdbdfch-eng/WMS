"""Historical integrity (v1.3): master data can change or leave active use without rewriting history.

The first test is the acceptance scenario from the specification, step by step. The rest cover
each rule on its own: permanent delete vs. archive, what blocks removal, restore, renames,
projects, suppliers, products, audit, search, reports and the upgrade of an existing database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from wms.core.enums import SiteStatus
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.db.models import Bin
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.reports import ReportFilters
from wms.services.search import SearchCriteria, movement_route

D = Decimal
TODAY = date.today()
B04 = "MAIN / A / R03 / S02 / B04"
B05 = "MAIN / A / R03 / S02 / B05"


def receive(svc, product, qty, bin_, number, supplier):
    return svc.goods_in.post({"dn_number": number, "supplier_id": supplier.id, "delivery_date": TODAY},
                             [ReceiptLine(product.id, D(str(qty)), bin_.id)])


def issue(svc, site, product, qty, bin_=None, ref=None):
    return svc.goods_out.post({"issue_date": TODAY, "site_id": site.id, "reference": ref},
                              [IssueLine(product.id, str(qty), bin_.id if bin_ else None)])


def bin_stock(svc, bin_id) -> Decimal:
    return sum((lvl.quantity for lvl in svc.locations.bin_contents(bin_id)), D("0"))


def history_rows(svc, **kw):
    return svc.search.movements(SearchCriteria(**kw)).rows


def audit_actions(svc, entity_type, entity_id) -> list[str]:
    return [a.action for a in svc.audit.for_entity(entity_type, entity_id)]


@pytest.fixture
def h3311(svc, sample):
    units = {u.code: u for u in svc.reference.units()}
    return svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "H3311", "name": "Oak Panel",
                                    "unit_id": units["SHEET"].id, "supplier_id": sample.supplier.id,
                                    "default_bin_id": sample.bin1.id})


@pytest.fixture
def project_a(svc):
    return svc.sites.save(None, {"project_number": "P-A", "name": "Project A", "status": "ACTIVE"})


# --- the scenario from the specification ---------------------------------------------------

def test_acceptance_scenario_history_survives_removal_rename_and_completion(svc, sample, h3311, project_a):
    # 1-2. Bin B04 (sample.bin1) and product H3311 exist.
    b04, b05 = sample.bin1, sample.bin2
    assert b04.code == "B04"
    # 3. Receive 100 units into B04.
    receive(svc, h3311, 100, b04, "DN-100", sample.supplier)
    # 4. Issue 40 units to Project A.
    issue(svc, project_a, h3311, 40, b04, ref="JOB-A")
    # 5. Transfer the remaining 60 units from B04 to B05.
    svc.stock.transfer(h3311.id, b04.id, b05.id, "60")
    # 6. B04 is empty.
    assert bin_stock(svc, b04.id) == 0
    assert bin_stock(svc, b05.id) == D("60")

    # 7. Remove B04 from active use: it has history, so it is archived, not deleted.
    plan = svc.locations.bin_removal_plan(b04.id)
    assert plan.allowed and not plan.permanent and plan.history == 3
    assert plan.action == "Remove from active use"
    assert "Bin B04 has 3 historical transactions. It will be removed from active warehouse operations, " \
           "but its historical transaction data will be preserved." in plan.message
    svc.locations.remove_bin(b04.id, reason="Racking reorganised")

    # 8. B04 is unavailable for new transactions.
    assert b04.id not in {b.id for b in svc.locations.bins()}
    with pytest.raises(ValidationError, match="removed from active use"):
        receive(svc, h3311, 1, svc.locations.reload(Bin, b04.id), "DN-101", sample.supplier)
    with pytest.raises(ValidationError, match="removed from active use"):
        svc.stock.transfer(h3311.id, b05.id, b04.id, "1")

    # 9-10. Historical search still finds B04 and the old transactions still show it.
    rows = history_rows(svc, text="B04")
    assert len(rows) == 3
    assert all(B04 in (m.from_bin_code, m.to_bin_code) for m in rows)
    hits = [h for h in svc.search.matches("B04") if h.entity_type == "BIN" and h.entity_id == b04.id]
    assert hits and hits[0].kind == "Historical Bin" and "3 historical movement" in hits[0].detail
    view = svc.search.bin_view(b04.id)
    assert view.is_archived and view.current_stock == 0 and view.history_count == 3
    assert view.removed_by == "admin" and view.removed_reason == "Racking reorganised"

    # 11-12. Rename H3311: old transactions keep the name they were posted with, new ones get the new one.
    p = svc.products.get(h3311.id)
    svc.products.save(p.id, {"item_type": p.item_type, "internal_code": "H3311", "name": "Oak Panel FSC 18mm",
                             "unit_id": p.unit_id, "supplier_id": p.supplier_id})
    old = history_rows(svc, text="H3311")
    assert {m.product_name for m in old} == {"Oak Panel"}
    issue(svc, project_a, h3311, 5, b05)
    newest = history_rows(svc, text="H3311")[0]
    assert newest.product_name == "Oak Panel FSC 18mm"

    # 13. Complete Project A: it leaves normal selection and takes no new material.
    svc.sites.set_status(project_a.id, SiteStatus.COMPLETED.value)
    assert project_a.id not in {s.id for s in svc.sites.open_sites()}
    with pytest.raises(ValidationError, match="completed"):
        issue(svc, project_a, h3311, 1, b05)

    # 14. Historical Goods Out still shows Project A.
    goods_out = [m for m in history_rows(svc, site_id=project_a.id) if m.movement_type == "GOODS_OUT"]
    assert len(goods_out) == 2 and {m.site_label for m in goods_out} == {"P-A - Project A"}
    first = min(goods_out, key=lambda m: m.id)
    assert movement_route(first) == (f"Bin {B04}", "P-A - Project A")
    found = [h for h in svc.search.matches("Project A") if h.entity_id == project_a.id]
    assert found and found[0].kind == "Historical Project"

    # 15-16. The historical report keeps every name and reference intact.
    report = svc.reports.run("movements", ReportFilters(product_id=h3311.id))
    cols = [c.title for c in report.columns]
    rows = [dict(zip(cols, r)) for r in report.rows]
    by_type = {}
    for r in rows:
        by_type.setdefault(r["Type"], []).append(r)
    gin = by_type["Goods In"][0]
    assert (gin["Code"], gin["Product"], gin["To"], gin["Supplier"]) == ("H3311", "Oak Panel", B04, "Egger UK Ltd")
    first_out = next(r for r in by_type["Goods Out"] if r["Product"] == "Oak Panel")
    assert (first_out["From"], first_out["Site / Project"], first_out["Reference"]) == (B04, "P-A - Project A",
                                                                                         "JOB-A")
    move = by_type["Bin Transfer"][0]
    assert (move["From"], move["To"], move["Quantity"]) == (B04, B05, D("60"))
    assert {r["User"] for r in rows} == {"Administrator"}

    # Restore B04: available again, history unchanged.
    svc.locations.restore_bin(b04.id, reason="Needed again")
    assert b04.id in {b.id for b in svc.locations.bins()}
    receive(svc, h3311, 2, svc.locations.reload(Bin, b04.id), "DN-102", sample.supplier)
    assert bin_stock(svc, b04.id) == D("2")
    assert {m.product_name for m in history_rows(svc, text="DN-100")} == {"Oak Panel"}
    assert audit_actions(svc, "BIN", b04.id)[:2] == ["BIN_RESTORED", "BIN_ARCHIVED"]


# --- bins ---------------------------------------------------------------------------------

def test_never_used_bin_is_deleted_permanently(svc, sample):
    b = svc.locations.save_bin(None, {"location_id": sample.shelf.id, "code": "B99"})
    svc.products.save(sample.edge.id, {"item_type": "RAW_MATERIAL", "internal_code": "EDGE-W22",
                                       "name": "ABS edging white 22mm", "unit_id": sample.edge.unit_id,
                                       "default_bin_id": b.id})
    plan = svc.locations.bin_removal_plan(b.id)
    assert plan.permanent and plan.action == "Delete permanently" and "default bin" in plan.message
    svc.locations.remove_bin(b.id, "typo")
    assert b.id not in {x.id for x in svc.locations.bins(include_inactive=True)}
    assert svc.products.get(sample.edge.id).default_bin_id is None  # setting cleared, not left dangling
    entry = next(a for a in svc.audit.search() if a.action == "BIN_DELETED")
    assert entry.entity_id == b.id and "Deleted permanently" in entry.summary and "typo" in entry.summary
    # the code is free again
    svc.locations.save_bin(None, {"location_id": sample.shelf.id, "code": "B99"})


def test_bin_with_stock_or_unfinished_stocktake_cannot_be_removed(svc, sample):
    sample.receive(sample.mdf, 3, sample.bin1)
    plan = svc.locations.bin_removal_plan(sample.bin1.id)
    assert not plan.allowed and "still holds stock" in plan.message
    with pytest.raises(ValidationError, match="still holds stock"):
        svc.locations.remove_bin(sample.bin1.id)
    count = svc.stocktake.create(sample.shelf.id)
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.bin2.id, "3")
    with pytest.raises(ValidationError, match=count.count_number):
        svc.locations.remove_bin(sample.bin1.id)
    svc.stocktake.cancel(count.id, "not needed")
    assert svc.locations.remove_bin(sample.bin1.id).outcome == "Removed from active use"
    with pytest.raises(ValidationError, match="already been removed"):
        svc.locations.remove_bin(sample.bin1.id)


def test_renaming_a_bin_or_location_does_not_rewrite_history(svc, sample):
    sample.receive(sample.mdf, 5, sample.bin1, "R1")
    svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B04-NEW"})
    svc.locations.save_location(sample.rack.id, {"kind": "RACK", "code": "R30", "parent_id": sample.zone.id})
    sample.receive(sample.mdf, 1, svc.locations.reload(Bin, sample.bin1.id), "R2")
    old, new = (m for m in sorted(history_rows(svc, text="MDF-18"), key=lambda m: m.id))
    assert old.to_bin_code == B04
    assert new.to_bin_code == "MAIN / A / R30 / S02 / B04-NEW"
    # the old code still finds the bin and its old movement
    assert any(h.entity_id == sample.bin1.id for h in svc.search.matches("R03 / S02 / B04"))
    assert len(history_rows(svc, text="R03 / S02 / B04")) == 1


def test_archived_bin_code_cannot_be_reused_by_mistake_and_restore_checks_its_location(svc, sample):
    sample.receive(sample.mdf, 1, sample.bin2)
    svc.stock.transfer(sample.mdf.id, sample.bin2.id, sample.bin1.id, "1")
    svc.locations.remove_bin(sample.bin2.id)
    with pytest.raises(DuplicateError, match="Restore it instead"):
        svc.locations.save_bin(None, {"location_id": sample.shelf.id, "code": "B05"})
    svc.stock.transfer(sample.mdf.id, sample.bin1.id, sample.quarantine.id, "1")
    svc.locations.save_location(sample.rack.id, {"kind": "RACK", "code": "R03", "parent_id": sample.zone.id,
                                                 "is_active": False})
    with pytest.raises(ValidationError, match="restore the location first"):
        svc.locations.restore_bin(sample.bin2.id)


def test_reversal_never_puts_stock_back_into_an_archived_bin(svc, sample):
    sample.receive(sample.mdf, 4, sample.bin1)
    gi = issue(svc, sample.site, sample.mdf, 4, sample.bin1)
    svc.locations.remove_bin(sample.bin1.id)
    with pytest.raises(ValidationError, match="Restore the bin"):
        svc.goods_out.reverse(gi.id, "posted in error")
    svc.locations.restore_bin(sample.bin1.id)
    svc.goods_out.reverse(gi.id, "posted in error")
    assert bin_stock(svc, sample.bin1.id) == D("4")


def test_unticking_active_on_the_bin_form_follows_the_same_rules(svc, sample):
    sample.receive(sample.mdf, 1, sample.bin2)
    svc.stock.transfer(sample.mdf.id, sample.bin2.id, sample.bin1.id, "1")
    svc.locations.save_bin(sample.bin2.id, {"location_id": sample.shelf.id, "code": "B05", "is_active": False})
    assert "BIN_ARCHIVED" in audit_actions(svc, "BIN", sample.bin2.id)
    svc.locations.save_bin(sample.bin2.id, {"location_id": sample.shelf.id, "code": "B05", "is_active": True})
    assert audit_actions(svc, "BIN", sample.bin2.id)[0] == "BIN_RESTORED"


# --- products --------------------------------------------------------------------------

def test_product_removal_archive_vs_delete_and_restore(svc, sample, h3311):
    # never used -> deleted
    plan = svc.products.removal_plan(sample.cabinet.id)
    assert plan.permanent
    svc.products.remove(sample.cabinet.id, "duplicate entry")
    with pytest.raises(NotFoundError):
        svc.products.get(sample.cabinet.id)
    # used -> discontinued, history intact
    receive(svc, h3311, 10, sample.bin1, "P1", sample.supplier)
    with pytest.raises(ValidationError, match="still shows"):
        svc.products.remove(h3311.id)
    issue(svc, sample.site, h3311, 10, sample.bin1)
    plan = svc.products.remove(h3311.id, "discontinued")
    assert not plan.permanent and plan.history == 2
    assert not svc.products.get(h3311.id).is_active
    with pytest.raises(ValidationError, match="inactive"):
        receive(svc, h3311, 1, sample.bin1, "P2", sample.supplier)
    rows = history_rows(svc, text="H3311")
    assert len(rows) == 2 and {m.product_code for m in rows} == {"H3311"}
    assert {m.supplier_label for m in rows if m.movement_type == "GOODS_IN"} == {"Egger UK Ltd"}
    with pytest.raises(DuplicateError, match="Restore it instead"):
        svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "h3311", "name": "x",
                                 "unit_id": h3311.unit_id})
    svc.products.restore(h3311.id, "back in range")
    assert svc.products.get(h3311.id).is_active
    assert audit_actions(svc, "PRODUCT", h3311.id)[:2] == ["PRODUCT_RESTORED", "PRODUCT_ARCHIVED"]


def test_document_lines_show_the_product_as_it_was(svc, sample, h3311):
    dn = receive(svc, h3311, 3, sample.bin1, "DOC-1", sample.supplier)
    p = svc.products.get(h3311.id)
    svc.products.save(p.id, {"item_type": p.item_type, "internal_code": "H3311-OLD", "name": "Renamed",
                             "unit_id": p.unit_id})
    svc.reference.save_supplier(sample.supplier.id, {"code": "EGG", "name": "Egger Group"})
    svc.locations.save_bin(sample.bin1.id, {"location_id": sample.shelf.id, "code": "B44"})
    dn = svc.goods_in.get(dn.id)
    line = dn.lines[0]
    assert (line.product_code, line.product_name, line.bin_code) == ("H3311", "Oak Panel", B04)
    assert dn.supplier_label == "Egger UK Ltd"
    report = svc.reports.run("goods_in", ReportFilters())
    assert report.rows[0][2:5] == ["Egger UK Ltd", "H3311", "Oak Panel"]


# --- projects ---------------------------------------------------------------------------

def test_project_statuses_and_removal(svc, sample, project_a):
    sample.receive(sample.mdf, 10)
    issue(svc, project_a, sample.mdf, 4)
    # completed: no new Goods Out, but leftovers can still come back
    svc.sites.set_status(project_a.id, "COMPLETED")
    assert svc.sites.get(project_a.id).end_date == TODAY
    from wms.services.stock_ops import ReturnLineInput
    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": project_a.id},
                          [ReturnLineInput(sample.mdf.id, "1", sample.bin1.id)])
    # delete -> archived (has history); archived takes nothing at all
    plan = svc.sites.remove(project_a.id, "closed out")
    assert not plan.permanent
    assert svc.sites.get(project_a.id).status == "ARCHIVED"
    with pytest.raises(ValidationError, match="archived"):
        svc.stock.post_return({"return_type": "FROM_SITE", "return_date": TODAY, "site_id": project_a.id},
                              [ReturnLineInput(sample.mdf.id, "1", sample.bin1.id)])
    assert project_a.id in {s.id for s in svc.sites.list(statuses=["ARCHIVED"])}
    assert [h.kind for h in svc.search.matches("Project A") if h.entity_id == project_a.id] == ["Historical Project"]
    assert len(svc.sites.movements(project_a.id)) == 2
    with pytest.raises(DuplicateError, match="Restore it instead"):
        svc.sites.save(None, {"project_number": "p-a", "name": "Again"})
    svc.sites.restore(project_a.id)
    assert svc.sites.get(project_a.id).status == "ACTIVE"
    issue(svc, project_a, sample.mdf, 1)
    # a project never used is deleted for real
    empty = svc.sites.save(None, {"project_number": "P-X", "name": "Typo"})
    assert svc.sites.remove(empty.id).permanent
    with pytest.raises(NotFoundError):
        svc.sites.get(empty.id)
    assert {"SITE_ARCHIVED", "SITE_RESTORED", "SITE_STATUS_CHANGED"} <= set(audit_actions(svc, "SITE", project_a.id))


def test_renamed_project_keeps_its_old_name_on_old_documents(svc, sample, project_a):
    sample.receive(sample.mdf, 5)
    gi = issue(svc, project_a, sample.mdf, 2)
    svc.sites.save(project_a.id, {"project_number": "P-A", "name": "Surrey County Hall", "status": "ACTIVE"})
    assert svc.goods_out.get(gi.id).site_label == "P-A - Project A"
    assert [m.site_label for m in svc.sites.movements(project_a.id)] == ["P-A - Project A"]
    # either name finds it
    assert len(history_rows(svc, text="Project A")) == 1
    assert len(history_rows(svc, text="Surrey County Hall")) == 1


# --- suppliers --------------------------------------------------------------------------

def test_supplier_archive_keeps_goods_in_history(svc, sample):
    sample.receive(sample.mdf, 5)
    plan = svc.reference.remove_supplier(sample.supplier.id, "no longer used")
    assert not plan.permanent
    with pytest.raises(ValidationError, match="archived"):
        sample.receive(sample.mdf, 1, number="DN-9")
    rows = history_rows(svc, text="Egger")
    assert rows and rows[0].supplier_label == "Egger UK Ltd"
    assert any(h.kind == "Historical Supplier" for h in svc.search.matches("Egger"))
    svc.reference.restore_supplier(sample.supplier.id)
    sample.receive(sample.mdf, 1, number="DN-2")
    unused = svc.reference.save_supplier(None, {"code": "NEW", "name": "Never Used Ltd"})
    assert svc.reference.remove_supplier(unused.id).permanent


# --- guards and upgrade -----------------------------------------------------------------

def test_snapshots_are_part_of_the_immutable_ledger(svc, sample):
    sample.receive(sample.mdf, 1)
    with pytest.raises(Exception, match="append-only"):
        with svc.ctx.db.session() as s:
            s.execute(text("UPDATE stock_movements SET to_bin_name_snapshot = 'X'"))


def test_permissions_are_required(svc, sample):
    from tests.test_qa_hardening import login_as
    from wms.core.exceptions import PermissionDeniedError
    login_as(svc, "OPERATIVE", "op1")
    with pytest.raises(PermissionDeniedError):
        svc.locations.remove_bin(sample.bin2.id)
    with pytest.raises(PermissionDeniedError):
        svc.products.remove(sample.edge.id)


def test_existing_database_is_upgraded_and_backfilled(svc, sample, cfg):
    """A v1.2 database (no snapshot columns) upgrades in place: old movements get the names that are
    current at upgrade time, every guard is put back, and new postings snapshot as normal."""
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR

    sample.receive(sample.mdf, 6, sample.bin1, "OLD-1")
    issue(svc, sample.site, sample.mdf, 2, sample.bin1)
    db = svc.ctx.db
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0003")
    with db.engine.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(stock_movements)"))}
        assert "product_code_snapshot" not in cols
    db.dispose()
    db.migrate()
    with db.engine.connect() as c:
        rows = c.execute(text("SELECT product_code_snapshot, product_name_snapshot, to_bin_name_snapshot, "
                              "from_bin_name_snapshot, site_name_snapshot, supplier_name_snapshot, "
                              "user_name_snapshot FROM stock_movements ORDER BY id")).all()
        assert rows[0] == ("MDF-18", "MDF 18mm 2440x1220", B04, None, None, "Egger UK Ltd", "Administrator")
        assert rows[1] == ("MDF-18", "MDF 18mm 2440x1220", None, B04, "Riverside Apartments", None, "Administrator")
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"trg_stock_movements_no_update", "trg_goods_issues_reversed_final"} <= names
        assert c.execute(text("SELECT supplier_name_snapshot FROM delivery_notes")).scalar() == "Egger UK Ltd"
        assert c.execute(text("SELECT site_name_snapshot FROM goods_issues")).scalar() == "Riverside Apartments"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
