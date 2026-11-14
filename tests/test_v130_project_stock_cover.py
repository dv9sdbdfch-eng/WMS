"""v1.30 stages 7 and 8: a unit draws on its own project's stock (material picker shows it first),
and blocks / levels / units are coloured green / yellow / red by how far that stock covers them."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from test_ui_smoke import _errors, ui  # noqa: F401 - the shared screen fixture

from wms.services.goods_in import ReceiptLine
from wms.services.unit_norms import GREEN, NO_NEED, RED, YELLOW

D = Decimal


@pytest.fixture
def site(svc, sample):
    """Tribeca: Block A -> Level 1 (Kitchen K1 x 2) and Level 2 (Kitchen K2 x 2), each kitchen
    3 sheets of H3311 + 2 m of edging; plus a Desk directly in the project (1 sheet)."""
    units = {u.code: u for u in svc.reference.units()}
    board = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "H3311", "name": "H3311 Board",
                                     "unit_id": units["SHEET"].id})
    t = svc.project_tree
    p = t.save_project(None, {"project_number": "TRI", "name": "Tribeca", "status": "ACTIVE"})
    block = t.save_block(None, {"name": "Block A"}, site_id=p.id)
    l1 = t.save_level(None, {"name": "Level 1"}, block_id=block.id)
    l2 = t.save_level(None, {"name": "Level 2"}, block_id=block.id)
    k1 = t.save_unit(None, {"name": "Kitchen", "unit_code": "K1", "quantity": "2"}, level_id=l1.id)
    k2 = t.save_unit(None, {"name": "Kitchen", "unit_code": "K2", "quantity": "2"}, level_id=l2.id)
    desk = t.save_unit(None, {"name": "Desk", "quantity": "1"}, site_id=p.id)
    for u in (k1, k2):
        t.add_materials(u.id, [(board.id, "3"), (sample.edge.id, "2")])
    t.add_materials(desk.id, [(board.id, "1")])
    return type("S", (), dict(project=p, block=block, l1=l1, l2=l2, k1=k1, k2=k2, desk=desk, board=board,
                              sample=sample))


def _deliver(svc, s, items):
    svc.goods_in.post({"dn_number": f"DN-{len(items)}-{items[0][1]}", "supplier_id": s.sample.supplier.id,
                       "delivery_date": date.today(), "site_id": s.project.id},
                      [ReceiptLine(pid, D(q), s.sample.bin1.id) for pid, q in items])


def _status(cov, s):
    return (cov.units[s.k1.id].status, cov.units[s.k2.id].status, cov.units[s.desk.id].status,
            cov.levels[s.l1.id].status, cov.levels[s.l2.id].status, cov.blocks[s.block.id].status,
            cov.project.status)


def test_nothing_in_stock_is_red(svc, site):
    assert _status(svc.unit_norms.project_coverage(site.project.id), site) == (RED,) * 7


def test_stock_goes_to_units_in_tree_order(svc, site):
    _deliver(svc, site, [(site.board.id, "6"), (site.sample.edge.id, "4")])  # enough for Level 1 only
    cov = svc.unit_norms.project_coverage(site.project.id)
    assert _status(cov, site) == (GREEN, RED, RED, GREEN, RED, YELLOW, YELLOW)
    assert cov.levels[site.l2.id].short == {"H3311": D(6), "EDGE-W22": D(4)}
    assert "0 of 2" in cov.levels[site.l2.id].text
    _deliver(svc, site, [(site.board.id, "7")])  # boards for everything, edging still short on Level 2
    cov = svc.unit_norms.project_coverage(site.project.id)
    assert _status(cov, site) == (GREEN, YELLOW, GREEN, GREEN, YELLOW, YELLOW, YELLOW)
    _deliver(svc, site, [(site.sample.edge.id, "4")])
    assert _status(svc.unit_norms.project_coverage(site.project.id), site) == (GREEN,) * 7


def test_units_made_need_nothing_more(svc, site):
    svc.unit_norms.record_completion(site.k1.id, "2")  # Level 1 finished
    _deliver(svc, site, [(site.board.id, "7"), (site.sample.edge.id, "4")])
    cov = svc.unit_norms.project_coverage(site.project.id)
    assert cov.units[site.k1.id].status == NO_NEED and cov.levels[site.l1.id].status == NO_NEED
    assert _status(cov, site)[1:] == (GREEN, GREEN, NO_NEED, GREEN, GREEN, GREEN)


def test_picker_lists_project_stock_first(svc, site):
    _deliver(svc, site, [(site.sample.edge.id, "4")])
    t = svc.project_tree
    assert t.project_stock_map(site.project.id) == {site.sample.edge.id: D(4)}
    rows = t.find_products("", site_id=site.project.id)
    assert rows[0].id == site.sample.edge.id and len(rows) > 1
    assert [p.id for p in t.find_products("", site_id=site.project.id, project_stock_only=True)] == [
        site.sample.edge.id]
    assert t.find_products("zzz-nothing", site_id=site.project.id, project_stock_only=True) == []


def test_tree_colours_and_picker_on_screen(ui, site, monkeypatch):  # noqa: F811
    from wms.ui.dialogs.project_tree import MaterialPickerDialog
    from wms.ui.pages.projects import C_INFO, COVER_COLOURS
    app, win, msgs, _ = ui
    _deliver(app.services, site, [(site.board.id, "6"), (site.sample.edge.id, "4")])
    win.go("projects")
    page = win.page("projects")
    page.refresh()
    page.items["N:"].setExpanded(True)
    page.items[f"P:{site.project.id}"].setExpanded(True)
    page.expand_all()

    def colour(key):
        return page.items[key].background(C_INFO).color()

    assert colour(f"V:{site.l1.id}") == COVER_COLOURS[GREEN]
    assert colour(f"V:{site.l2.id}") == COVER_COLOURS[RED]
    assert colour(f"B:{site.block.id}") == COVER_COLOURS[YELLOW]
    assert "Short: EDGE-W22 4, H3311 6" in page.items[f"V:{site.l2.id}"].toolTip(C_INFO)
    # more stock arrives elsewhere; a change on the screen repaints every row
    _deliver(app.services, site, [(site.board.id, "7"), (site.sample.edge.id, "4")])
    page._unit_changed(site.desk.id)
    assert colour(f"B:{site.block.id}") == COVER_COLOURS[GREEN]
    # the material picker of a unit shows its project's stock, those materials first
    dlg = MaterialPickerDialog(app, "Add", page, site_id=site.project.id)
    first = dlg.table.items()[0]
    assert first.id in (site.board.id, site.sample.edge.id) and dlg.held[site.board.id] == D(13)
    dlg.only_held.setChecked(True)
    assert {p.id for p in dlg.table.items()} == {site.board.id, site.sample.edge.id}
    assert "in this project's stock" in dlg.count.text()
    assert not _errors(msgs), msgs
