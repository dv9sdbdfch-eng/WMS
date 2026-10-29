"""Ledger integrity checks: is every stock figure explained by the movement ledger?

Run from Settings > Database health, at start-up (``python -m wms --check``) and by the test
suite after every step of the warehouse scenario. Each problem is reported with a severity:

* CRITICAL - stored figures disagree with the ledger, or the ledger's own links are broken.
  Stock shown on screen cannot be trusted until it is investigated.
* WARNING  - allowed but worth a look (e.g. negative stock after an authorised override).

The checks only read; nothing is corrected automatically. "Rebuild stock levels from history"
(Settings) resets balances to the ledger after a CRITICAL balance problem has been understood.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from wms.core.enums import DocumentStatus
from wms.core.numbers import ZERO
from wms.db.models import (
    Bin,
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    GoodsIssueLine,
    Location,
    MovementReversal,
    Product,
    ProductionLine,
    ProductionOrder,
    ReturnLine,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockReturn,
    StockTransfer,
)

CRITICAL = "CRITICAL"
WARNING = "WARNING"


@dataclass
class IntegrityIssue:
    severity: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.check}: {self.message}"


def _fmt(q: Decimal) -> str:
    return f"{q.normalize():f}"


def warehouse_of_bins(s: Session) -> dict[int, Location]:
    """bin id -> its warehouse (the top of its location tree)."""
    locs = {l.id: l for l in s.scalars(select(Location))}

    def root(loc_id: int) -> Location:
        loc = locs[loc_id]
        seen = set()
        while loc.parent_id is not None and loc.parent_id in locs and loc.id not in seen:
            seen.add(loc.id)
            loc = locs[loc.parent_id]
        return loc

    return {bid: root(lid) for bid, lid in s.execute(select(Bin.id, Bin.location_id))}


def ledger_bin_balances(s: Session) -> dict[tuple[int, int], Decimal]:
    """(product, bin) -> quantity, recalculated from every movement ever posted."""
    totals: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    for bin_col, sign in ((StockMovement.to_bin_id, 1), (StockMovement.from_bin_id, -1)):
        for pid, bid, total in s.execute(
                select(StockMovement.product_id, bin_col, func.sum(StockMovement.quantity))
                .where(bin_col.is_not(None)).group_by(StockMovement.product_id, bin_col)):
            totals[(pid, bid)] += sign * (total or ZERO)
    return dict(totals)


def stored_bin_balances(s: Session) -> dict[tuple[int, int], Decimal]:
    return {(p, b): q for p, b, q in s.execute(select(StockLevel.product_id, StockLevel.bin_id, StockLevel.quantity))}


def run_checks(s: Session) -> list[IntegrityIssue]:
    issues: list[IntegrityIssue] = []
    ledger = ledger_bin_balances(s)
    stored = stored_bin_balances(s)
    codes = dict(s.execute(select(Product.id, Product.internal_code)).all())
    bins = dict(s.execute(select(Bin.id, Bin.full_code)).all())
    issues += _balance_checks(ledger, stored, codes, bins, warehouse_of_bins(s))
    issues += _reversal_checks(s)
    issues += _document_checks(s)
    for (pid, bid), q in sorted(stored.items()):
        if q < ZERO:
            issues.append(IntegrityIssue(WARNING, "Negative stock",
                                         f"{codes.get(pid, pid)} in {bins.get(bid, bid)} is {_fmt(q)} "
                                         f"(allowed only by a recorded override)."))
    return issues


def _balance_checks(ledger, stored, codes, bins, warehouses) -> list[IntegrityIssue]:
    out: list[IntegrityIssue] = []
    for key in sorted(set(ledger) | set(stored)):
        lq, sq = ledger.get(key, ZERO), stored.get(key, ZERO)
        if lq != sq:
            out.append(IntegrityIssue(CRITICAL, "Bin balance",
                                      f"{codes.get(key[0], key[0])} in {bins.get(key[1], key[1])}: ledger "
                                      f"{_fmt(lq)}, current stock {_fmt(sq)} (difference {_fmt(sq - lq)})."))

    def by(keyfn):
        led, sto = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        for k, q in ledger.items():
            led[keyfn(k)] += q
        for k, q in stored.items():
            sto[keyfn(k)] += q
        return led, sto

    led, sto = by(lambda k: k[0])
    for pid in sorted(set(led) | set(sto)):
        if led[pid] != sto[pid]:
            out.append(IntegrityIssue(CRITICAL, "Product balance",
                                      f"{codes.get(pid, pid)}: ledger {_fmt(led[pid])}, current stock "
                                      f"{_fmt(sto[pid])}."))
    led, sto = by(lambda k: (k[0], warehouses[k[1]].code if k[1] in warehouses else "?"))
    for key in sorted(set(led) | set(sto)):
        if led[key] != sto[key]:
            out.append(IntegrityIssue(CRITICAL, "Warehouse balance",
                                      f"{codes.get(key[0], key[0])} in warehouse {key[1]}: ledger "
                                      f"{_fmt(led[key])}, current stock {_fmt(sto[key])}."))
    return out


def _reversal_checks(s: Session) -> list[IntegrityIssue]:
    out: list[IntegrityIssue] = []
    linked = dict(s.execute(select(StockMovement.id, StockMovement.reversal_of_id)
                            .where(StockMovement.reversal_of_id.is_not(None))).all())
    recorded = dict(s.execute(select(MovementReversal.reversal_movement_id,
                                     MovementReversal.original_movement_id)).all())
    for rev_id, orig_id in sorted(linked.items()):
        if recorded.get(rev_id) != orig_id:
            out.append(IntegrityIssue(CRITICAL, "Reversal record",
                                      f"Movement #{rev_id} reverses #{orig_id} but has no matching reversal "
                                      f"record (who / when / why)."))
    for rev_id, orig_id in sorted(recorded.items()):
        if linked.get(rev_id) != orig_id:
            out.append(IntegrityIssue(CRITICAL, "Reversal record",
                                      f"Reversal record says #{rev_id} reverses #{orig_id}, but the ledger "
                                      f"does not link them."))
    return out


def _document_checks(s: Session) -> list[IntegrityIssue]:
    """Every posted line points at a movement for the same product and quantity; a reversed
    document has all its movements reversed and a posted one none of them.

    Done in SQL (only mismatches come back): 1-2 s in Python on 100,000 movements, ~0.2 s here."""
    out: list[IntegrityIssue] = []
    M = StockMovement

    def links(what: str, number_col, product_col, qty_col, movement_col, joins=()) -> None:
        q = select(number_col, qty_col, M.id, M.quantity).select_from(qty_col.table)
        for j in joins:
            q = q.join(*j)
        q = q.outerjoin(M, M.id == movement_col).where(
            or_(M.id.is_(None), M.product_id != product_col, M.quantity != qty_col))
        for number, qty, mid, mqty in s.execute(q):
            msg = (f"{what} {number}: a posted line has no stock movement." if mid is None else
                   f"{what} {number}: line ({_fmt(qty)}) does not match its stock movement #{mid} "
                   f"({_fmt(mqty)}).")
            out.append(IntegrityIssue(CRITICAL, "Document link", msg))

    def states(what: str, number_col, status_col, movement_ids) -> None:
        """movement_ids: select(doc id, movement id) for every movement of each document."""
        sub = movement_ids.subquery()
        rev = func.sum(case((MovementReversal.id.is_not(None), 1), else_=0))
        q = (select(number_col, status_col, func.count(sub.c.movement_id), rev)
             .select_from(number_col.table).join(sub, sub.c.doc_id == number_col.table.c.id)
             .outerjoin(MovementReversal, MovementReversal.original_movement_id == sub.c.movement_id)
             .group_by(number_col.table.c.id))
        for number, status, n, n_rev in s.execute(q):
            n_rev = n_rev or 0
            if status == DocumentStatus.REVERSED.value and n_rev != n:
                out.append(IntegrityIssue(CRITICAL, "Reversed document",
                                          f"{what} {number} is marked reversed but {n - n_rev} of its "
                                          f"movements were not reversed."))
            if status == DocumentStatus.POSTED.value and n_rev:
                out.append(IntegrityIssue(CRITICAL, "Reversed document",
                                          f"{what} {number} is posted but {n_rev} of its movements were "
                                          f"reversed on their own."))

    for what, Doc, Line, parent_col, number_col in (
            ("Delivery note", DeliveryNote, DeliveryNoteLine, DeliveryNoteLine.delivery_note_id,
             DeliveryNote.dn_number),
            ("Goods Out", GoodsIssue, GoodsIssueLine, GoodsIssueLine.issue_id, GoodsIssue.issue_number),
            ("Return", StockReturn, ReturnLine, ReturnLine.return_id, StockReturn.return_number),
            ("Production", ProductionOrder, ProductionLine, ProductionLine.order_id, ProductionOrder.order_number)):
        links(what, number_col, Line.product_id, Line.quantity, Line.movement_id,
              joins=[(Doc, Doc.id == parent_col)])
        ids = select(parent_col.label("doc_id"), Line.movement_id.label("movement_id"))
        if Doc is ProductionOrder:
            ids = ids.union_all(select(Doc.id, Doc.output_movement_id))
        states(what, number_col, Doc.status, ids)
    links("Production", ProductionOrder.order_number, ProductionOrder.finished_product_id, ProductionOrder.quantity,
          ProductionOrder.output_movement_id)
    links("Transfer", StockTransfer.transfer_number, StockTransfer.product_id, StockTransfer.quantity,
          StockTransfer.movement_id)
    # an adjustment's movement carries the size of the difference, whichever direction it went
    for (number,) in s.execute(
            select(StockAdjustment.adjustment_number)
            .outerjoin(M, M.id == StockAdjustment.movement_id)
            .where(or_(M.id.is_(None), M.product_id != StockAdjustment.product_id))):
        out.append(IntegrityIssue(CRITICAL, "Document link", f"Adjustment {number}: no matching stock movement."))
    for number, diff, mid, mqty in s.execute(
            select(StockAdjustment.adjustment_number, StockAdjustment.difference, M.id, M.quantity)
            .join(M, M.id == StockAdjustment.movement_id)):
        if abs(diff) != mqty:
            out.append(IntegrityIssue(CRITICAL, "Document link",
                                      f"Adjustment {number}: difference {_fmt(diff)} does not match its stock "
                                      f"movement #{mid} ({_fmt(mqty)})."))
    return out
