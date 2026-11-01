from __future__ import annotations

from datetime import date

from sqlalchemy import Date, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wms.db.base import Base, IdMixin, TimestampMixin, VersionMixin, fk


class Site(IdMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "sites"

    project_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    customer: Mapped[str | None] = mapped_column(String(200), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(String(12))
    # Display copy of the manager's name (v1.15: the link itself is manager_id).
    project_manager: Mapped[str | None] = mapped_column(String(120))
    # v1.15: who manages the project. Optional; removing the manager leaves the project unassigned.
    manager_id: Mapped[int | None] = fk("managers.id", ondelete="SET NULL")
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(50))
    contact_email: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE", index=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def display(self) -> str:
        return f"{self.project_number} - {self.name}"
