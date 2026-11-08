"""v1.17 Project Tree expansion buttons: "Project Manager" and "Project Name".

The two buttons above the tree only open and close rows - they never filter or change data.
Scenarios A-F of the specification.
"""

from __future__ import annotations

from PySide6.QtWidgets import QTreeWidgetItem
from sqlalchemy import func, select
from test_project_tree import _open_page, tribeca  # noqa: F401 - shared fixture
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.db.models import AuditLog, ProjectMaterial, StockLevel, StockMovement
from wms.db.models.reservations import StockReservation
from wms.ui.pages.projects import MODE_MANAGERS, MODE_PROJECTS, ProjectsPage

SHOW = QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator


def _managers(svc, tribeca):  # noqa: F811
    """John Smith (Tribeca + Surrey County Hall + Luton Airport), Daniel Brown (Project A, B)."""
    t = svc.project_tree
    john = tribeca.john.id
    surrey = t.save_project(None, {"project_number": "SCH", "name": "Surrey County Hall", "manager_id": john})
    luton = t.save_project(None, {"project_number": "LTN", "name": "Luton Airport", "manager_id": john})
    daniel = t.save_manager(None, {"name": "Daniel Brown"})
    a = t.save_project(None, {"project_number": "PA", "name": "Project A", "manager_id": daniel.id})
    b = t.save_project(None, {"project_number": "PB", "name": "Project B", "manager_id": daniel.id})
    t.save_unit(None, {"name": "Desk", "unit_code": "D1", "quantity": "4"}, site_id=a.id)
    return type("M", (), dict(john=john, daniel=daniel.id, surrey=surrey, luton=luton, a=a, b=b))


def _top(page):
    root = page.tree.invisibleRootItem()
    return [root.child(i) for i in range(root.childCount())]


def _snapshot(svc):
    """Everything a view change must never touch: audit trail, required lists, stock, ledger,
    reservations and the last change time of every table row we can see."""
    with svc.ctx.db.read_session() as s:
        return (s.scalar(select(func.count(AuditLog.id))),
                s.scalar(select(func.count(StockMovement.id))),
                s.scalar(select(func.count(StockReservation.id))),
                sorted((x.site_id, x.product_id, x.required_qty, x.units_qty, x.updated_at)
                       for x in s.scalars(select(ProjectMaterial))),
                sorted((x.product_id, x.bin_id, x.quantity) for x in s.scalars(select(StockLevel))))


def test_only_two_expansion_buttons(ui, tribeca):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _open_page(win)
    assert [b.text() for b in page.mode_group.buttons()] == ["Project Manager", "Project Name"]
    assert page.mode_group.exclusive()
    assert not hasattr(page, "manager_filter") and not hasattr(page, "project_filter")
    assert not _errors(msgs), msgs


def test_a_project_manager_shows_only_managers(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    page.items[f"M:{m.john}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    page.mode_buttons[MODE_MANAGERS].click()
    assert page.expand_mode() == MODE_MANAGERS and page.mode_buttons[MODE_MANAGERS].isChecked()
    tops = _top(page)
    assert {t.text(0) for t in tops} >= {"John Smith", "Daniel Brown"}
    assert not any(t.isExpanded() for t in tops)
    assert not page.items[f"P:{tribeca.project.id}"].isExpanded()  # deeper rows closed too
    assert all(page.items[f"M:{i}"].childIndicatorPolicy() == SHOW for i in (m.john, m.daniel))  # arrows stay
    assert not _errors(msgs), msgs


def test_b_manual_expansion_opens_only_that_manager(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    page.set_expand_mode(MODE_MANAGERS)
    john = page.items[f"M:{m.john}"]
    john.setExpanded(True)
    assert {john.child(i).text(0) for i in range(john.childCount())} == {
        "Tribeca", "Surrey County Hall", "Luton Airport"}
    assert not page.items[f"M:{m.daniel}"].isExpanded()
    assert not any(john.child(i).isExpanded() for i in range(john.childCount()))
    page.items[f"M:{m.daniel}"].setExpanded(True)  # several managers open together
    assert john.isExpanded() and page.items[f"M:{m.daniel}"].isExpanded()
    assert page.expand_mode() == MODE_MANAGERS  # manual opening does not change the button


def test_c_project_name_opens_managers_not_contents(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    page.mode_buttons[MODE_PROJECTS].click()
    assert page.expand_mode() == MODE_PROJECTS
    for top in _top(page):
        if top.childIndicatorPolicy() == SHOW:
            assert top.isExpanded(), top.text(0)
    names = {page.items[k].text(0) for k in page.items if k.startswith("P:")}
    assert {"Tribeca", "Surrey County Hall", "Luton Airport", "Project A", "Project B"} <= names
    assert not any(page.items[k].isExpanded() for k in page.items if k[0] in "PBVU")
    assert f"U:{tribeca.kitchen.id}" not in page.items  # project contents not even read yet (lazy)
    assert page.items[f"P:{m.a.id}"].childIndicatorPolicy() == SHOW  # can be opened by hand
    assert not _errors(msgs), msgs


def test_d_manual_changes_are_independent(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    page.set_expand_mode(MODE_PROJECTS)
    page.items[f"M:{m.daniel}"].setExpanded(False)
    tri = page.items[f"P:{tribeca.project.id}"]
    tri.setExpanded(True)
    assert not page.items[f"M:{m.daniel}"].isExpanded()
    assert page.items[f"M:{m.john}"].isExpanded() and tri.isExpanded()
    assert {tri.child(i).text(0) for i in range(tri.childCount())} == {"Kitchen", "Wardrobe"}
    tri.setExpanded(False)
    assert not tri.isExpanded() and page.items[f"M:{m.john}"].isExpanded()
    assert page.expand_mode() == MODE_PROJECTS


def test_e_switching_modes_resets_every_time(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    for mode in (MODE_MANAGERS, MODE_PROJECTS, MODE_MANAGERS, MODE_PROJECTS):
        page.items[f"M:{m.john}"].setExpanded(True)  # some manual opening in between
        page.items[f"P:{tribeca.project.id}"].setExpanded(True)
        page.mode_buttons[mode].click()
        john, daniel = page.items[f"M:{m.john}"], page.items[f"M:{m.daniel}"]
        assert john.isExpanded() == daniel.isExpanded() == (mode == MODE_PROJECTS)
        assert not any(page.items[k].isExpanded() for k in page.items if k[0] in "PBVU")
        assert page.mode_buttons[mode].isChecked() and not page.mode_buttons[
            MODE_PROJECTS if mode == MODE_MANAGERS else MODE_MANAGERS].isChecked()
    # the last mode and the open rows survive a restart
    again = ProjectsPage(app)
    again.refresh()
    assert again.expand_mode() == MODE_PROJECTS
    assert again.items[f"M:{m.john}"].isExpanded() and not again.items[f"P:{tribeca.project.id}"].isExpanded()


def test_selection_kept_when_its_branch_closes(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    _managers(app.services, tribeca)
    page = _open_page(win)
    page.set_expand_mode(MODE_PROJECTS)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    page.select(f"U:{tribeca.kitchen.id}")
    page.set_expand_mode(MODE_MANAGERS)
    assert page.current_key() == f"U:{tribeca.kitchen.id}"
    assert page.panel.currentWidget() is not page.panel.empty  # side panel still shows the unit
    page.items[f"M:{tribeca.john.id}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    assert page.tree.currentItem() is page.items[f"U:{tribeca.kitchen.id}"]


def test_f_switching_never_writes_data(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    svc = app.services
    m = _managers(svc, tribeca)
    page = _open_page(win)
    before = _snapshot(svc)
    units_before = [(u.name, u.quantity) for u in svc.project_tree.units(tribeca.project.id)]
    req_before = svc.materials.project_summary(tribeca.project.id).required
    owners = {p.code: p.manager_id for p in svc.project_tree.projects(m.john)}
    for mode in (MODE_MANAGERS, MODE_PROJECTS, MODE_MANAGERS, MODE_PROJECTS):
        page.mode_buttons[mode].click()
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    page.items[f"U:{tribeca.kitchen.id}"].setExpanded(True)
    page.set_expand_mode(MODE_MANAGERS)
    assert _snapshot(svc) == before
    assert [(u.name, u.quantity) for u in svc.project_tree.units(tribeca.project.id)] == units_before
    assert svc.materials.project_summary(tribeca.project.id).required == req_before
    assert {p.code: p.manager_id for p in svc.project_tree.projects(m.john)} == owners
    assert not _errors(msgs), msgs


def test_editing_still_works_after_switching(ui, tribeca):  # noqa: F811
    app, win, msgs, _ = ui
    page = _open_page(win)
    page.set_expand_mode(MODE_PROJECTS)
    item = page.items[f"P:{tribeca.project.id}"]
    assert page.commit_edit(item, 0, "Tribeca Tower")
    assert app.services.project_tree.project(tribeca.project.id).name == "Tribeca Tower"
    item.setExpanded(True)
    page.items[f"U:{tribeca.kitchen.id}"].setExpanded(True)
    assert page.items[f"U:{tribeca.kitchen.id}"].childCount() == 3
    assert not _errors(msgs), msgs
