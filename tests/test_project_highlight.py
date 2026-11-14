"""v1.18 Projects screen: collapsible details panel and the light-green active project.

Tests 1-8 of the specification. The active project is found by the project id stored on every
row, never by its name or position.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from test_project_tree import _open_page, tower, tribeca  # noqa: F401 - shared fixtures
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.ui.pages.projects import ACTIVE_PROJECT, C_BLK, C_INFO, C_MGR, C_PRJ, KEY, PROJECT, ProjectsPage


def _surrey(svc, tribeca):  # noqa: F811
    t = svc.project_tree
    p = t.save_project(None, {"project_number": "SCH", "name": "Surrey County Hall", "manager_id": tribeca.john.id})
    t.save_unit(None, {"name": "Reception", "unit_code": "R1", "quantity": "1"}, site_id=p.id)
    return p


def _open(page, *keys):
    for k in keys:
        page.items[k].setExpanded(True)


def _green_keys(page):
    return {it.data(0, KEY) for it in page.project_rows()}


def _pixel(page, item, col=C_INFO):
    """Colour drawn in an empty-ish part of a cell (the Details column by default: proves the
    green runs across the full width, not only behind the name)."""
    tree = page.tree
    index = tree.indexFromItem(item, col)
    tree.scrollTo(index)
    QApplication.processEvents()
    rect = tree.visualRect(index)
    img = tree.viewport().grab().toImage()
    return QColor(img.pixel(rect.right() - 6, rect.center().y()))


def test_1_2_details_panel_collapses_and_restores(ui, tribeca):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _open_page(win)
    win.resize(1366, 768)
    QApplication.processEvents()
    _open(page, f"M:{tribeca.john.id}")
    page.select(f"M:{tribeca.john.id}")
    width = page.tree.width()
    page.panel_btn.click()  # hide
    QApplication.processEvents()
    assert not page.details_visible() and page.panel.isHidden()
    assert page.tree.width() > width  # the table takes the freed space
    assert page.split.width() - page.table_box.width() <= page.split.handleWidth() + 2  # no blank space left
    assert page.current_key() == f"M:{tribeca.john.id}"  # selection kept
    page.select(f"P:{tribeca.project.id}")
    assert page.panel.isHidden()  # selecting never reopens it
    page.panel_btn.click()  # show
    QApplication.processEvents()
    assert page.details_visible() and page.tree.width() == width
    assert page.panel.currentWidget() is page.panel.pages["P"]  # shows the row selected while hidden
    assert not _errors(msgs), msgs


def test_typed_text_survives_hiding(ui, tribeca):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _open_page(win)
    page.select(f"P:{tribeca.project.id}")
    page.panel.p_name.setText("Typed but not saved")
    page.set_details_visible(False)
    page.set_details_visible(True)
    assert page.panel.p_name.text() == "Typed but not saved"


def test_3_panel_state_survives_restart(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    page = _open_page(win)
    page.panel_btn.click()
    again = ProjectsPage(app)
    assert not again.details_visible()
    again.panel_btn.click()
    third = ProjectsPage(app)
    assert third.details_visible()


def test_4_selecting_any_cell_highlights_the_project(ui, tribeca, tower):  # noqa: F811
    app, win, msgs, _ = ui
    _surrey(app.services, tribeca)
    page = _open_page(win)
    tri = tribeca.project.id
    _open(page, f"M:{tribeca.john.id}", f"P:{tri}", f"B:{tower.block.id}", f"V:{tower.level.id}", f"U:{tower.k10.id}")
    for key in (f"P:{tri}", f"B:{tower.block.id}", f"V:{tower.level.id}", f"U:{tower.k10.id}"):
        page.select(key)
        assert page.active_project == tri, key
    page.tree.setCurrentItem(page.items[f"U:{tower.k10.id}"].child(0))  # a material
    assert page.active_project == tri
    green = _green_keys(page)
    assert {f"P:{tri}", f"B:{tower.block.id}", f"V:{tower.level.id}", f"U:{tower.k10.id}",
            f"U:{tower.b10.id}", f"U:{tribeca.kitchen.id}", f"U:{tribeca.wardrobe.id}"} <= green
    assert f"M:{tribeca.john.id}" not in green and not any(k.startswith("P:") and k != f"P:{tri}" for k in green)
    # painted across the full width: the Details column of a block row is green
    assert _pixel(page, page.items[f"B:{tower.block.id}"]) == ACTIVE_PROJECT
    assert _pixel(page, page.items[f"M:{tribeca.john.id}"]) != ACTIVE_PROJECT
    assert not _errors(msgs), msgs


def test_5_switching_project_moves_the_highlight(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    surrey = _surrey(app.services, tribeca)
    page = _open_page(win)
    _open(page, f"M:{tribeca.john.id}", f"P:{tribeca.project.id}", f"P:{surrey.id}")
    page.select(f"U:{tribeca.kitchen.id}")
    assert _pixel(page, page.items[f"P:{tribeca.project.id}"]) == ACTIVE_PROJECT
    page.select(f"P:{surrey.id}")
    assert page.active_project == surrey.id
    assert _pixel(page, page.items[f"P:{tribeca.project.id}"]) != ACTIVE_PROJECT
    reception = page.items[f"P:{surrey.id}"].child(0)
    assert _pixel(page, reception) == ACTIVE_PROJECT
    assert all(it.data(0, PROJECT) == surrey.id for it in page.project_rows())


def test_6_manager_or_no_selection_clears_it(ui, tribeca):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _open_page(win)
    _open(page, f"M:{tribeca.john.id}")
    page.select(f"P:{tribeca.project.id}")
    page.select(f"M:{tribeca.john.id}")
    assert page.active_project is None and page.project_rows() == []
    page.select(f"P:{tribeca.project.id}")
    page.tree.setCurrentItem(None)
    assert page.active_project is None


def test_7_expand_and_collapse_keep_it_right(ui, tribeca, tower):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _open_page(win)
    tri = tribeca.project.id
    _open(page, f"M:{tribeca.john.id}", f"P:{tri}", f"B:{tower.block.id}", f"V:{tower.level.id}")
    page.select(f"U:{tower.k10.id}")
    page.items[f"B:{tower.block.id}"].setExpanded(False)  # scenario A
    assert page.active_project == tri
    assert f"U:{tower.k10.id}" not in _green_keys(page) and f"B:{tower.block.id}" in _green_keys(page)
    page.items[f"U:{tribeca.kitchen.id}"].setExpanded(True)  # scenario B: new rows are green too
    assert any(k.startswith("L:") for k in _green_keys(page))
    assert _pixel(page, page.items[f"U:{tribeca.kitchen.id}"].child(0)) == ACTIVE_PROJECT
    page.tree.header().sectionClicked.emit(C_MGR)  # scenario C
    assert page.active_project == tri and page.project_rows() == []
    page.tree.header().sectionClicked.emit(C_PRJ)
    assert _green_keys(page) == {f"P:{tri}"}


def test_8_same_name_different_managers(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    t = app.services.project_tree
    anna = t.save_manager(None, {"name": "Anna Lee"})
    twin = t.save_project(None, {"project_number": "TRI-2", "name": "Tribeca 2", "manager_id": anna.id})
    # v1.23 refuses a second "Tribeca", but databases from before it can hold one: highlight by ID, not name
    from sqlalchemy import text
    with app.services.ctx.db.engine.begin() as c:
        c.execute(text("UPDATE sites SET name = 'Tribeca' WHERE id = :i"), {"i": twin.id})
    page = _open_page(win)
    page.tree.header().sectionClicked.emit(C_PRJ)
    page.select(f"P:{twin.id}")
    assert _green_keys(page) == {f"P:{twin.id}"}
    assert _pixel(page, page.items[f"P:{tribeca.project.id}"]) != ACTIVE_PROJECT


def test_selection_does_not_query_the_database(ui, tribeca, monkeypatch):  # noqa: F811
    app, win, _msgs, _ = ui
    page = _open_page(win)
    _open(page, f"M:{tribeca.john.id}", f"P:{tribeca.project.id}")
    page.set_details_visible(False)  # panel hidden: nothing to fill in
    calls = []
    for name in ("project", "unit", "units", "projects", "managers"):
        orig = getattr(page.svc, name)
        monkeypatch.setattr(page.svc, name, lambda *a, _o=orig, _n=name, **k: calls.append(_n) or _o(*a, **k))
    page.select(f"U:{tribeca.kitchen.id}")
    page.select(f"P:{tribeca.project.id}")
    assert calls == []


def test_selected_cell_differs_from_the_green_row(ui, tribeca, tower):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _open_page(win)
    _open(page, f"M:{tribeca.john.id}", f"P:{tribeca.project.id}", f"B:{tower.block.id}")
    block = page.items[f"B:{tower.block.id}"]
    page.tree.setCurrentItem(block, C_BLK)
    assert _pixel(page, block, C_BLK) != ACTIVE_PROJECT  # the clicked cell: selection colour + frame
    assert _pixel(page, block, C_INFO) == ACTIVE_PROJECT  # the rest of its row stays green
    assert [i.column() for i in page.tree.selectedIndexes()] == [C_BLK]


def test_button_sits_on_the_table_edge_and_scroll_is_kept(ui, tribeca):  # noqa: F811
    app, win, _msgs, _ = ui
    t = app.services.project_tree
    for i in range(40):
        t.save_project(None, {"project_number": f"X{i:02}", "name": f"Extra {i}", "manager_id": tribeca.john.id})
    page = _open_page(win)
    win.resize(1366, 768)
    QApplication.processEvents()
    btn, tree = page.panel_btn, page.tree
    assert btn.parentWidget() is page.table_box
    assert 0 <= btn.geometry().left() - tree.geometry().right() <= 8  # right next to the table
    _open(page, f"M:{tribeca.john.id}")
    bar = tree.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    pos = bar.value()
    visible = tree.itemAt(5, 5)
    tree.setCurrentItem(visible, C_PRJ)
    assert bar.value() == pos
