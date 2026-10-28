"""Stock documents. Each posted line links to the movement it created.

Documents are created already POSTED (stock changes in the same transaction).
They are never edited afterwards; a mistake is corrected by reversing the
document, which posts mirror-image movements and marks it REVERSED.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import Base, IdMixin, fk
from wms.db.types import Money, Quantity, UTCDateTime


class _PostedDocument:
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="POSTED", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reversal_reason: Mapped[str | None] = mapped_column(Text)


# --- Goods In ------------------------------------------------------------------

class DeliveryNote(IdMixin, _PostedDocument, Base):
    __tablename__ = "delivery_notes"
    __table_args__ = (
        # One live delivery note per supplier + number. Reversed ones can be re-entered.
        Index(
            "uq_delivery_notes_supplier_number",
            "supplier_id",
            "dn_number_norm",
            unique=True,
            sqlite_where=text("status = 'POSTED'"),
            postgresql_where=text("status = 'POSTED'"),
        ),
    )

    dn_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    dn_number_norm: Mapped[str] = mapped_column(String(80), nullable=False)
    supplier_id: Mapped[int] = fk("suppliers.id", nullable=False)
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    po_reference: Mapped[str | None] = mapped_column(String(80), index=True)
    received_by_id: Mapped[int] = fk("users.id", nullable=False)
    reversed_by_id: Mapped[int | None] = fk("users.id")

    supplier = relationship("Supplier", lazy="joined")
    received_by = relationship("User", foreign_keys=[received_by_id], lazy="joined")
    reversed_by = relationship("User", foreign_keys=[reversed_by_id], lazy="joined")
    lines: Mapped[list[DeliveryNoteLine]] = relationship(
        back_populates="delivery_note", order_by="DeliveryNoteLine.line_no", cascade="all, delete-orphan"
    )


class DeliveryNoteLine(IdMixin, Base):
    __tablename__ = "delivery_note_lines"

    delivery_note_id: Mapped[int] = fk("delivery_notes.id", nullable=False, ondelete="CASCADE")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    delivery_note: Mapped[DeliveryNote] = relationship(back_populates="lines")
    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")


# --- Goods Out -----------------------------------------------------------------

class GoodsIssue(IdMixin, _PostedDocument, Base):
    __tablename__ = "goods_issues"

    issue_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    site_id: Mapped[int] = fk("sites.id", nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(120))
    issued_by: Mapped[str] = mapped_column(String(120), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80), index=True)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    reversed_by_id: Mapped[int | None] = fk("users.id")

    site = relationship("Site", lazy="joined")
    user = relationship("User", foreign_keys=[user_id], lazy="joined")
    reversed_by = relationship("User", foreign_keys=[reversed_by_id], lazy="joined")
    lines: Mapped[list[GoodsIssueLine]] = relationship(
        back_populates="issue", order_by="GoodsIssueLine.line_no", cascade="all, delete-orphan"
    )


class GoodsIssueLine(IdMixin, Base):
    __tablename__ = "goods_issue_lines"

    issue_id: Mapped[int] = fk("goods_issues.id", nullable=False, ondelete="CASCADE")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    issue: Mapped[GoodsIssue] = relationship(back_populates="lines")
    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")


# --- Returns -------------------------------------------------------------------

class StockReturn(IdMixin, _PostedDocument, Base):
    __tablename__ = "stock_returns"

    return_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    return_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    return_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    site_id: Mapped[int | None] = fk("sites.id")
    supplier_id: Mapped[int | None] = fk("suppliers.id")
    reference: Mapped[str | None] = mapped_column(String(80), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    reversed_by_id: Mapped[int | None] = fk("users.id")

    site = relationship("Site", lazy="joined")
    supplier = relationship("Supplier", lazy="joined")
    user = relationship("User", foreign_keys=[user_id], lazy="joined")
    lines: Mapped[list[ReturnLine]] = relationship(
        back_populates="stock_return", order_by="ReturnLine.line_no", cascade="all, delete-orphan"
    )


class ReturnLine(IdMixin, Base):
    __tablename__ = "return_lines"

    return_id: Mapped[int] = fk("stock_returns.id", nullable=False, ondelete="CASCADE")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    condition: Mapped[str] = mapped_column(String(20), nullable=False, default="USABLE")
    notes: Mapped[str | None] = mapped_column(Text)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    stock_return: Mapped[StockReturn] = relationship(back_populates="lines")
    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")


# --- Adjustments and transfers ---------------------------------------------------

class StockAdjustment(IdMixin, Base):
    __tablename__ = "stock_adjustments"

    adjustment_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    previous_qty: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    new_qty: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    difference: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")
    user = relationship("User", lazy="joined")


class StockTransfer(IdMixin, Base):
    __tablename__ = "stock_transfers"

    transfer_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    from_bin_id: Mapped[int] = fk("bins.id", nullable=False)
    to_bin_id: Mapped[int] = fk("bins.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    product = relationship("Product", lazy="joined")
    from_bin = relationship("Bin", foreign_keys=[from_bin_id], lazy="joined")
    to_bin = relationship("Bin", foreign_keys=[to_bin_id], lazy="joined")
    user = relationship("User", lazy="joined")


# --- Production ------------------------------------------------------------------

class ProductionOrder(IdMixin, _PostedDocument, Base):
    """Materials consumed -> finished product produced, in one posting."""

    __tablename__ = "production_orders"

    order_number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    finished_product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    output_bin_id: Mapped[int] = fk("bins.id", nullable=False)
    site_id: Mapped[int | None] = fk("sites.id")
    production_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    output_movement_id: Mapped[int | None] = fk("stock_movements.id")
    reversed_by_id: Mapped[int | None] = fk("users.id")

    finished_product = relationship("Product", lazy="joined")
    output_bin = relationship("Bin", lazy="joined")
    site = relationship("Site", lazy="joined")
    user = relationship("User", foreign_keys=[user_id], lazy="joined")
    lines: Mapped[list[ProductionLine]] = relationship(
        back_populates="order", order_by="ProductionLine.line_no", cascade="all, delete-orphan"
    )


class ProductionLine(IdMixin, Base):
    __tablename__ = "production_lines"

    order_id: Mapped[int] = fk("production_orders.id", nullable=False, ondelete="CASCADE")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    movement_id: Mapped[int | None] = fk("stock_movements.id")

    order: Mapped[ProductionOrder] = relationship(back_populates="lines")
    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")
