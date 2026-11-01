"""v1.14.1: every screen fits a 1366 x 768 screen (laptop / warehouse PC) and the sidebar's last
entry never runs into the version label."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from test_ui_smoke import ui  # noqa: F401 - the shared screen fixture

from wms.services.goods_out import IssueLine
from wms.ui.main_window import _hidden, _nav

SCREEN_W, SCREEN_H = 1366, 768


@pytest.fixture
def themed(qapp):
    """The real look (style sheet, Fusion), with one known font on every machine.

    Text decides most widths, and offscreen test machines have different fonts (Windows CI has no
    Segoe UI offscreen and falls back to something much wider). Bitstream Vera Sans ships with
    reportlab, so it is always there; it is wider than Segoe UI, which the program uses on Windows,
    so a screen that fits here fits on the warehouse PC too.
    """
    import reportlab
    from PySide6.QtGui import QFontDatabase

    from wms.ui.theme import apply_theme
    fonts = Path(reportlab.__file__).parent / "fonts"
    for f in ("Vera.ttf", "VeraBd.ttf"):
        assert QFontDatabase.addApplicationFont(str(fonts / f)) != -1, f"cannot load {f}"
    old = qapp.styleSheet(), qapp.style().name(), qapp.palette()
    apply_theme(qapp)
    sheet = qapp.styleSheet()
    family = re.search(r"font-family: ([^;]+);", sheet).group(1)
    qapp.setStyleSheet(sheet.replace(family, '"Bitstream Vera Sans"'))
    yield
    qapp.setStyleSheet(old[0])
    qapp.setStyle(old[1])
    qapp.setPalette(old[2])


def _settle():
    for _ in range(5):
        QApplication.processEvents()


def _long_texts(svc, sample):
    """Data that makes the footers long: low stock, reservations, several rows everywhere."""
    sample.receive(sample.mdf, 12)
    sample.receive(sample.edge, 50, number="DN-2")
    svc.reservations.reserve({"product_id": sample.mdf.id, "site_id": sample.site.id, "quantity": "4"})
    svc.goods_out.post({"site_id": sample.site.id, "issue_date": date.today()},
                       [IssueLine(sample.mdf.id, "3")])


def test_every_screen_fits_1366_pixels(themed, ui, sample):  # noqa: F811
    app, win, _msgs, _opened = ui
    _long_texts(app.services, sample)
    win.resize(SCREEN_W, SCREEN_H)
    for section in list(win.sections):
        win._set_folded(section, False, save=False)  # MORE and ADMIN open: the worst case
    keys = [k for _s, k, _l, _p, f in _nav() if f] + [k for k, *_r in _hidden()]
    # Measure the page itself: the window's own hint is the widest page opened so far (QStackedWidget).
    limit = SCREEN_W - win.nav.parentWidget().width()
    too_wide = {}
    for key in keys:
        win.go(key)
        _settle()
        need = win.page(key).minimumSizeHint().width()
        if need > limit:
            too_wide[key] = need
    assert too_wide == {}, f"screens wider than {limit} px (page area next to the sidebar): {too_wide}"


def test_sidebar_shows_every_entry_without_scrolling(themed, ui):  # noqa: F811
    app, win, _msgs, _opened = ui
    win.resize(SCREEN_W, SCREEN_H)
    for section in list(win.sections):
        win._set_folded(section, False, save=False)
    _settle()
    nav = win.nav
    last = nav.item(nav.count() - 1)
    rect = nav.visualItemRect(last)
    assert rect.bottom() <= nav.viewport().height(), "the last sidebar entry is cut off at 1366 x 768"
    assert not nav.verticalScrollBar().isVisible()
