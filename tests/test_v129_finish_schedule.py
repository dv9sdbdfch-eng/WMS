"""v1.29: Projects tree shows only Project Manager / Project / Block / Level / Unit (+ Details);
double-clicking a unit opens its Finish Schedule - the materials to make it, the norm, the
project's stock, how many units that covers and the shortage."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture
from test_v123_norms import kit  # noqa: F401 - Tribeca / Kitchen K01 x 10 / H3311 x 3

from wms.services.goods_in import ReceiptLine
from wms.services.unit_norms import NORM_FUTURE

D = Decimal


def _deliver(svc, sample, k, qty):
    svc.goods_in.post({"dn_number": f"DN-{qty}", "supplier_id": sample.supplier.id, "delivery_date": date.today(),
                       "site_id": k.project.id}, [ReceiptLine(k.board.id, D(qty), sample.bin1.id)])


def test_schedule_shows_stock_cover_and_shortage(svc, sample, kit):  # noqa: F811
    [row] = svc.unit_norms.finish_schedule(kit.kitchen.id)
    assert (row.need.norm, row.need.total, row.project_stock, row.units_covered, row.shortage, row.coverage) == (
        D(3), D(30), D(0), D(0), D(30), "RED")
    _deliver(svc, sample, kit, 20)  # enough for 6 kitchens
    [row] = svc.unit_norms.finish_schedule(kit.kitchen.id)
    assert (row.project_stock, row.units_covered, row.shortage, row.coverage) == (D(20), D(6), D(10), "YELLOW")
    _deliver(svc, sample, kit, 15)
    [row] = svc.unit_norms.finish_schedule(kit.kitchen.id)
    assert (row.units_covered, row.shortage, row.coverage) == (D(10), D(0), "GREEN")  # never more than units left


def test_schedule_counts_units_made(svc, sample, kit):  # noqa: F811
    svc.unit_norms.record_completion(kit.kitchen.id, "4")
    svc.unit_norms.change_norm(kit.line, "5", NORM_FUTURE)
    _deliver(svc, sample, kit, 12)
    [row] = svc.unit_norms.finish_schedule(kit.kitchen.id)
    assert (row.need.used_by_done, row.still_needed, row.need.total) == (D(12), D(30), D(42))
    assert (row.units_covered, row.shortage) == (D(2), D(18))  # 12 sheets / 5 per kitchen


def test_double_click_unit_opens_schedule_and_edits_materials(ui, sample, kit, monkeypatch):  # noqa: F811
    from wms.ui.dialogs import finish_schedule
    from wms.ui.pages.projects import C_UNIT
    app, win, msgs, _ = ui
    win.go("projects")
    page = win.page("projects")
    page.refresh()
    opened = []
    monkeypatch.setattr(finish_schedule.FinishScheduleDialog, "exec",
                        lambda dlg: opened.append(dlg) or 0)
    page.items["N:"].setExpanded(True)
    for key in (f"P:{kit.project.id}", f"B:{kit.block.id}", f"V:{kit.level.id}"):
        page.items[key].setExpanded(True)
    unit = page.items[f"U:{kit.kitchen.id}"]
    page._double_clicked(unit, C_UNIT)
    [dlg] = opened
    assert [r.need.product_code for r in dlg.lines] == ["H3311"] and "1 of 1 material(s) short" in dlg.summary.text()
    # change the norm from the schedule (no units made: nothing asked)
    monkeypatch.setattr("wms.ui.pages.projects.ask_quantity", lambda *a, **k: "4")
    dlg.table.selectRow(0)
    dlg.edit()
    assert dlg.lines[0].need.norm == D(4) and dlg.lines[0].need.total == D(40)
    # add a second material, then remove it again
    picked = sample.edge.id

    class Picker:
        def __init__(self, *a, **k):
            self.chosen = [picked]
            self.qty, self.vpk, self.notes = (type("E", (), {"text": lambda s, v=v: v})() for v in ("2", "", ""))

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("wms.ui.pages.projects.MaterialPickerDialog", Picker)
    dlg.add()
    assert [r.need.product_code for r in dlg.lines] == ["H3311", "EDGE-W22"]
    assert "2 materials" in page.items[f"U:{kit.kitchen.id}"].text(5)  # the tree row follows
    dlg.table.selectRow(1)
    dlg.move(-1)
    assert [r.need.product_code for r in dlg.lines] == ["EDGE-W22", "H3311"]
    dlg.remove()  # confirmed automatically in tests
    assert [r.need.product_code for r in dlg.lines] == ["H3311"]
    assert not _errors(msgs), msgs


def test_viewer_sees_schedule_without_edit_buttons(ui, kit):  # noqa: F811
    from wms.ui.dialogs.finish_schedule import FinishScheduleDialog
    app, win, _msgs, _ = ui
    win.go("projects")
    page = win.page("projects")
    page.can_edit = False
    dlg = FinishScheduleDialog(page, kit.kitchen.id)
    assert dlg.edit_buttons == [] and len(dlg.lines) == 1
