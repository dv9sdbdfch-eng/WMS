"""v1.25: project structure (Project -> Block -> Level -> Unit + unit materials) imported from Excel."""

from __future__ import annotations

from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from wms.core.exceptions import ValidationError
from wms.db.models import AuditLog, ProjectMaterial, UnitMaterialNorm
from wms.services.structure_import import (
    ERROR,
    MATERIAL_FIELDS,
    NEW,
    SAME,
    STRUCTURE_FIELDS,
    UPDATE,
    export_problems,
    write_template,
)

D = Decimal
S_HEAD = [t for t, _w in STRUCTURE_FIELDS.values()]
M_HEAD = [t for t, _w in MATERIAL_FIELDS.values()]


def book(path, units, materials=None, s_head=S_HEAD, m_head=M_HEAD):
    wb = Workbook()
    ws = wb.active
    ws.title = "Structure"
    ws.append(s_head)
    for r in units:
        ws.append(list(r))
    if materials is not None:
        wm = wb.create_sheet("Materials")
        wm.append(m_head)
        for r in materials:
            wm.append(list(r))
    wb.save(path)
    return path


UNITS = [
    ("P-2001", "Tribeca", "John Smith", "Block A", "Level 1", "K01", "Kitchen A", 10, ""),
    ("P-2001", "", "", "Block A", "Level 1", "W01", "Wardrobe", 20, ""),
    ("P-2001", "", "", "Block A", "Level 2", "K01", "Kitchen A", 8, ""),
    ("P-2001", "", "", "Block B", "Level 1", "", "Kitchen B", 5, "show flat"),
    ("P-2001", "", "", "", "", "", "Concierge desk", 1, ""),
]


@pytest.fixture
def mats(sample):
    return [
        ("P-2001", "Block A", "Level 1", "K01", "", "MDF-18", 2.5, "VPK-1", ""),
        ("P-2001", "Block A", "Level 1", "K01", "", "edge w22", 30, "", ""),
        ("P-2001", "Block B", "Level 1", "", "Kitchen B", "MDF-18", 3, "", ""),
    ]


def test_template_has_the_sheets_and_columns(tmp_path, svc):
    p = write_template(tmp_path / "t.xlsx")
    wb = load_workbook(p)
    assert wb.sheetnames == ["Structure", "Materials", "How to fill in", "Example"]
    assert [c.value for c in wb["Structure"][1]] == S_HEAD
    assert [c.value for c in wb["Materials"][1]] == M_HEAD
    plan = svc.structure_import.prepare(p)  # the empty template: nothing to import, not a crash
    assert not plan.can_import and plan.problems


def test_new_project_with_blocks_levels_units_and_materials(tmp_path, svc, sample, mats):
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", UNITS, mats))
    assert plan.errors == 0, [r.problems for r in plan.rows]
    assert [r.status for r in plan.units] == [NEW] * 5
    assert [r.status for r in plan.materials] == [NEW] * 3
    proj = plan.projects["P-2001"]
    assert proj.is_new and proj.name == "Tribeca" and (proj.new_blocks, proj.new_levels) == (2, 3)
    assert plan.can_import

    res = svc.structure_import.run(plan)
    assert (res.projects_created, res.blocks_created, res.levels_created) == (1, 2, 3)
    assert (res.units_created, res.lines_created) == (5, 3)
    assert res.backup is not None and res.backup.exists()

    t = svc.project_tree
    site = next(s for s in svc.sites.list() if s.project_number == "P-2001")
    assert site.status == "PLANNED" and site.project_manager == "John Smith"
    blocks = t.blocks(site.id)
    assert [b.name for b in blocks] == ["Block A", "Block B"]
    assert [lv.name for lv in t.levels(blocks[0].id)] == ["Level 1", "Level 2"]
    units = t.units(site.id)
    assert len(units) == 5
    direct = [u for u in units if u.level_id is None]
    assert [u.name for u in direct] == ["Concierge desk"]
    k01 = next(u for u in units if u.code == "K01" and u.quantity == 10)
    lines = t.materials(k01.id)
    assert {(m.product_id, m.quantity_per_unit) for m in lines} == {(sample.mdf.id, D("2.5")), (sample.edge.id, 30)}
    # Required list follows the units (10 x 2.5 + 5 x 3 MDF; 10 x 30 edging)
    with svc.structure_import.db.read_session() as s:
        req = {pm.product_id: pm.required_qty for pm in s.scalars(select(ProjectMaterial)
                                                                  .where(ProjectMaterial.site_id == site.id))}
        assert req == {sample.mdf.id: D(40), sample.edge.id: D(300)}
        assert s.scalar(select(UnitMaterialNorm).limit(1)) is not None
        assert s.scalar(select(AuditLog).where(AuditLog.action == "PROJECT_STRUCTURE_IMPORTED")) is not None


def test_import_again_changes_nothing_and_updates_differences(tmp_path, svc, sample, mats):
    path = book(tmp_path / "a.xlsx", UNITS, mats)
    svc.structure_import.run(svc.structure_import.prepare(path))
    again = svc.structure_import.prepare(path)
    assert {r.status for r in again.rows} == {SAME}
    assert not again.can_import
    with pytest.raises(ValidationError):
        svc.structure_import.run(again)

    units = list(UNITS)
    units[0] = units[0][:7] + (12,) + units[0][8:]           # K01 on Level 1: 10 -> 12
    units.append(("P-2001", "", "", "Block A", "Level 3", "K01", "Kitchen A", 4, ""))  # new level
    m = list(mats)
    m[0] = m[0][:6] + (2.75,) + m[0][7:]                     # norm 2.5 -> 2.75
    plan = svc.structure_import.prepare(book(tmp_path / "b.xlsx", units, m))
    assert [r.status for r in plan.units] == [UPDATE, SAME, SAME, SAME, SAME, NEW]
    assert plan.units[0].changes == ["Quantity 10 -> 12"]
    assert [r.status for r in plan.materials] == [UPDATE, SAME, SAME]
    assert plan.projects["P-2001"].new_levels == 1 and plan.projects["P-2001"].new_blocks == 0
    res = svc.structure_import.run(plan)
    assert (res.projects_created, res.units_created, res.units_updated, res.lines_updated) == (0, 1, 1, 1)
    site = next(s for s in svc.sites.list() if s.project_number == "P-2001")
    assert len(svc.project_tree.units(site.id)) == 6


def test_existing_project_is_matched_by_number_and_never_renamed(tmp_path, svc, sample):
    t = svc.project_tree
    old = t.save_unit(None, {"name": "Old unit", "quantity": "3"}, site_id=sample.site.id)
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", [
        ("p-1001", "Another name", "", "", "", "", "Bathroom", 2, ""),
        ("P-1001", "", "", "", "", "", "old  UNIT", 3, ""),          # same unit by name (case / spaces)
    ]))
    assert plan.errors == 0
    assert [r.status for r in plan.units] == [NEW, SAME]
    assert "not changed" in plan.units[0].problems
    svc.structure_import.run(plan)
    assert svc.sites.get(sample.site.id).name == "Riverside Apartments"
    assert [u.name for u in t.units(sample.site.id)] == ["Old unit", "Bathroom"]
    assert t.unit(old.id).quantity == 3


def test_errors_block_the_import(tmp_path, svc, sample):
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", [
        ("P-3000", "", "", "", "", "", "Kitchen", 2, ""),                    # new project without a name
        ("P-3001", "Riverside apartments", "", "", "", "", "Kitchen", 2, ""),  # name of P-1001
        ("P-1001", "", "", "Block A", "", "", "Kitchen", 2, ""),             # block without level
        ("P-1001", "", "", "", "Level 1", "", "Kitchen", 2, ""),             # level without block
        ("P-1001", "", "", "", "", "", "", 2, ""),                           # no unit name
        ("P-1001", "", "", "", "", "", "Desk", "two", ""),                   # not a number
        ("P-1001", "", "", "", "", "", "Desk", -1, ""),                      # negative
        ("P-1001", "", "", "", "", "U1", "Hall", 1, ""),
        ("P-1001", "", "", "", "", "u-1", "Hall again", 1, ""),             # same code (punctuation ignored)
    ], [
        ("P-1001", "", "", "U1", "", "NO-SUCH", 1, "", ""),                  # unknown material
        ("P-1001", "", "", "", "Nowhere", "MDF-18", 1, "", ""),              # unknown unit
    ]))
    by_row = {r.row: r for r in plan.units}
    assert "Project Name" in by_row[2].problems
    assert "already exists" in by_row[3].problems
    assert "Level is empty" in by_row[4].problems
    assert "Block is empty" in by_row[5].problems
    assert "Unit Name is empty" in by_row[6].problems
    assert "not a number" in by_row[7].problems
    assert "negative" in by_row[8].problems
    assert by_row[9].status == NEW
    assert "already on row 9" in by_row[10].problems
    assert "not in the WMS" in plan.materials[0].problems
    assert "neither on the Structure sheet" in plan.materials[1].problems
    assert not plan.can_import
    with pytest.raises(ValidationError):
        svc.structure_import.run(plan)
    report = load_workbook(export_problems(plan, tmp_path / "problems.xlsx")).active
    assert report.max_row > 10


def test_new_project_without_a_name_is_an_error(tmp_path, svc, sample):
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", [("P-3000", "", "", "", "", "", "Kitchen", 2, "")]))
    assert plan.errors == 1 and "Project Name" in plan.units[0].problems
    assert not plan.can_import


def test_quantity_cannot_drop_below_units_done(tmp_path, svc, sample):
    t = svc.project_tree
    u = t.save_unit(None, {"name": "Kitchen", "unit_code": "K1", "quantity": "10"}, site_id=sample.site.id)
    svc.unit_norms.record_completion(u.id, "6")
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", [("P-1001", "", "", "", "", "K1", "Kitchen", 5, "")]))
    assert plan.units[0].status == ERROR and "already done" in plan.units[0].problems


def test_archived_project_is_refused(tmp_path, svc, sample):
    svc.sites.save(sample.site.id, {**{f: getattr(svc.sites.get(sample.site.id), f) for f in ("project_number",
                                                                                             "name")},
                                    "status": "ARCHIVED"})
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", [("P-1001", "", "", "", "", "", "Kitchen", 1, "")]))
    assert plan.units[0].status == ERROR and "archived" in plan.units[0].problems


def test_changed_since_preview_is_refused(tmp_path, svc, sample):
    path = book(tmp_path / "a.xlsx", [("P-1001", "", "", "", "", "", "Kitchen", 1, "")])
    plan = svc.structure_import.prepare(path)
    svc.project_tree.save_unit(None, {"name": "Kitchen", "quantity": "1"}, site_id=sample.site.id)
    with pytest.raises(ValidationError, match="changed since the preview"):
        svc.structure_import.run(plan)


def test_cancel_rolls_everything_back(tmp_path, svc, sample, mats):
    plan = svc.structure_import.prepare(book(tmp_path / "a.xlsx", UNITS, mats))
    with pytest.raises(ValidationError, match="cancelled"):
        svc.structure_import.run(plan, progress=lambda done, total: done < 4)
    assert not any(s.project_number == "P-2001" for s in svc.sites.list())


def test_other_header_words_and_header_below_a_title(tmp_path, svc, sample):
    wb = Workbook()
    ws = wb.active
    ws.title = "Units list"
    ws.append(["Tribeca structure from the architect"])
    ws.append([])
    ws.append(["Job No", "Building", "Floor", "Unit", "Qty"])
    ws.append(["P-1001", "Block A", "Level 1", "Kitchen", 4])
    ws.append(["Total", "", "", "", 4])
    wb.save(tmp_path / "x.xlsx")
    plan = svc.structure_import.prepare(tmp_path / "x.xlsx")
    assert plan.errors == 0 and len(plan.units) == 1
    assert plan.units[0].place[1:] == ("block a", "level 1")


def test_needs_projects_edit_permission(tmp_path, svc, sample):
    from wms.core.exceptions import PermissionDeniedError
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "viewer", "full_name": "V", "role_id": roles["VIEWER"].id}, "secret1")
    svc.auth.login("viewer", "secret1")
    with pytest.raises(PermissionDeniedError):
        svc.structure_import.prepare(book(tmp_path / "a.xlsx", UNITS))


# ---- the screen -----------------------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture


def test_import_window_preview_then_import(ui, sample, tmp_path, mats):  # noqa: F811
    from wms.ui.dialogs.structure_import import StructureImportDialog
    app, win, msgs, _ = ui
    win.go("projects")
    page = win.page("projects")
    from PySide6.QtWidgets import QPushButton
    assert any(b.text() == "Import from Excel..." for b in page.findChildren(QPushButton))
    dlg = StructureImportDialog(app, page)
    assert not dlg.import_btn.isEnabled()
    bad = book(tmp_path / "bad.xlsx", [("P-2001", "", "", "", "", "", "Kitchen", 1, "")])
    assert dlg.check(str(bad))
    assert not dlg.import_btn.isEnabled() and dlg.export_btn.isEnabled()
    assert dlg.filter.currentIndex() == 1 and dlg.units_table.rowCount() == 1  # errors shown first
    assert dlg.export_problems(str(tmp_path / "p.xlsx")) is not None

    assert dlg.check(str(book(tmp_path / "a.xlsx", UNITS, mats)))
    assert dlg.import_btn.isEnabled()
    assert dlg.units_table.rowCount() == 5 and dlg.mats_table.rowCount() == 3
    assert dlg.tabs.tabText(0) == "Structure (5)"
    assert dlg.do_import(ask=False)
    assert dlg.result.units_created == 5
    assert not dlg.import_btn.isEnabled()  # checked again: everything is "No change" now
    assert "nothing to import" in dlg.summary.text()
    page.refresh()
    assert any(k.startswith("P:") or k.startswith("M:") for k in page.items)
    assert not _errors(msgs)
