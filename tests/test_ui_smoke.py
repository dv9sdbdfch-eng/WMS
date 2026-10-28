"""UI smoke tests: every screen opens, every record dialog opens, and the main
operator flows (Goods In, Goods Out) work through the real screens.

Message boxes are answered automatically (Yes / OK) and recorded, so an
unexpected error dialog fails the test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from wms.ui.app import App


@pytest.fixture
def ui(qtbot, svc, monkeypatch):
    msgs = []

    def record(kind):
        def fn(parent, title, text, *a, **k):
            msgs.append((kind, title, text))
            return QMessageBox.StandardButton.Ok
        return fn

    monkeypatch.setattr(QMessageBox, "warning", record("warning"))
    monkeypatch.setattr(QMessageBox, "critical", record("critical"))
    monkeypatch.setattr(QMessageBox, "information", record("info"))

    def auto_yes(box):
        msgs.append(("confirm", box.windowTitle(), box.text()))
        for b in box.buttons():
            if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole:
                b.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", auto_yes)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("Test reason", True))
    opened = []

    def fake_exec(dlg):
        opened.append(type(dlg).__name__)
        dlg.show()
        dlg.close()
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    app = App(svc.ctx)
    from wms.ui.main_window import MainWindow
    win = MainWindow(app)
    qtbot.addWidget(win)
    win.show()
    return app, win, msgs, opened


def _errors(msgs):
    return [m for m in msgs if m[0] in ("warning", "critical")]


def test_every_page_opens(ui, sample):
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 40)
    keys = list(win.factories)
    assert len(keys) == 16
    for key in keys:
        win.go(key)
        page = win.page(key)
        assert page is not None, key
        page.refresh()
    assert not _errors(msgs), msgs


def test_record_dialogs_open(ui, sample):
    app, win, msgs, opened = ui
    dn = sample.receive(sample.mdf, 40)
    from wms.services.goods_out import IssueLine
    from wms.services.production import MaterialInput
    from wms.services.stock_ops import ReturnLineInput
    gi = app.services.goods_out.post({"site_id": sample.site.id, "issue_date": dn.delivery_date},
                                     [IssueLine(sample.mdf.id, Decimal("5"))])
    rt = app.services.stock.post_return({"return_type": "FROM_SITE", "return_date": dn.delivery_date,
                                         "site_id": sample.site.id}, [ReturnLineInput(sample.mdf.id, "1",
                                                                                      sample.bin1.id)])
    po = app.services.production.post({"finished_product_id": sample.cabinet.id, "quantity": "1",
                                       "production_date": dn.delivery_date},
                                      [MaterialInput(sample.mdf.id, "2", None)])
    for kind, oid in (("PRODUCT", sample.mdf.id), ("PRODUCT", sample.cabinet.id), ("DELIVERY_NOTE", dn.id),
                      ("GOODS_ISSUE", gi.id), ("SITE", sample.site.id), ("RETURN", rt.id), ("BIN", sample.bin1.id),
                      ("SUPPLIER", sample.supplier.id), ("PRODUCTION", po.id)):
        app.open_entity(kind, oid, win)
    from wms.ui.dialogs.stock_dialogs import AdjustmentDialog, ProductionEntryDialog, ReturnEntryDialog, TransferDialog
    from wms.ui.pages.movements import MovementDetailDialog
    TransferDialog(app, sample.mdf.id, win, sample.bin1.id)
    AdjustmentDialog(app, sample.mdf.id, sample.bin1.id, win)
    ReturnEntryDialog(app, "TO_SUPPLIER", win)
    ProductionEntryDialog(app, win)
    MovementDetailDialog(app, app.services.stock.movements()[0], win)
    assert len(opened) == 9
    assert not _errors(msgs), msgs


def test_goods_in_screen_posts_delivery(ui, sample):
    app, win, msgs, _ = ui
    win.go("goods_in")
    page = win.page("goods_in")
    page.supplier.set_value(sample.supplier.id)
    page.dn_number.setText("DN 7781")
    page.lines.clear()
    page.lines.add_row(sample.mdf.id, Decimal("12"))
    page.lines.add_row(sample.edge.id, Decimal("150.5"), sample.bin1.id)
    page.post()
    assert not _errors(msgs), msgs
    dns = app.services.goods_in.search("DN 7781")
    assert len(dns) == 1 and dns[0][1] == 2
    assert page.dn_number.text() == ""  # form cleared after posting
    # Entering the same delivery note again is blocked
    page.supplier.set_value(sample.supplier.id)
    page.dn_number.setText("dn7781")
    page.lines.add_row(sample.mdf.id, Decimal("1"))
    page.post()
    assert any(t == "Already entered" for _, t, _ in msgs)
    assert len(app.services.goods_in.search("7781")) == 1


def test_goods_out_screen_blocks_then_overrides_negative(ui, sample):
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 3)
    win.go("goods_out")
    page = win.page("goods_out")
    page.site.set_value(sample.site.id)
    page.lines.clear()
    page.lines.add_row(sample.mdf.id, Decimal("5"))
    page.post()  # admin may override: confirm + reason are auto-answered
    assert any(t == "Not enough stock" for _, t, _ in msgs)
    issues = app.services.goods_out.search()
    assert len(issues) == 1
    assert app.services.products.get(sample.mdf.id) is not None
    rows = {r.product.id: r for r in app.services.products.list()}
    assert rows[sample.mdf.id].on_hand == Decimal("-2")


def test_goods_out_double_post_and_repeat_are_caught(ui, sample):
    """Ctrl+S pressed twice / the same issue typed in twice must not silently create two issues."""
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 20)
    win.go("goods_out")
    page = win.page("goods_out")
    page.site.set_value(sample.site.id)
    page.lines.clear()
    page.lines.add_row(sample.mdf.id, Decimal("2"))
    token = page._token
    page.post()
    assert len(app.services.goods_out.search()) == 1 and page._token != token  # new form, new token
    # The server refuses the old form's token even if the screen somehow sent it again
    from wms.core.exceptions import AlreadyPostedError
    from wms.services.goods_out import IssueLine
    with pytest.raises(AlreadyPostedError):
        app.services.goods_out.post({"site_id": sample.site.id, "issue_date": page.date.value(),
                                     "client_token": token}, [IssueLine(sample.mdf.id, "2")])
    # Re-entering the same materials for the same site asks first (auto-answered "Post again" here)
    page.site.set_value(sample.site.id)
    page.lines.clear()
    page.lines.add_row(sample.mdf.id, Decimal("2"))
    page.post()
    assert any(t == "Possible duplicate" for _, t, _ in msgs)
    assert len(app.services.goods_out.search()) == 2
    # Re-entrancy: a post() arriving while one is running is ignored
    page._posting = True
    page.post()
    assert len(app.services.goods_out.search()) == 2
    assert not _errors(msgs), msgs


def test_scanner_flow_in_lines_editor(ui, sample):
    """A USB scanner types the barcode and presses Enter: product chosen, cursor to quantity, Enter -> next line."""
    app, win, msgs, _ = ui
    app.services.products.save(sample.mdf.id, {"item_type": "RAW_MATERIAL", "internal_code": "MDF-18",
                                               "name": "MDF 18mm 2440x1220", "unit_id": sample.mdf.unit_id,
                                               "barcode": "5012345678900", "default_bin_id": sample.bin1.id})
    win.go("goods_in")
    page = win.page("goods_in")
    page.refresh()
    ed = page.lines
    ed.clear()
    ed.add_row()
    prod = ed.table.cellWidget(0, 0)
    prod.setEditText("5012345678900")
    prod.lineEdit().returnPressed.emit()
    assert prod.value() == sample.mdf.id
    qty = ed.table.cellWidget(0, 1)
    qty.setText("4")
    qty.returnPressed.emit()
    assert ed.table.rowCount() == 2  # ready for the next scan
    unknown = ed.table.cellWidget(1, 0)
    unknown.setEditText("NO-SUCH-CODE")
    unknown.lineEdit().returnPressed.emit()
    assert unknown.value() is None and "No product" in ed.total_label.text()
    assert [(v.product_id, v.quantity) for v in ed.values()] == [(sample.mdf.id, Decimal("4"))]


def test_enter_in_a_dialog_field_never_presses_a_button(ui, sample):
    from PySide6.QtWidgets import QPushButton

    from wms.ui.dialogs.stock_dialogs import ReturnEntryDialog, TransferDialog
    app, *_ = ui
    for dlg in (TransferDialog(app), ReturnEntryDialog(app)):
        assert all(not b.autoDefault() and not b.isDefault() for b in dlg.findChildren(QPushButton))


def test_stocktake_screen_count_and_approve(ui, sample):
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 10)
    count = app.services.stocktake.create(None)
    win.go("stocktake")
    page = win.page("stocktake")
    page.refresh()
    page._show(app.services.stocktake.get(count.id))
    page.lines.cellWidget(0, 5).setText("8")
    page.lines.cellWidget(0, 7).setText("2 sheets water damaged")
    page.submit()
    assert app.services.stocktake.get(count.id).status == "SUBMITTED"
    page.approve()
    assert app.services.stocktake.get(count.id).status == "APPROVED"
    assert {r.product.id: r for r in app.services.products.list()}[sample.mdf.id].on_hand == Decimal("8")
    assert not _errors(msgs), msgs


def test_dashboard_shows_actionable_alerts(ui, sample):
    from datetime import date

    from wms.services.goods_out import IssueLine
    app, win, msgs, _ = ui
    sample.receive(sample.mdf, 1)
    app.services.goods_out.post({"site_id": sample.site.id, "issue_date": date.today()},
                                [IssueLine(sample.mdf.id, "3")], override_reason="sheets on site")
    alerts = app.services.dashboard.load().alerts
    texts = " | ".join(a.text for a in alerts)
    assert "negative" in texts and "out of stock" in texts and "no scan" in texts
    win.go("dashboard")
    win.page("dashboard").refresh()
    assert not _errors(msgs), msgs


def test_operative_sees_limited_menu(qtbot, svc, sample):
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "op1", "full_name": "Op One", "role_id": roles["OPERATIVE"].id,
                           "must_change_password": False}, "secret123")
    svc.auth.login("op1", "secret123")
    from wms.ui.main_window import MainWindow
    win = MainWindow(App(svc.ctx))
    qtbot.addWidget(win)
    assert "goods_in" in win.factories and "goods_out" in win.factories
    assert "users" not in win.factories and "audit" not in win.factories




def test_theme_ignores_windows_dark_mode(qapp):
    """Windows dark mode gives Qt white text; the app must still show dark text on light backgrounds."""
    from PySide6.QtGui import QColor, QPalette

    from wms.ui.theme import apply_theme
    old_pal, old_qss = qapp.palette(), qapp.styleSheet()
    dark = QPalette()
    dark.setColor(QPalette.ColorRole.WindowText, QColor("#FFFFFF"))
    dark.setColor(QPalette.ColorRole.Text, QColor("#FFFFFF"))
    qapp.setPalette(dark)
    try:
        apply_theme(qapp)
        pal = qapp.palette()
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            assert pal.color(role).lightness() < 100, role
        assert pal.color(QPalette.ColorRole.Base).lightness() > 240
    finally:
        qapp.setPalette(old_pal)
        qapp.setStyleSheet(old_qss)
