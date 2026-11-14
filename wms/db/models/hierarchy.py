"""Project structure: Manager -> Project -> [Block -> Level ->] Unit -> Materials (v1.15).

The database is the source of truth; the Projects tree on screen only shows and edits it.

* ``Manager`` - a person who looks after projects. A project points at its manager with the
  nullable ``sites.manager_id``; removing a manager never removes a project (it becomes
  unassigned). ``sites.project_manager`` keeps the manager's name as a display copy, so the
  existing project list, search and exports keep working unchanged.
* ``ProjectBlock`` / ``ProjectLevel`` - optional grouping of a project's units by block and level
  (floor). A unit with no level sits directly in the project.
* ``ProjectUnit`` - one unit type a project needs ("Kitchen K01") and how many of it (24).
* ``UnitMaterial`` - one material line of a unit: a link to the existing product master record and
  the quantity one unit needs. The product is never copied: the same product can sit in any number
  of units and projects.

* ``UnitMaterialNorm`` (v1.23) - the history of a line's norm (quantity per unit): every value it
  ever had, when it changed, who changed it and whether the change was meant for ALL units of the
  unit type (units already made are recalculated) or only for the FUTURE ones (units already made
  keep the norm they were made with).
* ``UnitCompletion`` (v1.23) - "N units of this unit type were made / installed on <date>". Together
  with the norm history this says which norm each finished unit used.

Total required is never stored: it is always ``unit quantity x quantity per unit`` and is worked
out by ``wms.services.project_demand``. Planning data only - nothing here changes stock or the
movement ledger.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, fk
from wms.db.types import Quantity, UTCDateTime


class Manager(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "managers"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# Names are compared ignoring case (one "John Smith", however it is typed).
Index("ix_managers_name_upper", func.upper(Manager.name))


class ProjectBlock(IdMixin, TimestampMixin, Base):
    """A block / building of a project ("Block A"). Optional: small projects hold units directly."""

    __tablename__ = "project_blocks"
    __table_args__ = (Index("ix_project_blocks_site_order", "site_id", "sort_order"),)

    site_id: Mapped[int] = fk("sites.id", nullable=False, ondelete="CASCADE")
    code: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")


class ProjectLevel(IdMixin, TimestampMixin, Base):
    """A level / floor inside a block ("Level 3"). Units can sit on a level."""

    __tablename__ = "project_levels"
    __table_args__ = (Index("ix_project_levels_block_order", "block_id", "sort_order"),)

    block_id: Mapped[int] = fk("project_blocks.id", nullable=False, ondelete="CASCADE")
    code: Mapped[str | None] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int | None] = fk("users.id")


class ProjectUnit(IdMixin, TimestampMixin, Base):
    __tablename__ = "project_units"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_not_negative"),
        Index("ix_project_units_site_order", "site_id", "sort_order"),
    )

    site_id: Mapped[int] = fk("sites.id", nullable=False, ondelete="CASCADE")
    # Empty: the unit sits directly in the project. Otherwise the level (of a block of the same project).
    level_id: Mapped[int | None] = fk("project_levels.id", ondelete="CASCADE")
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


#: what a norm change applies to
NORM_ALL = "ALL"        # every unit, also those already made (they are recalculated)
NORM_FUTURE = "FUTURE"  # only units not made yet; units already made keep their norm


class UnitMaterialNorm(IdMixin, Base):
    """One value of a material line's norm (quantity per unit) and when it was set. Append-only.

    The line's current norm is always ``unit_materials.quantity_per_unit``; this table keeps how it
    got there. The first row of a line is written when the line is added (``applies_to`` ALL).
    """

    __tablename__ = "unit_material_norms"
    __table_args__ = (
        CheckConstraint("quantity_per_unit >= 0", name="qty_not_negative"),
        CheckConstraint("applies_to IN ('ALL', 'FUTURE')", name="applies_to_valid"),
        Index("ix_unit_material_norms_line_time", "unit_material_id", "changed_at"),
    )

    unit_material_id: Mapped[int] = fk("unit_materials.id", nullable=False, ondelete="CASCADE", index=False)
    quantity_per_unit: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: the norm before this change (empty on the first row)
    previous_qty: Mapped[Decimal | None] = mapped_column(Quantity)
    applies_to: Mapped[str] = mapped_column(String(10), nullable=False, default=NORM_ALL)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    reason: Mapped[str | None] = mapped_column(Text)
    changed_by_id: Mapped[int | None] = fk("users.id", ondelete="SET NULL")
    changed_by_name_snapshot: Mapped[str | None] = mapped_column(String(120))


class UnitCompletion(IdMixin, Base):
    """N units of a unit type made / installed. Never edited; a mistake is cancelled (kept, not counted).

    ``recorded_at`` (when it was entered) decides which norm the units were made with: a norm change
    entered later for FUTURE units does not touch them. ``completed_on`` is the date shown in reports.
    """

    __tablename__ = "unit_completions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="qty_positive"),
        Index("ix_unit_completions_unit_time", "unit_id", "recorded_at"),
    )

    unit_id: Mapped[int] = fk("project_units.id", nullable=False, ondelete="CASCADE", index=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    completed_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = fk("users.id", ondelete="SET NULL")
    user_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_by_id: Mapped[int | None] = fk("users.id", ondelete="SET NULL")
    cancel_reason: Mapped[str | None] = mapped_column(Text)
