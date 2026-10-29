"""Stock ledger and balances.

``StockMovement`` is the immutable ledger; database triggers reject UPDATE and
DELETE on it. Each movement also keeps a *snapshot* of the names that were true when it was
posted (product code and name, bin codes, project, supplier, user), so renaming, archiving or
removing a bin, project, product or supplier later never changes what history shows. ``StockLevel`` is the current balance per product and bin and is
only ever changed by ``wms.services.stock_engine`` in the same transaction as
the movement that caused the change.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Index, String, Text, UniqueConstraint, text
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
        # A movement can be reversed once only (migration 0005).
        Index("uq_stock_movements_reversal_of", "reversal_of_id", unique=True,
              sqlite_where=text("reversal_of_id IS NOT NULL"),
              postgresql_where=text("reversal_of_id IS NOT NULL")),
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
    # What the records were called at the time (filled by StockEngine.post, never changed).
    product_code_snapshot: Mapped[str | None] = mapped_column(String(50))
    product_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    from_bin_name_snapshot: Mapped[str | None] = mapped_column(String(450))
    to_bin_name_snapshot: Mapped[str | None] = mapped_column(String(450))
    site_number_snapshot: Mapped[str | None] = mapped_column(String(50))
    site_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    supplier_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    user_name_snapshot: Mapped[str | None] = mapped_column(String(120))

    product = relationship("Product", lazy="joined")
    from_bin = relationship("Bin", foreign_keys=[from_bin_id], lazy="joined")
    to_bin = relationship("Bin", foreign_keys=[to_bin_id], lazy="joined")
    site = relationship("Site", lazy="joined")
    supplier = relationship("Supplier", lazy="joined")
    user = relationship("User", lazy="joined")

    # -- historical display values: the snapshot, or the live record for rows that have none ----
    @property
    def type_label(self) -> str:
        """e.g. "Goods Out", or "Reversal of Goods Out" for the mirror movement of a reversal."""
        from wms.core.enums import MovementType
        label = MovementType(self.movement_type).label
        return f"Reversal of {label}" if self.reversal_of_id else label

    @property
    def product_code(self) -> str:
        return self.product_code_snapshot or self.product.internal_code

    @property
    def product_name(self) -> str:
        return self.product_name_snapshot or self.product.name

    @property
    def from_bin_code(self) -> str:
        if self.from_bin_id is None:
            return ""
        return self.from_bin_name_snapshot or self.from_bin.full_code

    @property
    def to_bin_code(self) -> str:
        if self.to_bin_id is None:
            return ""
        return self.to_bin_name_snapshot or self.to_bin.full_code

    def bin_code_of(self, bin_id: int | None) -> str:
        """Historical code of whichever side of this movement is ``bin_id``."""
        if bin_id is not None and bin_id == self.from_bin_id:
            return self.from_bin_code
        if bin_id is not None and bin_id == self.to_bin_id:
            return self.to_bin_code
        return ""

    @property
    def site_number(self) -> str:
        if self.site_id is None:
            return ""
        return self.site_number_snapshot or self.site.project_number

    @property
    def site_label(self) -> str:
        """'P-1001 - Riverside Apartments' as it was when the movement was posted."""
        if self.site_id is None:
            return ""
        if self.site_number_snapshot or self.site_name_snapshot:
            return site_label(self.site_number_snapshot, self.site_name_snapshot)
        return self.site.display

    @property
    def supplier_label(self) -> str:
        if self.supplier_id is None:
            return ""
        return self.supplier_name_snapshot or self.supplier.name

    @property
    def user_label(self) -> str:
        return self.user_name_snapshot or self.user.full_name


def site_label(number: str | None, name: str | None) -> str:
    return " - ".join(x for x in (number, name) if x)


class MovementReversal(IdMixin, Base):
    """Links a ledger movement to the movement that reversed it, with who, when and why.

    Append-only like the ledger itself. One original can be reversed once (UNIQUE), and a
    reversal is never reversed again - post a new movement instead.
    """

    __tablename__ = "movement_reversals"

    original_movement_id: Mapped[int] = fk("stock_movements.id", nullable=False, index=False)
    reversal_movement_id: Mapped[int] = fk("stock_movements.id", nullable=False, index=False)
    reason_code: Mapped[str] = fk("reason_codes.code", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reversed_by_id: Mapped[int] = fk("users.id", nullable=False)
    reversed_by_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    reversed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("original_movement_id"),
        UniqueConstraint("reversal_movement_id"),
        CheckConstraint("original_movement_id <> reversal_movement_id", name="not_self"),
    )

    original = relationship("StockMovement", foreign_keys=[original_movement_id], lazy="joined")
    reversal = relationship("StockMovement", foreign_keys=[reversal_movement_id], lazy="joined")
    reversed_by = relationship("User", lazy="joined")
