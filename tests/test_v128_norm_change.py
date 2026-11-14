"""v1.28 stage 4: changing a norm asks "recalculate units already made, or future units only";
Required, the Projects tree and the reports follow the choice."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture
from test_v123_norms import kit  # noqa: F401 - Tribeca / Kitchen K01 x 10 / H3311 x 3

from wms.db.models import ProjectMaterial
from wms.services.unit_norms import NORM_ALL, NORM_FUTURE

D = Decimal


def _required(svc, site_id, product_id):
    with svc.ctx.db.read_session() as s:
        return s.scalar(select(ProjectMaterial.required_qty).where(ProjectMaterial.site_id == site_id,
                                                                   ProjectMaterial.product_id == product_id))


def _demand(svc, k):
    return {d.product_id: d.required for d in svc.project_demand.for_project(k.project.id)}[k.board.id]


def test_future_change_keeps_units_made_in_required_and_reports(svc, kit):  # noqa: F811
    svc.unit_norms.record_completion(kit.kitchen.id, "4")          # 4 kitchens made with 3 sheets
    svc.unit_norms.change_norm(kit.line, "5", NORM_FUTURE, "New fronts")
    assert _demand(svc, kit) == D(42)                               # 4 x 3 + 6 x 5
    assert _required(svc, kit.project.id, kit.board.id) == D(42)    # Required / Remaining follow
    for scope in (svc.project_demand.for_unit(kit.kitchen.id), svc.project_demand.for_level(kit.level.id),
                  svc.project_demand.for_block(kit.block.id), svc.project_demand.for_active_projects()):
        assert {d.product_id: d.required for d in scope}[kit.board.id] == D(42)
    node = svc.project_tree.material(kit.line)
    assert node.total == D(42) and node.keeps_old_norm and node.units_done == D(4)


def test_all_change_recalculates_units_made(svc, kit):  # noqa: F811
    svc.unit_norms.record_completion(kit.kitchen.id, "4")
    svc.unit_norms.change_norm(kit.line, "5", NORM_ALL)
    assert _demand(svc, kit) == _required(svc, kit.project.id, kit.board.id) == D(50)
    assert not svc.project_tree.material(kit.line).keeps_old_norm


def test_required_follows_units_made_and_cancelled(svc, kit):  # noqa: F811
    n = svc.unit_norms
    c = n.record_completion(kit.kitchen.id, "4")
    n.change_norm(kit.line, "5", NORM_FUTURE)
    n.record_completion(kit.kitchen.id, "2")                         # made with 5: no change
    assert _required(svc, kit.project.id, kit.board.id) == D(42)
    n.cancel_completion(c.id, "Wrong unit")                          # those 4 are not made after all
    assert _demand(svc, kit) == _required(svc, kit.project.id, kit.board.id) == D(50)


def test_production_dashboard_done_marks_count(svc, kit):  # noqa: F811
    p = svc.unit_progress
    [m1, _m2] = p.mark_many([(kit.kitchen.id, 1), (kit.kitchen.id, 2)], "DONE")
    svc.unit_norms.change_norm(kit.line, "5", NORM_FUTURE)
    assert _required(svc, kit.project.id, kit.board.id) == D(2 * 3 + 8 * 5)
    p.cancel(m1.id, "Not finished")
    assert _demand(svc, kit) == _required(svc, kit.project.id, kit.board.id) == D(3 + 9 * 5)


def test_manual_extra_on_required_is_kept(svc, kit):  # noqa: F811
    with svc.ctx.db.session() as s:  # someone typed 5 sheets extra on top of the units
        pm = s.scalar(select(ProjectMaterial).where(ProjectMaterial.site_id == kit.project.id))
        pm.required_qty += 5
    svc.unit_norms.record_completion(kit.kitchen.id, "4")
    svc.unit_norms.change_norm(kit.line, "5", NORM_FUTURE)
    assert _required(svc, kit.project.id, kit.board.id) == D(47)


# ---- screens ---------------------------------------------------------------------------------------------
def _answer(monkeypatch, future: bool, reason: str = "", accept: bool = True):
    from PySide6.QtWidgets import QDialog

    from wms.ui.dialogs.project_tree import NormChangeDialog
    seen = []

    def fake_exec(dlg):
        seen.append((dlg.total_all, dlg.total_future, dlg.effect.text()))
        (dlg.r_future if future else dlg.r_all).setChecked(True)
        dlg.reason.setText(reason)
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    monkeypatch.setattr(NormChangeDialog, "exec", fake_exec)
    return seen


def test_dialog_future_only(ui, kit, monkeypatch):  # noqa: F811
    app, win, msgs, _ = ui
    svc = app.services
    svc.unit_norms.record_completion(kit.kitchen.id, "4")
    seen = _answer(monkeypatch, future=True, reason="New fronts")
    win.go("projects")
    page = win.page("projects")
    assert page.save_norm(kit.line, {"quantity_per_unit": "5"}) is not None
    assert seen[0][:2] == (D(50), D(42)) and "42" in seen[0][2]
    last = svc.unit_norms.norm_history(kit.line)[-1]
    assert (last.quantity_per_unit, last.applies_to, last.reason) == (D(5), NORM_FUTURE, "New fronts")
    assert _required(svc, kit.project.id, kit.board.id) == D(42)
    assert _errors(msgs) == []


def test_dialog_recalculate_all_and_cancel(ui, kit, monkeypatch):  # noqa: F811
    app, win, msgs, _ = ui
    svc = app.services
    svc.unit_norms.record_completion(kit.kitchen.id, "4")
    win.go("projects")
    page = win.page("projects")
    _answer(monkeypatch, future=False, accept=False)
    assert page.save_norm(kit.line, {"quantity_per_unit": "5"}) is None   # cancelled: nothing saved
    assert svc.project_tree.material(kit.line).quantity_per_unit == D(3)
    _answer(monkeypatch, future=False)
    assert page.save_norm(kit.line, {"quantity_per_unit": "5"}) is not None
    assert svc.unit_norms.norm_history(kit.line)[-1].applies_to == NORM_ALL
    assert _required(svc, kit.project.id, kit.board.id) == D(50)


def test_no_units_made_no_question(ui, kit, monkeypatch):  # noqa: F811
    app, win, msgs, _ = ui
    seen = _answer(monkeypatch, future=True)
    win.go("projects")
    page = win.page("projects")
    assert page.save_norm(kit.line, {"quantity_per_unit": "4", "notes": "x"}) is not None
    assert seen == []  # nothing made yet: both choices give the same figures, so nothing is asked
    assert app.services.unit_norms.norm_history(kit.line)[-1].applies_to == NORM_ALL


@pytest.mark.parametrize("text", ["abc", "-1"])
def test_bad_number_still_reported_by_the_service(ui, kit, monkeypatch, text):  # noqa: F811
    app, win, msgs, _ = ui
    app.services.unit_norms.record_completion(kit.kitchen.id, "4")
    seen = _answer(monkeypatch, future=True)
    win.go("projects")
    assert win.page("projects").save_norm(kit.line, {"quantity_per_unit": text}) is None
    assert seen == [] and _errors(msgs)
