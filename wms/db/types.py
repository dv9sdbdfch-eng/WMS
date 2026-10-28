"""Custom column types.

Quantities and money are stored as scaled integers so arithmetic and SUM() in
the database are exact on every backend (SQLite has no true decimal type).
Timestamps are stored as UTC and returned timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.types import TypeDecorator


class _ScaledDecimal(TypeDecorator):
    impl = BigInteger
    cache_ok = True
    scale: int = 1000
    places: Decimal = Decimal("0.001")

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = Decimal(value).quantize(self.places, rounding=ROUND_HALF_UP)
        return int(d * self.scale)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return (Decimal(int(value)) / self.scale).quantize(self.places)

    def coerce_compared_value(self, op, value):
        return self


class Quantity(_ScaledDecimal):
    """Quantity with 3 decimal places (e.g. 12.345 m)."""

    cache_ok = True
    scale = 1000
    places = Decimal("0.001")


class Money(_ScaledDecimal):
    """GBP amount with 4 decimal places (unit prices of small fixings)."""

    cache_ok = True
    scale = 10000
    places = Decimal("0.0001")


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.astimezone()  # naive = local time
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
