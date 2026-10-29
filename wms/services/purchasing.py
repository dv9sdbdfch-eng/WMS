"""Purchasing: purchase orders, partial deliveries against them, delivery discrepancies.

Rules
-----
* A purchase order never changes stock. Goods arrive only through Goods In; each delivery note
  line can point at the order line it delivers. Received and outstanding quantities are worked
  out from those posted lines every time (never stored), so they always agree with the ledger,
  and reversing a delivery note puts the quantity back on order automatically.
* Status: DRAFT -> ORDERED -> PARTIALLY_RECEIVED -> RECEIVED, or CANCELLED. After ordering the
  status follows the deliveries. "Close short" ends an order the supplier will not complete
  (status RECEIVED, the shortfall recorded as Missing discrepancies).
* Discrepancies record short / over / damaged / rejected / wrong goods with photos. Over-deliveries
  and products that are not on the order are recorded automatically when the delivery is posted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from wms.core.enums import (
    OPEN_PO_STATUSES,
    OPEN_SITE_STATUSES,
    DiscrepancyStatus,
    DocumentStatus,
    EntityType,
    POStatus,
    ReasonUse,
)
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_money, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import (
    DeliveryDiscrepancy,
    DeliveryNote,
    DeliveryNoteLine,
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    Site,
    Supplier,
)
from wms.services import audit
from wms.services.attachments import AttachmentService
from wms.services.base import BaseService, check_client_token, clean_str, like_pattern
from wms.services.numbering import next_number
from wms.services.reasons import require_reason


@dataclass
class POLineInput:
    product_id: int
    quantity: Decimal | str
    unit_cost: Decimal | str | None = None
    notes: str | None = None


@dataclass
class POLineView:
    line: PurchaseOrderLine
    received: Decimal

    @property
    def ordered(self) -> Decimal:
        return self.line.quantity

    @property
    def outstanding(self) -> Decimal:
        return max(self.line.quantity - self.received, ZERO)

    @property
    def over(self) -> Decimal:
        return max(self.received - self.line.quantity, ZERO)


@dataclass
class POView:
    po: PurchaseOrder
    lines: list[POLineView]
    deliveries: list[DeliveryNote] = field(default_factory=list)
    discrepancies: list[DeliveryDiscrepancy] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.po.status in OPEN_PO_STATUSES

    @property
    def value(self) -> Decimal:
        return sum(((lv.line.unit_cost or ZERO) * lv.ordered for lv in self.lines), ZERO).quantize(Decimal("0.01"))


@dataclass
class PORow:
    po: PurchaseOrder
    line_count: int
    value: Decimal
    lines_outstanding: int  # lines still waiting for goods (open orders only)
    is_overdue: bool


# --- shared with Goods In ----------------------------------------------------------------------

def received_by_line(s: Session, po_id: int | None) -> dict[int, Decimal]:
    """Quantity received per order line, from posted delivery notes only (every order when po_id is None)."""
    q = (select(DeliveryNoteLine.purchase_order_line_id, DeliveryNoteLine.quantity)
         .join(DeliveryNote, DeliveryNote.id == DeliveryNoteLine.delivery_note_id)
         .where(DeliveryNote.status == DocumentStatus.POSTED.value,
                DeliveryNoteLine.purchase_order_line_id.is_not(None)))
    if po_id is not None:
        q = q.where(DeliveryNote.purchase_order_id == po_id)
    rows = s.execute(q).all()
    out: dict[int, Decimal] = {}
    for line_id, qty in rows:
        out[line_id] = out.get(line_id, ZERO) + qty
    return out


def product_totals(po: PurchaseOrder, received: dict[int, Decimal]) -> dict[int, tuple[Decimal, Decimal]]:
    """(ordered, received) per product on the order. A product on two lines counts as one: goods
    for it complete the order whichever line they were booked against."""
    out: dict[int, tuple[Decimal, Decimal]] = {}
    for l in po.lines:
        o, r = out.get(l.product_id, (ZERO, ZERO))
        out[l.product_id] = (o + l.quantity, r + received.get(l.id, ZERO))
    return out


def status_from_receipts(po: PurchaseOrder, received: dict[int, Decimal]) -> str:
    """What the status of an ordered PO should be, given what has been received."""
    if po.status in (POStatus.DRAFT.value, POStatus.CANCELLED.value):
        return po.status
    if po.closed_at is not None:  # closed short: stays received whatever arrives later
        return POStatus.RECEIVED.value
    totals = product_totals(po, received).values()
    if totals and all(r >= o for o, r in totals):
        return POStatus.RECEIVED.value
    if any(r > ZERO for _o, r in totals):
        return POStatus.PARTIALLY_RECEIVED.value
    return POStatus.ORDERED.value


def refresh_status(s: Session, ctx, po: PurchaseOrder, why: str) -> None:
    new = status_from_receipts(po, received_by_line(s, po.id))
    if new != po.status:
        audit.record(s, ctx, "PO_STATUS_CHANGED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number, why,
                     old={"status": po.status}, new={"status": new})
        po.status = new


def new_discrepancy(s: Session, ctx, *, supplier: Supplier, reason_code: str, quantity: Decimal,
                    product_id: int | None = None, purchase_order: PurchaseOrder | None = None,
                    po_line_id: int | None = None, delivery_note: DeliveryNote | None = None,
                    expected_qty: Decimal | None = None, received_qty: Decimal | None = None,
                    notes: str | None = None, automatic: bool = False,
                    client_token: str | None = None) -> DeliveryDiscrepancy:
    d = DeliveryDiscrepancy(
        number=next_number(s, "DISCREPANCY"), supplier_id=supplier.id, supplier_name_snapshot=supplier.name,
        purchase_order_id=purchase_order.id if purchase_order else None, purchase_order_line_id=po_line_id,
        delivery_note_id=delivery_note.id if delivery_note else None, product_id=product_id,
        reason_code=reason_code, expected_qty=expected_qty, received_qty=received_qty, quantity=quantity,
        notes=notes, is_automatic=automatic, status=DiscrepancyStatus.OPEN.value, created_by_id=ctx.user.id,
        client_token=client_token or None)
    s.add(d)
    s.flush()
    where = ", ".join(x for x in (purchase_order.po_number if purchase_order else "",
                                  delivery_note.dn_number if delivery_note else "") if x)
    audit.record(s, ctx, "DISCREPANCY_RECORDED", EntityType.DISCREPANCY.value, d.id, d.number,
                 f"{reason_code} {quantity} ({supplier.name}{', ' + where if where else ''})"
                 + (" - recorded automatically" if automatic else ""),
                 new={"reason_code": reason_code, "quantity": quantity, "product_id": product_id,
                      "purchase_order_id": d.purchase_order_id, "delivery_note_id": d.delivery_note_id})
    return d


class Receiving:
    """Links the lines of one delivery note to its purchase order (used inside GoodsInService.post).

    Lines without an explicit order line go to the first line for the same product that still has
    something outstanding (or the last one, as an over-delivery). Products not on the order are
    received - they are physically here - and recorded as an Incorrect Delivery discrepancy.
    """

    def __init__(self, s: Session, ctx, po_id: int, supplier_id: int | None):
        po = s.get(PurchaseOrder, po_id)
        if po is None:
            raise ValidationError("Purchase order not found.", "purchase_order_id")
        if po.status not in OPEN_PO_STATUSES:
            raise ValidationError(f"Purchase order {po.po_number} is {POStatus(po.status).label.lower()} - goods "
                                  f"cannot be received against it.", "purchase_order_id")
        if supplier_id and supplier_id != po.supplier_id:
            raise ValidationError(f"Purchase order {po.po_number} is with {po.supplier_label}, not with the supplier "
                                  f"chosen for this delivery.", "supplier_id")
        self.s, self.ctx, self.po = s, ctx, po
        self.before = received_by_line(s, po.id)
        self.left = {l.id: l.quantity - self.before.get(l.id, ZERO) for l in po.lines}
        self.delivered: dict[int, Decimal] = {}
        self.not_on_order: list[tuple[Product, Decimal]] = []

    def line_for(self, product: Product, qty: Decimal, po_line_id: int | None, line_no: int) -> int | None:
        lines = self.po.lines
        if po_line_id:
            line = next((l for l in lines if l.id == po_line_id), None)
            if line is None:
                raise ValidationError(f"Line {line_no}: that order line is not on purchase order {self.po.po_number}.")
            if line.product_id != product.id:
                raise ValidationError(f"Line {line_no}: {product.internal_code} is not the product on that order line.")
        else:
            same = [l for l in lines if l.product_id == product.id]
            line = next((l for l in same if self.left[l.id] >= qty), None) \
                or next((l for l in same if self.left[l.id] > ZERO), same[-1] if same else None)
        if line is None:
            self.not_on_order.append((product, qty))
            return None
        self.left[line.id] -= qty
        self.delivered[line.id] = self.delivered.get(line.id, ZERO) + qty
        return line.id

    def finish(self, dn: DeliveryNote) -> list[DeliveryDiscrepancy]:
        """Record over-deliveries and wrong products, then move the order to its new status."""
        s, po, out = self.s, self.po, []
        supplier = po.supplier
        before = product_totals(po, self.before)
        for product_id, (ordered, had) in before.items():
            got = sum((q for lid, q in self.delivered.items()
                       if next(l for l in po.lines if l.id == lid).product_id == product_id), ZERO)
            if not got:
                continue
            over = max(had + got - ordered, ZERO) - max(had - ordered, ZERO)
            if over > ZERO:
                line_id = next(lid for lid in reversed(list(self.delivered))
                               if next(l for l in po.lines if l.id == lid).product_id == product_id)
                outstanding = max(ordered - had, ZERO)
                out.append(new_discrepancy(
                    s, self.ctx, supplier=supplier, reason_code="OVER_DELIVERED", quantity=over,
                    product_id=product_id, purchase_order=po, po_line_id=line_id, delivery_note=dn,
                    expected_qty=outstanding, received_qty=got, automatic=True,
                    notes=f"{got} received against {outstanding} outstanding."))
        for product, qty in self.not_on_order:
            out.append(new_discrepancy(
                s, self.ctx, supplier=supplier, reason_code="INCORRECT_DELIVERY", quantity=qty,
                product_id=product.id, purchase_order=po, delivery_note=dn, expected_qty=ZERO, received_qty=qty,
                automatic=True, notes=f"{product.internal_code} is not on purchase order {po.po_number}."))
        s.flush()
        refresh_status(s, self.ctx, po, f"Delivery note {dn.dn_number} received.")
        return out


def after_delivery_reversed(s: Session, ctx, dn: DeliveryNote) -> None:
    """A reversed delivery goes back on order; problems recorded automatically for it are closed."""
    for d in s.scalars(select(DeliveryDiscrepancy).where(
            DeliveryDiscrepancy.delivery_note_id == dn.id, DeliveryDiscrepancy.is_automatic.is_(True),
            DeliveryDiscrepancy.status == DiscrepancyStatus.OPEN.value)):
        d.status = DiscrepancyStatus.RESOLVED.value
        d.resolution = f"Delivery note {dn.dn_number} was reversed."
        d.resolved_at, d.resolved_by_id = utcnow(), ctx.user.id
        audit.record(s, ctx, "DISCREPANCY_RESOLVED", EntityType.DISCREPANCY.value, d.id, d.number, d.resolution)
    if dn.purchase_order_id:
        s.flush()
        refresh_status(s, ctx, s.get(PurchaseOrder, dn.purchase_order_id),
                       f"Delivery note {dn.dn_number} reversed.")


# --- service ------------------------------------------------------------------------------------

class PurchasingService(BaseService):
    # -- purchase orders -------------------------------------------------------------------------
    def save(self, po_id: int | None, header: dict, lines: list[POLineInput]) -> PurchaseOrder:
        """Create a draft order, or change one that is not finished yet.

        Lines are matched to the existing order lines by product (in order). Lines that already had
        deliveries keep their product, cannot go below what was received and cannot be removed.
        """
        self.require(P.PURCHASING_EDIT)
        with self.db.session() as s:
            if po_id is None:
                check_client_token(s, PurchaseOrder, header.get("client_token"), "po_number",
                                   EntityType.PURCHASE_ORDER.value, "purchase order")
                po = PurchaseOrder(po_number=next_number(s, "PURCHASE_ORDER"), status=POStatus.DRAFT.value,
                                   created_by_id=self.user.id, client_token=header.get("client_token") or None)
                old = {}
            else:
                po = self._po(s, po_id)
                if po.status not in (POStatus.DRAFT.value, *OPEN_PO_STATUSES):
                    raise ValidationError(f"Purchase order {po.po_number} is {POStatus(po.status).label.lower()} "
                                          f"and can no longer be changed.")
                old = self._snapshot(po)
            self._apply_header(s, po, header)
            if po_id is None:
                s.add(po)
                s.flush()
            self._apply_lines(s, po, lines)
            s.flush()
            s.refresh(po)
            if po.status != POStatus.DRAFT.value:
                refresh_status(s, self.ctx, po, "Order changed.")
            if po_id is None:
                audit.record(s, self.ctx, "PO_CREATED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number,
                             f"Draft for {po.supplier_label}, {len(po.lines)} line(s).", new=self._snapshot(po))
            else:
                o, n = audit.diff(old, self._snapshot(po))
                if n or o:
                    audit.record(s, self.ctx, "PO_EDITED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number,
                                 old=o, new=n)
            new_id = po.id
        return self.get(new_id).po

    def _apply_header(self, s: Session, po: PurchaseOrder, header: dict) -> None:
        from wms.services.goods_in import validate_doc_date
        supplier_id = header.get("supplier_id") or po.supplier_id
        if po.id is not None and po.status != POStatus.DRAFT.value and supplier_id != po.supplier_id:
            raise ValidationError("The supplier cannot be changed after the order has been placed - cancel it and "
                                  "raise a new order instead.", "supplier_id")
        supplier = s.get(Supplier, supplier_id) if supplier_id else None
        if supplier is None:
            raise ValidationError("Supplier is required.", "supplier_id")
        if supplier_id != po.supplier_id and not supplier.is_active:
            raise ValidationError(f"Supplier {supplier.name} has been archived. Restore it first if you still "
                                  f"deal with them.", "supplier_id")
        po.supplier_id, po.supplier_name_snapshot = supplier.id, supplier.name
        po.order_date = validate_doc_date(header.get("order_date") or po.order_date or date.today(), "order_date")
        expected = header.get("expected_date") if "expected_date" in header else po.expected_date
        if expected is not None and expected < po.order_date:
            raise ValidationError("Expected delivery date cannot be before the order date.", "expected_date")
        po.expected_date = expected
        site_id = header.get("site_id") if "site_id" in header else po.site_id
        if site_id != po.site_id and site_id:
            site = s.get(Site, site_id)
            if site is None:
                raise ValidationError("Project not found.", "site_id")
            if site.status not in OPEN_SITE_STATUSES:
                raise ValidationError(f"Project {site.display} is closed - reopen it to order for it.", "site_id")
            po.site_number_snapshot, po.site_name_snapshot = site.project_number, site.name
        elif not site_id:
            po.site_number_snapshot = po.site_name_snapshot = None
        po.site_id = site_id or None
        if "supplier_reference" in header:
            po.supplier_reference = clean_str(header.get("supplier_reference"), "supplier_reference", max_len=80)
        if "notes" in header:
            po.notes = clean_str(header.get("notes"))

    def _apply_lines(self, s: Session, po: PurchaseOrder, lines: list[POLineInput]) -> None:
        received = received_by_line(s, po.id) if po.id else {}
        existing = list(po.lines)
        used: set[int] = set()
        next_no = max((l.line_no for l in existing), default=0) + 1
        for i, ln in enumerate(lines, start=1):
            product = s.get(Product, ln.product_id)
            if product is None:
                raise ValidationError(f"Line {i}: product not found.")
            qty = to_qty(ln.quantity, f"line {i} quantity")
            if qty <= ZERO:
                raise ValidationError(f"Line {i} ({product.internal_code}): quantity must be greater than zero.")
            if not product.unit.allow_decimal and qty != qty.to_integral_value():
                raise ValidationError(f"Line {i} ({product.internal_code}): {product.unit.code} quantities must "
                                      f"be whole numbers.")
            cost = to_money(ln.unit_cost) if ln.unit_cost not in (None, "") else product.unit_cost
            if cost is not None and cost < ZERO:
                raise ValidationError(f"Line {i}: unit cost cannot be negative.")
            line = next((l for l in existing if l.product_id == product.id and l.id not in used), None)
            if line is None:
                if not product.is_active:
                    raise ValidationError(f"Line {i}: {product.internal_code} has been archived and cannot be "
                                          f"ordered.")
                line = PurchaseOrderLine(line_no=next_no, product_id=product.id)
                next_no += 1
                po.lines.append(line)
            else:
                used.add(line.id)
                got = received.get(line.id, ZERO)
                if qty < got:
                    raise ValidationError(f"Line {i} ({product.internal_code}): {got} has already been received, so "
                                          f"the ordered quantity cannot be less than that.")
            line.quantity, line.unit_cost, line.notes = qty, cost, clean_str(ln.notes)
        for line in existing:
            if line.id not in used:
                if received.get(line.id, ZERO) > ZERO:
                    raise ValidationError(f"{line.product.internal_code} has already been delivered against this "
                                          f"order, so its line cannot be removed.")
                po.lines.remove(line)

    @staticmethod
    def _snapshot(po: PurchaseOrder) -> dict:
        return {"supplier": po.supplier_name_snapshot, "order_date": po.order_date,
                "expected_date": po.expected_date, "site_id": po.site_id,
                "supplier_reference": po.supplier_reference, "notes": po.notes,
                "lines": [{"product_id": l.product_id, "qty": l.quantity, "unit_cost": l.unit_cost}
                          for l in po.lines]}

    def mark_ordered(self, po_id: int) -> PurchaseOrder:
        """The order has been sent to the supplier: goods can now be received against it."""
        self.require(P.PURCHASING_EDIT)
        with self.db.session() as s:
            po = self._po(s, po_id)
            if po.status != POStatus.DRAFT.value:
                raise ValidationError(f"Purchase order {po.po_number} has already been placed.")
            if not po.lines:
                raise ValidationError("Add at least one product line before placing the order.")
            po.status, po.ordered_at = POStatus.ORDERED.value, utcnow()
            audit.record(s, self.ctx, "PO_ORDERED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number,
                         f"Placed with {po.supplier_label}.", old={"status": "DRAFT"}, new={"status": "ORDERED"})
        return self.get(po_id).po

    def cancel(self, po_id: int, reason: str) -> PurchaseOrder:
        """Cancel an order nothing has arrived against (otherwise use close_short)."""
        self.require(P.PURCHASING_EDIT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            po = self._po(s, po_id)
            if po.status not in (POStatus.DRAFT.value, POStatus.ORDERED.value):
                raise ValidationError(f"Purchase order {po.po_number} is {POStatus(po.status).label.lower()} and "
                                      f"cannot be cancelled." + (" Use 'Close short' to stop waiting for the rest."
                                                                if po.status == POStatus.PARTIALLY_RECEIVED.value
                                                                else ""))
            if any(q > ZERO for q in received_by_line(s, po.id).values()):
                raise ValidationError("Goods have been received against this order - use 'Close short' instead.")
            old = po.status
            po.status, po.closed_at, po.closed_by_id, po.close_reason = (
                POStatus.CANCELLED.value, utcnow(), self.user.id, reason)
            audit.record(s, self.ctx, "PO_CANCELLED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number, reason,
                         old={"status": old}, new={"status": "CANCELLED"})
        return self.get(po_id).po

    def close_short(self, po_id: int, reason: str, record_missing: bool = True) -> PurchaseOrder:
        """Stop waiting for the rest of a part-delivered order. The shortfall is recorded as
        Missing discrepancies (to chase a credit note) unless ``record_missing`` is False."""
        self.require(P.PURCHASING_EDIT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            po = self._po(s, po_id)
            if po.status not in OPEN_PO_STATUSES:
                raise ValidationError(f"Purchase order {po.po_number} is not waiting for goods.")
            received = received_by_line(s, po.id)
            if not any(q > ZERO for q in received.values()):
                raise ValidationError("Nothing has been received against this order - cancel it instead.")
            if record_missing:
                for line in po.lines:
                    short = line.quantity - received.get(line.id, ZERO)
                    if short > ZERO:
                        new_discrepancy(s, self.ctx, supplier=po.supplier, reason_code="MISSING", quantity=short,
                                        product_id=line.product_id, purchase_order=po, po_line_id=line.id,
                                        expected_qty=line.quantity, received_qty=received.get(line.id, ZERO),
                                        notes=f"Order closed short: {reason}", automatic=True)
            old = po.status
            po.status, po.closed_at, po.closed_by_id, po.close_reason = (
                POStatus.RECEIVED.value, utcnow(), self.user.id, reason)
            audit.record(s, self.ctx, "PO_CLOSED_SHORT", EntityType.PURCHASE_ORDER.value, po.id, po.po_number, reason,
                         old={"status": old}, new={"status": "RECEIVED"})
        return self.get(po_id).po

    def reopen(self, po_id: int) -> PurchaseOrder:
        """Undo 'Close short': the outstanding quantities are expected again."""
        self.require(P.PURCHASING_EDIT)
        with self.db.session() as s:
            po = self._po(s, po_id)
            if not po.closed_short:
                raise ValidationError("Only an order that was closed short can be reopened.")
            po.closed_at = po.closed_by_id = po.close_reason = None
            refresh_status(s, self.ctx, po, "Reopened.")
            audit.record(s, self.ctx, "PO_REOPENED", EntityType.PURCHASE_ORDER.value, po.id, po.po_number)
        return self.get(po_id).po

    @staticmethod
    def _po(s: Session, po_id: int) -> PurchaseOrder:
        po = s.get(PurchaseOrder, po_id)
        if po is None:
            raise NotFoundError("Purchase order not found.")
        return po

    def get(self, po_id: int) -> POView:
        with self.db.read_session() as s:
            po = self._po(s, po_id)
            received = received_by_line(s, po.id)
            _ = [(l.product.unit, l.product.default_bin_id) for l in po.lines]
            deliveries = list(s.scalars(select(DeliveryNote).where(DeliveryNote.purchase_order_id == po.id)
                                        .order_by(DeliveryNote.delivery_date, DeliveryNote.id)
                                        .options(selectinload(DeliveryNote.lines))))
            discrepancies = list(s.scalars(select(DeliveryDiscrepancy)
                                           .where(DeliveryDiscrepancy.purchase_order_id == po.id)
                                           .order_by(DeliveryDiscrepancy.id)))
            return POView(po, [POLineView(l, received.get(l.id, ZERO)) for l in po.lines], deliveries, discrepancies)

    def list(self, text: str | None = None, status: str | None = None, supplier_id: int | None = None,
             open_only: bool = False, date_from: date | None = None, date_to: date | None = None,
             limit: int = 2000) -> list[PORow]:
        q = (select(PurchaseOrder).options(selectinload(PurchaseOrder.lines).lazyload(PurchaseOrderLine.product))
             .order_by(PurchaseOrder.order_date.desc(), PurchaseOrder.id.desc()).limit(limit))
        if status:
            q = q.where(PurchaseOrder.status == status)
        if open_only:
            q = q.where(PurchaseOrder.status.in_(OPEN_PO_STATUSES))
        if supplier_id:
            q = q.where(PurchaseOrder.supplier_id == supplier_id)
        if date_from:
            q = q.where(PurchaseOrder.order_date >= date_from)
        if date_to:
            q = q.where(PurchaseOrder.order_date <= date_to)
        if text and text.strip():
            like = like_pattern(text)
            q = q.where(or_(
                PurchaseOrder.po_number.ilike(like, escape="\\"),
                PurchaseOrder.supplier_reference.ilike(like, escape="\\"),
                PurchaseOrder.notes.ilike(like, escape="\\"),
                PurchaseOrder.supplier_name_snapshot.ilike(like, escape="\\"),
                PurchaseOrder.site_number_snapshot.ilike(like, escape="\\"),
                PurchaseOrder.site_name_snapshot.ilike(like, escape="\\"),
                PurchaseOrder.id.in_(select(PurchaseOrderLine.purchase_order_id).join(Product).where(
                    or_(Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\"),
                        Product.supplier_code.ilike(like, escape="\\")))),
            ))
        today = date.today()
        with self.db.read_session() as s:
            orders = list(s.scalars(q).unique())
            received = self._received_for(s, [po.id for po in orders if po.status in OPEN_PO_STATUSES])
            out = []
            for po in orders:
                waiting = sum(1 for l in po.lines if po.status in OPEN_PO_STATUSES
                              and received.get(l.id, ZERO) < l.quantity)
                value = sum(((l.unit_cost or ZERO) * l.quantity for l in po.lines), ZERO).quantize(Decimal("0.01"))
                overdue = bool(waiting and po.expected_date and po.expected_date < today)
                out.append(PORow(po, len(po.lines), value, waiting, overdue))
            return out

    @staticmethod
    def _received_for(s: Session, po_ids: list[int]) -> dict[int, Decimal]:
        if not po_ids:
            return {}
        rows = s.execute(
            select(DeliveryNoteLine.purchase_order_line_id, DeliveryNoteLine.quantity)
            .join(DeliveryNote, DeliveryNote.id == DeliveryNoteLine.delivery_note_id)
            .where(DeliveryNote.purchase_order_id.in_(po_ids), DeliveryNote.status == DocumentStatus.POSTED.value,
                   DeliveryNoteLine.purchase_order_line_id.is_not(None))).all()
        out: dict[int, Decimal] = {}
        for line_id, qty in rows:
            out[line_id] = out.get(line_id, ZERO) + qty
        return out

    def open_orders(self, supplier_id: int | None = None) -> list[PurchaseOrder]:
        """Orders goods can be received against (Goods In picker)."""
        return [r.po for r in self.list(supplier_id=supplier_id, open_only=True)]

    def incoming(self, product_ids: list[int] | None = None) -> dict[int, Decimal]:
        """Quantity on order and not yet delivered, per product (open orders only)."""
        q = (select(PurchaseOrderLine).join(PurchaseOrder)
             .where(PurchaseOrder.status.in_(OPEN_PO_STATUSES)))
        if product_ids is not None:
            q = q.where(PurchaseOrderLine.product_id.in_(product_ids))
        with self.db.read_session() as s:
            lines = list(s.scalars(q))
            received = self._received_for(s, sorted({l.purchase_order_id for l in lines}))
            per_order: dict[tuple[int, int], Decimal] = {}  # (order, product) -> outstanding
            for l in lines:
                key = (l.purchase_order_id, l.product_id)
                per_order[key] = per_order.get(key, ZERO) + l.quantity - received.get(l.id, ZERO)
            out: dict[int, Decimal] = {}
            for (_po, product_id), left in per_order.items():
                if left > ZERO:
                    out[product_id] = out.get(product_id, ZERO) + left
            return out

    def draft_from_reorder(self, product_ids: list[int]) -> tuple[list[PurchaseOrder], list[str]]:
        """Suggested orders for the Reorder Required list: one DRAFT purchase order per preferred
        supplier with the suggested quantities. Nothing is sent - the drafts are checked and placed
        by hand. Returns (drafts, products skipped with the reason)."""
        self.require(P.PURCHASING_EDIT)
        from wms.services.products import ProductService
        rows = {r.product.id: r for r in ProductService(self.ctx).list() if r.product.id in set(product_ids)}
        with self.db.read_session() as s:
            in_draft = dict(s.execute(select(PurchaseOrderLine.product_id, PurchaseOrder.po_number)
                                      .join(PurchaseOrder)
                                      .where(PurchaseOrder.status == POStatus.DRAFT.value)).all())
        by_supplier: dict[int, list[POLineInput]] = {}
        skipped = []
        for pid in product_ids:
            r = rows.get(pid)
            if r is None:
                continue
            code = r.product.internal_code
            if not r.product.supplier_id:
                skipped.append(f"{code}: no supplier set on the product")
            elif pid in in_draft:
                skipped.append(f"{code}: already on draft order {in_draft[pid]}")
            elif r.shortfall <= ZERO:
                skipped.append(f"{code}: enough already on order")
            else:
                qty = r.shortfall
                if not r.product.unit.allow_decimal:
                    qty = qty.to_integral_value(rounding="ROUND_CEILING")
                by_supplier.setdefault(r.product.supplier_id, []).append(POLineInput(pid, qty))
        drafts = [self.save(None, {"supplier_id": sid, "notes": "Suggested from the Reorder Required list."}, lines)
                  for sid, lines in by_supplier.items()]
        return drafts, skipped

    def overdue(self) -> list[PORow]:
        return [r for r in self.list(open_only=True) if r.is_overdue]

    # -- discrepancies -----------------------------------------------------------------------------
    def record_discrepancy(self, data: dict, photos: list[str | Path] | None = None) -> DeliveryDiscrepancy:
        """Record a delivery problem by hand (with photos). The supplier comes from the delivery note
        or order when one is given."""
        if not (self.ctx.has(P.GOODS_IN_POST) or self.ctx.has(P.PURCHASING_EDIT)):
            self.require(P.PURCHASING_EDIT)
        att = AttachmentService(self.ctx)
        if photos:
            self.require(P.ATTACHMENTS_ADD)
        for f in photos or []:
            att.validate_file(f)
        staged = [att.stage(f) for f in photos or []]
        try:
            with self.db.session() as s:
                check_client_token(s, DeliveryDiscrepancy, data.get("client_token"), "number",
                                   EntityType.DISCREPANCY.value, "delivery problem")
                dn = s.get(DeliveryNote, data["delivery_note_id"]) if data.get("delivery_note_id") else None
                po = s.get(PurchaseOrder, data["purchase_order_id"]) if data.get("purchase_order_id") else None
                if po is None and dn is not None and dn.purchase_order_id:
                    po = s.get(PurchaseOrder, dn.purchase_order_id)
                supplier_id = (dn.supplier_id if dn else None) or (po.supplier_id if po else None) \
                    or data.get("supplier_id")
                if dn and po and dn.purchase_order_id not in (None, po.id):
                    raise ValidationError(f"Delivery note {dn.dn_number} is not for purchase order {po.po_number}.")
                if (dn and po and dn.supplier_id != po.supplier_id) or (
                        data.get("supplier_id") and supplier_id != data.get("supplier_id")):
                    raise ValidationError("The supplier does not match the delivery note / purchase order.",
                                          "supplier_id")
                supplier = s.get(Supplier, supplier_id) if supplier_id else None
                if supplier is None:
                    raise ValidationError("Choose the supplier, delivery note or purchase order.", "supplier_id")
                rc = require_reason(s, data.get("reason_code"), ReasonUse.DISCREPANCY)
                product_id = data.get("product_id") or None
                if product_id and s.get(Product, product_id) is None:
                    raise ValidationError("Product not found.", "product_id")
                qty = to_qty(data.get("quantity") if data.get("quantity") not in (None, "") else "0", "quantity")
                if qty < ZERO:
                    raise ValidationError("Quantity cannot be negative.", "quantity")
                expected = data.get("expected_qty")
                received = data.get("received_qty")
                po_line_id = None
                if po is not None and product_id:
                    po_line_id = next((l.id for l in po.lines if l.product_id == product_id), None)
                d = new_discrepancy(
                    s, self.ctx, supplier=supplier, reason_code=rc.code, quantity=qty, product_id=product_id,
                    purchase_order=po, po_line_id=po_line_id, delivery_note=dn,
                    expected_qty=to_qty(expected, "expected_qty") if expected not in (None, "") else None,
                    received_qty=to_qty(received, "received_qty") if received not in (None, "") else None,
                    notes=clean_str(data.get("notes")), client_token=data.get("client_token"))
                for f in staged:
                    att.attach_staged(s, EntityType.DISCREPANCY.value, d.id, f)
                new_id = d.id
        except BaseException:
            att.discard(staged)
            raise
        return self.get_discrepancy(new_id)

    def resolve_discrepancy(self, discrepancy_id: int, resolution: str) -> DeliveryDiscrepancy:
        """Close a problem: credit note received, replacement delivered, accepted as is..."""
        self.require(P.PURCHASING_EDIT)
        resolution = clean_str(resolution, "resolution", required=True)
        with self.db.session() as s:
            d = s.get(DeliveryDiscrepancy, discrepancy_id)
            if d is None:
                raise NotFoundError("Delivery problem not found.")
            if d.status != DiscrepancyStatus.OPEN.value:
                raise ValidationError(f"{d.number} has already been resolved.")
            d.status, d.resolution, d.resolved_at, d.resolved_by_id = (
                DiscrepancyStatus.RESOLVED.value, resolution, utcnow(), self.user.id)
            audit.record(s, self.ctx, "DISCREPANCY_RESOLVED", EntityType.DISCREPANCY.value, d.id, d.number, resolution,
                         old={"status": "OPEN"}, new={"status": "RESOLVED"})
        return self.get_discrepancy(discrepancy_id)

    def get_discrepancy(self, discrepancy_id: int) -> DeliveryDiscrepancy:
        with self.db.read_session() as s:
            d = s.get(DeliveryDiscrepancy, discrepancy_id)
            if d is None:
                raise NotFoundError("Delivery problem not found.")
            _ = (d.product.unit if d.product else None, d.purchase_order, d.delivery_note)
            return d

    def discrepancies(self, text: str | None = None, status: str | None = None, supplier_id: int | None = None,
                      purchase_order_id: int | None = None, delivery_note_id: int | None = None,
                      date_from: date | None = None, date_to: date | None = None,
                      limit: int = 2000) -> list[DeliveryDiscrepancy]:
        q = select(DeliveryDiscrepancy).order_by(DeliveryDiscrepancy.id.desc()).limit(limit)
        if status:
            q = q.where(DeliveryDiscrepancy.status == status)
        if supplier_id:
            q = q.where(DeliveryDiscrepancy.supplier_id == supplier_id)
        if purchase_order_id:
            q = q.where(DeliveryDiscrepancy.purchase_order_id == purchase_order_id)
        if delivery_note_id:
            q = q.where(DeliveryDiscrepancy.delivery_note_id == delivery_note_id)
        if date_from:
            q = q.where(DeliveryDiscrepancy.created_at >= datetime.combine(date_from, time(0)).astimezone())
        if date_to:
            q = q.where(DeliveryDiscrepancy.created_at
                        < datetime.combine(date_to + timedelta(days=1), time(0)).astimezone())
        if text and text.strip():
            like = like_pattern(text)
            q = q.where(or_(
                DeliveryDiscrepancy.number.ilike(like, escape="\\"),
                DeliveryDiscrepancy.notes.ilike(like, escape="\\"),
                DeliveryDiscrepancy.supplier_name_snapshot.ilike(like, escape="\\"),
                DeliveryDiscrepancy.purchase_order_id.in_(
                    select(PurchaseOrder.id).where(PurchaseOrder.po_number.ilike(like, escape="\\"))),
                DeliveryDiscrepancy.delivery_note_id.in_(
                    select(DeliveryNote.id).where(DeliveryNote.dn_number.ilike(like, escape="\\"))),
                DeliveryDiscrepancy.product_id.in_(select(Product.id).where(
                    or_(Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\")))),
            ))
        with self.db.read_session() as s:
            rows = list(s.scalars(q).unique())
            _ = [d.product.unit for d in rows if d.product]
            return rows
