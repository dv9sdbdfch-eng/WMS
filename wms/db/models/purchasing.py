"""Purchasing: purchase orders to suppliers and delivery discrepancies.

A purchase order never changes stock by itself. Stock comes in only through Goods In (delivery
notes); each delivery note line can point at the purchase order line it delivers, and the
received / outstanding quantities of an order are always worked out from those posted lines, so
they cannot drift from the stock ledger. Several deliveries against one order = partial receipts,
each with its own delivery note and stock movements.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import Base, IdMixin, fk
from wms.db.models.documents import _ClientToken, _SiteSnapshot
from wms.db.types import Money, Quantity, UTCDateTime

PO_STATUSES = ("DRAFT", "ORDERED", "PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED")


class PurchaseOrder(IdMixin, _SiteSnapshot, _ClientToken, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED')",
                        name="status_valid"),
    )

    po_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    supplier_id: Mapped[int] = fk("suppliers.id", nullable=False)
    supplier_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    site_id: Mapped[int | None] = fk("sites.id")  # project the materials are for (optional)
    order_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expected_date: Mapped[date | None] = mapped_column(Date, index=True)
    supplier_reference: Mapped[str | None] = mapped_column(String(80))  # supplier's quote / order confirmation
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int] = fk("users.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    ordered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Cancelled, or closed with quantities still outstanding (the supplier will not send the rest).
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_by_id: Mapped[int | None] = fk("users.id")
    close_reason: Mapped[str | None] = mapped_column(Text)

    supplier = relationship("Supplier", lazy="joined")
    site = relationship("Site", lazy="joined")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")
    closed_by = relationship("User", foreign_keys=[closed_by_id], lazy="joined")
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="purchase_order", order_by="PurchaseOrderLine.line_no", cascade="all, delete-orphan"
    )

    @property
    def supplier_label(self) -> str:
        return self.supplier_name_snapshot or self.supplier.name

    @property
    def closed_short(self) -> bool:
        return self.status == "RECEIVED" and self.closed_at is not None


class PurchaseOrderLine(IdMixin, Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_no"),
        CheckConstraint("quantity > 0", name="qty_positive"),
    )

    purchase_order_id: Mapped[int] = fk("purchase_orders.id", nullable=False, ondelete="CASCADE")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)  # ordered
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    notes: Mapped[str | None] = mapped_column(Text)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    product = relationship("Product", lazy="joined")


class DeliveryDiscrepancy(IdMixin, _ClientToken, Base):
    """A problem with a delivery: short, over, damaged, rejected or wrong goods.

    The kind of problem is a reason code (reason_codes with for_discrepancy). Photos are attachments
    (entity type DISCREPANCY). It records what happened and who follows it up; stock is changed only
    by the Goods In / return / adjustment that goes with it.
    """

    __tablename__ = "delivery_discrepancies"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'RESOLVED')", name="status_valid"),
        CheckConstraint("quantity >= 0", name="qty_not_negative"),
    )

    number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    supplier_id: Mapped[int] = fk("suppliers.id", nullable=False)
    supplier_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    purchase_order_id: Mapped[int | None] = fk("purchase_orders.id")
    purchase_order_line_id: Mapped[int | None] = fk("purchase_order_lines.id")
    delivery_note_id: Mapped[int | None] = fk("delivery_notes.id")
    product_id: Mapped[int | None] = fk("products.id")
    reason_code: Mapped[str] = fk("reason_codes.code", nullable=False)
    expected_qty: Mapped[Decimal | None] = mapped_column(Quantity)
    received_qty: Mapped[Decimal | None] = mapped_column(Quantity)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)  # short / over / damaged / rejected
    notes: Mapped[str | None] = mapped_column(Text)
    is_automatic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN", index=True)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by_id: Mapped[int | None] = fk("users.id")
    created_by_id: Mapped[int] = fk("users.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    supplier = relationship("Supplier", lazy="joined")
    purchase_order = relationship("PurchaseOrder", lazy="joined")
    delivery_note = relationship("DeliveryNote", lazy="joined")
    product = relationship("Product", lazy="joined")
    reason = relationship("ReasonCode", lazy="joined")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")
    resolved_by = relationship("User", foreign_keys=[resolved_by_id], lazy="joined")

    @property
    def supplier_label(self) -> str:
        return self.supplier_name_snapshot or self.supplier.name

    @property
    def kind_label(self) -> str:
        return self.reason.label if self.reason is not None else self.reason_code
