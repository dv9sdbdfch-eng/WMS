"""Product Passport (spec 21): purchasing, projects and documents of one product, for its screen.

The stock part (On Hand / Reserved / Available / Incoming, bins) comes from ProductService.row and
stock_by_bin; this module adds what the warehouse asks next: where did it come from, what is still
on order, which projects took it, and which papers go with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from wms.core.enums import OPEN_PO_STATUSES, AttachmentKind, DocumentStatus, EntityType, MovementType
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import (
    Attachment,
    DeliveryNote,
    DeliveryNoteLine,
    PurchaseOrder,
    PurchaseOrderLine,
)


@dataclass
class LastDelivery:
    delivery_note_id: int
    dn_number: str
    delivery_date: date
    supplier: str
    quantity: Decimal


@dataclass
class OpenOrderLine:
    """What one open purchase order still has to bring of this product."""

    po_id: int
    po_number: str
    supplier: str
    expected_date: date | None
    ordered: Decimal
    received: Decimal

    @property
    def id(self) -> int:
        return self.po_id

    @property
    def outstanding(self) -> Decimal:
        return max(self.ordered - self.received, ZERO)

    @property
    def is_overdue(self) -> bool:
        return self.expected_date is not None and self.expected_date < date.today()


@dataclass
class ProjectUse:
    """How much of this product went to one project and came back (reversals netted off)."""

    site_id: int
    project: str
    issued: Decimal = ZERO
    returned: Decimal = ZERO
    last: datetime | None = None

    @property
    def id(self) -> int:
        return self.site_id

    @property
    def net(self) -> Decimal:
        return self.issued - self.returned


@dataclass
class RelatedDocument:
    """A file that belongs with this product: its own documents, or the delivery note / order it came on."""

    attachment: Attachment
    source: str  # "Product", "Delivery Note DN-1047", "Purchase Order PO-000012"
    entity_type: str
    entity_id: int

    @property
    def id(self) -> int:
        return self.attachment.id


@dataclass
class ProductPassport:
    last_delivery: LastDelivery | None = None
    open_orders: list[OpenOrderLine] = field(default_factory=list)
    projects: list[ProjectUse] = field(default_factory=list)
    documents: list[RelatedDocument] = field(default_factory=list)

    @property
    def outstanding(self) -> Decimal:
        return sum((o.outstanding for o in self.open_orders), ZERO)


def build_passport(ctx, product_id: int, projects: int = 10, documents: int = 30) -> ProductPassport:
    from wms.services.purchasing import PurchasingService
    from wms.services.sites import SITE_MOVEMENTS
    from wms.services.stock_ops import StockOpsService

    out = ProductPassport()
    posted = DocumentStatus.POSTED.value
    with ctx.db.read_session() as s:
        last = s.execute(
            select(DeliveryNote.id, DeliveryNote.dn_number, DeliveryNote.delivery_date,
                   DeliveryNote.supplier_name_snapshot, DeliveryNote.supplier_id, DeliveryNoteLine.quantity)
            .join(DeliveryNoteLine, DeliveryNoteLine.delivery_note_id == DeliveryNote.id)
            .where(DeliveryNoteLine.product_id == product_id, DeliveryNote.status == posted)
            .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc()).limit(1)).first()
        if last:
            supplier = last[3] or (s.get(DeliveryNote, last[0]).supplier.name)
            out.last_delivery = LastDelivery(last[0], last[1], last[2], supplier, last[5])

        lines = s.execute(
            select(PurchaseOrderLine.id, PurchaseOrderLine.quantity, PurchaseOrder)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(PurchaseOrderLine.product_id == product_id, PurchaseOrder.status.in_(OPEN_PO_STATUSES))
            .order_by(PurchaseOrder.expected_date, PurchaseOrder.id)).all()
        received = PurchasingService._received_for(s, sorted({po.id for _, _, po in lines}))
        orders: dict[int, OpenOrderLine] = {}
        for line_id, qty, po in lines:
            o = orders.setdefault(po.id, OpenOrderLine(po.id, po.po_number, po.supplier_label, po.expected_date,
                                                        ZERO, ZERO))
            o.ordered += qty
            o.received += received.get(line_id, ZERO)
        out.open_orders = [o for o in orders.values() if o.outstanding > ZERO]

        docs: list[RelatedDocument] = []
        for att in s.scalars(select(Attachment).where(
                Attachment.entity_type == EntityType.PRODUCT.value, Attachment.entity_id == product_id,
                Attachment.kind != AttachmentKind.PHOTO.value, Attachment.is_deleted.is_(False))
                .order_by(Attachment.uploaded_at.desc())):
            docs.append(RelatedDocument(att, "Product", EntityType.PRODUCT.value, product_id))
        dns = s.execute(select(DeliveryNote.id, DeliveryNote.dn_number)
                        .join(DeliveryNoteLine, DeliveryNoteLine.delivery_note_id == DeliveryNote.id)
                        .where(DeliveryNoteLine.product_id == product_id)
                        .group_by(DeliveryNote.id, DeliveryNote.dn_number)
                        .order_by(DeliveryNote.id.desc()).limit(documents)).all()
        pos = s.execute(select(PurchaseOrder.id, PurchaseOrder.po_number)
                        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
                        .where(PurchaseOrderLine.product_id == product_id)
                        .group_by(PurchaseOrder.id, PurchaseOrder.po_number)
                        .order_by(PurchaseOrder.id.desc()).limit(documents)).all()
        for etype, rows, title in ((EntityType.DELIVERY_NOTE.value, dns, "Delivery Note"),
                                   (EntityType.PURCHASE_ORDER.value, pos, "Purchase Order")):
            names = dict(rows)
            if not names:
                continue
            for att in s.scalars(select(Attachment).where(
                    Attachment.entity_type == etype, Attachment.entity_id.in_(names),
                    Attachment.is_deleted.is_(False)).order_by(Attachment.uploaded_at.desc())):
                docs.append(RelatedDocument(att, f"{title} {names[att.entity_id]}", etype, att.entity_id))
        out.documents = docs[:documents]

    if not ctx.has(P.STOCK_VIEW):
        return out
    uses: dict[int, ProjectUse] = {}
    for m in StockOpsService(ctx).movement_lines(product_id=product_id, movement_types=list(SITE_MOVEMENTS)):
        if m.site_id is None:
            continue
        u = uses.setdefault(m.site_id, ProjectUse(m.site_id, m.site_label))
        if u.last is None or m.occurred_at > u.last:
            u.last = m.occurred_at
        sign = 1 if m.from_bin_id else -1  # a reversal moves the stock the other way
        if m.movement_type == MovementType.GOODS_OUT.value:
            u.issued += sign * m.quantity
        else:
            u.returned -= sign * m.quantity
    out.projects = sorted((u for u in uses.values() if u.issued or u.returned),
                          key=lambda u: u.last, reverse=True)[:projects]
    return out
