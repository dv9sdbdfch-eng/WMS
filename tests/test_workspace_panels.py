"""v1.16: one Projects screen, panels that fold away for a bigger work area, Required from units."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from PySide6.QtWidgets import QDialog
from sqlalchemy import text

from wms.ui.app import App
from wms.ui.main_window import PANELS, MainWindow, _hidden, _nav


@pytest.fixture
def make_win(qtbot, svc, monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda d: 0)

    def make():
        w = MainWindow(App(svc.ctx))
        qtbot.addWidget(w)
        w.resize(1366, 768)
        w.show()
        return w
    return make


def test_one_projects_entry_in_the_sidebar():
    labels = [text for section, _k, text, *_r in _nav() if section == "PROJECTS"]
    assert labels == ["Projects"]
    hidden = {k: parent for k, _t, _p, _f, parent in _hidden()}
    assert hidden["sites"] == "projects" and hidden["reservations"] == "projects"


def test_project_list_opens_from_projects_with_a_way_back(make_win):
    win = make_win()
    win.go("projects")
    win.go("sites")
    page = win.page("sites")
    assert win.stack.currentWidget() is page
    assert win.nav.currentItem().text() == "Projects"  # where the person is


def test_every_panel_hides_and_is_remembered(make_win):
    win = make_win()
    win.go("products")
    table_h = win.page("products").view.table.height()
    for key, _t, _sc in PANELS:
        win.set_panel(key, False)
    assert not win.sidebar.isVisible() and not win.topbar.isVisible() and not win.statusBar().isVisible()
    assert not win.page("products").titles_widget.isVisible()
    assert win.panels_btn.isVisible()  # the way back while the top bar is hidden
    win.page("products").view.filters_btn.setChecked(True)  # fold the filter row too
    win.resize(1366, 769)
    win.resize(1366, 768)
    assert win.page("products").view.table.height() > table_h
    # a new window (next start) comes back the same
    again = make_win()
    assert not again.sidebar.isVisible() and not again.topbar.isVisible()
    again.go("products")
    assert not again.page("products").titles_widget.isVisible()
    assert again.page("products").view.filter_row.isHidden()
    again.show_all_panels()
    assert again.sidebar.isVisible() and again.topbar.isVisible() and not again.panels_btn.isVisible()


def test_focus_mode_hides_all_and_restores_exactly(make_win):
    win = make_win()
    win.set_panel("statusbar", False)
    win.toggle_focus()
    assert not any(win.panels.values())
    win.toggle_focus()
    assert win.panels == {"sidebar": True, "topbar": True, "titles": True, "statusbar": False}


def test_dashboard_stock_table_filters_fold(make_win):
    win = make_win()
    view = win.page("dashboard").stock
    view.filters_btn.setChecked(True)
    assert view.filter_row.isHidden() and view.search.isVisible()
    view.filters_btn.setChecked(False)
    assert view.filter_row.isVisible()


def test_upgrade_adds_unit_materials_and_keeps_typed_figures(svc, sample):
    """Migration 0013: units' demand is taken as already counted on existing rows; missing
    materials of open projects are added; then later changes move Required by the change."""
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR
    t = svc.project_tree
    unit = t.save_unit(None, {"name": "Kitchen", "quantity": "10"}, site_id=sample.site.id)
    t.add_materials(unit.id, [(sample.mdf.id, "2"), (sample.edge.id, "3")])
    db = svc.ctx.db
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0012")
    with db.engine.begin() as c:  # as before v1.16: MDF typed by hand (25), edging never put on the list
        c.execute(text("DELETE FROM project_materials"))
        c.execute(text("INSERT INTO project_materials (site_id, product_id, required_qty, created_at, updated_at) "
                       "VALUES (:s, :p, 25000, '2026-11-01 10:00:00', '2026-11-01 10:00:00')"),
                  {"s": sample.site.id, "p": sample.mdf.id})
    db.dispose()
    db.migrate()
    with db.engine.connect() as c:
        rows = {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT product_id, required_qty, units_qty FROM project_materials WHERE site_id = :s"),
            {"s": sample.site.id})}
    assert rows[sample.mdf.id] == (25000, 20000)  # typed 25 kept; 20 of it counted as from units
    assert rows[sample.edge.id] == (30000, 30000)  # added from the units
    t.save_unit(unit.id, {"quantity": "11"})  # +2 MDF, +3 edging
    with db.engine.connect() as c:
        after = {r[0]: r[1] for r in c.execute(text(
            "SELECT product_id, required_qty FROM project_materials WHERE site_id = :s"), {"s": sample.site.id})}
    assert after == {sample.mdf.id: 27000, sample.edge.id: 33000}
    assert svc.materials.project_summary(sample.site.id).required == D("60")  # 27 + 33
