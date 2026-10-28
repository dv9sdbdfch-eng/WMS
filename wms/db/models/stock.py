"""Stock ledger and balances.

``StockMovement`` is the immutable ledger; database triggers reject UPDATE and
DELETE on it. ``StockLevel`` is the current balance per product and bin and is
only ever changed by ``wms.services.stock_engine`` in the same transaction as
the movement that caused the change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import Base, IdMixin, VersionMixin, fk
from wms.db.types import Money, Quantity, UTCDateTime


class StockLevel(IdMixin, VersionMixin, Base):
    __tablename__ = "stock_levels"
    __table_args__ = (UniqueConstraint("product_id", "bin_id"),)

    product_id: Mapped[int] = fk("products.id", nullable=False)
    bin_id: Mapped[int] = fk("bins.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

    product = relationship("Product", lazy="joined")
    bin = relationship("Bin", lazy="joined")


class StockMovement(IdMixin, Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="qty_positive"),
        CheckConstraint("from_bin_id IS NOT NULL OR to_bin_id IS NOT NULL", name="has_bin"),
        Index("ix_stock_movements_product_time", "product_id", "occurred_at"),
        Index("ix_stock_movements_doc", "doc_type", "doc_id"),
    )

    movement_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    from_bin_id: Mapped[int | None] = fk("bins.id")
    to_bin_id: Mapped[int | None] = fk("bins.id")
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    site_id: Mapped[int | None] = fk("sites.id")
    supplier_id: Mapped[int | None] = fk("suppliers.id")
    # Source document (e.g. DELIVERY_NOTE #12, number "DN 44851")
    doc_type: Mapped[str | None] = mapped_column(String(30))
    doc_id: Mapped[int | None] = mapped_column()
    doc_number: Mapped[str | None] = mapped_column(String(80), index=True)
    reference: Mapped[str | None] = mapped_column(String(120), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    reversal_of_id: Mapped[int | None] = fk("stock_movements.id")
    is_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(Text)

    product = relationship("Product", lazy="joined")
    from_bin = relationship("Bin", foreign_keys=[from_bin_id], lazy="joined")
    to_bin = relationship("Bin", foreign_keys=[to_bin_id], lazy="joined")
    site = relationship("Site", lazy="joined")
    supplier = relationship("Supplier", lazy="joined")
    user = relationship("User", lazy="joined")
