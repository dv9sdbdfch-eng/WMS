"""v1.19 Projects details panel: unsaved changes are never lost by accident.

Typing in the panel marks it as changed. Selecting another row, changing the search or status
filter, opening another screen or closing the program then asks: Save Changes / Discard Changes /
Cancel. Tests 1-7 of the specification.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QCloseEvent
from test_project_tree import _open_page, tower, tribeca  # noqa: F401 - shared fixtures
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

import wms.ui.pages.projects as projects
from wms.ui.pages.projects import CANCEL, DISCARD, SAVE, ask_unsaved


@pytest.fixture
def answers(monkeypatch):
    """Answer the Unsaved Changes dialog from a list; records how often it was shown."""
    state = {"queue": [], "asked": 0}

    def fake(_parent):
        state["asked"] += 1
        return state["queue"].pop(0) if state["queue"] else CANCEL
    monkeypatch.setattr(projects, "ask_unsaved", fake)
    yield state
    from PySide6.QtWidgets import QApplication  # windows closed at the end must not ask again
    for w in QApplication.allWidgets():
        if isinstance(w, projects.NodePanel):
            w.mark_clean()


def _page(win, tribeca):  # noqa: F811
    page = _open_page(win)
    page.items[f"M:{tribeca.john.id}"].setExpanded(True)
    page.items[f"P:{tribeca.project.id}"].setExpanded(True)
    page.select(f"U:{tribeca.kitchen.id}")
    return page


def test_1_6_changes_detected_and_no_question_without_them(ui, tribeca, answers):  # noqa: F811
    _app, win, msgs, _ = ui
    page = _page(win, tribeca)
    assert not page.panel.is_dirty()
    page.select(f"U:{tribeca.wardrobe.id}")  # nothing typed: no question
    page.select(f"P:{tribeca.project.id}")
    assert answers["asked"] == 0 and page.current_key() == f"P:{tribeca.project.id}"
    page.panel.p_notes.setPlainText("call site manager")
    assert page.panel.is_dirty()
    page.panel.p_notes.setPlainText("")  # typed back to what it was: nothing to save
    assert not page.panel.is_dirty()
    page.panel.p_status.setCurrentIndex(page.panel.p_status.findData("ON_HOLD"))
    assert page.panel.is_dirty()
    assert not _errors(msgs), msgs


def test_2_3_cancel_keeps_selection_and_typing(ui, tribeca, answers):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    answers["queue"] = [CANCEL]
    page.select(f"U:{tribeca.wardrobe.id}")
    assert answers["asked"] == 1
    assert page.current_key() == f"U:{tribeca.kitchen.id}"
    assert page.panel.u_name.text() == "Kitchen deluxe" and page.panel.is_dirty()


def test_3_save_saves_then_moves(ui, tribeca, answers):  # noqa: F811
    app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    page.panel.u_qty.setText("30")
    answers["queue"] = [SAVE]
    page.select(f"U:{tribeca.wardrobe.id}")
    unit = app.services.project_tree.unit(tribeca.kitchen.id)
    assert unit.name == "Kitchen deluxe" and unit.quantity == 30
    assert page.current_key() == f"U:{tribeca.wardrobe.id}" and page.panel.u_name.text() == "Wardrobe"
    assert not page.panel.is_dirty()


def test_3_discard_forgets_then_moves(ui, tribeca, answers):  # noqa: F811
    app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    answers["queue"] = [DISCARD]
    page.select(f"U:{tribeca.wardrobe.id}")
    assert app.services.project_tree.unit(tribeca.kitchen.id).name == "Kitchen"  # nothing saved
    assert page.current_key() == f"U:{tribeca.wardrobe.id}" and not page.panel.is_dirty()
    page.select(f"U:{tribeca.kitchen.id}")
    assert page.panel.u_name.text() == "Kitchen"


def test_4_failed_save_keeps_row_and_values(ui, tribeca, answers):  # noqa: F811
    app, win, msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_qty.setText("-5")  # refused by the service
    answers["queue"] = [SAVE]
    page.select(f"U:{tribeca.wardrobe.id}")
    assert any(m[0] == "warning" for m in msgs)  # the reason is shown
    assert page.current_key() == f"U:{tribeca.kitchen.id}"
    assert page.panel.u_qty.text() == "-5" and page.panel.is_dirty()
    assert app.services.project_tree.unit(tribeca.kitchen.id).quantity == 24


def test_5_hiding_the_panel_keeps_edits_and_still_protects(ui, tribeca, answers):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_notes.setPlainText("wall units only")
    page.set_details_visible(False)
    answers["queue"] = [CANCEL]
    page.select(f"U:{tribeca.wardrobe.id}")
    assert answers["asked"] == 1 and page.current_key() == f"U:{tribeca.kitchen.id}"
    page.set_details_visible(True)
    assert page.panel.u_notes.toPlainText() == "wall units only"


def test_filters_screens_and_closing_are_protected(ui, tribeca, answers):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    # status filter: Cancel puts the old choice back and keeps the list
    answers["queue"] = [CANCEL]
    page.status_filter.setCurrentIndex(2)
    assert page.status_filter.currentIndex() == 0 and page.panel.u_name.text() == "Kitchen deluxe"
    # search box
    answers["queue"] = [CANCEL]
    page.search.setText("hinge")
    page.search.search.emit("hinge")
    assert page.search.text() == "" and page.matches is None
    # another screen: stays on Projects
    answers["queue"] = [CANCEL]
    win.go("products")
    assert win.stack.currentWidget() is page
    # closing the program: refused while Cancel is chosen
    answers["queue"] = [CANCEL]
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted() and win.stack.currentWidget() is page
    assert answers["asked"] == 4 and page.panel.u_name.text() == "Kitchen deluxe"
    # Discard lets the other screen open
    answers["queue"] = [DISCARD]
    win.go("products")
    assert win.stack.currentWidget() is win.page("products")


def test_refresh_from_elsewhere_keeps_typing(ui, tribeca, answers):  # noqa: F811
    app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    page.refresh()  # e.g. another screen saved something
    assert answers["asked"] == 0
    assert page.current_key() == f"U:{tribeca.kitchen.id}" and page.panel.u_name.text() == "Kitchen deluxe"


def test_every_kind_of_row_is_protected(ui, tribeca, tower, answers):  # noqa: F811
    _app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.items[f"B:{tower.block.id}"].setExpanded(True)
    page.items[f"V:{tower.level.id}"].setExpanded(True)
    page.items[f"U:{tower.k10.id}"].setExpanded(True)
    edits = {f"M:{tribeca.john.id}": lambda p: p.m_phone.setText("01234"),
             f"P:{tribeca.project.id}": lambda p: p.p_name.setText("X"),
             f"B:{tower.block.id}": lambda p: p.b_code.setText("Z"),
             f"V:{tower.level.id}": lambda p: p.v_notes.setPlainText("n"),
             f"U:{tower.k10.id}": lambda p: p.u_code.setText("K99"),
             page.items[f"U:{tower.k10.id}"].child(0).data(0, projects.KEY): lambda p: p.l_vpk.setText("V1")}
    for key, edit in edits.items():
        page.select(key)
        edit(page.panel)
        assert page.panel.is_dirty(), key
        answers["queue"] = [DISCARD]
        page.select(f"U:{tribeca.wardrobe.id}")
        assert page.current_key() == f"U:{tribeca.wardrobe.id}", key


def test_mouse_click_cancel_stays(ui, tribeca, answers):  # noqa: F811
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    _app, win, _msgs, _ = ui
    page = _page(win, tribeca)
    page.panel.u_name.setText("Kitchen deluxe")
    target = page.items[f"U:{tribeca.wardrobe.id}"]
    rect = page.tree.visualItemRect(target)
    answers["queue"] = [CANCEL]
    QTest.mouseClick(page.tree.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 40, rect.center().y()))
    assert answers["asked"] == 1 and page.current_key() == f"U:{tribeca.kitchen.id}"
    assert page.tree.currentItem() is page.items[f"U:{tribeca.kitchen.id}"]
    assert page.tree.currentColumn() == projects.C_UNIT  # back on the same cell
    answers["queue"] = [DISCARD]
    QTest.mouseClick(page.tree.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.left() + 40, rect.center().y()))
    assert page.current_key() == f"U:{tribeca.wardrobe.id}"


def test_dialog_buttons(ui):  # noqa: F811
    """The real dialog: three buttons with the wording of the specification."""
    from PySide6.QtWidgets import QMessageBox
    _app, win, _msgs, _ = ui
    seen = {}

    def look(box):
        seen["title"], seen["text"] = box.windowTitle(), box.text()
        seen["buttons"] = [b.text() for b in box.buttons()]
        box.buttons()[[b.text() for b in box.buttons()].index("Cancel")].click()
        return 0
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(QMessageBox, "exec", look)
    try:
        assert ask_unsaved(win) == CANCEL
    finally:
        mp.undo()
    assert seen["title"] == "Unsaved Changes"
    assert seen["text"] == "You have unsaved changes. What would you like to do?"
    assert set(seen["buttons"]) == {"Save Changes", "Discard Changes", "Cancel"}
