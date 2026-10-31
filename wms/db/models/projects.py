"""Project material lists: what a project needs, kept apart from what physically happened.

A ``ProjectMaterial`` row says "project X requires N of product Y". It is planning data only:
adding, changing or removing it never touches stock levels or the movement ledger. Reserved,
Issued and Returned for the project are always worked out from reservations and posted
movements, so the four figures can never contradict each other.

One product can be on many projects; the product master record is never copied per project.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import Base, IdMixin, TimestampMixin, fk
from wms.db.types import Quantity


class ProjectMaterial(IdMixin, TimestampMixin, Base):
    __tablename__ = "project_materials"
    __table_args__ = (
        UniqueConstraint("site_id", "product_id"),
        CheckConstraint("required_qty >= 0", name="required_not_negative"),
    )

    site_id: Mapped[int] = fk("sites.id", nullable=False, ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id", nullable=False)
    required_qty: Mapped[Decimal] = mapped_column(Quantity, nullable=False, default=Decimal(0))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")

    product = relationship("Product", lazy="joined")
    site = relationship("Site", lazy="joined")
