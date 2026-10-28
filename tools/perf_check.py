"""Time the operations a warehouse user waits for, against a (generated) data folder.

    python -m tools.perf_check <data-folder> [--ui]

Prints one line per operation with the time in milliseconds (best of 3).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from wms.config import AppConfig
from wms.db.engine import Database
from wms.services import Services
from wms.services.base import AppContext
from wms.services.reports import REPORTS, ReportFilters


def timed(label: str, fn, repeat: int = 3, results: dict | None = None):
    best, out = None, None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        ms = (time.perf_counter() - t) * 1000
        best = ms if best is None else min(best, ms)
    size = len(out) if hasattr(out, "__len__") else ""
    print(f"{best:9.0f} ms  {label} {f'({size} rows)' if size != '' else ''}")
    if results is not None:
        results[label] = round(best)
    return out


def run(data_dir: Path, ui: bool = False) -> dict:
    res: dict = {}
    cfg = AppConfig(data_dir=Path(data_dir))
    t = time.perf_counter()
    db = Database(cfg.resolved_database_url)
    db.migrate()
    res["open + migrate database"] = round((time.perf_counter() - t) * 1000)
    print(f"{res['open + migrate database']:9.0f} ms  open + migrate database")
    svc = Services(AppContext(db=db, config=cfg))
    svc.auth.login("admin", "admin")
    timed("product list (all active)", lambda: svc.products.list(), results=res)
    timed("product search 'oak'", lambda: svc.products.list(text="oak"), results=res)
    timed("product search exact code", lambda: svc.products.list(text="PRD-01234"), results=res)
    timed("barcode lookup", lambda: [svc.products.find_by_code("5000000001234")], results=res)
    timed("global search 'DN1012'", lambda: svc.search.search("DN1012"), results=res)
    timed("global search 'walnut'", lambda: svc.search.search("walnut"), results=res)
    timed("delivery notes list", lambda: svc.goods_in.search(), results=res)
    timed("delivery notes by supplier + dates", lambda: svc.goods_in.search(
        supplier_id=1, date_from=date.today() - timedelta(days=90), date_to=date.today()), results=res)
    timed("goods out list", lambda: svc.goods_out.search(), results=res)
    timed("movement history (first 5000)", lambda: svc.stock.movements(), results=res)
    timed("movement history one product", lambda: svc.stock.movements(product_id=1234), results=res)
    timed("where is product (stock by bin)", lambda: svc.products.stock_by_bin(1234), results=res)
    timed("site material summary", lambda: svc.sites.material_summary(1), results=res)
    timed("dashboard", lambda: [svc.dashboard.load()], results=res)
    timed("low stock", lambda: svc.products.low_stock(), results=res)
    for r in REPORTS:
        timed(f"report: {r.title}", lambda r=r: svc.reports.run(r.key, ReportFilters()).rows, repeat=1, results=res)
    timed("integrity check", lambda: [svc.backup.integrity_check()], repeat=1, results=res)
    if ui:
        from PySide6.QtWidgets import QApplication

        from wms.ui.app import App
        from wms.ui.widgets.lines import LinesEditor
        QApplication.instance() or QApplication([])
        app = App(svc.ctx)
        editor = timed("UI: open lines editor", lambda: LinesEditor(app, show_bin=True), repeat=1, results=res)
        timed("UI: add one line", lambda: [editor.add_row()], repeat=3, results=res)
    db.dispose()
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--ui", action="store_true")
    a = ap.parse_args(argv)
    run(a.data_dir, a.ui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
