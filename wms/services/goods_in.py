"""Goods In: delivery notes from suppliers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import selectinload

from wms.core.enums import OPEN_SITE_STATUSES, DocumentStatus, EntityType, MovementType
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_money, to_money, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    Attachment,
    AuditLog,
    Bin,
    DeliveryNote,
    DeliveryNoteLine,
    Product,
    PurchaseOrderLine,
    Site,
    StockMovement,
    Supplier,
)
from wms.services import audit
from wms.services.attachments import AttachmentService
from wms.services.base import BaseService, check_client_token, clean_str, like_pattern
from wms.services.reservations import reserve_delivery, unreserve_delivery
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


# Price check on posting (v1.34, B-013): a warning only, never a block - real prices do move.
PO_PRICE_TOLERANCE = Decimal("0.25")   # more than 25 % away from the agreed purchase order price
LAST_COST_FACTOR = Decimal("3")        # without a PO: 3x the last cost or a third of it (a slipped decimal)


def _audit_cost(value) -> Decimal | None:
    return None if value in (None, "") else to_money(value)


COST_AUDIT_ACTIONS = ("PRODUCT_COST_UPDATED", "PRODUCT_COST_RESTORED", "PRODUCT_UPDATED")


def restore_costs(s, ctx, dn: DeliveryNote) -> list[str]:
    """Put back the unit cost a reversed delivery note set on its products (v1.34, B-013).

    Only when nothing has touched the cost since this note set it: a later delivery note with a unit
    cost for the product (even at the same price, which writes no cost change), a manual edit of the
    cost or another restore all leave the cost alone, and the user is told. Notes posted before v1.34
    did not record which note changed the cost, so they are left alone too. Three queries in total,
    whatever the number of lines (many PCs over VPN)."""
    pids = list(dict.fromkeys(line.product_id for line in dn.lines))
    rows: dict[int, list[AuditLog]] = {pid: [] for pid in pids}
    for row in s.scalars(select(AuditLog).where(AuditLog.action.in_(COST_AUDIT_ACTIONS),
                                                AuditLog.entity_type == "PRODUCT", AuditLog.entity_id.in_(pids))
                         .order_by(AuditLog.id)):
        rows[row.entity_id].append(row)
    later_dn = set(s.scalars(
        select(DeliveryNoteLine.product_id).join(DeliveryNote, DeliveryNote.id == DeliveryNoteLine.delivery_note_id)
        .where(DeliveryNoteLine.product_id.in_(pids), DeliveryNoteLine.unit_cost.is_not(None),
               DeliveryNote.id > dn.id, DeliveryNote.status == DocumentStatus.POSTED.value).distinct()))
    products = {p.id: p for p in s.scalars(select(Product).where(Product.id.in_(pids))
                                           .with_for_update().execution_options(populate_existing=True))}
    messages: list[str] = []
    for pid in pids:
        changes, touched_after = [], False
        for row in rows[pid]:
            new = json.loads(row.new_values or "{}")
            if row.action == "PRODUCT_COST_UPDATED" and new.get("delivery_note_id") == dn.id:
                changes.append((json.loads(row.old_values or "{}"), new))
                touched_after = False
            elif changes and "unit_cost" in new:
                touched_after = True  # another change of the cost after this note's own
        if not changes:
            continue
        product = products[pid]
        before, set_by_note = _audit_cost(changes[0][0].get("unit_cost")), _audit_cost(changes[-1][1].get("unit_cost"))
        if touched_after or pid in later_dn or product.unit_cost != set_by_note:
            messages.append(f"{product.internal_code}: unit cost not restored - the current {fmt_money(product.unit_cost)} "
                            f"was set or confirmed after delivery note {dn.dn_number}.")
            continue
        audit.record(s, ctx, "PRODUCT_COST_RESTORED", "PRODUCT", product.id, product.internal_code,
                     f"Unit cost restored: delivery note {dn.dn_number} was reversed.",
                     old={"unit_cost": product.unit_cost}, new={"unit_cost": before, "delivery_note_id": dn.id})
        product.unit_cost = before
        messages.append(f"{product.internal_code}: unit cost restored to {fmt_money(before) or 'no cost'} "
                        f"(was {fmt_money(set_by_note)} from this note).")
    return messages


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

    def price_warnings(self, lines: list[ReceiptLine], purchase_order_id: int | None = None) -> list[str]:
        """Lines whose unit cost looks wrong, to confirm before posting (warning only, v1.34).

        With a purchase order: more than 25 % away from the PO line's price. Otherwise: more than 3x
        the product's last cost, or less than a third of it."""
        self.require(P.GOODS_IN_POST)
        out: list[str] = []
        with self.db.read_session() as s:
            po_cost: dict[int, Decimal] = {}
            if purchase_order_id:
                for pid, cost in s.execute(select(PurchaseOrderLine.product_id, PurchaseOrderLine.unit_cost)
                                           .where(PurchaseOrderLine.purchase_order_id == purchase_order_id,
                                                  PurchaseOrderLine.unit_cost.is_not(None))):
                    po_cost.setdefault(pid, cost)
            products = {p.id: p for p in s.scalars(select(Product).where(
                Product.id.in_({ln.product_id for ln in lines if ln.product_id is not None})))}
            for i, ln in enumerate(lines, start=1):
                if ln.unit_cost in (None, ""):
                    continue
                cost = to_money(ln.unit_cost)
                product = products.get(ln.product_id)
                if product is None or cost <= ZERO:
                    continue
                agreed = po_cost.get(product.id)
                if agreed:
                    if abs(cost - agreed) > agreed * PO_PRICE_TOLERANCE:
                        out.append(f"Line {i} {product.internal_code}: {fmt_money(cost)} vs purchase order price "
                                   f"{fmt_money(agreed)}.")
                elif product.unit_cost and product.unit_cost > ZERO:
                    last = product.unit_cost
                    if cost > last * LAST_COST_FACTOR or cost * LAST_COST_FACTOR < last:
                        out.append(f"Line {i} {product.internal_code}: {fmt_money(cost)} vs last cost "
                                   f"{fmt_money(last)}.")
        return out

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
            site = None
            if header.get("site_id"):
                site = s.get(Site, header["site_id"])
                if site is None:
                    raise ValidationError("Project not found.", "site_id")
                if site.status not in OPEN_SITE_STATUSES:
                    raise ValidationError(f"Project {site.display} is closed - reopen it to receive materials for "
                                          f"it.", "site_id")
            dn = DeliveryNote(
                site_id=site.id if site else None,
                site_number_snapshot=site.project_number if site else None,
                site_name_snapshot=site.name if site else None,
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
                                 old={"unit_cost": product.unit_cost},
                                 new={"unit_cost": cost, "delivery_note_id": dn.id})
                    product.unit_cost = cost
            s.flush()
            if site is not None:
                totals: dict[int, Decimal] = {}
                for line in dn.lines:
                    totals[line.product_id] = totals.get(line.product_id, ZERO) + line.quantity
                reserve_delivery(s, self.ctx, dn, site, totals)
            if receiving:
                receiving.finish(dn)
            audit.record(s, self.ctx, "GOODS_IN_POSTED", EntityType.DELIVERY_NOTE.value, dn.id,
                         f"{dn_number} ({supplier.name})", f"{len(lines)} line(s) received.",
                         new={"dn_number": dn_number, "supplier": supplier.name, "delivery_date": delivery_date,
                              "purchase_order_id": dn.purchase_order_id, "site_id": dn.site_id,
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
            if dn.site_id:
                unreserve_delivery(s, self.ctx, dn, f"Delivery note {dn.dn_number} reversed: {reason}")
            cost_messages = restore_costs(s, self.ctx, dn)
            dn.status = DocumentStatus.REVERSED.value
            dn.reversed_at = utcnow()
            dn.reversed_by_id = self.user.id
            dn.reversal_reason = reason
            from wms.services.purchasing import after_delivery_reversed
            after_delivery_reversed(s, self.ctx, dn)
            audit.record(s, self.ctx, "GOODS_IN_REVERSED", EntityType.DELIVERY_NOTE.value, dn.id, dn.dn_number,
                         reason, old={"status": "POSTED"}, new={"status": "REVERSED"})
        result = self.get(dn_id)
        result.cost_messages = cost_messages  # shown to the user after the reversal (v1.34)
        return result

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
               limit: int = 1000, scan: str | None = None,
               site_id: int | None = None) -> list[tuple[DeliveryNote, int, Decimal]]:
        """Returns (delivery note, line count, total value). ``scan``: "MISSING" = no scan / photo
        attached, "PRESENT" = at least one (the Dashboard's "missing scan" counter opens this)."""
        q = (
            select(DeliveryNote, func.count(DeliveryNoteLine.id))
            .join(DeliveryNoteLine, isouter=True)
            .group_by(DeliveryNote.id)
            .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc())
            .limit(limit)
            # Lines only for the count and value: not each line's product (10 joined tables per line).
            .options(selectinload(DeliveryNote.lines).lazyload(DeliveryNoteLine.product))
        )
        if supplier_id:
            q = q.where(DeliveryNote.supplier_id == supplier_id)
        if site_id:
            q = q.where(DeliveryNote.site_id == site_id)
        if date_from:
            q = q.where(DeliveryNote.delivery_date >= date_from)
        if date_to:
            q = q.where(DeliveryNote.delivery_date <= date_to)
        if status:
            q = q.where(DeliveryNote.status == status)
        if scan in ("MISSING", "PRESENT"):
            has = exists().where(Attachment.entity_type == EntityType.DELIVERY_NOTE.value,
                                 Attachment.entity_id == DeliveryNote.id, Attachment.is_deleted.is_(False))
            q = q.where(~has if scan == "MISSING" else has)
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
