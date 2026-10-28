"""Service layer foundations.

Every screen talks to the database only through a service. A service method
is one unit of work: it opens a transaction, checks permissions, validates,
writes (including the audit record) and commits, or changes nothing at all.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from wms.config import AppConfig
from wms.core.exceptions import AlreadyPostedError, PermissionDeniedError, ValidationError
from wms.db.engine import Database

if TYPE_CHECKING:
    from wms.db.models import User


@dataclass
class AppContext:
    """Everything a service needs: the database, settings and who is logged in."""

    db: Database
    config: AppConfig
    user: "User | None" = None
    workstation: str = field(default_factory=socket.gethostname)

    def has(self, permission: str) -> bool:
        return self.user is not None and self.user.has(permission)


class BaseService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    @property
    def db(self) -> Database:
        return self.ctx.db

    @property
    def user(self) -> "User":
        if self.ctx.user is None:
            raise PermissionDeniedError("You must be logged in.")
        return self.ctx.user

    def reload(self, model, obj_id: int):
        """Fresh copy of a record with its relationships loaded, safe to use after the session closes."""
        with self.db.read_session() as s:
            return s.get(model, obj_id)

    def require(self, permission: str) -> None:
        if not self.ctx.has(permission):
            raise PermissionDeniedError("You do not have permission to perform this action.")


def clean_str(value, field: str | None = None, required: bool = False, max_len: int | None = None) -> str | None:
    """Trim a text field; empty becomes None. Raises if required and missing or too long."""
    text = (str(value).strip() if value is not None else "") or None
    if required and not text:
        label = (field or "value").replace("_", " ").capitalize()
        raise ValidationError(f"{label} is required.", field)
    if text and max_len and len(text) > max_len:
        label = (field or "value").replace("_", " ").capitalize()
        raise ValidationError(f"{label} must be at most {max_len} characters.", field)
    return text


def normalise_code(value: str | None) -> str:
    """Product/bin/doc codes: trimmed, single spaces, upper case."""
    return " ".join((value or "").split()).upper()


def clean_code(value: str | None, field: str, label: str, max_len: int) -> str:
    """A required code, normalised and length-checked (SQLite would silently store any length;
    PostgreSQL would reject it later, so the limit is enforced here for both)."""
    code = normalise_code(value)
    if not code:
        raise ValidationError(f"{label} is required.", field)
    if len(code) > max_len:
        raise ValidationError(f"{label} must be at most {max_len} characters.", field)
    return code


def like_pattern(text: str) -> str:
    """Case-insensitive 'contains' pattern for ``ilike(..., escape='\\')``.

    ``%`` and ``_`` typed by the user are matched literally instead of acting as wildcards.
    """
    t = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


def prefix_pattern(path: str) -> str:
    """Everything below a location path ("MAIN / A" -> "MAIN / A / ..."), wildcards escaped."""
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " / %"


def check_client_token(session, model, token: str | None, number_attr: str, entity_type: str, what: str) -> None:
    """Refuse a second posting of the same entry form (see ``_ClientToken`` in the models)."""
    if not token:
        return
    existing = session.scalar(select(model).where(model.client_token == token))
    if existing is not None:
        raise AlreadyPostedError(
            f"This {what} has already been posted as {getattr(existing, number_attr)}. "
            f"It was not posted a second time.", entity_type, existing.id)
