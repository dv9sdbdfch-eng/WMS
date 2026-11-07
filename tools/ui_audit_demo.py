"""Demo warehouse data (Egger/Blum/Hafele/Latham, 4 projects, POs, issues, returns, stocktake, archive)."""
from datetime import date, timedelta
from decimal import Decimal as D

from wms.config import AppConfig
from wms.db.engine import Database
from wms.db.seed import seed_if_empty
from wms.services import Services
from wms.services.base import AppContext
from wms.services.goods_in import ReceiptLine
from wms.services.goods_out import IssueLine
from wms.services.production import MaterialInput
from wms.services.purchasing import POLineInput
from wms.services.stock_ops import ReturnLineInput
from wms.services.stocktake import CountEntry


def build():
    T = date.today()
    cfg = AppConfig.load()
    cfg.ensure_dirs()
    db = Database(cfg.resolved_database_url)
    db.migrate()
    with db.session() as s:
        seed_if_empty(s)
    ctx = AppContext(db=db, config=cfg)
    svc = Services(ctx)
    svc.auth.login("admin", "admin")

    units = {u.code: u for u in svc.reference.units()}
    main = next(l for l in svc.locations.tree() if l.code == "MAIN")
    bins = {}
    for z in ("A", "B"):
        zone = svc.locations.save_location(None, {"kind": "ZONE", "code": z, "name": f"Zone {z}", "parent_id": main.id})
        for r in ("R01", "R02"):
            rack = svc.locations.save_location(None, {"kind": "RACK", "code": r, "parent_id": zone.id})
            for b in ("B01", "B02", "B03"):
                bins[f"{z}-{r}-{b}"] = svc.locations.save_bin(None, {"location_id": rack.id, "code": f"{z}{r[1:]}{b}"})
    bl = list(bins.values())

    sup = {c: svc.reference.save_supplier(None, {"code": c, "name": n}) for c, n in [
        ("EGGER", "Egger UK Ltd"), ("BLUM", "Blum UK Ltd"), ("HAFELE", "Häfele UK Ltd"), ("JJS", "James Latham plc")]}

    M = [  # code, name, unit, supplier, cost, min
        ("H3311", "Egger Halifax Oak H3311 18mm 2800x2070", "SHEET", "EGGER", "41.20", "10"),
        ("W1000", "Egger Premium White W1000 18mm", "SHEET", "EGGER", "32.80", "15"),
        ("U999", "Egger Black U999 18mm", "SHEET", "EGGER", "36.40", "6"),
        ("MDF18", "MDF 18mm 2440x1220", "SHEET", "JJS", "24.50", "20"),
        ("MDF6", "MDF 6mm 2440x1220", "SHEET", "JJS", "11.90", "10"),
        ("PLY18", "Birch Ply 18mm 2440x1220", "SHEET", "JJS", "58.00", "8"),
        ("OAK27", "Solid Oak Worktop 27mm 3000x620", "EA", "JJS", "189.00", "5"),
        ("EDGE-H3311", "ABS edging Halifax Oak 22x1mm", "M", "EGGER", "0.42", "200"),
        ("EDGE-W22", "ABS edging white 22x1mm", "M", "EGGER", "0.18", "300"),
        ("BLUM-CLIP", "Blum Clip Top hinge 110° soft close", "EA", "BLUM", "3.85", "100"),
        ("BLUM-TBX", "Blum Tandembox drawer 500mm", "SET", "BLUM", "48.60", "20"),
        ("BLUM-LEG", "Blum cabinet leg 150mm", "EA", "BLUM", "0.95", "200"),
        ("HAF-HDL128", "Häfele bar handle 128mm brushed steel", "EA", "HAFELE", "2.40", "100"),
        ("HAF-SCR4", "Häfele chipboard screw 4x30 (box 500)", "BOX", "HAFELE", "6.20", "30"),
        ("DOWEL8", "Beech dowel 8x35 (bag 1000)", "BAG", "HAFELE", "9.80", "15"),
        ("GLUE-PVA", "PVA wood glue 5L", "EA", "JJS", "14.50", "12"),
    ]
    P = {}
    for i, (code, name, unit, s_, cost, mn) in enumerate(M):
        u = units.get(unit) or units["EA"]
        P[code] = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": code, "name": name,
                                           "unit_id": u.id, "supplier_id": sup[s_].id, "supplier_code": f"{s_[:3]}-{code}",
                                           "default_bin_id": bl[i % len(bl)].id, "min_stock": mn, "unit_cost": cost})
    fin = {}
    for code, name in [("KIT-BASE600", "Kitchen base unit 600 Halifax Oak"), ("KIT-WALL800", "Wall unit 800 white"),
                       ("WARD-1200", "Fitted wardrobe 1200 black")]:
        fin[code] = svc.products.save(None, {"item_type": "FINISHED", "internal_code": code, "name": name,
                                             "unit_id": units["EA"].id, "default_bin_id": bl[-1].id, "status": "READY"})

    sites = {}
    for num, name, cust in [("P-1042", "Tribeca Apartments", "Berkeley Homes"),
                            ("P-1051", "Riverside Kitchens", "Acme Developments"),
                            ("P-1060", "Hampstead Wardrobes", "Private client"),
                            ("P-1066", "Canary Wharf Office Fit-out", "Mace Ltd")]:
        sites[num] = svc.sites.save(None, {"project_number": num, "name": name, "customer": cust, "status": "ACTIVE"})

    # Purchase orders + receipts
    qty_in = {"H3311": 120, "W1000": 90, "U999": 30, "MDF18": 140, "MDF6": 60, "PLY18": 24, "OAK27": 6,
              "EDGE-H3311": 1500, "EDGE-W22": 2000, "BLUM-CLIP": 600, "BLUM-TBX": 80, "BLUM-LEG": 900,
              "HAF-HDL128": 400, "HAF-SCR4": 30, "DOWEL8": 12, "GLUE-PVA": 10}
    n = 1
    for s_code in ("EGGER", "JJS", "BLUM", "HAFELE"):
        items = [c for c, *_r in M if _r[2] == s_code]
        po = svc.purchasing.save(None, {"supplier_id": sup[s_code].id, "order_date": T - timedelta(days=12),
                                        "expected_date": T - timedelta(days=5), "supplier_reference": f"Q-{7700+n}"},
                                 [POLineInput(P[c].id, str(qty_in[c])) for c in items])
        svc.purchasing.mark_ordered(po.id)
        svc.goods_in.post({"purchase_order_id": po.id, "dn_number": f"DN-{1040+n}", "delivery_date": T - timedelta(days=4)},
                          [ReceiptLine(P[c].id, D(str(qty_in[c] if c != "U999" else 26)), P[c].default_bin_id, D(dict((x[0], x[4]) for x in M)[c])) for c in items])
        n += 1
    # open / overdue POs
    po2 = svc.purchasing.save(None, {"supplier_id": sup["EGGER"].id, "order_date": T - timedelta(days=9),
                                     "expected_date": T - timedelta(days=2), "site_id": sites["P-1042"].id},
                              [POLineInput(P["H3311"].id, "60"), POLineInput(P["EDGE-H3311"].id, "800")])
    svc.purchasing.mark_ordered(po2.id)
    po3 = svc.purchasing.save(None, {"supplier_id": sup["BLUM"].id, "order_date": T,
                                     "expected_date": T + timedelta(days=7)},
                              [POLineInput(P["BLUM-TBX"].id, "40"), POLineInput(P["BLUM-CLIP"].id, "300")])
    svc.purchasing.mark_ordered(po3.id)
    svc.purchasing.save(None, {"supplier_id": sup["HAFELE"].id, "order_date": T}, [POLineInput(P["HAF-HDL128"].id, "200")])
    # delivery problem
    try:
        svc.purchasing.record_discrepancy({"supplier_id": sup["EGGER"].id, "product_id": P["U999"].id,
                                           "reason_code": "DAMAGED", "quantity": "2",
                                           "notes": "Two sheets with crushed corners"})
    except Exception as e:
        print("discrepancy:", e)

    # project requirements
    req = {"P-1042": {"H3311": 489, "EDGE-H3311": 900, "BLUM-CLIP": 320, "BLUM-TBX": 64, "HAF-HDL128": 210, "BLUM-LEG": 480},
           "P-1051": {"W1000": 70, "MDF18": 40, "EDGE-W22": 1200, "BLUM-CLIP": 140, "OAK27": 4},
           "P-1060": {"U999": 24, "PLY18": 10, "BLUM-TBX": 12, "HAF-HDL128": 36},
           "P-1066": {"MDF18": 60, "MDF6": 30, "HAF-SCR4": 8}}
    for num, items in req.items():
        svc.materials.set_required(sites[num].id, [(P[c].id, str(q)) for c, q in items.items()])

    for num, c, q in [("P-1042", "H3311", 30), ("P-1042", "BLUM-TBX", 20), ("P-1051", "OAK27", 2), ("P-1060", "U999", 10)]:
        svc.reservations.reserve({"product_id": P[c].id, "site_id": sites[num].id, "quantity": str(q)})

    for num, lines, ref in [("P-1042", [("H3311", 60), ("EDGE-H3311", 450), ("BLUM-CLIP", 160)], "JOB-1042-01"),
                            ("P-1042", [("BLUM-TBX", 12), ("HAF-HDL128", 90)], "JOB-1042-02"),
                            ("P-1051", [("W1000", 36), ("EDGE-W22", 600), ("MDF18", 20)], "JOB-1051-01"),
                            ("P-1066", [("MDF18", 30), ("MDF6", 12), ("HAF-SCR4", 4)], "JOB-1066-01")]:
        svc.goods_out.post({"site_id": sites[num].id, "issue_date": T - timedelta(days=1), "reference": ref},
                           [IssueLine(P[c].id, D(str(q))) for c, q in lines])

    svc.stock.post_return({"return_type": "FROM_SITE", "return_date": T, "site_id": sites["P-1042"].id},
                          [ReturnLineInput(P["H3311"].id, "4", P["H3311"].default_bin_id)])
    svc.stock.transfer(P["MDF18"].id, P["MDF18"].default_bin_id, bl[10].id, "25")

    try:
        svc.production.post({"finished_product_id": fin["KIT-BASE600"].id, "quantity": "6", "production_date": T,
                             "site_id": sites["P-1042"].id},
                            [MaterialInput(P["H3311"].id, "6"), MaterialInput(P["BLUM-CLIP"].id, "12")])
    except Exception as e:
        print("production:", e)

    cnt = svc.stocktake.create(bin_id=P["H3311"].default_bin_id, description="Weekly count Zone A")
    ln = next(l for l in cnt.lines if l.product_id == P["H3311"].id)
    svc.stocktake.record_counts(cnt.id, [CountEntry(ln.id, str(ln.system_qty - 2))])
    svc.stocktake.create(location_id=main.id, description="Month-end full count")
    old = svc.products.save(None, {"item_type": "RAW_MATERIAL", "internal_code": "MEL-BEIGE", "name": "Melamine beige 15mm (discontinued)",
                                  "unit_id": units["SHEET"].id, "supplier_id": sup["EGGER"].id})
    svc.goods_in.post({"dn_number": "DN-0990", "supplier_id": sup["EGGER"].id, "delivery_date": T - timedelta(days=30)},
                      [ReceiptLine(old.id, D("4"), bl[5].id, D("19.90"))])
    svc.goods_out.post({"site_id": sites["P-1066"].id, "issue_date": T - timedelta(days=20), "reference": "JOB-1066-00"},
                       [IssueLine(old.id, D("4"))])
    print("archive:", svc.products.remove(old.id, "Discontinued by supplier"))
    svc.backup.create(note="Weekly manual backup")

    pt = svc.project_tree
    m1 = pt.save_manager(None, {"name": "Olena Kovalenko"})
    pt.assign_manager([sites["P-1042"].id, sites["P-1051"].id], m1.id)
    blk = pt.save_block(None, {"name": "Block A", "code": "A"}, site_id=sites["P-1042"].id)
    lvl = pt.save_level(None, {"name": "Level 1", "code": "L1"}, block_id=blk.id)
    u1 = pt.save_unit(None, {"unit_code": "K-01", "name": "Kitchen type 1", "quantity": "12"}, level_id=lvl.id)
    pt.add_materials(u1.id, [(P["H3311"].id, "8"), (P["BLUM-CLIP"].id, "14")], vpk_code="VPK-1")
    u2 = pt.save_unit(None, {"unit_code": "W-01", "name": "Wardrobe", "quantity": "4"}, site_id=sites["P-1060"].id)
    pt.add_materials(u2.id, [(P["U999"].id, "3")])

    return ctx, svc, P, sites, sup
