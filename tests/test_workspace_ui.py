"""v1.14 Dashboard workspace: TODAY removed, Dashboard and Action Required fold, the Remaining / Stock
table takes all the height that is left, and useful UI preferences survive a restart."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.services.goods_out import IssueLine
from wms.ui.pages.dashboard import DashboardPage


def _settle():
    for _ in range(4):
        QApplication.processEvents()


def _home(win):
    win.go("dashboard")
    dash = win.page("dashboard")
    dash.refresh()
    _settle()
    return dash


def _click(widget):
    pos = QPointF(widget.rect().center())
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        ev = QMouseEvent(kind, pos, widget.mapToGlobal(pos), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(widget, ev)


def test_today_is_gone_and_the_screen_order_is_right(ui, sample):  # noqa: F811
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 5)
    dash = _home(win)
    assert not hasattr(dash, "today_tiles") and not hasattr(dash, "open_today")
    assert not dash.head_widget.isVisible()  # the Dashboard section header is the title
    # Dashboard, Action Required, then search + filters + the stock table filling the rest
    order = [dash.body.itemAt(i).widget() for i in range(dash.body.count())]
    assert order[:3] == [dash.dash_section, dash.action_section, dash.stock]
    assert dash.body.stretch(2) == 1
    v = dash.stock
    for w in (v.search, v.project, v.status, v.stock, v.clear_btn, v.columns_btn):
        assert w.isVisible()
    assert v.model.total == 3 and v.table.horizontalHeader().isVisible()
    assert not _errors(msgs), msgs


def test_sections_fold_and_the_table_takes_the_space(ui, sample):  # noqa: F811
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 1)
    app.services.goods_out.post({"site_id": sample.site.id, "issue_date": date.today()},
                                [IssueLine(sample.mdf.id, "3")], override_reason="sheets on site")
    win.resize(1366, 768)
    dash = _home(win)
    ds, act = dash.dash_section, dash.action_section
    # Dashboard folded by default, only its header shown; Action Required open while it has entries
    assert not ds.expanded and not ds.content.isVisible() and ds.header.isVisible()
    assert act.expanded and act.content.isVisible()
    n = len(dash.data.attention())
    assert n >= 2 and act.count_label.text() == f"({n})"
    assert act.count_label.property("severity") == "critical"  # negative balance + no backup

    h_open = dash.stock.table.height()
    _click(act.header)  # a click anywhere on the header folds it
    _settle()
    assert not act.expanded and not act.content.isVisible()
    assert act.count_label.isVisible() and act.count_label.text() == f"({n})"  # count stays visible
    assert "critical" in act.hint_label.text()
    h_folded = dash.stock.table.height()
    assert h_folded > h_open  # the freed height went straight to the table

    ds.chevron.click()  # the chevron does the same
    _settle()
    assert ds.expanded and dash.t_mv.isVisible()
    assert dash.stock.table.height() < h_folded
    ds.chevron.click()
    _settle()
    assert dash.stock.table.height() == h_folded

    # a bigger window = a bigger table (no fixed height)
    win.resize(1366, 1000)
    _settle()
    assert dash.stock.table.height() >= h_folded + 200
    win.resize(1366, 768)
    _settle()
    assert dash.stock.table.height() == h_folded
    assert not _errors(msgs), msgs


def test_states_columns_and_sort_survive_a_restart_but_filters_do_not(ui, sample):  # noqa: F811
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 5)
    sample.receive(sample.edge, 9, number="DN-2")
    dash = _home(win)
    dash.dash_section.toggle()  # open Dashboard
    dash.action_section.toggle()  # fold Action Required
    v = dash.stock
    col = [c.key for c in v.model.cols].index("qty")
    v._header_clicked(col)  # sort by On Hand (largest first)
    assert (v.filter.sort, v.filter.descending) == ("qty", True)
    v.table.horizontalHeader().resizeSection([c.key for c in v.model.cols].index("name"), 333)
    v.search.setText("MDF")
    v.search.returnPressed.emit()
    v.stock.setCurrentIndex(v.stock.findData("IN_STOCK"))
    assert v.filter.text == "MDF" and v.filter.stock == "IN_STOCK"

    again = DashboardPage(app)  # = the next start of the program
    again.refresh()
    assert again.dash_section.expanded and not again.action_section.expanded
    w = again.stock
    assert (w.filter.sort, w.filter.descending) == ("qty", True)
    assert w.table.columnWidth([c.key for c in w.model.cols].index("name")) == 333
    assert w.filter.text == "" and w.search.text() == ""  # search text is never kept
    assert w.filter.stock is None and w.filter.status == "ACTIVE"  # all active stock, never a part
    assert w.model.total == 3
    again.deleteLater()
    assert not _errors(msgs), msgs


def test_action_required_folds_itself_when_empty(ui):  # noqa: F811
    app, win, msgs, _ = ui
    app.services.backup.create(note="healthy")  # no backup = a Critical entry
    dash = _home(win)
    items = dash.data.attention()
    if items:  # an empty test database may still list data-quality entries
        assert dash.action_section.expanded
    else:
        assert not dash.action_section.expanded and dash.action_section.count_label.text() == "(0)"
    assert not _errors(msgs), msgs


def test_clear_filters_and_status_on_the_workspace(ui, sample):  # noqa: F811
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 5)
    dash = _home(win)
    v = dash.stock
    assert not v.clear_btn.isEnabled()  # nothing to clear
    v.status.setCurrentIndex(v.status.findData("ALL"))
    assert v.filter.status == "ALL" and v.clear_btn.isEnabled()
    v.project.set_value(sample.site.id)
    v.project.picked.emit(sample.site.id)
    assert v.filter.site_id == sample.site.id and "remaining" in [c.key for c in v.model.cols]
    assert not v.chips_scroll.isVisible()  # the drop-downs already show these filters
    v.clear_btn.click()
    assert v.filter.site_id is None and v.filter.status == "ACTIVE" and v.status.currentData() == "ACTIVE"
    assert not v.clear_btn.isEnabled()
    assert not _errors(msgs), msgs
