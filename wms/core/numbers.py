"""Decimal parsing and UK formatting helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from wms.core.exceptions import ValidationError

QTY_PLACES = Decimal("0.001")
MONEY_PLACES = Decimal("0.0001")
ZERO = Decimal("0")


def to_decimal(value, field: str = "value", places: Decimal = QTY_PLACES) -> Decimal:
    """Parse user input (str/int/float/Decimal) to a Decimal rounded to ``places``.

    Accepts "1,250.5" (thousands separator) and "£12.50".
    """
    if value is None or value == "":
        raise ValidationError(f"{field.replace('_', ' ').capitalize()} is required.", field)
    if isinstance(value, Decimal):
        d = value
    else:
        text = str(value).strip().replace("£", "").replace(",", "").replace(" ", "")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise ValidationError(f"'{value}' is not a valid number.", field) from None
    if not d.is_finite():
        raise ValidationError(f"'{value}' is not a valid number.", field)
    return d.quantize(places, rounding=ROUND_HALF_UP)


def to_qty(value, field: str = "quantity") -> Decimal:
    return to_decimal(value, field, QTY_PLACES)


def to_money(value, field: str = "unit_cost") -> Decimal:
    return to_decimal(value, field, MONEY_PLACES)


def fmt_qty(value: Decimal | None) -> str:
    if value is None:
        return ""
    d = Decimal(value).quantize(QTY_PLACES)
    text = f"{d:,.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def fmt_money(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return ""
    d = Decimal(value)
    if places == 2 and d != d.quantize(Decimal("0.01")):
        places = 4  # show sub-penny unit prices (e.g. screws) in full
    sign = "-" if d < 0 else ""
    return f"{sign}£{abs(d):,.{places}f}"


def fmt_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = to_local(value)
        return value.strftime("%d/%m/%Y")
    return value.strftime("%d/%m/%Y")


def fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return to_local(value).strftime("%d/%m/%Y %H:%M")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone()


def parse_uk_date(text: str) -> date:
    text = (text or "").strip()
    for f in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    raise ValidationError(f"'{text}' is not a valid date (use DD/MM/YYYY).", "date")
