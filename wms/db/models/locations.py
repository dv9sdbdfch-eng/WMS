"""Warehouse structure: Warehouse > Zone > Rack > Shelf (locations) > Bin.

Stock is always held in a bin. A location tree can be as shallow as a single
warehouse with bins directly under it.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import ActiveMixin, Base, IdMixin, TimestampMixin, fk


class Location(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("parent_id", "code"),)

    parent_id: Mapped[int | None] = fk("locations.id")
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Materialised path of codes, e.g. "MAIN / A / R03 / S02". Kept in sync by the service.
    path: Mapped[str] = mapped_column(String(400), nullable=False, default="", index=True)

    parent: Mapped[Location | None] = relationship(remote_side="Location.id", back_populates="children")
    children: Mapped[list[Location]] = relationship(back_populates="parent", order_by="Location.code")
    bins: Mapped[list[Bin]] = relationship(back_populates="location", order_by="Bin.code")


class Bin(IdMixin, TimestampMixin, ActiveMixin, Base):
    __tablename__ = "bins"
    __table_args__ = (UniqueConstraint("location_id", "code"),)

    location_id: Mapped[int] = fk("locations.id", nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200))
    # Unavailable bins (quarantine, damaged) hold stock that cannot be issued.
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Full display path, e.g. "MAIN / A / R03 / S02 / B04". Unique across the warehouse.
    full_code: Mapped[str] = mapped_column(String(450), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    location: Mapped[Location] = relationship(back_populates="bins", lazy="joined")

    def __repr__(self) -> str:
        return f"<Bin {self.full_code}>"
