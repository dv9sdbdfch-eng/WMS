"""Products: raw materials, components, consumables and finished products."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, VersionMixin, fk
from wms.db.types import Money, Quantity


class Product(IdMixin, TimestampMixin, ActiveMixin, VersionMixin, Base):
    __tablename__ = "products"

    item_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    internal_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    supplier_code: Mapped[str | None] = mapped_column(String(80), index=True)
    barcode: Mapped[str | None] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = fk("categories.id")
    unit_id: Mapped[int] = fk("units.id", nullable=False)
    supplier_id: Mapped[int | None] = fk("suppliers.id")
    default_bin_id: Mapped[int | None] = fk("bins.id")
    min_stock: Mapped[Decimal | None] = mapped_column(Quantity)
    reorder_qty: Mapped[Decimal | None] = mapped_column(Quantity)
    unit_cost: Mapped[Decimal | None] = mapped_column(Money)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")

    category = relationship("Category", lazy="joined")
    unit = relationship("Unit", lazy="joined")
    supplier = relationship("Supplier", lazy="joined")
    default_bin = relationship("Bin", lazy="joined")
    finished_info: Mapped[FinishedProductInfo | None] = relationship(
        back_populates="product", uselist=False, cascade="all, delete-orphan", lazy="joined"
    )

    def __repr__(self) -> str:
        return f"<Product {self.internal_code}>"


class FinishedProductInfo(Base):
    """Extra fields for products with item_type FINISHED (1:1)."""

    __tablename__ = "finished_product_info"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    production_date: Mapped[date | None] = mapped_column(Date)
    site_id: Mapped[int | None] = fk("sites.id")
    customer: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="READY")

    product: Mapped[Product] = relationship(back_populates="finished_info")
    site = relationship("Site", lazy="joined")


class BomItem(IdMixin, Base):
    """Bill of materials line: how much of a component one finished product needs.

    Not used by v1 screens; in place so production planning can be added
    without a schema rebuild.
    """

    __tablename__ = "bom_items"

    parent_product_id: Mapped[int] = fk("products.id", nullable=False, ondelete="CASCADE")
    component_product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
