"""How fast the main operations feel from another building (network delay to the server).

    python -m tools.latency_check postgresql+psycopg://user:pass@server/storewms --delay-ms 20

Runs a small local relay that adds the given delay to every packet in each direction (20 ms each
way = 40 ms round trip, a typical VPN between UK sites), fills a fresh test database through it
and times the operations people do all day. Use a test database: it must be empty.
Prints the time and the number of database round trips of each operation.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from contextlib import contextmanager
from datetime import date
from decimal import Decimal


def _relay(listen_port: int, host: str, port: int, delay: float) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen_port))
    srv.listen(50)

    def pipe(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                time.sleep(delay)
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def serve():
        while True:
            c, _ = srv.accept()
            u = socket.create_connection((host, port))
            for x, y in ((c, u), (u, c)):
                threading.Thread(target=pipe, args=(x, y), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--delay-ms", type=float, default=20.0)
    ap.add_argument("--port", type=int, default=55432)
    args = ap.parse_args()
    from sqlalchemy import event
    from sqlalchemy.engine import make_url

    from wms.config import AppConfig
    from wms.db.engine import Database
    from wms.db.seed import seed_if_empty
    from wms.services import Services
    from wms.services.base import AppContext
    from wms.services.goods_in import ReceiptLine
    from wms.services.goods_out import IssueLine
    from wms.services.materials import MaterialFilter

    u = make_url(args.url)
    _relay(args.port, u.host or "127.0.0.1", u.port or 5432, args.delay_ms / 1000)
    url = u.set(host="127.0.0.1", port=args.port).render_as_string(hide_password=False)
    cfg = AppConfig.load()
    cfg.database_url = url
    db = Database(url)
    counter = {"n": 0}
    event.listen(db.engine, "before_cursor_execute", lambda *a: counter.__setitem__("n", counter["n"] + 1))

    @contextmanager
    def timed(name):
        counter["n"] = 0
        t = time.perf_counter()
        yield
        print(f"{name:<38} {1000 * (time.perf_counter() - t):7.0f} ms  {counter['n']:4d} round trips")

    with timed("open database / upgrade check"):
        db.migrate()
        with db.session() as s:
            seed_if_empty(s)
    svc = Services(AppContext(db=db, config=cfg))
    with timed("sign in"):
        svc.auth.login("admin", "admin")
    units = {x.code: x for x in svc.reference.units()}
    main_loc = next(x for x in svc.locations.tree() if x.code == "MAIN")
    b = svc.locations.save_bin(None, {"location_id": main_loc.id, "code": "LAT1"})
    sup = svc.reference.save_supplier(None, {"code": "LAT", "name": "Latency Supplier"})
    site = svc.sites.save(None, {"project_number": "LAT-1", "name": "Latency", "status": "ACTIVE"})
    products = [svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": f"LAT-{i:03d}",
                                         "name": f"Board {i}", "unit_id": units["SHEET"].id, "default_bin_id": b.id})
                for i in range(60)]
    with timed("Goods In, 10 lines"):
        svc.goods_in.post({"dn_number": "LAT-DN", "supplier_id": sup.id, "delivery_date": date.today()},
                          [ReceiptLine(p.id, Decimal(50), b.id, None) for p in products[:10]])
    with timed("Goods Out, 5 lines"):
        svc.goods_out.post({"site_id": site.id, "issue_date": date.today()},
                           [IssueLine(p.id, Decimal(2), b.id) for p in products[:5]])
    with timed("Materials list, first page"):
        svc.materials.page(MaterialFilter())
    with timed("Materials list, project view"):
        svc.materials.page(MaterialFilter(site_id=site.id))
    with timed("Dashboard"):
        svc.dashboard.load()
    with timed("Find product by code"):
        svc.search.exact_product("LAT-007")
    db.dispose()


if __name__ == "__main__":
    main()
