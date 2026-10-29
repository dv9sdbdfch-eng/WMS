"""Reference data: categories, units of measure, suppliers."""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, fk


class Category(IdMixin, ActiveMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("parent_id", "name"),)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    parent_id: Mapped[int | None] = fk("categories.id")
    description: Mapped[str | None] = mapped_column(Text)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id", lazy="joined", join_depth=3)

    @property
    def full_name(self) -> str:
        return f"{self.parent.full_name} / {self.name}" if self.parent else self.name


class Unit(IdMixin, ActiveMixin, Base):
    __tablename__ = "units"

    code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    allow_decimal: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Supplier(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "suppliers"

    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(Text)
    account_number: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)


class ReasonCode(IdMixin, ActiveMixin, Base):
    """Configurable reasons for adjustments, reversals, returns and delivery discrepancies.

    ``code`` is what documents store; it never changes once created. System codes are used by
    the application itself (e.g. STOCK_COUNT for stocktake approvals) and cannot be retired.
    """

    __tablename__ = "reason_codes"

    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    for_adjustment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    for_reversal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    for_return: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    for_discrepancy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(default=100, nullable=False)
