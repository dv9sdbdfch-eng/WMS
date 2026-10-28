"""Stocktake (physical inventory).

    1. Start a count for a location (or the whole warehouse): every product/bin with stock
       there is copied onto the count with its system quantity at that moment.
    2. Count: enter the physical quantity per line (lines can be added for items found in a bin
       the system did not expect). Stock is NOT changed.
    3. Submit: the count is locked for review and the differences are shown.
    4. Approve (needs "Stock adjustments"): every difference is posted as a normal stock
       adjustment, reason "Stock Count Difference", linked to its count line - all in one
       transaction. If stock moved in a counted bin after the count started, approval is
       refused for those lines until they are re-checked, so a count can never silently undo
       a Goods In or Goods Out that happened while counting.

Counts can be sent back for recount or cancelled; nothing is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from wms.core.enums import AdjustmentReason, CountStatus, EntityType
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_qty, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, Location, Product, StockCount, StockCountLine, StockLevel
from wms.services import audit
from wms.services.base import BaseService, clean_str, prefix_pattern
from wms.services.numbering import next_number
from wms.services.stock_engine import StockEngine


@dataclass
class CountEntry:
    line_id: int
    counted_qty: Decimal | str | None
    notes: str | None = None


class StocktakeService(BaseService):
    # -- reading ------------------------------------------------------------
    def list(self, status: str | None = None, limit: int = 500) -> list[StockCount]:
        q = (select(StockCount).options(selectinload(StockCount.lines))
             .order_by(StockCount.created_at.desc(), StockCount.id.desc()).limit(limit))
        if status:
            q = q.where(StockCount.status == status)
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    def get(self, count_id: int) -> StockCount:
        with self.db.read_session() as s:
            c = s.get(StockCount, count_id)
            if c is None:
                raise NotFoundError("Stock count not found.")
            _ = [(l.product.unit, l.bin) for l in c.lines]
            return c

    # -- workflow -----------------------------------------------------------
    def create(self, location_id: int | None = None, description: str | None = None,
               include_zero_bins: bool = False) -> StockCount:
        """Start a count. ``include_zero_bins`` also lists each product's default bin in the area
        when it shows no stock (useful to confirm a bin really is empty)."""
        self.require(P.STOCKTAKE_COUNT)
        with self.db.session() as s:
            loc = s.get(Location, location_id) if location_id else None
            if location_id and loc is None:
                raise ValidationError("Location not found.", "location_id")
            in_area = Bin.is_active.is_(True)
            if loc is not None:
                in_area = in_area & Bin.full_code.like(prefix_pattern(loc.path), escape="\\")
            levels = list(s.execute(
                select(StockLevel.product_id, StockLevel.bin_id, StockLevel.quantity)
                .join(Bin, Bin.id == StockLevel.bin_id)
                .join(Product, Product.id == StockLevel.product_id)
                .where(in_area, StockLevel.quantity != ZERO)
                .order_by(Bin.full_code, Product.internal_code)))
            keys = {(p, b) for p, b, _ in levels}
            if include_zero_bins:
                for pid, bid in s.execute(select(Product.id, Product.default_bin_id)
                                          .join(Bin, Bin.id == Product.default_bin_id)
                                          .where(in_area, Product.is_active.is_(True))):
                    if (pid, bid) not in keys:
                        levels.append((pid, bid, ZERO))
                        keys.add((pid, bid))
            if not levels:
                raise ValidationError("There is no stock to count in this area.")
            count = StockCount(count_number=next_number(s, "STOCK_COUNT"), status=CountStatus.OPEN.value,
                               location_id=loc.id if loc else None,
                               description=clean_str(description, "description", max_len=200),
                               created_by_id=self.user.id)
            s.add(count)
            s.flush()
            for i, (pid, bid, qty) in enumerate(levels, start=1):
                s.add(StockCountLine(count_id=count.id, line_no=i, product_id=pid, bin_id=bid, system_qty=qty))
            audit.record(s, self.ctx, "STOCK_COUNT_STARTED", EntityType.STOCK_COUNT.value, count.id,
                         count.count_number, f"{len(levels)} line(s) in {loc.path if loc else 'whole warehouse'}.")
            count_id = count.id
        return self.get(count_id)

    def _open_count(self, s, count_id: int, allowed: tuple[CountStatus, ...]) -> StockCount:
        c = s.get(StockCount, count_id)
        if c is None:
            raise NotFoundError("Stock count not found.")
        if c.status not in {a.value for a in allowed}:
            raise ValidationError(f"Count {c.count_number} is {CountStatus(c.status).label.lower()} - "
                                  f"it cannot be changed now.")
        return c

    def record_counts(self, count_id: int, entries: list[CountEntry]) -> StockCount:
        """Save physical quantities (blank = not counted yet). Stock does not change."""
        self.require(P.STOCKTAKE_COUNT)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.OPEN,))
            lines = {l.id: l for l in c.lines}
            now = utcnow()
            for e in entries:
                line = lines.get(e.line_id)
                if line is None:
                    raise ValidationError("A count line does not belong to this count.")
                qty = None if e.counted_qty in (None, "") else to_qty(e.counted_qty, "counted quantity")
                if qty is not None and qty < ZERO:
                    raise ValidationError(f"{line.product.internal_code}: counted quantity cannot be negative.")
                if qty is not None and not line.product.unit.allow_decimal and qty != qty.to_integral_value():
                    raise ValidationError(f"{line.product.internal_code} is counted in whole "
                                          f"{line.product.unit.code}.")
                notes = clean_str(e.notes)
                if qty != line.counted_qty or notes != line.notes:
                    line.counted_qty, line.notes = qty, notes
                    line.counted_by_id, line.counted_at = (self.user.id, now) if qty is not None else (None, None)
        return self.get(count_id)

    def add_line(self, count_id: int, product_id: int, bin_id: int) -> StockCount:
        """Add an item found in a bin that was not on the count sheet."""
        self.require(P.STOCKTAKE_COUNT)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.OPEN,))
            product, b = s.get(Product, product_id), s.get(Bin, bin_id)
            if product is None or b is None:
                raise ValidationError("Choose a product and a bin.")
            if any(l.product_id == product_id and l.bin_id == bin_id for l in c.lines):
                raise ValidationError(f"{product.internal_code} in {b.full_code} is already on this count.")
            system = StockEngine(s, self.ctx).qty_in_bin(product_id, bin_id)
            s.add(StockCountLine(count_id=c.id, line_no=max((l.line_no for l in c.lines), default=0) + 1,
                                 product_id=product_id, bin_id=bin_id, system_qty=system))
        return self.get(count_id)

    def submit(self, count_id: int) -> StockCount:
        self.require(P.STOCKTAKE_COUNT)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.OPEN,))
            missing = [l for l in c.lines if l.counted_qty is None]
            if missing:
                raise ValidationError(f"{len(missing)} line(s) have not been counted yet (first: "
                                      f"{missing[0].product.internal_code} in {missing[0].bin.full_code}). "
                                      f"Enter 0 for items that are not there.")
            c.status = CountStatus.SUBMITTED.value
            c.submitted_by_id, c.submitted_at = self.user.id, utcnow()
            diffs = sum(1 for l in c.lines if l.difference)
            audit.record(s, self.ctx, "STOCK_COUNT_SUBMITTED", EntityType.STOCK_COUNT.value, c.id, c.count_number,
                         f"{len(c.lines)} line(s) counted, {diffs} difference(s).")
        return self.get(count_id)

    def send_back(self, count_id: int, reason: str) -> StockCount:
        """Reviewer returns a submitted count for recounting."""
        self.require(P.STOCK_ADJUST)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.SUBMITTED,))
            c.status = CountStatus.OPEN.value
            audit.record(s, self.ctx, "STOCK_COUNT_SENT_BACK", EntityType.STOCK_COUNT.value, c.id, c.count_number,
                         reason)
        return self.get(count_id)

    def changed_since_count(self, count_id: int) -> list[StockCountLine]:
        """Lines whose bin balance moved after the count started (approval is blocked for these)."""
        with self.db.read_session() as s:
            c = s.get(StockCount, count_id)
            if c is None:
                raise NotFoundError("Stock count not found.")
            engine = StockEngine(s, self.ctx)
            return [l for l in c.lines if engine.qty_in_bin(l.product_id, l.bin_id) != l.system_qty]

    def refresh_system_qty(self, count_id: int) -> int:
        """Re-read system quantities for lines that moved, clearing their count so they are re-checked.

        Only for counts still being counted or sent back - the count goes (back) to OPEN."""
        self.require(P.STOCKTAKE_COUNT)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.OPEN, CountStatus.SUBMITTED))
            engine = StockEngine(s, self.ctx)
            n = 0
            for l in c.lines:
                now_qty = engine.qty_in_bin(l.product_id, l.bin_id)
                if now_qty != l.system_qty:
                    l.system_qty, l.counted_qty, l.counted_by_id, l.counted_at = now_qty, None, None, None
                    n += 1
            if n:
                c.status = CountStatus.OPEN.value
                audit.record(s, self.ctx, "STOCK_COUNT_REFRESHED", EntityType.STOCK_COUNT.value, c.id,
                             c.count_number, f"{n} line(s) changed since the count started and must be recounted.")
        return n

    def approve(self, count_id: int, note: str | None = None) -> StockCount:
        """Post every difference as a stock adjustment, all or nothing."""
        self.require(P.STOCK_ADJUST)
        from wms.services.stock_ops import StockOpsService

        ops = StockOpsService(self.ctx)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.SUBMITTED,))
            engine = StockEngine(s, self.ctx)
            moved = [l for l in c.lines if engine.qty_in_bin(l.product_id, l.bin_id) != l.system_qty]
            if moved:
                first = moved[0]
                raise ValidationError(
                    f"Stock moved in {len(moved)} counted bin(s) after the count started (first: "
                    f"{first.product.internal_code} in {first.bin.full_code}). Use 'Re-check moved lines' and "
                    f"count those again before approving.")
            posted = 0
            for l in c.lines:
                if not l.difference:
                    continue
                adj = ops.adjust_in_session(
                    s, l.product_id, l.bin_id, l.counted_qty, AdjustmentReason.STOCK_COUNT.value,
                    f"{c.count_number}: counted {fmt_qty(l.counted_qty)}, system {fmt_qty(l.system_qty)}"
                    + (f" - {l.notes}" if l.notes else ""),
                    expected_previous=l.system_qty)
                l.adjustment_id = adj.id
                posted += 1
            c.status = CountStatus.APPROVED.value
            c.closed_by_id, c.closed_at, c.close_reason = self.user.id, utcnow(), clean_str(note)
            audit.record(s, self.ctx, "STOCK_COUNT_APPROVED", EntityType.STOCK_COUNT.value, c.id, c.count_number,
                         f"{posted} adjustment(s) posted from {len(c.lines)} counted line(s).")
        return self.get(count_id)

    def cancel(self, count_id: int, reason: str) -> StockCount:
        self.require(P.STOCKTAKE_COUNT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            c = self._open_count(s, count_id, (CountStatus.OPEN, CountStatus.SUBMITTED))
            c.status = CountStatus.CANCELLED.value
            c.closed_by_id, c.closed_at, c.close_reason = self.user.id, utcnow(), reason
            audit.record(s, self.ctx, "STOCK_COUNT_CANCELLED", EntityType.STOCK_COUNT.value, c.id, c.count_number,
                         reason)
        return self.get(count_id)

    # -- printing -----------------------------------------------------------
    def count_sheet(self, count_id: int, show_system_qty: bool = False):
        """A ReportResult for printing/exporting the count sheet (blind by default)."""
        from wms.services.reports import QTY, Column, ReportResult

        c = self.get(count_id)
        cols = [Column("Bin"), Column("Code"), Column("Product"), Column("Unit")]
        if show_system_qty:
            cols.append(Column("System Qty", QTY))
        cols += [Column("Counted Qty", QTY), Column("Notes")]
        rows = []
        for l in sorted(c.lines, key=lambda x: (x.bin.full_code, x.product.internal_code)):
            r = [l.bin.full_code, l.product.internal_code, l.product.name, l.product.unit.code]
            if show_system_qty:
                r.append(l.system_qty)
            r += [l.counted_qty, l.notes or ""]
            rows.append(r)
        res = ReportResult(f"Stock Count {c.count_number}", cols, rows)
        res.filters_text = (f"{c.location.path if c.location else 'Whole warehouse'}; started "
                            f"{c.created_at.astimezone():%d/%m/%Y %H:%M}")
        return res
