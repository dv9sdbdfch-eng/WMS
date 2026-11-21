"""v1.21: Remaining / To Allocate (v1.35: Not covered), picking list and Load from project."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import hand_reserve

from wms.core.exceptions import ValidationError
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
    hand_reserve(svc, {"product_id": proj.mdf.id, "site_id": proj.site.id, "quantity": "5"})
    r = mdf_row(svc, proj)
    assert (r.required, r.proj_reserved, r.net, r.remaining, r.to_allocate) == (D(14), D(5), 0, D(14), D(9))


def test_reserving_by_hand_is_gone(svc):
    """v1.35 (Ihor 21.11.2026): no reservations - only project stock (Goods In for a project) and issue."""
    for name in ("reserve", "update", "release"):
        assert not hasattr(svc.reservations, name)
    for name in ("reserve_plan", "reserve_remaining"):
        assert not hasattr(svc.allocation, name)


def test_area_caps_the_quantity(svc, proj):
    pick = svc.allocation.pick_list(proj.site.id, f"UNIT:{proj.desk.id}", "REMAINING")
    assert sum(r.quantity for r in pick.rows) == D(2)
    pick = svc.allocation.pick_list(proj.site.id, f"LEVEL:{proj.level.id}", "REMAINING")
    assert sum(r.quantity for r in pick.rows) == D(12)
    with pytest.raises(ValidationError):
        svc.allocation.pick_list(proj.site.id, "UNIT:999999")


def test_picking_list_walking_order_and_shortage(svc, proj):
    # Remaining 14: default bin B04 first (10), then B05 (4); listed in walking order (by bin code)
    pick = svc.allocation.pick_list(proj.site.id, source="REMAINING")
    assert [(r.bin_code, r.quantity) for r in pick.rows] == [("B04", D(10)), ("B05", D(4))]
    assert pick.short == []
    # reserved: nothing reserved yet -> empty
    assert svc.allocation.pick_list(proj.site.id, source="RESERVED").rows == []
    # another project holds 10: only 6 can be picked, 8 short
    other = svc.sites.save(None, {"project_number": "P-2002", "name": "Other"})
    hand_reserve(svc, {"product_id": proj.mdf.id, "site_id": other.id, "quantity": "10"})
    pick = svc.allocation.pick_list(proj.site.id, source="REMAINING")
    assert sum(r.quantity for r in pick.rows) == D(6)
    assert [(x.code, x.wanted, x.missing) for x in pick.short] == [("MDF-18", D(14), D(8))]
    rep = svc.allocation.pick_report(pick)
    assert rep.rows[-1][0] == "SHORT" and "walking order" in rep.filters_text


def test_pick_list_pdf_and_goods_out_from_it(svc, proj, tmp_path):
    from wms.exporters.export import export_pdf
    hand_reserve(svc, {"product_id": proj.mdf.id, "site_id": proj.site.id, "quantity": "12"})
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


def test_site_dialog_buttons_and_goods_out_load_from_project(svc, proj, win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QPushButton

    import wms.ui.dialogs.allocation as dlgmod
    from wms.ui.dialogs.site_dialog import SiteDialog
    sd = SiteDialog(win.app, proj.site.id)
    texts = [b.text() for b in sd.findChildren(QPushButton)]
    assert "Picking List..." in texts and not [x for x in texts if x.startswith("Reserve")]
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
