"""v1.21: Remaining / To Allocate, Reserve remaining, picking list and Load from project."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from wms.core.exceptions import AlreadyPostedError, ValidationError
from wms.services.goods_out import IssueLine

D = Decimal
TODAY = date.today()


@pytest.fixture
def proj(svc, sample):
    """Riverside: a block with one level and a kitchen unit there, plus a desk directly in the project.

    MDF: kitchen 4 x 3 = 12, desk 2 x 1 = 2 -> 14 required. Stock: 10 in B04, 6 in B05."""
    t = svc.project_tree
    block = t.save_block(None, {"name": "Block A"}, site_id=sample.site.id)
    level = t.save_level(None, {"name": "Level 1"}, block_id=block.id)
    kitchen = t.save_unit(None, {"name": "Kitchen", "unit_code": "K01", "quantity": "4"}, level_id=level.id)
    t.add_materials(kitchen.id, [(sample.mdf.id, "3")])
    desk = t.save_unit(None, {"name": "Desk", "quantity": "2"}, site_id=sample.site.id)
    t.add_materials(desk.id, [(sample.mdf.id, "1")])
    sample.receive(sample.mdf, 10, sample.bin1, number="DN-2001")
    sample.receive(sample.mdf, 6, sample.bin2, number="DN-2002")
    sample.block, sample.level, sample.kitchen, sample.desk = block, level, kitchen, desk
    return sample


def mdf_row(svc, p):
    from wms.services.materials import MaterialFilter
    return next(r for r in svc.materials.page(MaterialFilter(site_id=p.site.id)) if r.id == p.mdf.id)


def test_areas_list_blocks_levels_and_units(svc, proj):
    keys = dict((k, lbl) for lbl, k in svc.allocation.areas(proj.site.id))
    assert keys[f"BLOCK:{proj.block.id}"].startswith("Block: Block A")
    assert keys[f"LEVEL:{proj.level.id}"] == "Level: Block A / Level 1"
    assert keys[f"UNIT:{proj.kitchen.id}"] == "Unit: Block A / Level 1 / K01 Kitchen"
    assert keys[f"UNIT:{proj.desk.id}"] == "Unit: Desk"


def test_remaining_ignores_reservations_and_to_allocate_does_not(svc, proj):
    svc.reservations.reserve({"product_id": proj.mdf.id, "site_id": proj.site.id, "quantity": "5"})
    r = mdf_row(svc, proj)
    assert (r.required, r.proj_reserved, r.net, r.remaining, r.to_allocate) == (D(14), D(5), 0, D(14), D(9))


def test_reserve_remaining_preview_then_one_transaction(svc, proj):
    svc.reservations.reserve({"product_id": proj.mdf.id, "site_id": proj.site.id, "quantity": "2"})
    plan = svc.allocation.reserve_plan(proj.site.id)
    assert [(x.code, x.need, x.available, x.quantity, x.short) for x in plan] == \
        [("MDF-18", D(12), D(14), D(12), 0)]
    token = uuid.uuid4().hex
    numbers = svc.allocation.reserve_remaining(proj.site.id, [(x.product_id, x.quantity) for x in plan],
                                               client_token=token)
    assert len(numbers) == 1
    r = mdf_row(svc, proj)
    assert (r.proj_reserved, r.to_allocate) == (D(14), 0)
    assert svc.allocation.reserve_plan(proj.site.id) == []  # nothing left to reserve
    with pytest.raises(AlreadyPostedError):  # the same form twice
        svc.allocation.reserve_remaining(proj.site.id, [(proj.mdf.id, D(1))], client_token=token)
    with pytest.raises(ValidationError):
        svc.allocation.reserve_remaining(proj.site.id, [])


def test_reserve_remaining_never_more_than_available(svc, proj):
    other = svc.sites.save(None, {"project_number": "P-2002", "name": "Other"})
    svc.reservations.reserve({"product_id": proj.mdf.id, "site_id": other.id, "quantity": "10"})
    [line] = svc.allocation.reserve_plan(proj.site.id)
    assert (line.need, line.available, line.quantity, line.short) == (D(14), D(6), D(6), D(8))
    # asking for more than is free is refused and nothing is saved
    with pytest.raises(ValidationError):
        svc.allocation.reserve_remaining(proj.site.id, [(proj.mdf.id, D(7))])
    assert mdf_row(svc, proj).proj_reserved == 0


def test_area_caps_the_quantity(svc, proj):
    [line] = svc.allocation.reserve_plan(proj.site.id, f"UNIT:{proj.desk.id}")
    assert line.quantity == D(2)
    [line] = svc.allocation.reserve_plan(proj.site.id, f"LEVEL:{proj.level.id}")
    assert line.quantity == D(12)
    with pytest.raises(ValidationError):
        svc.allocation.reserve_plan(proj.site.id, "UNIT:999999")


def test_picking_list_walking_order_and_shortage(svc, proj):
    # Remaining 14: default bin B04 first (10), then B05 (4); listed in walking order (by bin code)
    pick = svc.allocation.pick_list(proj.site.id, source="REMAINING")
    assert [(r.bin_code, r.quantity) for r in pick.rows] == [("B04", D(10)), ("B05", D(4))]
    assert pick.short == []
    # reserved: nothing reserved yet -> empty
    assert svc.allocation.pick_list(proj.site.id, source="RESERVED").rows == []
    # another project holds 10: only 6 can be picked, 8 short
    other = svc.sites.save(None, {"project_number": "P-2002", "name": "Other"})
    svc.reservations.reserve({"product_id": proj.mdf.id, "site_id": other.id, "quantity": "10"})
    pick = svc.allocation.pick_list(proj.site.id, source="REMAINING")
    assert sum(r.quantity for r in pick.rows) == D(6)
    assert [(x.code, x.wanted, x.missing) for x in pick.short] == [("MDF-18", D(14), D(8))]
    rep = svc.allocation.pick_report(pick)
    assert rep.rows[-1][0] == "SHORT" and "walking order" in rep.filters_text


def test_pick_list_pdf_and_goods_out_from_it(svc, proj, tmp_path):
    from wms.exporters.export import export_pdf
    svc.allocation.reserve_remaining(proj.site.id, [(proj.mdf.id, D(12))])
    pick = svc.allocation.pick_list(proj.site.id, f"LEVEL:{proj.level.id}", "RESERVED")
    assert sum(r.quantity for r in pick.rows) == D(12)
    out = export_pdf(svc.allocation.pick_report(pick), tmp_path / "pick.pdf")
    assert out.stat().st_size > 1000
    # the same lines post as Goods Out and deliver the reservation
    svc.goods_out.post({"site_id": proj.site.id, "issue_date": TODAY},
                       [IssueLine(r.product_id, r.quantity, r.bin_id) for r in pick.rows])
    r = mdf_row(svc, proj)
    assert (r.net, r.proj_reserved, r.remaining, r.to_allocate) == (D(12), 0, D(2), D(2))


# --- screens ----------------------------------------------------------------------------------------

@pytest.fixture
def win(qtbot, svc):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    w = MainWindow(App(svc.ctx))
    qtbot.addWidget(w)
    return w


def test_reserve_remaining_dialog(svc, proj, win, monkeypatch):
    import wms.ui.dialogs.allocation as dlgmod
    monkeypatch.setattr(dlgmod, "info", lambda *a, **k: None)
    dlg = dlgmod.ReserveRemainingDialog(win.app, proj.site.id, win)
    assert dlg.table.model_.rowCount() == 1 and dlg.ok.isEnabled()
    dlg.area.setCurrentIndex(dlg.area.findData(f"UNIT:{proj.desk.id}"))
    assert dlg.plan[0].quantity == D(2)
    dlg.save()
    assert len(dlg.numbers) == 1
    assert mdf_row(svc, proj).proj_reserved == D(2)


def test_site_dialog_buttons_and_goods_out_load_from_project(svc, proj, win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QPushButton

    import wms.ui.dialogs.allocation as dlgmod
    from wms.ui.dialogs.site_dialog import SiteDialog
    sd = SiteDialog(win.app, proj.site.id)
    texts = [b.text() for b in sd.findChildren(QPushButton)]
    assert "Reserve Remaining..." in texts and "Picking List..." in texts
    # the picking list dialog saves a PDF
    pl = dlgmod.PickListDialog(win.app, proj.site.id, win)
    pl.source.setCurrentIndex(pl.source.findData("REMAINING"))
    assert pl.table.model_.rowCount() == 2
    assert pl.save_pdf(str(tmp_path / "pick.pdf")).stat().st_size > 1000
    # Goods Out: Load from project fills one line per pick, with its bin
    win.go("goods_out")
    page = win.page("goods_out")
    page.site.set_value(proj.site.id)

    def fake_exec(self):
        self.source.setCurrentIndex(self.source.findData("REMAINING"))
        self.accept_lines()
        return 1
    monkeypatch.setattr(dlgmod.PickListDialog, "exec", fake_exec)
    assert page.load_from_project()
    vals = page.lines.values()
    assert [(v.product_id, v.quantity, v.bin_id) for v in vals] == \
        [(proj.mdf.id, D(10), proj.bin1.id), (proj.mdf.id, D(4), proj.bin2.id)]


def test_materials_project_status_filter_works(svc, proj):
    """It used to fail (a UNION of three selects built the wrong way)."""
    from wms.services.materials import MaterialFilter
    active = [r.id for r in svc.materials.page(MaterialFilter(project_status="OPEN"))]
    closed = [r.id for r in svc.materials.page(MaterialFilter(project_status="CLOSED"))]
    assert proj.mdf.id in active and proj.mdf.id not in closed
