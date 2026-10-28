"""Domain exceptions.

Services raise these; the UI catches them and shows a friendly message.
Anything else is treated as an unexpected error and logged with a traceback.
"""

from __future__ import annotations

from decimal import Decimal


class WMSError(Exception):
    """Base class for all expected, user-facing errors."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(WMSError):
    """Input failed validation. ``field`` names the offending field when known."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class NotFoundError(WMSError):
    pass


class DuplicateError(WMSError):
    pass


class PermissionDeniedError(WMSError):
    pass


class ConcurrencyError(WMSError):
    """The record was changed by someone else since it was loaded."""


class AuthenticationError(WMSError):
    pass


class InsufficientStockError(WMSError):
    """Posting would take a bin below zero.

    The UI can offer an override to users holding ``stock.override_negative``
    and re-submit with an override reason.
    """

    def __init__(self, product_code: str, bin_code: str, available: Decimal, requested: Decimal):
        self.product_code = product_code
        self.bin_code = bin_code
        self.available = available
        self.requested = requested
        super().__init__(
            f"Insufficient stock for {product_code} in bin {bin_code}: "
            f"available {available.normalize():f}, requested {requested.normalize():f}."
        )
