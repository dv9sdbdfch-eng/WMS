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
    assert len(keys) == 15
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
