"""Fill a NEW database with realistic volume data for performance testing.

    python -m tools.generate_test_data <data-folder> [--products 10000] [--deliveries 5000] ...

Never point this at the live data folder: it refuses to run on a database
that already contains products. Rows are written with bulk inserts, but
balances, ledger and documents are kept consistent so the integrity check
passes afterwards (that is part of the test).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, insert, select, update

from wms.config import AppConfig
from wms.db.engine import Database
from wms.db.models import (
    AuditLog,
    Bin,
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    GoodsIssueLine,
    Location,
    NumberSequence,
    Product,
    Site,
    StockLevel,
    StockMovement,
    StockTransfer,
    Supplier,
    Unit,
    User,
)
from wms.db.seed import seed_if_empty

WORDS = ["MDF", "Oak", "Birch Ply", "Melamine", "Walnut", "Hinge", "Runner", "Handle", "Screw", "Dowel",
         "Edging", "Worktop", "Glue", "Lacquer", "Cam", "Bracket", "Leg", "Panel", "Drawer", "Shelf"]


def generate(data_dir: Path, products: int = 10_000, deliveries: int = 5_000, issues: int = 4_000,
             transfers: int = 10_500, sites: int = 300, suppliers: int = 50, lines_per_doc: int = 10,
             seed: int = 42) -> dict:
    rnd = random.Random(seed)
    cfg = AppConfig(data_dir=Path(data_dir))
    cfg.ensure_dirs()
    db = Database(cfg.resolved_database_url)
    db.migrate()
    with db.session() as s:
        seed_if_empty(s)
    t0 = time.perf_counter()
    with db.engine.begin() as c:
        if c.scalar(select(func.count(Product.id))):
            raise SystemExit("This database already has products - use an empty data folder.")
        admin = c.scalar(select(User.id).limit(1))
        main = c.scalar(select(Location.id).where(Location.code == "MAIN"))
        units = {r.code: r.id for r in c.execute(select(Unit.code, Unit.id))}
        now = datetime.now(UTC)

        # Locations: 4 zones x 10 racks x 5 shelves, 2 bins per shelf = 400 bins.
        bins: list[int] = []
        for z in range(1, 5):
            zid = c.execute(insert(Location).values(parent_id=main, kind="ZONE", code=f"Z{z}", name=f"Zone {z}",
                                                    path=f"MAIN / Z{z}", created_at=now, updated_at=now,
                                                    is_active=True)).inserted_primary_key[0]
            for r in range(1, 11):
                rp = f"MAIN / Z{z} / R{r:02d}"
                rid = c.execute(insert(Location).values(parent_id=zid, kind="RACK", code=f"R{r:02d}", name=rp,
                                                        path=rp, created_at=now, updated_at=now,
                                                        is_active=True)).inserted_primary_key[0]
                for sh in range(1, 6):
                    sp = f"{rp} / S{sh}"
                    sid = c.execute(insert(Location).values(parent_id=rid, kind="SHELF", code=f"S{sh}", name=sp,
                                                            path=sp, created_at=now, updated_at=now,
                                                            is_active=True)).inserted_primary_key[0]
                    for b in (1, 2):
                        bins.append(c.execute(insert(Bin).values(
                            location_id=sid, code=f"B{b}", full_code=f"{sp} / B{b}", is_available=True,
                            created_at=now, updated_at=now, is_active=True)).inserted_primary_key[0])

        c.execute(insert(Supplier), [dict(code=f"SUP{i:03d}", name=f"Supplier {i} Ltd", created_at=now,
                                          updated_at=now, is_active=True) for i in range(1, suppliers + 1)])
        sup_ids = list(c.scalars(select(Supplier.id)))
        c.execute(insert(Site), [dict(project_number=f"P-{2000 + i}", name=f"Project {i} {rnd.choice(WORDS)} House",
                                      customer=f"Customer {i % 40}", status="ACTIVE", created_at=now, updated_at=now,
                                      version=1) for i in range(sites)])
        site_ids = list(c.scalars(select(Site.id)))

        prows = []
        for i in range(products):
            whole = i % 3 != 0
            prows.append(dict(item_type="RAW_MATERIAL" if i % 20 else "FINISHED", internal_code=f"PRD-{i:05d}",
                              supplier_code=f"SC{i:06d}", barcode=f"50{i:011d}",
                              name=f"{rnd.choice(WORDS)} {rnd.choice(WORDS)} {i}",
                              description=f"Test product {i} for volume testing",
                              unit_id=units["EA" if whole else "M"], supplier_id=rnd.choice(sup_ids),
                              default_bin_id=rnd.choice(bins), min_stock=Decimal(rnd.choice([0, 5, 10, 20])),
                              unit_cost=Decimal(rnd.randint(10, 50000)) / 100, created_by_id=admin,
                              created_at=now, updated_at=now, is_active=True, version=1))
        c.execute(insert(Product), prows)
        prod = [(r.id, r.default_bin_id) for r in c.execute(select(Product.id, Product.default_bin_id))]

        levels: dict[tuple[int, int], Decimal] = {}
        movements: list[dict] = []
        audit: list[dict] = []
        start = date.today() - timedelta(days=730)

        def when(day: date) -> datetime:
            return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)

        # Goods In
        dn_lines = []
        for n in range(deliveries):
            day = start + timedelta(days=n * 730 // max(deliveries, 1))
            sup = rnd.choice(sup_ids)
            dn_id = c.execute(insert(DeliveryNote).values(
                dn_number=f"DN{100000 + n}", dn_number_norm=f"DN{100000 + n}", supplier_id=sup, delivery_date=day,
                received_by_id=admin, status="POSTED", created_at=when(day))).inserted_primary_key[0]
            for ln in range(1, lines_per_doc + 1):
                pid, b = rnd.choice(prod)
                qty = Decimal(rnd.randint(5, 200))
                levels[(pid, b)] = levels.get((pid, b), Decimal(0)) + qty
                movements.append(dict(movement_type="GOODS_IN", product_id=pid, quantity=qty, to_bin_id=b,
                                      supplier_id=sup, doc_type="DELIVERY_NOTE", doc_id=dn_id,
                                      doc_number=f"DN{100000 + n}", user_id=admin, occurred_at=when(day),
                                      recorded_at=when(day), is_override=False))
                dn_lines.append(dict(delivery_note_id=dn_id, line_no=ln, product_id=pid, quantity=qty, bin_id=b))
            audit.append(dict(occurred_at=when(day), user_id=admin, username="admin", action="GOODS_IN_POSTED",
                              entity_type="DELIVERY_NOTE", entity_id=dn_id, entity_label=f"DN{100000 + n}"))
        # Goods Out (only from stock that exists)
        gi_lines = []
        stocked = list(levels)
        for n in range(issues):
            day = start + timedelta(days=30 + n * 700 // max(issues, 1))
            site = rnd.choice(site_ids)
            gi_id = c.execute(insert(GoodsIssue).values(
                issue_number=f"GO-{n + 1:06d}", issue_date=day, site_id=site, issued_by="Admin", user_id=admin,
                reference=f"JOB{n % 900}", status="POSTED", created_at=when(day))).inserted_primary_key[0]
            for ln in range(1, lines_per_doc + 1):
                pid, b = rnd.choice(stocked)
                have = levels[(pid, b)]
                if have < 1:
                    continue
                qty = Decimal(rnd.randint(1, max(1, int(have) // 3)))
                levels[(pid, b)] = have - qty
                movements.append(dict(movement_type="GOODS_OUT", product_id=pid, quantity=qty, from_bin_id=b,
                                      site_id=site, doc_type="GOODS_ISSUE", doc_id=gi_id, doc_number=f"GO-{n + 1:06d}",
                                      reference=f"JOB{n % 900}", user_id=admin, occurred_at=when(day),
                                      recorded_at=when(day), is_override=False))
                gi_lines.append(dict(issue_id=gi_id, line_no=ln, product_id=pid, quantity=qty, bin_id=b))
        # Transfers
        tr_rows = []
        for n in range(transfers):
            pid, b = rnd.choice(stocked)
            have = levels[(pid, b)]
            if have < 1:
                continue
            dest = rnd.choice(bins)
            if dest == b:
                continue
            qty = Decimal(rnd.randint(1, int(have)))
            levels[(pid, b)] = have - qty
            levels[(pid, dest)] = levels.get((pid, dest), Decimal(0)) + qty
            day = start + timedelta(days=60 + n * 660 // max(transfers, 1))
            movements.append(dict(movement_type="TRANSFER", product_id=pid, quantity=qty, from_bin_id=b,
                                  to_bin_id=dest, doc_type="TRANSFER", doc_number=f"TRF-{n + 1:06d}", user_id=admin,
                                  occurred_at=when(day), recorded_at=when(day), is_override=False))
            tr_rows.append(dict(transfer_number=f"TRF-{n + 1:06d}", product_id=pid, from_bin_id=b, to_bin_id=dest,
                                quantity=qty, user_id=admin, created_at=when(day)))

        movements.sort(key=lambda m: m["occurred_at"])
        keys = ("from_bin_id", "to_bin_id", "site_id", "supplier_id", "doc_id", "reference")
        movements = [{k: None for k in keys} | m for m in movements]
        c.execute(insert(StockMovement), movements)
        c.execute(insert(DeliveryNoteLine), dn_lines)
        c.execute(insert(GoodsIssueLine), gi_lines)
        c.execute(insert(StockTransfer), tr_rows)
        c.execute(insert(StockLevel), [dict(product_id=p, bin_id=b, quantity=q, updated_at=now, version=1)
                                       for (p, b), q in levels.items()])
        c.execute(insert(AuditLog), audit)
        for name, used in (("GOODS_ISSUE", issues), ("TRANSFER", transfers)):
            c.execute(update(NumberSequence).where(NumberSequence.name == name).values(next_value=used + 1))
    db.dispose()
    return {"products": products, "bins": len(bins), "sites": sites, "delivery_notes": deliveries,
            "goods_out": issues, "movements": len(movements), "stock_levels": len(levels),
            "seconds": round(time.perf_counter() - t0, 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--products", type=int, default=10_000)
    ap.add_argument("--deliveries", type=int, default=5_000)
    ap.add_argument("--issues", type=int, default=4_000)
    ap.add_argument("--transfers", type=int, default=10_500)
    ap.add_argument("--sites", type=int, default=300)
    a = ap.parse_args(argv)
    print(generate(a.data_dir, a.products, a.deliveries, a.issues, a.transfers, a.sites))
    return 0


if __name__ == "__main__":
    sys.exit(main())
