"""The single place where stock quantities change.

Every document service (Goods In, Goods Out, returns, adjustments, transfers,
production) calls ``StockEngine.post`` for each line, inside its own
transaction. ``post`` writes one immutable ``StockMovement`` and updates the
``StockLevel`` balance of the bins involved. If anything fails, the caller's
transaction rolls back and neither the movement nor the balance changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from wms.core.enums import MovementType
from wms.core.exceptions import InsufficientStockError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, Product, StockLevel, StockMovement
from wms.services import audit
from wms.services.base import AppContext


@dataclass
class Discrepancy:
    product_id: int
    product_code: str
    bin_id: int
    bin_code: str
    ledger_qty: Decimal
    level_qty: Decimal


class StockEngine:
    def __init__(self, session: Session, ctx: AppContext):
        self.s = session
        self.ctx = ctx

    # -- balances -----------------------------------------------------------
    def level(self, product_id: int, bin_id: int, lock: bool = False) -> StockLevel | None:
        q = select(StockLevel).where(StockLevel.product_id == product_id, StockLevel.bin_id == bin_id)
        if lock:
            q = q.with_for_update()
        return self.s.scalar(q)

    def qty_in_bin(self, product_id: int, bin_id: int) -> Decimal:
        lvl = self.level(product_id, bin_id)
        return lvl.quantity if lvl else ZERO

    def _adjust_level(self, product_id: int, bin_id: int, delta: Decimal) -> StockLevel:
        lvl = self.level(product_id, bin_id, lock=True)
        if lvl is None:
            lvl = StockLevel(product_id=product_id, bin_id=bin_id, quantity=ZERO)
            self.s.add(lvl)
        lvl.quantity = (lvl.quantity or ZERO) + delta
        return lvl

    # -- posting ------------------------------------------------------------
    def post(
        self,
        *,
        movement_type: MovementType,
        product: Product,
        quantity,
        from_bin: Bin | None = None,
        to_bin: Bin | None = None,
        occurred_at: datetime | None = None,
        unit_cost: Decimal | None = None,
        site_id: int | None = None,
        supplier_id: int | None = None,
        doc_type: str | None = None,
        doc_id: int | None = None,
        doc_number: str | None = None,
        reference: str | None = None,
        notes: str | None = None,
        override_reason: str | None = None,
        reversal_of_id: int | None = None,
        allow_inactive: bool = False,
    ) -> StockMovement:
        qty = to_qty(quantity)
        if qty <= ZERO:
            raise ValidationError(f"Quantity for {product.internal_code} must be greater than zero.", "quantity")
        if not product.unit.allow_decimal and qty != qty.to_integral_value():
            raise ValidationError(
                f"{product.internal_code} is counted in whole {product.unit.code} - quantity must be a whole number.",
                "quantity",
            )
        if from_bin is None and to_bin is None:
            raise ValidationError("A movement needs a From bin or a To bin.")
        if from_bin is not None and to_bin is not None and from_bin.id == to_bin.id:
            raise ValidationError("From and To bins must be different.")
        if not allow_inactive:
            if not product.is_active:
                raise ValidationError(f"Product {product.internal_code} is inactive.")
            for b in (from_bin, to_bin):
                if b is not None and not b.is_active:
                    raise ValidationError(f"Bin {b.full_code} is inactive.")

        is_override = False
        if from_bin is not None:
            available = self.qty_in_bin(product.id, from_bin.id)
            if available < qty:
                if not override_reason:
                    raise InsufficientStockError(product.internal_code, from_bin.full_code, available, qty)
                if not self.ctx.has(P.STOCK_OVERRIDE_NEGATIVE):
                    raise InsufficientStockError(product.internal_code, from_bin.full_code, available, qty)
                is_override = True
            self._adjust_level(product.id, from_bin.id, -qty)
        if to_bin is not None:
            self._adjust_level(product.id, to_bin.id, qty)

        mv = StockMovement(
            movement_type=MovementType(movement_type).value,
            product_id=product.id,
            quantity=qty,
            from_bin_id=from_bin.id if from_bin else None,
            to_bin_id=to_bin.id if to_bin else None,
            unit_cost=unit_cost if unit_cost is not None else product.unit_cost,
            site_id=site_id,
            supplier_id=supplier_id,
            doc_type=doc_type,
            doc_id=doc_id,
            doc_number=doc_number,
            reference=reference,
            notes=notes,
            user_id=self.ctx.user.id,
            occurred_at=occurred_at or utcnow(),
            recorded_at=utcnow(),
            reversal_of_id=reversal_of_id,
            is_override=is_override,
            override_reason=override_reason if is_override else None,
        )
        self.s.add(mv)
        self.s.flush()
        if is_override:
            audit.record(
                self.s,
                self.ctx,
                "NEGATIVE_STOCK_OVERRIDE",
                "PRODUCT",
                product.id,
                product.internal_code,
                f"{qty.normalize():f} {product.unit.code} taken from {from_bin.full_code} "
                f"leaving negative stock. Reason: {override_reason}",
            )
        return mv

    def reverse(self, mv: StockMovement, reason: str, occurred_at: datetime | None = None,
                override_reason: str | None = None) -> StockMovement:
        """Post the mirror image of ``mv`` (swap From/To)."""
        product = self.s.get(Product, mv.product_id)
        return self.post(
            movement_type=MovementType(mv.movement_type),
            product=product,
            quantity=mv.quantity,
            from_bin=self.s.get(Bin, mv.to_bin_id) if mv.to_bin_id else None,
            to_bin=self.s.get(Bin, mv.from_bin_id) if mv.from_bin_id else None,
            occurred_at=occurred_at or utcnow(),
            unit_cost=mv.unit_cost,
            site_id=mv.site_id,
            supplier_id=mv.supplier_id,
            doc_type=mv.doc_type,
            doc_id=mv.doc_id,
            doc_number=mv.doc_number,
            reference=mv.reference,
            notes=f"REVERSAL: {reason}",
            reversal_of_id=mv.id,
            override_reason=override_reason,
            allow_inactive=True,
        )

    # -- integrity ----------------------------------------------------------
    def ledger_balances(self) -> dict[tuple[int, int], Decimal]:
        """Recalculate every product/bin balance from the movement ledger."""
        totals: dict[tuple[int, int], Decimal] = {}
        for bin_col, sign in ((StockMovement.to_bin_id, 1), (StockMovement.from_bin_id, -1)):
            rows = self.s.execute(
                select(StockMovement.product_id, bin_col, func.sum(StockMovement.quantity))
                .where(bin_col.is_not(None))
                .group_by(StockMovement.product_id, bin_col)
            )
            for product_id, bin_id, total in rows:
                key = (product_id, bin_id)
                totals[key] = totals.get(key, ZERO) + sign * (total or ZERO)
        return totals

    def find_discrepancies(self) -> list[Discrepancy]:
        ledger = self.ledger_balances()
        levels = {(l.product_id, l.bin_id): l for l in self.s.scalars(select(StockLevel))}
        out: list[Discrepancy] = []
        for key in set(ledger) | set(levels):
            lq = ledger.get(key, ZERO)
            lvl = levels.get(key)
            sq = lvl.quantity if lvl else ZERO
            if lq != sq:
                product = self.s.get(Product, key[0])
                b = self.s.get(Bin, key[1])
                out.append(Discrepancy(key[0], product.internal_code, key[1], b.full_code, lq, sq))
        return sorted(out, key=lambda d: (d.product_code, d.bin_code))

    def rebuild_levels(self) -> int:
        """Reset all balances to what the ledger says. Returns rows corrected."""
        fixed = 0
        for d in self.find_discrepancies():
            lvl = self.level(d.product_id, d.bin_id, lock=True)
            if lvl is None:
                lvl = StockLevel(product_id=d.product_id, bin_id=d.bin_id, quantity=ZERO)
                self.s.add(lvl)
            lvl.quantity = d.ledger_qty
            fixed += 1
        if fixed:
            audit.record(self.s, self.ctx, "STOCK_LEVELS_REBUILT", "SYSTEM", None, None,
                         f"{fixed} stock balance(s) recalculated from the movement ledger.")
        return fixed


def product_totals_query():
    """(product_id, on_hand, available) aggregated over bins - for list screens and reports."""
    return (
        select(
            StockLevel.product_id,
            func.coalesce(func.sum(StockLevel.quantity), 0).label("on_hand"),
            func.coalesce(
                func.sum(case((Bin.is_available.is_(True), StockLevel.quantity), else_=0)), 0
            ).label("available"),
        )
        .join(Bin, Bin.id == StockLevel.bin_id)
        .group_by(StockLevel.product_id)
    )
