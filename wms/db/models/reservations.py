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

    product = relationship("Product", lazy="joined")
    site = relationship("Site", lazy="joined")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="joined")
    released_by = relationship("User", foreign_keys=[released_by_id], lazy="joined")
