"""Returns, stock adjustments, bin transfers and the movement history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from wms.core.enums import AdjustmentReason, DocumentStatus, EntityType, MovementType, ReturnCondition, ReturnType
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    Product,
    ReturnLine,
    Site,
    StockAdjustment,
    StockMovement,
    StockReturn,
    StockTransfer,
    Supplier,
)
from wms.services import audit
from wms.services.base import BaseService, clean_str
from wms.services.goods_in import doc_datetime, validate_doc_date
from wms.services.numbering import next_number
from wms.services.stock_engine import StockEngine


@dataclass
class ReturnLineInput:
    product_id: int
    quantity: Decimal | str
    bin_id: int
    condition: str = ReturnCondition.USABLE.value
    notes: str | None = None


class StockOpsService(BaseService):
    # -- returns ------------------------------------------------------------
    def post_return(self, header: dict, lines: list[ReturnLineInput], override_reason: str | None = None) -> StockReturn:
        """Return from site (stock back in) or return to supplier (stock out).

        Usable material from site goes to the chosen bin; damaged material
        should go to a quarantine bin (not available for issue).
        """
        self.require(P.RETURNS_POST)
        try:
            rtype = ReturnType(header.get("return_type"))
        except ValueError:
            raise ValidationError("Choose the return type.", "return_type") from None
        rdate = validate_doc_date(header.get("return_date"), "return_date")
        if not lines:
            raise ValidationError("Add at least one product line.")
        with self.db.session() as s:
            site = supplier = None
            if rtype is ReturnType.FROM_SITE:
                site = s.get(Site, header.get("site_id")) if header.get("site_id") else None
                if site is None:
                    raise ValidationError("Choose the site the material is coming back from.", "site_id")
                seq, mtype = "RETURN_FROM_SITE", MovementType.RETURN_FROM_SITE
            else:
                supplier = s.get(Supplier, header.get("supplier_id")) if header.get("supplier_id") else None
                if supplier is None:
                    raise ValidationError("Choose the supplier the goods are going back to.", "supplier_id")
                seq, mtype = "RETURN_TO_SUPPLIER", MovementType.RETURN_TO_SUPPLIER
            ret = StockReturn(
                return_number=next_number(s, seq), return_type=rtype.value, return_date=rdate,
                site_id=site.id if site else None, supplier_id=supplier.id if supplier else None,
                reference=clean_str(header.get("reference"), max_len=80), reason=clean_str(header.get("reason")),
                notes=clean_str(header.get("notes")), user_id=self.user.id, status=DocumentStatus.POSTED.value,
            )
            s.add(ret)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(rdate)
            for i, ln in enumerate(lines, start=1):
                product = s.get(Product, ln.product_id)
                b = s.get(Bin, ln.bin_id) if ln.bin_id else None
                if product is None or b is None:
                    raise ValidationError(f"Line {i}: choose a product and a bin.")
                cond = ReturnCondition(ln.condition or ReturnCondition.USABLE).value
                qty = to_qty(ln.quantity, f"line {i} quantity")
                kwargs = dict(to_bin=b) if rtype is ReturnType.FROM_SITE else dict(from_bin=b)
                mv = engine.post(
                    movement_type=mtype, product=product, quantity=qty, occurred_at=when,
                    site_id=ret.site_id, supplier_id=ret.supplier_id, doc_type=EntityType.RETURN.value,
                    doc_id=ret.id, doc_number=ret.return_number, reference=ret.reference,
                    notes=" ".join(x for x in (ReturnCondition(cond).label, clean_str(ln.notes) or "") if x),
                    override_reason=override_reason, **kwargs,
                )
                ret.lines.append(ReturnLine(line_no=i, product_id=product.id, quantity=qty, bin_id=b.id,
                                            condition=cond, notes=clean_str(ln.notes), movement_id=mv.id))
            s.flush()
            label = site.display if site else supplier.name
            audit.record(s, self.ctx, "RETURN_POSTED", EntityType.RETURN.value, ret.id,
                         f"{ret.return_number} {rtype.label} ({label})", f"{len(lines)} line(s).")
            ret_id = ret.id
        return self.get_return(ret_id)

    def reverse_return(self, return_id: int, reason: str, override_reason: str | None = None) -> StockReturn:
        self.require(P.RETURNS_POST)
        self.require(P.STOCK_ADJUST)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            ret = s.get(StockReturn, return_id)
            if ret is None:
                raise NotFoundError("Return not found.")
            if ret.status != DocumentStatus.POSTED.value:
                raise ValidationError("This return has already been reversed.")
            engine = StockEngine(s, self.ctx)
            for line in ret.lines:
                engine.reverse(s.get(StockMovement, line.movement_id), f"{ret.return_number} reversed: {reason}",
                               override_reason=override_reason)
            ret.status = DocumentStatus.REVERSED.value
            ret.reversed_at = utcnow()
            ret.reversed_by_id = self.user.id
            ret.reversal_reason = reason
            audit.record(s, self.ctx, "RETURN_REVERSED", EntityType.RETURN.value, ret.id, ret.return_number, reason)
        return self.get_return(return_id)

    def get_return(self, return_id: int) -> StockReturn:
        with self.db.read_session() as s:
            ret = s.get(StockReturn, return_id)
            if ret is None:
                raise NotFoundError("Return not found.")
            _ = [(l.product, l.bin) for l in ret.lines]
            return ret

    def returns(self, text: str | None = None, return_type: str | None = None, date_from: date | None = None,
                date_to: date | None = None, limit: int = 1000) -> list[StockReturn]:
        q = (select(StockReturn).options(selectinload(StockReturn.lines))
             .order_by(StockReturn.return_date.desc(), StockReturn.id.desc()).limit(limit))
        if return_type:
            q = q.where(StockReturn.return_type == return_type)
        if date_from:
            q = q.where(StockReturn.return_date >= date_from)
        if date_to:
            q = q.where(StockReturn.return_date <= date_to)
        if text and text.strip():
            like = f"%{text.strip()}%"
            q = q.where(or_(StockReturn.return_number.ilike(like), StockReturn.reference.ilike(like),
                            StockReturn.reason.ilike(like),
                            StockReturn.site_id.in_(select(Site.id).where(or_(Site.name.ilike(like),
                                                                              Site.project_number.ilike(like)))),
                            StockReturn.supplier_id.in_(select(Supplier.id).where(Supplier.name.ilike(like)))))
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    # -- adjustments --------------------------------------------------------
    def adjust(self, product_id: int, bin_id: int, new_qty, reason_code: str, reason: str,
               expected_previous: Decimal | None = None) -> StockAdjustment:
        """Set the quantity of a product in a bin to what was physically counted."""
        self.require(P.STOCK_ADJUST)
        try:
            code = AdjustmentReason(reason_code).value
        except ValueError:
            raise ValidationError("Choose an adjustment reason.", "reason_code") from None
        reason = clean_str(reason, "reason", required=True)
        new_qty = to_qty(new_qty, "new_quantity")
        if new_qty < ZERO:
            raise ValidationError("New quantity cannot be negative.", "new_quantity")
        with self.db.session() as s:
            product = s.get(Product, product_id)
            b = s.get(Bin, bin_id)
            if product is None or b is None:
                raise ValidationError("Choose a product and a bin.")
            engine = StockEngine(s, self.ctx)
            previous = engine.qty_in_bin(product.id, b.id)
            if expected_previous is not None and previous != expected_previous:
                raise ValidationError(
                    f"The quantity in {b.full_code} changed to {previous.normalize():f} while you were entering the "
                    f"adjustment. Please check and enter it again.")
            diff = new_qty - previous
            if diff == ZERO:
                raise ValidationError("The new quantity is the same as the current quantity - nothing to adjust.")
            number = next_number(s, "ADJUSTMENT")
            adj = StockAdjustment(adjustment_number=number, product_id=product.id, bin_id=b.id,
                                  previous_qty=previous, new_qty=new_qty, difference=diff, reason_code=code,
                                  reason=reason, user_id=self.user.id)
            s.add(adj)
            s.flush()
            if diff > ZERO:
                mtype = (MovementType.OPENING_BALANCE if code == AdjustmentReason.OPENING_BALANCE.value
                         else MovementType.ADJUSTMENT_IN)
                mv = engine.post(movement_type=mtype, product=product, quantity=diff, to_bin=b,
                                 doc_type=EntityType.ADJUSTMENT.value, doc_id=adj.id, doc_number=number,
                                 notes=f"{AdjustmentReason(code).label}: {reason}")
            else:
                # Taking the bin down to new_qty >= 0 never goes negative, but the
                # balance may already be negative from an override - allow that path.
                mv = engine.post(movement_type=MovementType.ADJUSTMENT_OUT, product=product, quantity=-diff,
                                 from_bin=b, doc_type=EntityType.ADJUSTMENT.value, doc_id=adj.id, doc_number=number,
                                 notes=f"{AdjustmentReason(code).label}: {reason}")
            adj.movement_id = mv.id
            audit.record(s, self.ctx, "STOCK_ADJUSTED", "PRODUCT", product.id, f"{product.internal_code} @ {b.full_code}",
                         f"{number}: {AdjustmentReason(code).label} - {reason}",
                         old={"quantity": previous}, new={"quantity": new_qty, "difference": diff})
            adj_id = adj.id
        with self.db.read_session() as s:
            return s.get(StockAdjustment, adj_id)

    def adjustments(self, text: str | None = None, date_from: date | None = None, date_to: date | None = None,
                    limit: int = 2000) -> list[StockAdjustment]:
        q = select(StockAdjustment).order_by(StockAdjustment.created_at.desc()).limit(limit)
        if text and text.strip():
            like = f"%{text.strip()}%"
            q = q.where(or_(StockAdjustment.adjustment_number.ilike(like), StockAdjustment.reason.ilike(like),
                            StockAdjustment.product_id.in_(select(Product.id).where(
                                or_(Product.internal_code.ilike(like), Product.name.ilike(like))))))
        with self.db.read_session() as s:
            rows = list(s.scalars(q))
        return _filter_dates(rows, "created_at", date_from, date_to)

    # -- transfers ----------------------------------------------------------
    def transfer(self, product_id: int, from_bin_id: int, to_bin_id: int, quantity, notes: str | None = None
                 ) -> StockTransfer:
        """Move stock between bins. Not a Goods Out: total stock is unchanged."""
        self.require(P.STOCK_TRANSFER)
        with self.db.session() as s:
            product = s.get(Product, product_id)
            src, dst = s.get(Bin, from_bin_id), s.get(Bin, to_bin_id)
            if product is None or src is None or dst is None:
                raise ValidationError("Choose a product, a From bin and a To bin.")
            qty = to_qty(quantity)
            number = next_number(s, "TRANSFER")
            tr = StockTransfer(transfer_number=number, product_id=product.id, from_bin_id=src.id, to_bin_id=dst.id,
                               quantity=qty, notes=clean_str(notes), user_id=self.user.id)
            s.add(tr)
            s.flush()
            mv = StockEngine(s, self.ctx).post(
                movement_type=MovementType.TRANSFER, product=product, quantity=qty, from_bin=src, to_bin=dst,
                doc_type=EntityType.TRANSFER.value, doc_id=tr.id, doc_number=number, notes=clean_str(notes))
            tr.movement_id = mv.id
            audit.record(s, self.ctx, "STOCK_TRANSFERRED", "PRODUCT", product.id, product.internal_code,
                         f"{number}: {qty.normalize():f} {product.unit.code} {src.full_code} -> {dst.full_code}")
            tr_id = tr.id
        with self.db.read_session() as s:
            return s.get(StockTransfer, tr_id)

    # -- movement history ---------------------------------------------------
    def get_movement(self, movement_id: int) -> StockMovement:
        self.require(P.STOCK_VIEW)
        with self.db.read_session() as s:
            mv = s.get(StockMovement, movement_id)
            if mv is None:
                raise NotFoundError("Stock movement not found.")
            return mv

    def movements(self, text: str | None = None, movement_types: list[str] | None = None,
                  product_id: int | None = None, site_id: int | None = None, supplier_id: int | None = None,
                  bin_id: int | None = None, user_id: int | None = None, date_from: date | None = None,
                  date_to: date | None = None, limit: int = 5000) -> list[StockMovement]:
        self.require(P.STOCK_VIEW)
        q = select(StockMovement).order_by(StockMovement.occurred_at.desc(), StockMovement.id.desc()).limit(limit)
        if movement_types:
            q = q.where(StockMovement.movement_type.in_(movement_types))
        if product_id:
            q = q.where(StockMovement.product_id == product_id)
        if site_id:
            q = q.where(StockMovement.site_id == site_id)
        if supplier_id:
            q = q.where(StockMovement.supplier_id == supplier_id)
        if bin_id:
            q = q.where(or_(StockMovement.from_bin_id == bin_id, StockMovement.to_bin_id == bin_id))
        if user_id:
            q = q.where(StockMovement.user_id == user_id)
        if date_from:
            q = q.where(StockMovement.occurred_at >= datetime.combine(date_from, datetime.min.time()).astimezone())
        if date_to:
            q = q.where(StockMovement.occurred_at <= datetime.combine(date_to, datetime.max.time()).astimezone())
        if text and text.strip():
            like = f"%{text.strip()}%"
            q = q.where(or_(
                StockMovement.doc_number.ilike(like), StockMovement.reference.ilike(like),
                StockMovement.notes.ilike(like),
                StockMovement.product_id.in_(select(Product.id).where(or_(
                    Product.internal_code.ilike(like), Product.name.ilike(like), Product.supplier_code.ilike(like)))),
                StockMovement.site_id.in_(select(Site.id).where(or_(Site.name.ilike(like),
                                                                    Site.project_number.ilike(like)))),
                StockMovement.from_bin_id.in_(select(Bin.id).where(Bin.full_code.ilike(like))),
                StockMovement.to_bin_id.in_(select(Bin.id).where(Bin.full_code.ilike(like))),
            ))
        with self.db.read_session() as s:
            return list(s.scalars(q))


def _filter_dates(rows, attr: str, date_from: date | None, date_to: date | None):
    if date_from:
        rows = [r for r in rows if getattr(r, attr).astimezone().date() >= date_from]
    if date_to:
        rows = [r for r in rows if getattr(r, attr).astimezone().date() <= date_to]
    return rows
