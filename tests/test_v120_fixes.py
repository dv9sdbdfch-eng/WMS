"""v1.20.0 (Ihor, 09.11.2026): period filter on document lists instead of a hidden row limit,
and fast product pick lists."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from wms.services.goods_out import IssueLine
from wms.ui.pages.base import period_start


def test_period_start_dates():
    today = date(2026, 3, 31)
    assert period_start("WEEK", today) == date(2026, 3, 24)
    assert period_start("MONTH", today) == date(2026, 2, 28)  # no 31st of February
    assert period_start("QUARTER", today) == date(2025, 12, 31)
    assert period_start("YEAR", today) == date(2025, 3, 31)
    assert period_start("ALL", today) is None


def test_product_options_are_names_only_and_respect_filters(svc, sample):
    opts = svc.products.options()
    ids = {pid for _, pid in opts}
    assert sample.mdf.id in ids
    label = next(text for text, pid in opts if pid == sample.mdf.id)
    assert label.startswith(sample.mdf.internal_code + " - ")
    assert svc.products.options(item_types=["FINISHED"]) == [
        (t, i) for t, i in opts if svc.products.get(i).item_type == "FINISHED"]


@pytest.fixture
def win(qtbot, svc):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    w = MainWindow(App(svc.ctx))
    qtbot.addWidget(w)
    return w


def test_goods_out_history_has_no_hidden_limit_and_filters_by_period(svc, sample, win):
    sample.receive(sample.mdf, 2000)
    old = date.today() - timedelta(days=200)
    for i in range(1005):  # more than the old hidden 1 000 limit
        svc.goods_out.post({"issue_date": old if i < 5 else date.today(), "site_id": sample.site.id},
                           [IssueLine(sample.mdf.id, "1")])
    win.go("goods_out")
    hist = win.page("goods_out").history
    assert hist.period.currentData() == "MONTH"
    hist.period.setCurrentIndex(hist.period.findData("ALL"))
    assert hist.table.model_.rowCount() == 1005
    hist.period.setCurrentIndex(hist.period.findData("MONTH"))
    assert hist.table.model_.rowCount() == 1000
    assert hist.date_from.value() == period_start("MONTH")
    hist.period.setCurrentIndex(hist.period.findData("YEAR"))
    assert hist.table.model_.rowCount() == 1005
    assert svc.goods_out.available_for_issue(sample.mdf.id) == D("995")


def test_purchase_orders_and_problems_open_on_all_periods(win):
    for key in ("purchase_orders", "discrepancies"):
        win.go(key)
        assert win.page(key).period.currentData() == "ALL"
