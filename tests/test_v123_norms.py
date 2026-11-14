"""v1.23 stage 1: project data model - duplicate-safe project names, norm history per unit
material (ALL / FUTURE), completed units, and the database upgrade."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from wms.core.exceptions import DuplicateError, ValidationError
from wms.db.models import AuditLog, UnitMaterialNorm
from wms.services.sites import project_name_key
from wms.services.unit_norms import NORM_ALL, NORM_FUTURE

D = Decimal


@pytest.fixture
def kit(svc, sample):
    """Tribeca -> Block A -> Level 1 -> Kitchen K01 x 10 (H3311 x 3 per kitchen)."""
    units = {u.code: u for u in svc.reference.units()}
    board = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "H3311", "name": "H3311 Board",
                                     "unit_id": units["SHEET"].id})
    t = svc.project_tree
    p = t.save_project(None, {"project_number": "TRI", "name": "Tribeca", "status": "ACTIVE"})
    block = t.save_block(None, {"name": "Block A"}, site_id=p.id)
    level = t.save_level(None, {"name": "Level 1"}, block_id=block.id)
    kitchen = t.save_unit(None, {"name": "Kitchen", "unit_code": "K01", "quantity": "10"}, level_id=level.id)
    [line] = t.add_materials(kitchen.id, [(board.id, "3")])
    return type("K", (), dict(project=p, block=block, level=level, kitchen=kitchen, line=line, board=board))


# ---- project names ---------------------------------------------------------------------------------------
def test_name_key_ignores_case_spaces_and_punctuation():
    assert project_name_key("Riverside Apartments") == project_name_key("  riverside   APARTMENTS ")
    assert project_name_key("Riverside-Apartments") == project_name_key("Riverside Apartments")
    assert project_name_key("Riverside Apartments 2") != project_name_key("Riverside Apartments")
    assert project_name_key("Київ Хаус") == project_name_key("київ  хаус")
    assert project_name_key("  ") is None


def test_duplicate_project_name_is_refused(svc, sample):
    t = svc.project_tree
    with pytest.raises(DuplicateError) as e:
        t.save_project(None, {"project_number": "P-2", "name": "riverside  apartments"})
    assert "P-1001" in str(e.value)  # says which project already has the name
    with pytest.raises(DuplicateError):
        svc.sites.save(None, {"project_number": "P-3", "name": "Riverside-Apartments"})
    other = t.save_project(None, {"project_number": "P-4", "name": "Mayfair"})
    with pytest.raises(DuplicateError):  # renaming onto another project's name
        t.save_project(other.id, {"name": "RIVERSIDE APARTMENTS"})
    t.save_project(sample.site.id, {"name": "Riverside apartments"})  # own name, other case: fine
    with pytest.raises(DuplicateError):
        t.duplicate_project(other.id, "P-5", "Mayfair")


def test_archived_project_name_suggests_restore(svc, sample):
    t = svc.project_tree
    old = t.save_project(None, {"project_number": "OLD", "name": "Soho Lofts"})
    svc.sites.set_status(old.id, "ARCHIVED", "Finished long ago")
    with pytest.raises(DuplicateError) as e:
        t.save_project(None, {"project_number": "NEW", "name": "Soho lofts"})
    assert "Restore" in str(e.value)


def test_projects_sharing_a_name_before_upgrade_can_still_be_saved(svc, sample):
    t = svc.project_tree
    p = t.save_project(None, {"project_number": "P-9", "name": "Twin"})
    with svc.ctx.db.engine.begin() as c:  # two projects with one name, as could happen before v1.23
        c.execute(text("UPDATE sites SET name = 'Riverside Apartments', name_key = 'riverside apartments' "
                       "WHERE id = :i"), {"i": p.id})
    t.save_project(p.id, {"notes": "still editable"})
    assert svc.sites.get(p.id).notes == "still editable"


# ---- structure: blocks / levels optional, unit on a level --------------------------------------------------
def test_unit_sits_on_a_level_or_directly_in_the_project(svc, kit):
    t = svc.project_tree
    assert t.unit(kit.kitchen.id).level_id == kit.level.id
    desk = t.save_unit(None, {"name": "Reception desk", "quantity": "1"}, site_id=kit.project.id)
    assert t.unit(desk.id).level_id is None  # simple project: no block, no level


# ---- norm history ---------------------------------------------------------------------------------------
def test_every_line_starts_its_norm_history(svc, kit):
    [first] = svc.unit_norms.norm_history(kit.line)
    assert first.quantity_per_unit == D(3) and first.previous_qty is None and first.applies_to == NORM_ALL
    assert first.user  # who set it


def test_changing_the_norm_keeps_history_with_date(svc, kit):
    n = svc.unit_norms
    row = n.change_norm(kit.line, "3.5", NORM_FUTURE, reason="New drawer fronts")
    assert row.quantity_per_unit == D("3.5") and row.previous_qty == D(3) and row.applies_to == NORM_FUTURE
    assert row.reason == "New drawer fronts" and row.changed_at is not None
    assert [r.quantity_per_unit for r in n.norm_history(kit.line)] == [D(3), D("3.5")]
    assert svc.project_tree.material(kit.line).quantity_per_unit == D("3.5")
    # unchanged norm: no new history row
    n.change_norm(kit.line, "3.5", NORM_ALL)
    assert len(n.norm_history(kit.line)) == 2
    with svc.ctx.db.read_session() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.action == "UNIT_NORM_CHANGED"))
    with pytest.raises(ValidationError):
        n.change_norm(kit.line, "4", "SOMETIMES")


def test_editing_quantity_in_the_tree_is_recorded_as_a_norm_change(svc, kit):
    svc.project_tree.update_material(kit.line, {"quantity_per_unit": "4"})
    hist = svc.unit_norms.norm_history(kit.line)
    assert [(r.quantity_per_unit, r.applies_to) for r in hist] == [(D(3), NORM_ALL), (D(4), NORM_ALL)]


def test_copies_start_their_own_norm_history(svc, kit):
    t = svc.project_tree
    copy = t.duplicate_unit(kit.kitchen.id)
    [line] = t.materials(copy.id)
    assert [r.quantity_per_unit for r in svc.unit_norms.norm_history(line.id)] == [D(3)]


# ---- completed units ------------------------------------------------------------------------------------
def test_record_and_cancel_completed_units(svc, kit):
    n = svc.unit_norms
    c = n.record_completion(kit.kitchen.id, "4", date.today() - timedelta(days=1), notes="Level 1 east")
    assert n.units_done(kit.kitchen.id) == D(4)
    with pytest.raises(ValidationError):  # only 6 left
        n.record_completion(kit.kitchen.id, "7")
    with pytest.raises(ValidationError):
        n.record_completion(kit.kitchen.id, "1", date.today() + timedelta(days=1))
    with pytest.raises(ValidationError):  # unit quantity cannot drop below units done
        svc.project_tree.save_unit(kit.kitchen.id, {"quantity": "3"})
    with pytest.raises(ValidationError):  # done units are history: no deleting the unit / level / block
        svc.project_tree.delete_unit(kit.kitchen.id)
    with pytest.raises(ValidationError):
        svc.project_tree.delete_level(kit.level.id)
    with pytest.raises(ValidationError):
        svc.project_tree.delete_block(kit.block.id)
    n.cancel_completion(c.id, "Wrong floor")
    assert n.units_done(kit.kitchen.id) == 0
    [row] = n.completions(kit.kitchen.id)
    assert row.cancelled and row.cancel_reason == "Wrong floor"
    with pytest.raises(ValidationError):
        n.cancel_completion(c.id, "again")
    svc.project_tree.delete_unit(kit.kitchen.id)  # nothing done any more: can go


# ---- recalculate done units vs future only ----------------------------------------------------------------
def test_future_norm_keeps_units_already_done(svc, kit):
    n = svc.unit_norms
    n.record_completion(kit.kitchen.id, "4")
    n.change_norm(kit.line, "5", NORM_FUTURE)
    [need] = n.unit_needs(kit.kitchen.id)
    assert (need.units_done, need.units_left) == (D(4), D(6))
    assert need.used_by_done == D(12)   # 4 kitchens made with 3 sheets each
    assert need.needed_by_rest == D(30)  # 6 kitchens to make with the new 5 sheets
    assert need.total == D(42)


def test_all_norm_recalculates_units_already_done(svc, kit):
    n = svc.unit_norms
    n.record_completion(kit.kitchen.id, "4")
    n.change_norm(kit.line, "5", NORM_ALL)
    [need] = n.unit_needs(kit.kitchen.id)
    assert need.used_by_done == D(20) and need.total == D(50)  # = 10 x 5, as Required shows


def test_mixed_changes_follow_the_rule(svc, kit):
    n = svc.unit_norms
    n.record_completion(kit.kitchen.id, "2")    # made with 3
    n.change_norm(kit.line, "4", NORM_FUTURE)
    n.record_completion(kit.kitchen.id, "3")    # made with 4
    n.change_norm(kit.line, "6", NORM_FUTURE)
    [need] = n.unit_needs(kit.kitchen.id)
    assert need.used_by_done == D(2 * 3 + 3 * 4) and need.needed_by_rest == D(5 * 6)
    n.change_norm(kit.line, "2", NORM_ALL)       # correction: everything recalculated
    [need] = n.unit_needs(kit.kitchen.id)
    assert need.total == D(20)


def test_no_completions_matches_required(svc, kit):
    n = svc.unit_norms
    n.change_norm(kit.line, "5", NORM_FUTURE)
    [need] = n.project_needs(kit.project.id)[kit.kitchen.id]
    assert need.total == D(50) and need.used_by_done == 0


# ---- database upgrade -------------------------------------------------------------------------------------
def test_upgrade_fills_name_keys_and_first_norms(svc, kit):
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR
    db = svc.ctx.db
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0014")
    with db.engine.begin() as c:
        c.execute(text("UPDATE sites SET name_key = NULL"))
    db.dispose()
    db.migrate()
    with db.read_session() as s:
        keys = dict(s.execute(text("SELECT project_number, name_key FROM sites")).all())
        norms = list(s.scalars(select(UnitMaterialNorm)))
    assert keys["TRI"] == "tribeca" and keys["P-1001"] == "riverside apartments"
    assert [(r.unit_material_id, r.quantity_per_unit) for r in norms] == [(kit.line, D(3))]
    with pytest.raises(DuplicateError):
        svc.project_tree.save_project(None, {"project_number": "X", "name": "TRIBECA"})
