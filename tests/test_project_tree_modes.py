"""v1.17 Projects table: one column per level (Project Manager, Project, Block, Level, Unit) and
two clickable headers - Project Manager (collapse to managers) and Project (show every project).

The headers only open and close rows - they never sort, filter or change data. Tests 1-6 of the
specification plus the column rules.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QTreeWidgetItem
from sqlalchemy import func, select
from test_project_tree import _name, _open_page, tower, tribeca  # noqa: F401 - shared fixtures
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.db.models import AuditLog, ProjectMaterial, StockLevel, StockMovement
from wms.db.models.reservations import StockReservation
from wms.ui.pages.projects import (
    C_BLK,
    C_INFO,
    C_LVL,
    C_MGR,
    C_PRJ,
    C_UNIT,
    COLUMNS,
    MODE_MANAGERS,
    MODE_PROJECTS,
    name_col,
)

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


def _click_header(page, col):
    page.tree.header().sectionClicked.emit(col)


def _open_rows(page):
    return {k for k, it in page.items.items() if it.isExpanded()}


def _expand_deep(page, tribeca, tower):  # noqa: F811
    for key in (f"M:{tribeca.john.id}", f"P:{tribeca.project.id}", f"B:{tower.block.id}", f"V:{tower.level.id}",
                f"U:{tower.k10.id}", f"U:{tribeca.kitchen.id}"):
        page.items[key].setExpanded(True)


def test_columns_and_no_buttons(ui, tribeca):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _open_page(win)
    assert COLUMNS[:5] == ["Project Manager", "Project", "Block", "Level", "Unit"]
    assert [page.tree.headerItem().text(c) for c in range(5)] == COLUMNS[:5]
    assert "Structure" not in COLUMNS
    assert page.tree.header().sectionsClickable()
    texts = {b.text() for b in page.findChildren(QAbstractButton)}
    assert "Project Manager" not in texts and "Project Name" not in texts  # standalone buttons gone
    assert page.tree.indentation() == 0 and not page.tree.rootIsDecorated()
    assert not page.tree.isSortingEnabled()  # headers are commands, not sorting
    assert not _errors(msgs), msgs


def test_1_project_manager_header_collapses_everything(ui, tribeca, tower):  # noqa: F811
    app, win, msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    _expand_deep(page, tribeca, tower)
    page.items[f"M:{m.daniel}"].setExpanded(True)
    _click_header(page, C_MGR)
    assert page.expand_mode() == MODE_MANAGERS
    assert _open_rows(page) == set()
    tops = _top(page)
    assert {_name(t) for t in tops} >= {"John Smith", "Daniel Brown"}
    assert all(t.text(C_PRJ) == "" for t in tops)  # managers only in the manager column
    assert page.has_arrow(page.items[f"M:{m.john}"])  # each manager can still be opened
    assert not _errors(msgs), msgs


def test_2_project_header_shows_all_projects(ui, tribeca, tower):  # noqa: F811
    app, win, msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    _expand_deep(page, tribeca, tower)
    _click_header(page, C_PRJ)
    assert page.expand_mode() == MODE_PROJECTS
    for top in _top(page):
        if page.has_arrow(top):
            assert top.isExpanded(), _name(top)
    names = {page.items[k].text(C_PRJ) for k in page.items if k.startswith("P:")}
    assert {"Tribeca", "Surrey County Hall", "Luton Airport", "Project A", "Project B"} <= names
    assert not any(page.items[k].isExpanded() for k in page.items if k[0] in "PBVU")
    assert page.has_arrow(page.items[f"P:{m.a.id}"])  # project can be opened by hand
    assert not _errors(msgs), msgs


def test_3_each_level_in_its_own_column(ui, tribeca, tower):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _open_page(win)
    _click_header(page, C_PRJ)
    _expand_deep(page, tribeca, tower)
    expect = {f"M:{tribeca.john.id}": (C_MGR, "John Smith"), f"P:{tribeca.project.id}": (C_PRJ, "Tribeca"),
              f"B:{tower.block.id}": (C_BLK, "Block A"), f"V:{tower.level.id}": (C_LVL, "Level 1"),
              f"U:{tower.k10.id}": (C_UNIT, "Kitchen"), f"U:{tribeca.wardrobe.id}": (C_UNIT, "Wardrobe")}
    for key, (col, name) in expect.items():
        item = page.items[key]
        assert name_col(key) == col and item.text(col) == name, key
        assert all(item.text(c) == "" for c in range(C_INFO) if c != col), key  # nothing in other levels
    assert page.items[f"U:{tower.k10.id}"].childCount() == 0  # materials: in the unit's Finish Schedule
    assert not _errors(msgs), msgs


def test_arrow_click_opens_only_that_row(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    _click_header(page, C_MGR)
    john = page.items[f"M:{m.john}"]
    rect = page.tree.visualRect(page.tree.indexFromItem(john, C_MGR))
    from PySide6.QtTest import QTest
    QTest.mouseClick(page.tree.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 6, rect.center().y()))
    assert john.isExpanded() and not page.items[f"M:{m.daniel}"].isExpanded()
    assert {john.child(i).text(C_PRJ) for i in range(john.childCount())} == {
        "Tribeca", "Surrey County Hall", "Luton Airport"}
    QTest.mouseClick(page.tree.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 6, rect.center().y()))
    assert not john.isExpanded()
    assert page.expand_mode() == MODE_MANAGERS  # manual opening does not change the header command


def test_manual_changes_are_independent(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    _click_header(page, C_PRJ)
    page.items[f"M:{m.daniel}"].setExpanded(False)
    tri = page.items[f"P:{tribeca.project.id}"]
    tri.setExpanded(True)
    assert not page.items[f"M:{m.daniel}"].isExpanded()
    assert page.items[f"M:{m.john}"].isExpanded() and tri.isExpanded()
    assert {tri.child(i).text(C_UNIT) for i in range(tri.childCount())} == {"Kitchen", "Wardrobe"}


def test_4_switching_headers(ui, tribeca, tower):  # noqa: F811
    app, win, _msgs, _ = ui
    m = _managers(app.services, tribeca)
    page = _open_page(win)
    for col in (C_MGR, C_PRJ, C_MGR, C_PRJ, C_PRJ):
        _expand_deep(page, tribeca, tower)  # some manual opening in between
        _click_header(page, col)
        john, daniel = page.items[f"M:{m.john}"], page.items[f"M:{m.daniel}"]
        assert john.isExpanded() == daniel.isExpanded() == (col == C_PRJ)
        assert not any(page.items[k].isExpanded() for k in page.items if k[0] in "PBVU")
    for col in (C_BLK, C_LVL, C_UNIT):  # these headers are not commands
        before = _open_rows(page)
        _click_header(page, col)
        assert _open_rows(page) == before and page.expand_mode() == MODE_PROJECTS


def test_headers_keep_search_and_status(ui, tribeca):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _open_page(win)
    page.search.setText("hinge")
    page.refresh()
    status = page.status_filter.currentIndex()
    _click_header(page, C_MGR)
    assert page.search.text() == "hinge" and page.status_filter.currentIndex() == status
    page.items[f"M:{tribeca.john.id}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    assert f"U:{tribeca.kitchen.id}" in page.items  # the match is revealed again by opening its branch
    assert f"U:{tribeca.wardrobe.id}" not in page.items


def test_selection_kept_when_its_branch_closes(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    _managers(app.services, tribeca)
    page = _open_page(win)
    _click_header(page, C_PRJ)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    page.select(f"U:{tribeca.kitchen.id}")
    _click_header(page, C_MGR)
    assert page.current_key() == f"U:{tribeca.kitchen.id}"
    assert page.panel.currentWidget() is not page.panel.empty  # side panel still shows the unit


def test_5_editing_every_level(ui, tribeca, tower):  # noqa: F811
    app, win, msgs, _ = ui
    t = app.services.project_tree
    page = _open_page(win)
    _click_header(page, C_PRJ)
    _expand_deep(page, tribeca, tower)
    edits = [(f"M:{tribeca.john.id}", "John A. Smith"), (f"P:{tribeca.project.id}", "Tribeca Tower"),
             (f"B:{tower.block.id}", "Block North"), (f"V:{tower.level.id}", "Ground Floor"),
             (f"U:{tower.k10.id}", "Kitchen Type 10")]
    for key, text in edits:
        item = page.items[key]
        assert page._editable(key, name_col(key)) and not page._editable(key, C_INFO)
        assert page.commit_edit(item, name_col(key), text), key
        assert page.items[key].text(name_col(key)) == text
    assert [m.name for m in t.managers() if m.id == tribeca.john.id] == ["John A. Smith"]
    assert t.project(tribeca.project.id).name == "Tribeca Tower"
    assert t.block(tower.block.id).name == "Block North" and t.level(tower.level.id).name == "Ground Floor"
    assert t.unit(tower.k10.id).name == "Kitchen Type 10"
    assert not _errors(msgs), msgs


def test_6_switching_never_writes_data(ui, tribeca, tower):  # noqa: F811
    app, win, msgs, _ = ui
    svc = app.services
    m = _managers(svc, tribeca)
    page = _open_page(win)
    before = _snapshot(svc)
    units_before = [(u.name, u.quantity) for u in svc.project_tree.units(tribeca.project.id)]
    req_before = svc.materials.project_summary(tribeca.project.id).required
    owners = {p.code: p.manager_id for p in svc.project_tree.projects(m.john)}
    for col in (C_MGR, C_PRJ, C_MGR, C_PRJ):
        _expand_deep(page, tribeca, tower)
        _click_header(page, col)
    assert _snapshot(svc) == before
    assert [(u.name, u.quantity) for u in svc.project_tree.units(tribeca.project.id)] == units_before
    assert svc.materials.project_summary(tribeca.project.id).required == req_before
    assert {p.code: p.manager_id for p in svc.project_tree.projects(m.john)} == owners
    assert not _errors(msgs), msgs
