"""Read side of the stock ledger, single-movement reversal and the integrity check.

Balances come from ``stock_levels`` (fast) and are always reconcilable against the ledger with
``integrity_check``. Nothing here changes stock except ``reverse_movement``, which goes through
``StockEngine`` like every other posting.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_, select

from wms.core.enums import EntityType
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import Bin, MovementReversal, Product, StockLevel, StockMovement
from wms.services import audit
from wms.services.base import BaseService, clean_str
from wms.services.integrity import CRITICAL, IntegrityIssue, run_checks, warehouse_of_bins
from wms.services.stock_engine import StockEngine

# Movements that belong to a multi-line document are reversed with the whole document, so the
# document's status always matches its movements. These stand alone and are reversed one by one.
SINGLE_MOVEMENT_DOCS = (EntityType.TRANSFER.value, EntityType.ADJUSTMENT.value)


@dataclass
class BinBalance:
    bin_id: int
    bin_code: str
    warehouse_code: str
    quantity: Decimal
    is_available: bool


@dataclass
class ProductBalance:
    product_id: int
    on_hand: Decimal = ZERO
    in_unavailable_bins: Decimal = ZERO  # quarantine / damaged bins: held but cannot be issued
    by_bin: list[BinBalance] = field(default_factory=list)
    by_warehouse: dict[str, Decimal] = field(default_factory=dict)


class LedgerService(BaseService):
    # -- balances -----------------------------------------------------------
    def product_balance(self, product_id: int) -> ProductBalance:
        """On hand for one product: total, per bin and per warehouse."""
        self.require(P.STOCK_VIEW)
        with self.db.read_session() as s:
            wh = warehouse_of_bins(s)
            bal = ProductBalance(product_id)
            rows = s.execute(select(StockLevel.bin_id, Bin.full_code, Bin.is_available, StockLevel.quantity)
                             .join(Bin, Bin.id == StockLevel.bin_id)
                             .where(StockLevel.product_id == product_id, StockLevel.quantity != 0)
                             .order_by(Bin.full_code))
            for bin_id, code, available, qty in rows:
                w = wh[bin_id].code
                bal.by_bin.append(BinBalance(bin_id, code, w, qty, available))
                bal.on_hand += qty
                if not available:
                    bal.in_unavailable_bins += qty
                bal.by_warehouse[w] = bal.by_warehouse.get(w, ZERO) + qty
            return bal

    def warehouse_balances(self) -> dict[tuple[int, str], Decimal]:
        """(product id, warehouse code) -> quantity for every product held anywhere."""
        self.require(P.STOCK_VIEW)
        with self.db.read_session() as s:
            wh = warehouse_of_bins(s)
            out: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
            for pid, bid, qty in s.execute(select(StockLevel.product_id, StockLevel.bin_id, StockLevel.quantity)
                                           .where(StockLevel.quantity != 0)):
                out[(pid, wh[bid].code)] += qty
            return dict(out)

    # -- reversals ----------------------------------------------------------
    def reversal_of(self, movement_id: int) -> MovementReversal | None:
        """The reversal record that involves this movement, as original or as the reversal."""
        with self.db.read_session() as s:
            return s.scalar(select(MovementReversal).where(or_(
                MovementReversal.original_movement_id == movement_id,
                MovementReversal.reversal_movement_id == movement_id)))

    def can_reverse_alone(self, mv: StockMovement) -> bool:
        return mv.reversal_of_id is None and (mv.doc_type is None or mv.doc_type in SINGLE_MOVEMENT_DOCS)

    def reverse_movement(self, movement_id: int, reason_code: str, reason: str,
                         override_reason: str | None = None) -> StockMovement:
        """Reverse one transfer or adjustment: posts the mirror movement, linked to the original.

        Movements from a delivery note, Goods Out, return or production record are reversed with
        their whole document (Reverse on that document), so the document never says "posted"
        while part of it has been undone."""
        self.require(P.STOCK_ADJUST)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            mv = s.get(StockMovement, movement_id)
            if mv is None:
                raise NotFoundError("Stock movement not found.")
            if mv.reversal_of_id is None and mv.doc_type and mv.doc_type not in SINGLE_MOVEMENT_DOCS:
                raise ValidationError(f"This movement belongs to {EntityType(mv.doc_type).label} {mv.doc_number}. "
                                      f"Open that document and reverse it there.")
            rev = StockEngine(s, self.ctx).reverse(mv, reason, override_reason=override_reason,
                                                   reason_code=reason_code)
            product = s.get(Product, mv.product_id)
            audit.record(s, self.ctx, "MOVEMENT_REVERSED", "PRODUCT", product.id, product.internal_code,
                         f"Movement #{mv.id} ({mv.doc_number or mv.movement_type}) reversed by #{rev.id}: {reason}",
                         new={"original_movement_id": mv.id, "reversal_movement_id": rev.id,
                              "reason_code": reason_code})
            rev_id = rev.id
        with self.db.read_session() as s:
            return s.get(StockMovement, rev_id)

    # -- integrity ----------------------------------------------------------
    def integrity_check(self) -> list[IntegrityIssue]:
        """Every problem found; CRITICAL ones mean stock figures cannot be trusted."""
        with self.db.read_session() as s:
            return run_checks(s)

    def critical_issues(self) -> list[IntegrityIssue]:
        return [i for i in self.integrity_check() if i.severity == CRITICAL]
