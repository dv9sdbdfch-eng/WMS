"""Stock reserved for projects.

A reservation is not a stock movement: nothing leaves a bin and On Hand does not change. It lowers
Available (On Hand - Reserved) until the material is issued with Goods Out to the same project;
each Goods Out line that delivers a reservation points at it, and the quantity still reserved is
worked out from those posted lines (so reversing a Goods Out puts the quantity back on reserve).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wms.core.numbers import utcnow
from wms.db.base import Base, IdMixin, fk
from wms.db.models.documents import _ClientToken, _SiteSnapshot
from wms.db.types import Quantity, UTCDateTime


class StockReservation(IdMixin, _SiteSnapshot, _ClientToken, Base):
    __tablename__ = "stock_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="qty_positive"),
        CheckConstraint("status IN ('ACTIVE', 'FULFILLED', 'RELEASED')", name="status_valid"),
    )

    number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    site_id: Mapped[int] = fk("sites.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    required_by: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE", index=True)
    created_by_id: Mapped[int] = fk("users.id", nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    released_by_id: Mapped[int | None] = fk("users.id")
    release_reason: Mapped[str | None] = mapped_column(Text)
    # v1.26: made by a delivery note received for the project (Goods In with a project): the
    # project's own stock. Empty: reserved by hand from warehouse stock.
    delivery_note_id: Mapped[int | None] = fk("delivery_notes.id")

    product = relationship("Product", lazy="joined")
    site = relationship("Site", lazy="joined")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")
    released_by = relationship("User", foreign_keys=[released_by_id], lazy="joined")


class ProjectStockRelease(IdMixin, _SiteSnapshot, Base):
    """v1.27: material given back from a project's stock to the main warehouse stock.

    Nothing moves physically and the ledger is not touched: what was set aside for the project
    (its reservations) is reduced by the quantity, so the material is free for any work again.
    One row per material; rows that were released together share one number (PSR-000001).
    Never edited or deleted.
    """

    __tablename__ = "project_stock_releases"
    __table_args__ = (CheckConstraint("quantity > 0", name="qty_positive"),)

    number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    site_id: Mapped[int] = fk("sites.id", nullable=False)
    product_id: Mapped[int] = fk("products.id", nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int] = fk("users.id", nullable=False)
    user_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    released_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False, index=True)

    product = relationship("Product", lazy="joined")
    site = relationship("Site", lazy="joined")
