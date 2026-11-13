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


def _cell(page, unit_id, piece, col):
    t = page.table
    for i in range(t.proxy.rowCount()):
        idx = t.proxy.index(i, col)
        r = page.rows[t.proxy.mapToSource(idx).row()]
        if r.key == (unit_id, piece):
            return idx
    raise AssertionError("row not shown")


def test_screen_rows_cards_and_columns(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win)
    m = page.table.model_
    assert [m.headerData(i, __import__("PySide6.QtCore").QtCore.Qt.Orientation.Horizontal)
            for i in range(m.columnCount())] == ["Project", "Block", "Level", "Unit", "Quantity", "Done",
                                                 "Dispatched", "On site"]
    assert page.table.proxy.rowCount() == 8
    assert set(page.cards) == {None, site.tri.id, site.may.id}
    assert not _errors(msgs)


def test_click_marks_today_and_colours_row(ui, site):  # noqa: F811
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor

    from wms.ui.pages.production_board import ROW_COLOURS
    _app, win, msgs, _ = ui
    page = _page(win)
    idx = _cell(page, site.k01.id, 1, 6)
    assert idx.data() == ""                      # Dispatched waits for Done
    page.table.clicked.emit(idx)
    assert row(svc_of(win), site.k01.id).status == NOT_STARTED
    page.table.clicked.emit(_cell(page, site.k01.id, 1, 5))
    assert _cell(page, site.k01.id, 1, 5).data().startswith("✔")
    assert row(svc_of(win), site.k01.id).date_of(STAGE_DONE) == TODAY
    bg = _cell(page, site.k01.id, 1, 0).data(Qt.ItemDataRole.BackgroundRole).color()
    assert bg == QColor(ROW_COLOURS[STAGE_DONE])
    card = page.cards[site.tri.id]
    assert "Done <b>1</b>" in card.nums.text()
    assert _cell(page, site.k01.id, 1, 6).data() == "○ Mark"
    assert not _errors(msgs)


def svc_of(win):
    return win.app.services


def test_card_click_and_filters(ui, site):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win)
    page.pick_project(site.may.id)
    assert page.table.proxy.rowCount() == 2 and page.cards[site.may.id].active
    page.pick_project(site.may.id)               # the active card again: all projects
    assert page.table.proxy.rowCount() == 8
    page.f_manager.setCurrentIndex(page.f_manager.findData("John Smith"))
    assert page.table.proxy.rowCount() == 6
    page.f_block.setCurrentIndex(page.f_block.findData("Block A"))
    page.f_level.setCurrentIndex(page.f_level.findData("Level 1"))
    assert page.table.proxy.rowCount() == 4      # kitchen K01 + 3 wardrobes
    svc_of(win).unit_progress.mark(site.wardrobe.id, 2, STAGE_DONE)
    page.refresh()
    page.f_status.setCurrentIndex(page.f_status.findData(STAGE_DONE))
    assert page.table.proxy.rowCount() == 1
    page.clear_filters()
    page.search.setText("wardrobe")
    page.search.search.emit("wardrobe")
    assert page.table.proxy.rowCount() == 3
    assert not _errors(msgs)


def test_select_several_and_mark(ui, site):  # noqa: F811
    from PySide6.QtCore import QItemSelectionModel
    _app, win, msgs, _ = ui
    page = _page(win)
    sm = page.table.selectionModel()
    for p in (1, 2, 3):
        sm.select(_cell(page, site.wardrobe.id, p, 0),
                  QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    page.table.clicked.emit(_cell(page, site.wardrobe.id, 2, 5))
    assert svc_of(win).unit_norms.units_done(site.wardrobe.id) == D(3)
    assert not _errors(msgs)


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
