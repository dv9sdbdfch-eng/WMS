"""The single place where stock quantities change.

Every document service (Goods In, Goods Out, returns, adjustments, transfers,
production) calls ``StockEngine.post`` for each line, inside its own
transaction. ``post`` writes one immutable ``StockMovement`` and updates the
``StockLevel`` balance of the bins involved. If anything fails, the caller's
transaction rolls back and neither the movement nor the balance changes.

``post`` also records what the product, bins, project, supplier and user were called at that
moment (the ``*_snapshot`` columns), so history never changes when master data is renamed or
removed from active use later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from wms.core.enums import CLOSED_SITE_STATUSES, MovementType, ReasonUse, SiteStatus
from wms.core.exceptions import InsufficientStockError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, MovementReversal, Product, Site, StockLevel, StockMovement, Supplier
from wms.services import audit
from wms.services.base import AppContext
from wms.services.reasons import require_reason


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
                raise ValidationError(f"Product {product.internal_code} is inactive - it has been removed from "
                                      f"active use. Restore it first if it is needed again.")
            for b in (from_bin, to_bin):
                if b is not None and not b.is_active:
                    raise ValidationError(f"Bin {b.full_code} is inactive - it has been removed from active "
                                          f"use. Restore it first if it is needed again.")
        site = self.s.get(Site, site_id) if site_id else None
        supplier = self.s.get(Supplier, supplier_id) if supplier_id else None
        if not allow_inactive:
            self._check_site(site, MovementType(movement_type))
            if supplier is not None and not supplier.is_active:
                raise ValidationError(f"Supplier {supplier.name} has been archived. Restore it first if you "
                                      f"still deal with them.")

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
            product_code_snapshot=product.internal_code,
            product_name_snapshot=product.name,
            from_bin_name_snapshot=from_bin.full_code if from_bin else None,
            to_bin_name_snapshot=to_bin.full_code if to_bin else None,
            site_number_snapshot=site.project_number if site else None,
            site_name_snapshot=site.name if site else None,
            supplier_name_snapshot=supplier.name if supplier else None,
            user_name_snapshot=self.ctx.user.full_name,
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

    @staticmethod
    def _check_site(site: Site | None, movement_type: MovementType) -> None:
        """Completed / cancelled projects take no new material; archived ones take nothing at all.
        (Leftover material can still come back from a completed project as a return.)"""
        if site is None:
            return
        closed = site.status == SiteStatus.ARCHIVED.value or (
            site.status in CLOSED_SITE_STATUSES and movement_type is not MovementType.RETURN_FROM_SITE)
        if closed:
            raise ValidationError(f"Project {site.project_number} is {SiteStatus(site.status).label.lower()} - "
                                  f"reopen it before posting to it.", "site_id")

    def reverse(self, mv: StockMovement, reason: str, occurred_at: datetime | None = None,
                override_reason: str | None = None, reason_code: str = "OTHER") -> StockMovement:
        """Post the mirror image of ``mv`` (swap From/To) and record who reversed it and why.

        A movement is reversed once only, and a reversal is never reversed again (also enforced
        by the database). A reversal may take stock out of any bin, but it never puts stock back
        into a bin that has been removed from active use: such a bin must stay empty, so it is
        restored first."""
        if mv.reversal_of_id is not None:
            raise ValidationError("This movement is itself a reversal and cannot be reversed. "
                                  "Post a new movement instead.")
        if self.s.scalar(select(MovementReversal.id).where(MovementReversal.original_movement_id == mv.id)):
            raise ValidationError("This movement has already been reversed.")
        code = require_reason(self.s, reason_code, ReasonUse.REVERSAL)
        product = self.s.get(Product, mv.product_id)
        to_bin = self.s.get(Bin, mv.from_bin_id) if mv.from_bin_id else None
        if to_bin is not None and not to_bin.is_active:
            raise ValidationError(f"Bin {to_bin.full_code} has been removed from active use. Restore the bin "
                                  f"first, then reverse this document.")
        rev = self.post(
            movement_type=MovementType(mv.movement_type),
            product=product,
            quantity=mv.quantity,
            from_bin=self.s.get(Bin, mv.to_bin_id) if mv.to_bin_id else None,
            to_bin=to_bin,
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
        self.s.add(MovementReversal(original_movement_id=mv.id, reversal_movement_id=rev.id,
                                    reason_code=code.code, reason=reason, reversed_by_id=self.ctx.user.id,
                                    reversed_by_name_snapshot=self.ctx.user.full_name, reversed_at=rev.recorded_at))
        self.s.flush()
        return rev

    # -- integrity ----------------------------------------------------------
    def ledger_balances(self) -> dict[tuple[int, int], Decimal]:
        """Recalculate every product/bin balance from the movement ledger."""
        from wms.services.integrity import ledger_bin_balances
        return ledger_bin_balances(self.s)

    def find_discrepancies(self) -> list[Discrepancy]:
        from wms.services.integrity import stored_bin_balances
        ledger = self.ledger_balances()
        levels = stored_bin_balances(self.s)
        out: list[Discrepancy] = []
        for key in set(ledger) | set(levels):
            lq, sq = ledger.get(key, ZERO), levels.get(key, ZERO)
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
