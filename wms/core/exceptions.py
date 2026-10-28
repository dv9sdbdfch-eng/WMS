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


class AlreadyPostedError(DuplicateError):
    """The same entry form was submitted again (double-click, repeated Enter, retry after a freeze)."""

    def __init__(self, message: str, entity_type: str, entity_id: int):
        super().__init__(message)
        self.entity_type = entity_type
        self.entity_id = entity_id


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


class ReturnExceedsIssuedError(WMSError):
    """A return from site is for more than was ever issued to that site.

    Usually a typing mistake or the wrong site. The UI asks for confirmation (e.g. material
    bought directly for the site) and re-submits with ``allow_more_than_issued``.
    """

    def __init__(self, product_code: str, project_number: str, issued: Decimal, returning: Decimal):
        self.product_code = product_code
        self.project_number = project_number
        self.issued = issued
        self.returning = returning
        super().__init__(
            f"{product_code}: returning {returning.normalize():f} from {project_number}, but only "
            f"{issued.normalize():f} (net) was issued to that site.")
