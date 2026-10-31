"""Unfinished documents (UI refactor spec 40).

A ``DocumentDraft`` holds what someone typed into a long form (a Goods In with many lines) so they
can stop and carry on later - on this PC or another one. It is only the form's content as JSON:
saving, changing or deleting a draft never touches stock, the movement ledger or any posted
document. Only "Post" on the form creates the real transaction, and the draft is then deleted.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.db.base import Base, IdMixin, TimestampMixin, fk


class DocumentDraft(IdMixin, TimestampMixin, Base):
    __tablename__ = "document_drafts"

    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # GOODS_IN
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)  # the form as JSON
    created_by_id: Mapped[int] = fk("users.id", nullable=False)

    created_by = relationship("User", lazy="joined")
