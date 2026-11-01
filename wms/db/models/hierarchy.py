"""Project structure: Manager -> Project -> Unit -> Materials (v1.15).

The database is the source of truth; the Projects tree on screen only shows and edits it.

* ``Manager`` - a person who looks after projects. A project points at its manager with the
  nullable ``sites.manager_id``; removing a manager never removes a project (it becomes
  unassigned). ``sites.project_manager`` keeps the manager's name as a display copy, so the
  existing project list, search and exports keep working unchanged.
* ``ProjectUnit`` - one unit type a project needs ("Kitchen K01") and how many of it (24).
* ``UnitMaterial`` - one material line of a unit: a link to the existing product master record and
  the quantity one unit needs. The product is never copied: the same product can sit in any number
  of units and projects.

Total required is never stored: it is always ``unit quantity x quantity per unit`` and is worked
out by ``wms.services.project_demand``. Planning data only - nothing here changes stock or the
movement ledger.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, fk
from wms.db.types import Quantity


class Manager(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "managers"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# Names are compared ignoring case (one "John Smith", however it is typed).
Index("ix_managers_name_upper", func.upper(Manager.name))


class ProjectUnit(IdMixin, TimestampMixin, Base):
    __tablename__ = "project_units"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_not_negative"),
        Index("ix_project_units_site_order", "site_id", "sort_order"),
    )

    site_id: Mapped[int] = fk("sites.id", nullable=False, ondelete="CASCADE")
    unit_code: Mapped[str | None] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False, default=Decimal(1))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")

    site = relationship("Site", lazy="joined")


class UnitMaterial(IdMixin, TimestampMixin, Base):
    __tablename__ = "unit_materials"
    __table_args__ = (
        CheckConstraint("quantity_per_unit >= 0", name="qty_not_negative"),
        Index("ix_unit_materials_unit_order", "unit_id", "sort_order"),
    )

    unit_id: Mapped[int] = fk("project_units.id", nullable=False, ondelete="CASCADE")
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity_per_unit: Mapped[Decimal] = mapped_column(Quantity, nullable=False, default=Decimal(0))
    # A customer / designer reference for this line where one is used (e.g. a VPK code).
    vpk_code: Mapped[str | None] = mapped_column(String(80), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")

    product = relationship("Product", lazy="joined")
    unit = relationship("ProjectUnit", lazy="joined")
