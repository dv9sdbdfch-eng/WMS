"""v1.15 Projects module: Manager -> Project -> Unit -> Materials.

Covers the acceptance list of the specification (section 16) through the services, the demand
calculation, the database upgrade and the Project Tree screen.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from wms.core.exceptions import DuplicateError, ValidationError
from wms.db.models import AuditLog, StockLevel, StockMovement
from wms.services.goods_out import IssueLine
from wms.services.project_demand import DemandScope
from wms.services.project_tree import UNASSIGNED, TreeFilter

D = Decimal


@pytest.fixture
def tribeca(svc, sample):
    """John Smith -> Tribeca -> Kitchen K01 x 24 (H3311 x3, H1180 x2, Hinge x8), Wardrobe W02 x 12."""
    units = {u.code: u for u in svc.reference.units()}
    mk = lambda code, name, unit="SHEET": svc.products.save(None, {  # noqa: E731
        "item_type": "RAW_MATERIAL", "internal_code": code, "name": name, "unit_id": units[unit].id})
    h3311, h1180, hinge = mk("H3311", "H3311 Board"), mk("H1180", "H1180 Board"), mk("HINGE-XYZ", "Hinge XYZ", "EA")
    t = svc.project_tree
    john = t.save_manager(None, {"name": "John Smith"})
    p = t.save_project(None, {"project_number": "TRI", "name": "Tribeca", "manager_id": john.id, "status": "ACTIVE"})
    kitchen = t.save_unit(None, {"name": "Kitchen", "unit_code": "K01", "quantity": "24"}, site_id=p.id)
    t.add_materials(kitchen.id, [(h3311.id, "3"), (h1180.id, "2"), (hinge.id, "8")])
    wardrobe = t.save_unit(None, {"name": "Wardrobe", "unit_code": "W02", "quantity": "12"}, site_id=p.id)
    t.add_materials(wardrobe.id, [(h3311.id, "1.5")], vpk_code="VPK-778")
    return type("T", (), dict(john=john, project=p, kitchen=kitchen, wardrobe=wardrobe, h3311=h3311, h1180=h1180,
                              hinge=hinge, svc=svc, sample=sample))


def _required(lines):
    return {d.code: d.required for d in lines}


# ---- acceptance 1-4: managers and projects ----------------------------------------------------------
def test_managers_projects_move_and_unassign(svc, tribeca):
    t = svc.project_tree
    anna = t.save_manager(None, {"name": "Anna Lee", "email": "anna@example.co.uk"})
    second = t.save_project(None, {"project_number": "MAY", "name": "Mayfair", "manager_id": tribeca.john.id})
    third = t.save_project(None, {"project_number": "SOH", "name": "Soho", "manager_id": tribeca.john.id})
    assert {p.code for p in t.projects(tribeca.john.id)} == {"TRI", "MAY", "SOH"}
    assert t.assign_manager([second.id], anna.id) == 1  # move to another manager
    assert [p.code for p in t.projects(anna.id)] == ["MAY"]
    assert svc.sites.get(second.id).project_manager == "Anna Lee"  # project list keeps showing the name
    t.assign_manager([third.id], None)  # unassign
    assert [p.code for p in t.projects(UNASSIGNED)] == ["P-1001", "SOH"]  # P-1001: sample project, no manager
    assert t.unassigned_count() == 2
    with pytest.raises(DuplicateError):
        t.save_manager(None, {"name": "  john   SMITH "})


def test_deleting_a_manager_never_deletes_projects(svc, tribeca):
    t = svc.project_tree
    anna = t.save_manager(None, {"name": "Anna Lee"})
    t.save_project(None, {"project_number": "MAY", "name": "Mayfair", "manager_id": anna.id})
    assert t.delete_manager(anna.id) == 1
    may = [p for p in t.projects(UNASSIGNED) if p.code == "MAY"]
    assert may and svc.sites.get(may[0].id).project_manager is None
    # or hand them to someone else
    bob = t.save_manager(None, {"name": "Bob"})
    assert t.delete_manager(tribeca.john.id, move_projects_to=bob.id) == 1
    assert [p.code for p in t.projects(bob.id)] == ["TRI"]
    assert len(t.units(tribeca.project.id)) == 2  # the structure is untouched


def test_rename_manager_updates_project_display_name(svc, tribeca):
    svc.project_tree.save_manager(tribeca.john.id, {"name": "John A. Smith"})
    assert svc.sites.get(tribeca.project.id).project_manager == "John A. Smith"


def test_project_form_name_links_to_manager(svc, sample):
    """The old free-text 'Project manager' field now links to (or creates) a manager record."""
    site = svc.sites.save(None, {"project_number": "P-2", "name": "Two", "project_manager": "Kate Moss"})
    kate = [m for m in svc.project_tree.managers() if m.name == "Kate Moss"]
    assert kate and site.manager_id == kate[0].id
    again = svc.sites.save(None, {"project_number": "P-3", "name": "Three", "project_manager": "kate moss"})
    assert again.manager_id == kate[0].id and again.project_manager == "Kate Moss"


# ---- acceptance 5-10: units and materials -----------------------------------------------------------
def test_units_quantities_reorder_and_materials(svc, tribeca):
    t = svc.project_tree
    vanity = t.save_unit(None, {"name": "Vanity Unit", "quantity": "6"}, site_id=tribeca.project.id)
    assert [u.name for u in t.units(tribeca.project.id)] == ["Kitchen", "Wardrobe", "Vanity Unit"]
    t.move_unit(vanity.id, -1)
    assert [u.name for u in t.units(tribeca.project.id)] == ["Kitchen", "Vanity Unit", "Wardrobe"]
    t.set_unit_position(vanity.id, 0)
    assert [u.name for u in t.units(tribeca.project.id)][0] == "Vanity Unit"
    t.save_unit(tribeca.kitchen.id, {"quantity": "30"})
    assert t.unit(tribeca.kitchen.id).quantity == D("30")
    with pytest.raises(ValidationError):
        t.save_unit(tribeca.kitchen.id, {"quantity": "-1"})
    lines = t.materials(tribeca.kitchen.id)
    assert [m.code for m in lines] == ["H3311", "H1180", "HINGE-XYZ"]
    assert lines[2].total == D("240")  # 30 x 8
    t.update_material(lines[0].id, {"quantity_per_unit": "4", "notes": "carcass"})
    t.move_material(lines[2].id, -2)
    t.replace_material(lines[1].id, tribeca.sample.mdf.id)
    t.remove_materials([lines[0].id])
    assert [m.code for m in t.materials(tribeca.kitchen.id)] == ["HINGE-XYZ", "MDF-18"]


def test_duplicate_unit_and_project_copy_structure(svc, tribeca):
    t = svc.project_tree
    copy = t.duplicate_unit(tribeca.kitchen.id)
    assert [u.name for u in t.units(tribeca.project.id)] == ["Kitchen", "Kitchen (copy)", "Wardrobe"]
    assert len(t.materials(copy.id)) == 3
    dup = t.duplicate_project(tribeca.project.id, "tri-2", "Tribeca Phase 2")
    assert dup.code == "TRI-2" and dup.status == "PLANNED" and dup.manager_id == tribeca.john.id
    assert [u.name for u in t.units(dup.id)] == ["Kitchen", "Kitchen (copy)", "Wardrobe"]
    with pytest.raises(DuplicateError):
        t.duplicate_project(tribeca.project.id, "TRI", "x")


def test_move_unit_to_another_project(svc, tribeca):
    t = svc.project_tree
    other = t.save_project(None, {"project_number": "MAY", "name": "Mayfair"})
    t.move_unit_to_project(tribeca.wardrobe.id, other.id)
    assert [u.name for u in t.units(other.id)] == ["Wardrobe"]
    assert len(t.materials(tribeca.wardrobe.id)) == 1


def test_product_is_linked_never_copied(svc, tribeca):
    t = svc.project_tree
    other = t.save_project(None, {"project_number": "MAY", "name": "Mayfair"})
    u = t.save_unit(None, {"name": "Desk", "quantity": "2"}, site_id=other.id)
    t.add_materials(u.id, [(tribeca.h3311.id, "1")])
    with svc.ctx.db.read_session() as s:
        from wms.db.models import Product
        assert s.scalar(select(func.count(Product.id)).where(Product.internal_code == "H3311")) == 1
    # a product used in a unit is archived (never deleted) when removed
    assert svc.products.removal_plan(tribeca.h3311.id).permanent is False


# ---- acceptance 14: demand ---------------------------------------------------------------------------
def test_demand_per_unit_project_manager_and_all(svc, tribeca):
    d = svc.project_demand
    assert _required(d.for_unit(tribeca.kitchen.id)) == {"H1180": D("48"), "H3311": D("72"), "HINGE-XYZ": D("192")}
    # 24 x 3 + 12 x 1.5 = 90
    assert _required(d.for_project(tribeca.project.id)) == {"H1180": D("48"), "H3311": D("90"), "HINGE-XYZ": D("192")}
    assert _required(d.for_manager(tribeca.john.id))["H3311"] == D("90")
    other = svc.project_tree.save_project(None, {"project_number": "MAY", "name": "Mayfair", "status": "PLANNED"})
    u = svc.project_tree.save_unit(None, {"name": "Desk", "quantity": "2.5"}, site_id=other.id)
    svc.project_tree.add_materials(u.id, [(tribeca.h3311.id, "0.333")])
    assert _required(d.for_active_projects())["H3311"] == D("90.833")  # 2.5 x 0.333 = 0.8325 -> 0.833
    closed = svc.project_tree.save_project(other.id, {"status": "COMPLETED"})
    assert closed.status == "COMPLETED"
    assert _required(d.for_active_projects())["H3311"] == D("90")
    assert _required(d.calculate(DemandScope(statuses=None)))["H3311"] == D("90.833")


def test_demand_feeds_project_material_list_on_request(svc, tribeca):
    rows = svc.project_demand.compare_with_project_list(tribeca.project.id)
    assert all(r.on_list is None for r in rows)
    assert svc.project_demand.apply_to_project_list(tribeca.project.id) == 3
    summ = svc.materials.project_summary(tribeca.project.id)
    assert summ.required == D("330")  # 48 + 90 + 192
    assert all(r.in_step for r in svc.project_demand.compare_with_project_list(tribeca.project.id))


# ---- acceptance 12: search ---------------------------------------------------------------------------
def test_search_finds_every_level_and_its_path(svc, tribeca):
    t = svc.project_tree
    m = t.search("hinge")
    assert len(m.materials) == 1 and m.unit_paths == {tribeca.kitchen.id}
    assert m.project_paths == {tribeca.project.id} and m.manager_paths == {tribeca.john.id}
    assert t.search("VPK-778").materials and t.search("w02").units == {tribeca.wardrobe.id}
    assert t.search("tribeca").projects == {tribeca.project.id}
    assert t.search("smith").managers == {tribeca.john.id}
    assert t.search("nothing like this").total == 0
    # filters
    assert t.search("hinge", TreeFilter(statuses=["COMPLETED"])).total == 0
    assert t.search("hinge", TreeFilter(manager_id=UNASSIGNED)).total == 0


# ---- acceptance 10 / 15: protection --------------------------------------------------------------------
def test_deleting_project_says_what_goes_and_keeps_history(svc, tribeca):
    plan = svc.sites.removal_plan(tribeca.project.id)
    assert "2 units with 4 material lines" in plan.message and plan.permanent
    svc.sites.remove(tribeca.project.id)
    with svc.ctx.db.read_session() as s:
        assert s.execute(text("SELECT count(*) FROM project_units")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM unit_materials")).scalar() == 0


def test_project_with_history_is_archived_with_its_structure(svc, tribeca):
    sample = tribeca.sample
    sample.receive(sample.mdf, 10)
    svc.goods_out.post({"site_id": tribeca.project.id, "issue_date": date.today()}, [IssueLine(sample.mdf.id, "2")])
    plan = svc.sites.removal_plan(tribeca.project.id)
    assert not plan.permanent and "kept with the archived project" in plan.message
    svc.sites.remove(tribeca.project.id)
    assert len(svc.project_tree.units(tribeca.project.id)) == 2
    with pytest.raises(ValidationError):  # archived: read only
        svc.project_tree.save_unit(tribeca.kitchen.id, {"quantity": "1"})


def test_structure_never_touches_stock_or_ledger(svc, tribeca):
    sample = tribeca.sample
    sample.receive(sample.mdf, 10)

    def snapshot():
        with svc.ctx.db.read_session() as s:
            return (s.scalar(select(func.count(StockMovement.id))),
                    sorted((x.product_id, x.bin_id, x.quantity) for x in s.scalars(select(StockLevel))))
    before = snapshot()
    t = svc.project_tree
    u = t.save_unit(None, {"name": "Desk", "quantity": "5"}, site_id=tribeca.project.id)
    t.add_materials(u.id, [(sample.mdf.id, "2")])
    t.duplicate_project(tribeca.project.id, "TRI-9", "Copy")
    t.delete_unit(u.id)
    t.delete_manager(tribeca.john.id)
    assert snapshot() == before


def test_every_change_is_audited(svc, tribeca):
    with svc.ctx.db.read_session() as s:
        actions = set(s.scalars(select(AuditLog.action)))
    assert {"MANAGER_CREATED", "UNIT_CREATED", "UNIT_MATERIAL_ADDED"} <= actions


def test_failed_edit_changes_nothing(svc, tribeca):
    """One transaction per operation: a bad value leaves everything as it was."""
    t = svc.project_tree
    before = t.materials(tribeca.kitchen.id)
    with pytest.raises(ValidationError):
        t.add_materials(tribeca.kitchen.id, [(tribeca.h3311.id, "1"), (tribeca.h1180.id, "-2")])
    assert t.materials(tribeca.kitchen.id) == before


def test_viewer_cannot_change_the_structure(svc, tribeca):
    from wms.core.exceptions import PermissionDeniedError
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "viewer", "full_name": "V", "role_id": roles["VIEWER"].id}, "secret1")
    svc.auth.login("viewer", "secret1")
    assert svc.project_tree.units(tribeca.project.id)
    with pytest.raises(PermissionDeniedError):
        svc.project_tree.save_unit(tribeca.kitchen.id, {"quantity": "1"})


# ---- acceptance 13: upgrade of an existing database ----------------------------------------------------
def test_upgrade_turns_typed_manager_names_into_managers(svc, sample):
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR
    db = svc.ctx.db
    for n, who in (("P-2", "John Smith"), ("P-3", " john  smith "), ("P-4", "Anna Lee")):
        svc.sites.save(None, {"project_number": n, "name": n})
        with db.engine.begin() as c:  # as typed before v1.15 (no link)
            c.execute(text("UPDATE sites SET project_manager = :w, manager_id = NULL WHERE project_number = :n"),
                      {"w": who, "n": n})
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0011")
    db.dispose()
    db.migrate()
    from pathlib import Path
    file = Path(db.engine.url.database)
    assert file.with_name(f"{file.stem}.before-0012{file.suffix}").exists()  # copy made before the upgrade
    managers = {m.name: m for m in svc.project_tree.managers()}
    assert set(managers) == {"Anna Lee", "John Smith"}
    assert {p.code for p in svc.project_tree.projects(managers["John Smith"].id)} == {"P-2", "P-3"}
    assert svc.sites.get(sample.site.id).manager_id is None  # no name typed: unassigned


# ---- the Project Tree screen ------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture

from wms.ui.pages.projects import KEY  # noqa: E402


def _open_page(win):
    win.go("projects")
    page = win.page("projects")
    page.refresh()
    return page


def _texts(item):
    return [item.text(c) for c in range(5)]


def test_tree_screen_lazy_branches_and_totals(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    john = page.items[f"M:{tribeca.john.id}"]
    assert john.text(0) == "John Smith" and john.childCount() == 0  # not read until opened
    assert f"P:{tribeca.project.id}" not in page.items
    john.setExpanded(True)
    tri = page.items[f"P:{tribeca.project.id}"]
    tri.setExpanded(True)
    kitchen = page.items[f"U:{tribeca.kitchen.id}"]
    assert _texts(kitchen)[:3] == ["Kitchen", "K01", "24"]
    kitchen.setExpanded(True)
    hinge = [kitchen.child(i) for i in range(kitchen.childCount()) if kitchen.child(i).text(1) == "HINGE-XYZ"][0]
    assert hinge.text(2) == "8 / unit" and hinge.text(3) == "192 EA"
    assert f"P:{tribeca.sample.site.id}" not in page.items  # unassigned group not opened
    assert page.items["N:"].text(0) == "Unassigned projects"
    # every row has its own menu
    for key in (f"M:{tribeca.john.id}", "N:", f"P:{tribeca.project.id}", f"U:{tribeca.kitchen.id}"):
        assert page.build_menu(page.items[key]).actions()
    assert [a.text() for a in page.build_menu(hinge).actions() if a.text()][:2] == ["Edit Quantity...", "Replace..."]
    assert not _errors(msgs), msgs


def test_tree_inline_edit_and_side_panel(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.items[f"M:{tribeca.john.id}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    kitchen = page.items[f"U:{tribeca.kitchen.id}"]
    kitchen.setExpanded(True)
    assert page.commit_edit(kitchen, 2, "10")  # Kitchen x 10
    assert app.services.project_tree.unit(tribeca.kitchen.id).quantity == Decimal("10")
    kitchen = page.items[f"U:{tribeca.kitchen.id}"]
    totals = {kitchen.child(i).text(1): kitchen.child(i).text(3) for i in range(kitchen.childCount())}
    assert totals["HINGE-XYZ"] == "80 EA"  # the materials below follow
    assert not page.commit_edit(kitchen, 2, "-3")  # refused, old value back
    assert [m for m in msgs if m[0] == "warning"]
    msgs.clear()
    assert page.items[f"U:{tribeca.kitchen.id}"].text(2) == "10"
    # side panel: select a project, change its name there
    page.select(f"P:{tribeca.project.id}")
    assert page.panel.currentWidget() is page.panel.pages["P"]
    assert page.panel.p_table.items()  # units' demand vs the material list
    page.panel.p_name.setText("Tribeca Tower")
    page.panel.save_project()
    assert page.items[f"P:{tribeca.project.id}"].text(0) == "Tribeca Tower"
    page.select(f"U:{tribeca.kitchen.id}")
    assert {d.code for d in page.panel.u_table.items()} == {"H3311", "H1180", "HINGE-XYZ"}
    assert not _errors(msgs), msgs


def test_tree_search_opens_the_matching_branch(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.search.setText("hinge")
    page.refresh()
    assert page.items[f"M:{tribeca.john.id}"].isExpanded()
    assert page.items[f"P:{tribeca.project.id}"].isExpanded()
    assert page.items[f"U:{tribeca.kitchen.id}"].isExpanded()
    assert f"U:{tribeca.wardrobe.id}" not in page.items  # no match below it: hidden while searching
    kitchen = page.items[f"U:{tribeca.kitchen.id}"]
    assert kitchen.childCount() == 1 and kitchen.child(0).text(1) == "HINGE-XYZ"
    assert "1 match" in page.status.text()
    page.search.setText("")
    page.refresh()
    assert page.status.text().endswith("Delete removes.")
    page.manager_filter.setCurrentIndex(page.manager_filter.findData(UNASSIGNED))
    assert f"M:{tribeca.john.id}" not in page.items and "N:" in page.items
    assert not _errors(msgs), msgs


def test_tree_open_branches_survive_a_restart(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.items[f"M:{tribeca.john.id}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    from wms.ui.pages.projects import ProjectsPage
    again = ProjectsPage(app)
    again.refresh()
    assert again.items[f"P:{tribeca.project.id}"].isExpanded()
    assert not again.items[f"U:{tribeca.kitchen.id}"].isExpanded()
    again.collapse_all()
    third = ProjectsPage(app)
    third.refresh()
    assert not third.items[f"M:{tribeca.john.id}"].isExpanded()


def test_tree_actions_move_duplicate_delete(ui, tribeca, monkeypatch):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.expand_all()
    page.select(f"U:{tribeca.wardrobe.id}")
    page.move_selected(-1)
    assert [u.name for u in app.services.project_tree.units(tribeca.project.id)] == ["Wardrobe", "Kitchen"]
    page.duplicate_unit(app.services.project_tree.unit(tribeca.kitchen.id))
    assert page.current_key().startswith("U:")
    page.delete_unit(app.services.project_tree.unit(tribeca.kitchen.id))  # confirmed automatically in tests
    assert [u.name for u in app.services.project_tree.units(tribeca.project.id)] == ["Wardrobe", "Kitchen (copy)"]
    assert any("3 material lines" in m[2] for m in msgs if m[0] == "confirm")
    page.change_manager(app.services.project_tree.project(tribeca.project.id), None)
    assert page.items["N:"].isExpanded() and f"P:{tribeca.project.id}" in page.items
    page.apply_demand(app.services.project_tree.project(tribeca.project.id))
    assert app.services.materials.project_summary(tribeca.project.id).required > 0
    assert not _errors(msgs), msgs


# ---- Block and Level (Manager -> Project -> Block -> Level -> Unit -> Material) ----------------------
@pytest.fixture
def tower(svc, tribeca):
    """Tribeca -> Block A -> Level 1 -> Kitchen K10 x 4 (H3311 x 2) and Bath B10 x 2 (Hinge x 6)."""
    t = svc.project_tree
    block = t.save_block(None, {"name": "Block A", "code": "A"}, tribeca.project.id)
    level = t.save_level(None, {"name": "Level 1", "code": "L1"}, block.id)
    k10 = t.save_unit(None, {"name": "Kitchen", "unit_code": "K10", "quantity": "4"}, level_id=level.id)
    t.add_materials(k10.id, [(tribeca.h3311.id, "2")])
    b10 = t.save_unit(None, {"name": "Bath", "unit_code": "B10", "quantity": "2"}, level_id=level.id)
    t.add_materials(b10.id, [(tribeca.hinge.id, "6")])
    return type("W", (), dict(block=block, level=level, k10=k10, b10=b10))


def test_blocks_and_levels_hold_units(svc, tribeca, tower):
    t = svc.project_tree
    p = t.project(tribeca.project.id)
    assert (p.blocks, p.direct_units, p.units) == (1, 2, 4)  # Kitchen K01 + Wardrobe sit directly in the project
    assert [b.name for b in t.blocks(p.id)] == ["Block A"] and t.blocks(p.id)[0].units == 2
    assert [lv.name for lv in t.levels(tower.block.id)] == ["Level 1"]
    assert [u.code for u in t.units(None, level_id=tower.level.id)] == ["K10", "B10"]
    assert [u.code for u in t.units(p.id, level_id=None)] == ["K01", "W02"]  # DIRECT
    assert t.unit(tower.k10.id).site_id == p.id and t.unit(tower.k10.id).parent_key == f"V:{tower.level.id}"
    with pytest.raises(ValidationError):
        t.save_level(None, {"name": " "}, tower.block.id)


def test_demand_for_block_level_and_project(svc, tribeca, tower):
    d = svc.project_demand
    assert _required(d.for_level(tower.level.id)) == {"H3311": D(8), "HINGE-XYZ": D(12)}
    assert _required(d.for_block(tower.block.id)) == {"H3311": D(8), "HINGE-XYZ": D(12)}
    # project = direct units (H3311 24x3 + 12x1.5 = 90, hinge 192) + the level (8 / 12)
    proj = _required(d.for_project(tribeca.project.id))
    assert proj["H3311"] == D(98) and proj["HINGE-XYZ"] == D(204)


def test_duplicate_level_copies_a_whole_floor(svc, tribeca, tower):
    t = svc.project_tree
    copy = t.duplicate_level(tower.level.id)
    assert copy.name == "Level 1 (copy)" and copy.units == 2
    t.save_level(copy.id, {"name": "Level 2", "code": "L2"})
    assert [lv.name for lv in t.levels(tower.block.id)] == ["Level 1", "Level 2"]
    assert _required(svc.project_demand.for_block(tower.block.id))["HINGE-XYZ"] == D(24)
    t.move_level(copy.id, -1)
    assert [lv.name for lv in t.levels(tower.block.id)] == ["Level 2", "Level 1"]
    block2 = t.duplicate_block(tower.block.id)
    assert block2.levels == 2 and block2.units == 4
    assert [b.name for b in t.blocks(tribeca.project.id)] == ["Block A", "Block A (copy)"]
    # duplicating the project copies blocks and levels too
    dup = t.duplicate_project(tribeca.project.id, "TRI-2", "Tribeca 2")
    assert (dup.blocks, dup.units) == (2, 8 + 2)


def test_move_unit_between_levels_and_projects(svc, tribeca, tower):
    t = svc.project_tree
    lvl2 = t.save_level(None, {"name": "Level 2"}, tower.block.id)
    t.move_unit_to(tower.b10.id, None, lvl2.id)
    assert [u.code for u in t.units(None, level_id=lvl2.id)] == ["B10"]
    t.move_unit_to(tower.b10.id, tribeca.project.id, None)  # straight into the project
    assert t.unit(tower.b10.id).level_id is None
    places = t.unit_places()
    assert any(lid == lvl2.id and "Block A" in text and "Level 2" in text for text, _sid, lid in places)
    t.move_unit(tower.b10.id, -1)  # ordered among the project's direct units only
    assert [u.code for u in t.units(tribeca.project.id, level_id=None)] == ["K01", "B10", "W02"]


def test_delete_level_and_block_say_what_goes(svc, tribeca, tower):
    t = svc.project_tree
    assert t.level_contents(tower.level.id) == (2, 2)
    assert t.block_contents(tower.block.id) == (1, 2, 2)
    assert t.delete_level(tower.level.id) == (2, 2)
    assert t.levels(tower.block.id) == [] and len(t.units(tribeca.project.id)) == 2
    lv = t.save_level(None, {"name": "Level 3"}, tower.block.id)
    t.save_unit(None, {"name": "Desk", "quantity": "1"}, level_id=lv.id)
    assert t.delete_block(tower.block.id) == (1, 1, 0)
    assert t.blocks(tribeca.project.id) == []
    with svc.ctx.db.read_session() as s:
        actions = set(s.scalars(select(AuditLog.action)))
    assert {"BLOCK_CREATED", "LEVEL_CREATED", "LEVEL_DELETED", "BLOCK_DELETED"} <= actions


def test_search_finds_blocks_levels_and_units_inside(svc, tribeca, tower):
    m = svc.project_tree.search("level 1")
    assert m.levels == {tower.level.id} and tower.block.id in m.block_paths
    m = svc.project_tree.search("B10")
    assert m.units == {tower.b10.id} and tower.level.id in m.level_paths and tower.block.id in m.block_paths
    assert tribeca.project.id in m.project_paths


def test_tree_screen_blocks_levels(ui, tribeca, tower, monkeypatch):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.expand_all()
    tri = page.items[f"P:{tribeca.project.id}"]
    assert tri.child(0).data(0, KEY) == f"B:{tower.block.id}"  # blocks first, then direct units
    assert "1 block" in tri.text(4)
    block = page.items[f"B:{tower.block.id}"]
    assert block.text(0) == "Block A" and "1 level" in block.text(4)
    level = page.items[f"V:{tower.level.id}"]
    assert level.isExpanded() and level.childCount() == 2
    k10 = page.items[f"U:{tower.k10.id}"]
    k10.setExpanded(True)
    assert k10.child(0).text(3) == "8 SHEET"
    for key in (f"B:{tower.block.id}", f"V:{tower.level.id}"):
        assert page.build_menu(page.items[key]).actions()
    # rename inline, side panel shows the level's demand
    assert page.commit_edit(level, 0, "Ground Floor")
    assert app.services.project_tree.level(tower.level.id).name == "Ground Floor"
    page.select(f"V:{tower.level.id}")
    assert page.panel.currentWidget() is page.panel.pages["V"]
    assert {d.code for d in page.panel.v_table.items()} == {"H3311", "HINGE-XYZ"}
    page.select(f"B:{tower.block.id}")
    assert page.panel.currentWidget() is page.panel.pages["B"]
    # duplicate the level, then delete the block (confirmed automatically in tests)
    page.duplicate_group(f"V:{tower.level.id}", app.services.project_tree.level(tower.level.id))
    assert page.items[f"B:{tower.block.id}"].childCount() == 2
    page.delete_group(f"B:{tower.block.id}", app.services.project_tree.block(tower.block.id))
    assert any("2 levels" in m[2] for m in msgs if m[0] == "confirm")
    assert f"B:{tower.block.id}" not in page.items
    assert app.services.project_tree.project(tribeca.project.id).blocks == 0
    assert not _errors(msgs), msgs
