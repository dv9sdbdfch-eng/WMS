"""Goods In: delivery notes from suppliers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from wms.core.enums import DocumentStatus, EntityType, MovementType
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_money, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, DeliveryNote, DeliveryNoteLine, Product, StockMovement, Supplier
from wms.services import audit
from wms.services.attachments import AttachmentService
from wms.services.base import BaseService, check_client_token, clean_str, like_pattern
from wms.services.stock_engine import StockEngine


@dataclass
class ReceiptLine:
    product_id: int
    quantity: Decimal | str
    bin_id: int | None = None
    unit_cost: Decimal | str | None = None
    notes: str | None = None
    po_line_id: int | None = None  # order line delivered; found by product when left empty


def normalise_dn_number(value: str | None) -> str:
    """Key used for duplicate detection: 'DN 7781', 'dn-7781' and 'DN7781' are the same note."""
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def doc_datetime(d: date) -> datetime:
    """Movements for a dated document: today = now, past date = midday that day (local)."""
    if d == date.today():
        return utcnow()
    return datetime.combine(d, time(12, 0)).astimezone()


def validate_doc_date(d: date | None, field: str = "date") -> date:
    if d is None:
        raise ValidationError("Date is required.", field)
    if isinstance(d, datetime):
        d = d.date()
    if d > date.today():
        raise ValidationError("Date cannot be in the future.", field)
    if d < date.today() - timedelta(days=3650):
        raise ValidationError("Date is more than 10 years ago - please check it.", field)
    return d


class GoodsInService(BaseService):
    def find_existing(self, supplier_id: int, dn_number: str) -> DeliveryNote | None:
        norm = normalise_dn_number(dn_number)
        with self.db.read_session() as s:
            return s.scalar(select(DeliveryNote).where(DeliveryNote.supplier_id == supplier_id,
                                                       DeliveryNote.dn_number_norm == norm,
                                                       DeliveryNote.status == DocumentStatus.POSTED.value))

    def post(self, header: dict, lines: list[ReceiptLine], attachments: list[str | Path] | None = None,
             update_costs: bool = True) -> DeliveryNote:
        """Create a delivery note and receive all its lines into stock in one transaction."""
        self.require(P.GOODS_IN_POST)
        dn_number = clean_str(header.get("dn_number"), "dn_number", required=True, max_len=80)
        delivery_date = validate_doc_date(header.get("delivery_date"), "delivery_date")
        if not lines:
            raise ValidationError("Add at least one product line.")
        att_service = AttachmentService(self.ctx)
        if attachments:
            self.require(P.ATTACHMENTS_ADD)
        for f in attachments or []:
            att_service.validate_file(f)
        # Scans are copied into the store first and recorded in the same transaction as the stock,
        # so either the delivery, its lines and its scans are all saved, or none of them are.
        staged = [att_service.stage(f) for f in attachments or []]
        try:
            dn_id = self._post(header, lines, dn_number, delivery_date, update_costs, att_service, staged)
        except BaseException:
            att_service.discard(staged)
            raise
        return self.get(dn_id)

    def _post(self, header, lines, dn_number, delivery_date, update_costs, att_service, staged) -> int:
        with self.db.session() as s:
            check_client_token(s, DeliveryNote, header.get("client_token"), "dn_number",
                               EntityType.DELIVERY_NOTE.value, "delivery note")
            receiving = None
            if header.get("purchase_order_id"):
                from wms.services.purchasing import Receiving
                receiving = Receiving(s, self.ctx, header["purchase_order_id"], header.get("supplier_id"))
                header = {**header, "supplier_id": receiving.po.supplier_id}
            supplier = s.get(Supplier, header.get("supplier_id")) if header.get("supplier_id") else None
            if supplier is None:
                raise ValidationError("Supplier is required.", "supplier_id")
            if not supplier.is_active:
                raise ValidationError(f"Supplier {supplier.name} has been archived. Restore it first if you "
                                      f"still deal with them.", "supplier_id")
            norm = normalise_dn_number(dn_number)
            if s.scalar(select(DeliveryNote.id).where(DeliveryNote.supplier_id == supplier.id,
                                                      DeliveryNote.dn_number_norm == norm,
                                                      DeliveryNote.status == DocumentStatus.POSTED.value)):
                raise DuplicateError(
                    f"Delivery note '{dn_number}' from {supplier.name} has already been entered. "
                    f"Open it from Delivery Notes instead of entering it again."
                )
            dn = DeliveryNote(
                dn_number=dn_number,
                dn_number_norm=norm,
                supplier_id=supplier.id,
                supplier_name_snapshot=supplier.name,
                delivery_date=delivery_date,
                po_reference=clean_str(header.get("po_reference"), max_len=80)
                or (receiving.po.po_number if receiving else None),
                purchase_order_id=receiving.po.id if receiving else None,
                notes=clean_str(header.get("notes")),
                received_by_id=self.user.id,
                status=DocumentStatus.POSTED.value,
                client_token=header.get("client_token") or None,
            )
            s.add(dn)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(delivery_date)
            for i, ln in enumerate(lines, start=1):
                product = s.get(Product, ln.product_id)
                if product is None:
                    raise ValidationError(f"Line {i}: product not found.")
                bin_id = ln.bin_id or product.default_bin_id
                b = s.get(Bin, bin_id) if bin_id else None
                if b is None:
                    raise ValidationError(f"Line {i} ({product.internal_code}): choose a bin to put the goods in.")
                qty = to_qty(ln.quantity, f"line {i} quantity")
                cost = to_money(ln.unit_cost) if ln.unit_cost not in (None, "") else None
                if cost is not None and cost < ZERO:
                    raise ValidationError(f"Line {i}: unit cost cannot be negative.")
                mv = engine.post(
                    movement_type=MovementType.GOODS_IN, product=product, quantity=qty, to_bin=b,
                    occurred_at=when, unit_cost=cost, supplier_id=supplier.id,
                    doc_type=EntityType.DELIVERY_NOTE.value, doc_id=dn.id, doc_number=dn_number,
                    reference=dn.po_reference, notes=clean_str(ln.notes),
                )
                po_line_id = receiving.line_for(product, qty, ln.po_line_id, i) if receiving else None
                dn.lines.append(DeliveryNoteLine(line_no=i, product_id=product.id, quantity=qty, unit_cost=cost,
                                                 bin_id=b.id, notes=clean_str(ln.notes), movement_id=mv.id,
                                                 purchase_order_line_id=po_line_id))
                if update_costs and cost is not None and cost != product.unit_cost:
                    audit.record(s, self.ctx, "PRODUCT_COST_UPDATED", "PRODUCT", product.id, product.internal_code,
                                 f"Unit cost updated from delivery note {dn_number}.",
                                 old={"unit_cost": product.unit_cost}, new={"unit_cost": cost})
                    product.unit_cost = cost
            s.flush()
            if receiving:
                receiving.finish(dn)
            audit.record(s, self.ctx, "GOODS_IN_POSTED", EntityType.DELIVERY_NOTE.value, dn.id,
                         f"{dn_number} ({supplier.name})", f"{len(lines)} line(s) received.",
                         new={"dn_number": dn_number, "supplier": supplier.name, "delivery_date": delivery_date,
                              "purchase_order_id": dn.purchase_order_id,
                              "lines": [{"product_id": l.product_id, "qty": l.quantity, "bin_id": l.bin_id}
                                        for l in dn.lines]})
            for f in staged:
                att_service.attach_staged(s, EntityType.DELIVERY_NOTE.value, dn.id, f)
            return dn.id

    def reverse(self, dn_id: int, reason: str, override_reason: str | None = None,
                reason_code: str = "OTHER") -> DeliveryNote:
        self.require(P.GOODS_IN_REVERSE)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            dn = s.get(DeliveryNote, dn_id)
            if dn is None:
                raise NotFoundError("Delivery note not found.")
            if dn.status != DocumentStatus.POSTED.value:
                raise ValidationError("This delivery note has already been reversed.")
            engine = StockEngine(s, self.ctx)
            for line in dn.lines:
                mv = s.get(StockMovement, line.movement_id)
                engine.reverse(mv, f"Delivery note {dn.dn_number} reversed: {reason}", override_reason=override_reason,
                               reason_code=reason_code)
            dn.status = DocumentStatus.REVERSED.value
            dn.reversed_at = utcnow()
            dn.reversed_by_id = self.user.id
            dn.reversal_reason = reason
            from wms.services.purchasing import after_delivery_reversed
            after_delivery_reversed(s, self.ctx, dn)
            audit.record(s, self.ctx, "GOODS_IN_REVERSED", EntityType.DELIVERY_NOTE.value, dn.id, dn.dn_number,
                         reason, old={"status": "POSTED"}, new={"status": "REVERSED"})
        return self.get(dn_id)

    def update_header(self, dn_id: int, po_reference: str | None, notes: str | None) -> DeliveryNote:
        """Correct the PO reference / notes of a posted delivery note. Supplier, number, date and lines
        never change after posting (reverse and re-enter instead); every edit is audited."""
        self.require(P.GOODS_IN_POST)
        with self.db.session() as s:
            dn = s.get(DeliveryNote, dn_id)
            if dn is None:
                raise NotFoundError("Delivery note not found.")
            if dn.status != DocumentStatus.POSTED.value:
                raise ValidationError("A reversed delivery note cannot be edited.")
            old = {"po_reference": dn.po_reference, "notes": dn.notes}
            dn.po_reference = clean_str(po_reference, "po_reference", max_len=80)
            dn.notes = clean_str(notes)
            o, n = audit.diff(old, {"po_reference": dn.po_reference, "notes": dn.notes})
            if n:
                audit.record(s, self.ctx, "DELIVERY_NOTE_EDITED", EntityType.DELIVERY_NOTE.value, dn.id,
                             f"{dn.dn_number} ({dn.supplier.name})", old=o, new=n)
        return self.get(dn_id)

    def get(self, dn_id: int) -> DeliveryNote:
        with self.db.read_session() as s:
            dn = s.get(DeliveryNote, dn_id)
            if dn is None:
                raise NotFoundError("Delivery note not found.")
            _ = [(l.product, l.bin) for l in dn.lines]
            return dn

    def movements(self, dn_id: int) -> list[StockMovement]:
        with self.db.read_session() as s:
            return list(s.scalars(select(StockMovement).where(
                StockMovement.doc_type == EntityType.DELIVERY_NOTE.value, StockMovement.doc_id == dn_id
            ).order_by(StockMovement.id)))

    def search(self, text: str | None = None, supplier_id: int | None = None, date_from: date | None = None,
               date_to: date | None = None, status: str | None = None, product_id: int | None = None,
               limit: int = 1000) -> list[tuple[DeliveryNote, int, Decimal]]:
        """Returns (delivery note, line count, total value)."""
        q = (
            select(DeliveryNote, func.count(DeliveryNoteLine.id))
            .join(DeliveryNoteLine, isouter=True)
            .group_by(DeliveryNote.id)
            .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc())
            .limit(limit)
            .options(selectinload(DeliveryNote.lines))
        )
        if supplier_id:
            q = q.where(DeliveryNote.supplier_id == supplier_id)
        if date_from:
            q = q.where(DeliveryNote.delivery_date >= date_from)
        if date_to:
            q = q.where(DeliveryNote.delivery_date <= date_to)
        if status:
            q = q.where(DeliveryNote.status == status)
        if product_id:
            q = q.where(DeliveryNote.id.in_(select(DeliveryNoteLine.delivery_note_id)
                                            .where(DeliveryNoteLine.product_id == product_id)))
        if text and text.strip():
            like = like_pattern(text)
            norm = normalise_dn_number(text)
            q = q.where(or_(
                DeliveryNote.dn_number.ilike(like, escape="\\"),
                *([DeliveryNote.dn_number_norm.contains(norm, autoescape=True)] if norm else []),
                DeliveryNote.po_reference.ilike(like, escape="\\"),
                DeliveryNote.notes.ilike(like, escape="\\"),
                DeliveryNote.supplier_id.in_(select(Supplier.id).where(Supplier.name.ilike(like, escape="\\"))),
                DeliveryNote.id.in_(select(DeliveryNoteLine.delivery_note_id).join(Product).where(
                    or_(Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\"),
                        Product.supplier_code.ilike(like, escape="\\")))),
            ))
        with self.db.read_session() as s:
            rows = s.execute(q).unique().all()
            out = []
            for dn, count in rows:
                total = sum(((l.unit_cost or ZERO) * l.quantity for l in dn.lines), ZERO).quantize(Decimal("0.01"))
                out.append((dn, count, total))
            return out
