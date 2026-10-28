"""Attachments, audit log, document numbering and settings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import Base, IdMixin, fk
from wms.db.types import UTCDateTime


class Attachment(IdMixin, Base):
    """A photo or document linked to any record (product, delivery note, site...).

    Files are copied into the app's attachment store; ``stored_name`` is the
    path relative to that store so the store can be moved (e.g. to a network
    share) without touching the database.
    """

    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    original_name: Mapped[str] = mapped_column(String(260), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    caption: Mapped[str | None] = mapped_column(String(200))
    uploaded_by_id: Mapped[int] = fk("users.id", nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deleted_by_id: Mapped[int | None] = fk("users.id")

    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id], lazy="joined")

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf"


class AuditLog(IdMixin, Base):
    """Append-only record of important actions. Triggers reject UPDATE/DELETE."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity_type", "entity_id"),)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)
    user_id: Mapped[int | None] = fk("users.id")
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    entity_label: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    old_values: Mapped[str | None] = mapped_column(Text)  # JSON
    new_values: Mapped[str | None] = mapped_column(Text)  # JSON
    workstation: Mapped[str | None] = mapped_column(String(100))


class NumberSequence(Base):
    __tablename__ = "number_sequences"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=6)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
