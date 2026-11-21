"""v1.24 Production dashboard: one row per piece, Done / Dispatched / On site marks, project cards."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from wms.core.exceptions import ValidationError
from wms.db.models.hierarchy import STAGE_DISPATCHED, STAGE_DONE, STAGE_ON_SITE
from wms.services.unit_progress import NOT_STARTED

TODAY = date.today()
D = Decimal


@pytest.fixture
def site(svc, sample):
    """Tribeca (John Smith): Block A / Level 1: Kitchen K01 x1, Wardrobe x3; Block A / Level 2: Kitchen
    K02 x1; directly in the project: Reception x1. Mayfair (Daniel Brown): Desk x2."""
    t = svc.project_tree
    john = t.save_manager(None, {"name": "John Smith"})
    dan = t.save_manager(None, {"name": "Daniel Brown"})
    tri = t.save_project(None, {"project_number": "TRI", "name": "Tribeca", "status": "ACTIVE", "manager_id": john.id})
    may = t.save_project(None, {"project_number": "MAY", "name": "Mayfair", "status": "ACTIVE", "manager_id": dan.id})
    block = t.save_block(None, {"name": "Block A"}, site_id=tri.id)
    l1 = t.save_level(None, {"name": "Level 1"}, block_id=block.id)
    l2 = t.save_level(None, {"name": "Level 2"}, block_id=block.id)
    reception = t.save_unit(None, {"name": "Reception"}, site_id=tri.id)
    k01 = t.save_unit(None, {"name": "Kitchen", "unit_code": "K01"}, level_id=l1.id)
    wardrobe = t.save_unit(None, {"name": "Wardrobe", "quantity": "3"}, level_id=l1.id)
    k02 = t.save_unit(None, {"name": "Kitchen", "unit_code": "K02"}, level_id=l2.id)
    desk = t.save_unit(None, {"name": "Desk", "quantity": "2"}, site_id=may.id)
    return type("S", (), dict(tri=tri, may=may, block=block, l1=l1, l2=l2, reception=reception, k01=k01,
                              wardrobe=wardrobe, k02=k02, desk=desk))


def rows_of(svc):
    return svc.unit_progress.board().rows


def row(svc, unit_id, piece=1):
    return next(r for r in rows_of(svc) if r.key == (unit_id, piece))


def test_one_row_per_piece_in_tree_order(svc, site):
    rows = rows_of(svc)
    got = [(r.project, r.block, r.level, r.unit) for r in rows]
    assert got == [
        ("Mayfair", "", "", "Desk  #1 of 2"), ("Mayfair", "", "", "Desk  #2 of 2"),
        ("Tribeca", "Block A", "Level 1", "Kitchen · K01"),
        ("Tribeca", "Block A", "Level 1", "Wardrobe  #1 of 3"), ("Tribeca", "Block A", "Level 1", "Wardrobe  #2 of 3"),
        ("Tribeca", "Block A", "Level 1", "Wardrobe  #3 of 3"),
        ("Tribeca", "Block A", "Level 2", "Kitchen · K02"),
        ("Tribeca", "", "", "Reception"),
    ]
    assert {r.manager for r in rows if r.project == "Tribeca"} == {"John Smith"}
    assert all(r.status == NOT_STARTED for r in rows)
    cards = {c.title: c for c in svc.unit_progress.board().cards}
    assert (cards["Tribeca"].total, cards["Tribeca"].done, cards["Mayfair"].total) == (6, 0, 2)
    assert cards["Tribeca"].manager == "John Smith"


def test_mark_stages_in_order_with_today_by_default(svc, site):
    up = svc.unit_progress
    with pytest.raises(ValidationError, match="not Done yet"):
        up.mark(site.k01.id, 1, STAGE_DISPATCHED)
    m = up.mark(site.k01.id, 1, STAGE_DONE)
    assert m.on == TODAY
    assert row(svc, site.k01.id).status == STAGE_DONE
    with pytest.raises(ValidationError, match="already marked Done"):
        up.mark(site.k01.id, 1, STAGE_DONE)
    up.mark(site.k01.id, 1, STAGE_DISPATCHED)
    up.mark(site.k01.id, 1, STAGE_ON_SITE)
    r = row(svc, site.k01.id)
    assert r.status == STAGE_ON_SITE and r.date_of(STAGE_ON_SITE) == TODAY
    card = next(c for c in up.board().cards if c.title == "Tribeca")
    assert (card.done, card.dispatched, card.on_site) == (1, 1, 1)
    with pytest.raises(ValidationError, match="future"):
        up.mark(site.k02.id, 1, STAGE_DONE, TODAY + timedelta(days=1))


def test_each_piece_has_its_own_marks_and_counts_as_units_done(svc, site):
    up = svc.unit_progress
    up.mark(site.wardrobe.id, 2, STAGE_DONE, TODAY - timedelta(days=3))
    assert row(svc, site.wardrobe.id, 1).status == NOT_STARTED
    assert row(svc, site.wardrobe.id, 2).date_of(STAGE_DONE) == TODAY - timedelta(days=3)
    assert svc.unit_norms.units_done(site.wardrobe.id) == D(1)   # the norms see one wardrobe made
    up.mark(site.wardrobe.id, 2, STAGE_DISPATCHED)                # Dispatched is not "made" again
    assert svc.unit_norms.units_done(site.wardrobe.id) == D(1)
    with pytest.raises(ValidationError, match="no piece #4"):
        up.mark(site.wardrobe.id, 4, STAGE_DONE)
    with pytest.raises(ValidationError, match="cannot be less than 2"):
        svc.project_tree.save_unit(site.wardrobe.id, {"quantity": "1"})
    svc.project_tree.save_unit(site.wardrobe.id, {"quantity": "2"})  # piece 2 still exists


def test_dates_must_follow_the_stages(svc, site):
    up = svc.unit_progress
    up.mark(site.k01.id, 1, STAGE_DONE, TODAY - timedelta(days=2))
    with pytest.raises(ValidationError, match="cannot be before Done"):
        up.mark(site.k01.id, 1, STAGE_DISPATCHED, TODAY - timedelta(days=5))
    disp = up.mark(site.k01.id, 1, STAGE_DISPATCHED, TODAY - timedelta(days=1))
    done = row(svc, site.k01.id).marks[STAGE_DONE]
    with pytest.raises(ValidationError, match="cannot be after Dispatched"):
        up.change_date(done.id, TODAY)
    assert disp.on == TODAY - timedelta(days=1)


def test_change_date_keeps_history_and_recorded_time(svc, site):
    up = svc.unit_progress
    m = up.mark(site.k02.id, 1, STAGE_DONE)
    new = up.change_date(m.id, TODAY - timedelta(days=4))
    assert new.id != m.id and new.on == TODAY - timedelta(days=4)
    assert row(svc, site.k02.id).date_of(STAGE_DONE) == TODAY - timedelta(days=4)
    hist = up.history(site.k02.id, 1)
    assert [(h.on, h.cancelled) for h in hist] == [(TODAY, True), (TODAY - timedelta(days=4), False)]
    assert hist[0].cancel_reason.startswith("Date corrected")
    assert hist[0].recorded_at == hist[1].recorded_at  # the norm the piece was made with stays
    assert svc.unit_norms.units_done(site.k02.id) == D(1)


def test_cancel_latest_stage_first_and_cancelled_stay_in_history(svc, site):
    up = svc.unit_progress
    done = up.mark(site.reception.id, 1, STAGE_DONE)
    disp = up.mark(site.reception.id, 1, STAGE_DISPATCHED)
    with pytest.raises(ValidationError, match="cancel that mark first"):
        up.cancel(done.id, "wrong unit")
    up.cancel(disp.id, "not dispatched after all")
    assert row(svc, site.reception.id).status == STAGE_DONE
    with pytest.raises(ValidationError, match="already cancelled"):
        up.cancel(disp.id, "again")
    up.mark(site.reception.id, 1, STAGE_DISPATCHED)  # can be marked again
    assert len(up.history(site.reception.id, 1)) == 3


def test_mark_many_and_skip(svc, site):
    up = svc.unit_progress
    up.mark(site.desk.id, 1, STAGE_DONE)
    pieces = [(site.desk.id, 1), (site.desk.id, 2)]
    with pytest.raises(ValidationError):
        up.mark_many(pieces, STAGE_DONE)          # all or none
    assert row(svc, site.desk.id, 2).status == NOT_STARTED
    made = up.mark_many(pieces, STAGE_DONE, skip_done=True)
    assert len(made) == 1 and row(svc, site.desk.id, 2).status == STAGE_DONE


def test_batches_from_the_unit_card_fill_pieces(svc, site):
    svc.unit_norms.record_completion(site.wardrobe.id, "2", TODAY - timedelta(days=6))
    up = svc.unit_progress
    rows = [row(svc, site.wardrobe.id, n) for n in (1, 2, 3)]
    assert [r.status for r in rows] == [STAGE_DONE, STAGE_DONE, NOT_STARTED]
    assert rows[0].marks[STAGE_DONE].batch
    with pytest.raises(ValidationError, match="already marked Done"):
        up.mark(site.wardrobe.id, 1, STAGE_DONE)
    up.mark(site.wardrobe.id, 1, STAGE_DISPATCHED)     # dispatch a piece made in a batch
    up.mark(site.wardrobe.id, 3, STAGE_DONE)
    assert svc.unit_norms.units_done(site.wardrobe.id) == D(3)
    with pytest.raises(ValidationError):              # the card cannot go past the quantity either
        svc.unit_norms.record_completion(site.wardrobe.id, "1")


def test_marked_units_cannot_be_deleted(svc, site):
    svc.unit_progress.mark(site.k02.id, 1, STAGE_DONE)
    with pytest.raises(ValidationError, match="On site marks"):
        svc.project_tree.delete_level(site.l2.id)


def test_closed_and_archived_projects(svc, site):
    svc.project_tree.save_project(site.may.id, {"status": "COMPLETED"})
    assert {c.title for c in svc.unit_progress.board().cards} == {"Tribeca"}
    assert {c.title for c in svc.unit_progress.board(include_closed=True).cards} == {"Tribeca", "Mayfair"}


def test_audit_and_permissions(svc, site):
    from sqlalchemy import select

    from wms.db.models import AuditLog
    svc.unit_progress.mark(site.k01.id, 1, STAGE_DONE)
    with svc.ctx.db.read_session() as s:
        actions = list(s.scalars(select(AuditLog.action)))
    assert "UNIT_STAGE_MARKED" in actions


def test_upgrade_from_v123_keeps_completed_units(svc, site):
    """Down to v1.23 (0015) and up again: Done batches stay (stage DONE, no piece); Dispatched /
    On site marks have no meaning in v1.23 and are dropped by the downgrade."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from wms.db.engine import MIGRATIONS_DIR
    svc.unit_norms.record_completion(site.wardrobe.id, "2")
    svc.unit_progress.mark(site.k01.id, 1, STAGE_DONE)
    svc.unit_progress.mark(site.k01.id, 1, STAGE_DISPATCHED)
    db = svc.ctx.db
    acfg = Config()
    acfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    acfg.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(acfg, "0015")
    with db.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM unit_completions")).scalar() == 2
    db.dispose()
    db.migrate()
    with db.engine.connect() as c:
        got = sorted(c.execute(text("SELECT stage, piece_no FROM unit_completions")).all(), key=str)
    assert got == [("DONE", None), ("DONE", None)]
    assert row(svc, site.k01.id).status == STAGE_DONE           # an old single Done shows on the piece
    assert svc.unit_norms.units_done(site.wardrobe.id) == D(2)
    svc.unit_progress.mark(site.k01.id, 1, STAGE_DISPATCHED)


# ---- the screen -------------------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture


def _page(win):
    from PySide6.QtWidgets import QApplication
    win.go("production_board")
    QApplication.processEvents()
    return win.page("production_board")


def _item(page, key):
    """The tree row with this node key (opens the groups above it)."""
    page.expand_all(True)
    for it in page.tree.items():
        n = page.tree.node(it)
        if n is not None and n.kind == "type":
            it.setExpanded(True)
    for it in page.tree.items():
        n = page.tree.node(it)
        if n is not None and n.key == key:
            return it
    raise AssertionError(f"row {key} not shown")


def svc_of(win):
    return win.app.services


def test_screen_tree_and_project_cards(ui, site):  # noqa: F811
    from wms.ui.pages.production_board import COLUMNS
    _app, win, msgs, _ = ui
    page = _page(win)
    hdr = page.tree.headerItem()
    assert [hdr.text(i) for i in range(hdr.columnCount())] == list(COLUMNS)
    tops = [page.tree.node(page.tree.topLevelItem(i)) for i in range(page.tree.topLevelItemCount())]
    assert [n.kind for n in tops] == ["project", "project"] and [n.qty for n in tops] == [2, 6]
    assert set(page.cards) == {None, site.tri.id, site.may.id}
    page.pick_project(site.tri.id)
    tops = [page.tree.node(page.tree.topLevelItem(i)) for i in range(page.tree.topLevelItemCount())]
    # one project: its blocks, then the units directly in the project
    assert [(n.kind, n.text) for n in tops] == [("block", "Block A"), ("piece", "Reception")]
    ward = page.tree.node(_item(page, f"U{site.wardrobe.id}"))
    assert ward.kind == "type" and ward.qty == 3       # one row for the unit type, pieces under it
    assert _item(page, f"P{site.wardrobe.id}:3").text(0) == "#3 of 3"
    assert not _errors(msgs)


def test_click_marks_today_and_colours_row(ui, site):  # noqa: F811
    from PySide6.QtGui import QColor

    from wms.ui.pages.production_board import ROW_COLOURS
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.tri.id)
    key = f"P{site.k01.id}:1"
    it = _item(page, key)
    assert it.text(3) == ""                          # Dispatched waits for Done
    page.tree.itemClicked.emit(it, 3)
    assert row(svc_of(win), site.k01.id).status == NOT_STARTED
    page.tree.itemClicked.emit(it, 2)
    assert row(svc_of(win), site.k01.id).date_of(STAGE_DONE) == TODAY
    it = _item(page, key)
    assert it.text(2).startswith("✔") and it.text(3) == "○ Mark"
    assert it.background(0).color() == QColor(ROW_COLOURS[STAGE_DONE])
    assert "Done <b>1</b>" in page.cards[site.tri.id].nums.text()
    level = page.tree.node(_item(page, f"L{site.l1.id}"))
    assert level.count(STAGE_DONE) == 1 and level.qty == 4   # running total on the level row
    assert not _errors(msgs)


def test_mark_next_units_from_a_group(ui, site, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QDialog

    from wms.ui.pages import production_board as pb
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.tri.id)
    ward = page.tree.node(_item(page, f"U{site.wardrobe.id}"))
    page.mark_next(ward, STAGE_DONE, count=2)
    assert svc_of(win).unit_norms.units_done(site.wardrobe.id) == D(2)
    assert [r.piece_no for r in rows_of(svc_of(win)) if r.unit_id == site.wardrobe.id
            and STAGE_DONE in r.marks] == [1, 2]                 # the first pieces
    # a click on the "2/3" opens the dialog with the units that can still be marked
    seen = {}

    def accept(dlg):
        seen["most"] = dlg.count.maximum()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(pb.MarkDialog, "exec", accept)
    page.tree.itemClicked.emit(_item(page, f"U{site.wardrobe.id}"), 2)
    assert seen["most"] == 1 and svc_of(win).unit_norms.units_done(site.wardrobe.id) == D(3)
    # Dispatched for the whole level: only the made units can go
    level = page.tree.node(_item(page, f"L{site.l1.id}"))
    page.mark_next(level, STAGE_DISPATCHED, count=3)
    rows = [r for r in rows_of(svc_of(win)) if r.level == "Level 1"]
    assert sum(STAGE_DISPATCHED in r.marks for r in rows) == 3
    assert not _errors(msgs)


def test_show_filter_search_and_project_search(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    svc_of(win).unit_progress.mark(site.wardrobe.id, 2, STAGE_DONE)
    page = _page(win)
    page.refresh()
    assert len(page.shown_rows()) == 8
    page.show_combo.setCurrentIndex(page.show_combo.findData("MADE"))
    assert [r.key for r in page.shown_rows()] == [(site.wardrobe.id, 2)]
    page.show_combo.setCurrentIndex(page.show_combo.findData("TODO"))
    assert len(page.shown_rows()) == 7
    page.show_combo.setCurrentIndex(0)
    page.search.setText("wardrobe")
    page.search.search.emit("wardrobe")
    assert len(page.shown_rows()) == 3
    assert page.count.text() == "3 of 8 units shown"
    page.search.setText("")
    page.search.search.emit("")
    page.pick_project(site.may.id)
    assert len(page.shown_rows()) == 2 and page.cards[site.may.id].active
    page.pick_project(site.may.id)               # the active card again: all projects
    assert len(page.shown_rows()) == 8 and page.cards[None].active
    page.find_project.setText("john")            # manager name finds the project card
    assert [page.plist.item(i).isHidden() for i in range(page.plist.count())] == [False, True, False]
    assert not _errors(msgs)


def test_select_several_and_mark(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.tri.id)
    items = [_item(page, f"P{site.wardrobe.id}:{p}") for p in (1, 2, 3)]
    for it in items:
        it.setSelected(True)
    page.tree.itemClicked.emit(items[1], 2)
    assert svc_of(win).unit_norms.units_done(site.wardrobe.id) == D(3)
    assert not _errors(msgs)


def test_mark_next_only_marks_units_the_filter_shows(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.tri.id)
    page.search.setText("wardrobe")
    page.search.search.emit("wardrobe")
    level = page.tree.node(_item(page, f"L{site.l1.id}"))
    assert len(level.markable(STAGE_DONE, page.filtering)) == 3      # not the kitchen K01 next to them
    page.mark_next(level, STAGE_DONE, count=3)
    rows = rows_of(svc_of(win))
    assert sum(STAGE_DONE in r.marks for r in rows if r.unit_id == site.wardrobe.id) == 3
    assert row(svc_of(win), site.k01.id).status == NOT_STARTED
    assert not _errors(msgs)


def test_open_groups_stay_open_after_a_mark(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.tri.id)
    _item(page, f"U{site.wardrobe.id}")             # opens the wardrobe pieces
    page.mark_rows([r for r in page.rows if r.key == (site.wardrobe.id, 1)], STAGE_DONE)
    ward = next(it for it in page.tree.items() if page.tree.node(it) and page.tree.node(it).key == f"U{site.wardrobe.id}")
    assert ward.isExpanded() and ward.child(0).text(2).startswith("✔")
    assert not _errors(msgs)


def test_view_only_user_cannot_mark(ui, site, monkeypatch):  # noqa: F811
    from wms.core.permissions import P
    _app, win, msgs, _ = ui
    page = _page(win)
    has = win.app.ctx.has
    monkeypatch.setattr(win.app.ctx, "has", lambda perm: perm != P.SITES_EDIT and has(perm))
    page.refresh()
    page.pick_project(site.tri.id)
    it = _item(page, f"P{site.k01.id}:1")
    assert it.text(2) == ""                          # no "○ Mark" to click
    page.tree.itemClicked.emit(it, 2)
    page.tree.itemClicked.emit(_item(page, f"U{site.wardrobe.id}"), 2)
    assert all(not r.marks for r in rows_of(svc_of(win)))
    assert not _errors(msgs)


def test_status_bar_keys_are_links(ui):  # noqa: F811
    from PySide6.QtWidgets import QApplication
    _app, win, msgs, _ = ui
    assert "href='goods_in'" in win.keys_hint.text() and "F2 Goods In" in win.keys_hint.text()
    win.keys_hint.linkActivated.emit("goods_in")     # a click on "F2 Goods In" opens Goods In
    QApplication.processEvents()
    assert win.stack.currentWidget() is win.page("goods_in")
    assert not _errors(msgs)


def test_opening_a_big_level_is_quick():
    import time

    from wms.services.unit_progress import PieceRow
    from wms.ui.pages.production_board import Node, children
    rows = [PieceRow(u, p, 50, 1, "TRI", "Tribeca", None, "", 1, "Block A", 1, "Level 1", f"K{u}", "Kitchen")
            for u in range(1000) for p in range(1, 51)]
    level = Node("level", "L1", "Level 1", rows, rows)
    t = time.perf_counter()
    kids = children(level)
    assert len(kids) == 1000 and kids[0].qty == 50
    assert time.perf_counter() - t < 0.5


def test_end_date_and_dispatched_warnings():
    from wms.services.unit_progress import Mark, PieceRow, ProjectCard
    from wms.ui.pages.production_board import end_warning, stale_dispatched
    card = ProjectCard(1, "TRI", "Tribeca", "", "ACTIVE", TODAY + timedelta(days=6))
    assert end_warning(card, 5, 16) == "⚠ ends in 6 day(s), 69% not made"
    assert end_warning(card, 16, 16) == ""
    card.end_date = TODAY - timedelta(days=2)
    assert end_warning(card, 0, 4).startswith("⚠ 2 day(s) overdue")
    card.end_date = TODAY + timedelta(days=60)
    assert end_warning(card, 0, 4) == ""
    r = PieceRow(1, 1, 1, 1, "TRI", "Tribeca", None, "", None, "", None, "", "K01", "Kitchen")
    r.marks = {STAGE_DONE: Mark(1, STAGE_DONE, TODAY - timedelta(days=9)),
               STAGE_DISPATCHED: Mark(2, STAGE_DISPATCHED, TODAY - timedelta(days=6))}
    assert stale_dispatched([r]) == [r]
    r.marks[STAGE_DISPATCHED] = Mark(2, STAGE_DISPATCHED, TODAY - timedelta(days=2))
    assert stale_dispatched([r]) == []


def test_export(ui, site, tmp_path, monkeypatch):  # noqa: F811
    from openpyxl import load_workbook
    from PySide6.QtWidgets import QFileDialog
    _app, win, msgs, _ = ui
    svc_of(win).unit_progress.mark(site.k01.id, 1, STAGE_DONE)
    page = _page(win)
    page.refresh()
    out = tmp_path / "p.xlsx"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    page.export()
    ws = load_workbook(out).active
    values = [c for row_ in ws.iter_rows(values_only=True) for c in row_ if c]
    assert "Kitchen · K01" in values and "Done" in values
    assert not _errors(msgs)
