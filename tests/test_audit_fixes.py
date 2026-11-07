"""Fixes from the screen-by-screen audit (v1.15.1): keyboard conflicts and screen titles."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QDialog

from wms.ui.app import App
from wms.ui.main_window import MainWindow, _hidden, _nav


@pytest.fixture
def win(qtbot, svc, sample, monkeypatch):
    opened = []
    monkeypatch.setattr(QDialog, "exec", lambda d: opened.append(type(d).__name__) or 0)
    sample.receive(sample.mdf, 10)
    app = App(svc.ctx)
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.show()
    w.activateWindow()
    qtbot.waitExposed(w)
    w.opened = opened
    return w


def _scan(w, code="MDF-18"):
    w.search.setText(code)
    w.global_search()
    page = w.page("search")
    assert page.exact_w.isVisible()
    return page


def test_no_key_has_two_owners_on_search(win):
    """F4 was both the top-bar Transfer and Search's 'View product': Qt then ignores the key."""
    page = _scan(win)
    ambiguous = []
    for sc in win.findChildren(QShortcut):
        sc.activatedAmbiguously.connect(lambda s=sc: ambiguous.append(s.key().toString()))
    keys = {}
    for b in page.findChildren(QAbstractButton):
        if b.isVisible() and not b.shortcut().isEmpty():
            keys.setdefault(b.shortcut().toString(), []).append(b.text())
    for sc in win.findChildren(QShortcut):
        if sc.context() == Qt.ShortcutContext.WindowShortcut and sc.parent().isVisible():
            keys.setdefault(sc.key().toString(), []).append("window")
    assert not {k: v for k, v in keys.items() if len(v) > 1}
    QTest.keyClick(page, Qt.Key.Key_F4)
    assert not ambiguous


def test_f4_on_scanned_product_transfers_it(win, qtbot):
    page = _scan(win)
    QTest.keyClick(page, Qt.Key.Key_F4)
    qtbot.wait(50)
    assert win.opened == ["TransferDialog"]


def test_f2_on_scanned_product_starts_goods_in_with_it(win, sample):
    _scan(win)
    win.quick_action("goods_in")
    page = win.page("goods_in")
    assert win.stack.currentWidget() is page
    assert sample.mdf.id in [ln.product_id for ln in page.lines.values(validate=False)]


def test_quick_actions_unchanged_without_a_match(win):
    win.go("search")
    win.quick_action("goods_out")
    assert win.stack.currentWidget() is win.page("goods_out")


def test_screen_title_matches_sidebar(qtbot, svc):
    """The heading of a screen says the same as the sidebar entry that opened it."""
    for _section, _key, text, _perm, factory in _nav():
        if factory is not None:
            assert factory.title == text, (text, factory.title)
    for _key, text, _perm, factory, _parent in _hidden():
        assert factory.title == text, (text, factory.title)
