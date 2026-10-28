"""Audit trail writer and reader."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.core.numbers import utcnow
from wms.core.permissions import P
from wms.db.models import AuditLog
from wms.services.base import AppContext, BaseService


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, tuple)):
        return list(value)
    return value


def snapshot(obj, fields: Iterable[str]) -> dict[str, Any]:
    return {f: _jsonable(getattr(obj, f, None)) for f in fields}


def diff(old: dict[str, Any], new: dict[str, Any]) -> tuple[dict, dict]:
    changed = [k for k in new if old.get(k) != new.get(k)]
    return {k: old.get(k) for k in changed}, {k: new.get(k) for k in changed}


def record(
    session: Session,
    ctx: AppContext,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    label: str | None = None,
    summary: str | None = None,
    old: dict | None = None,
    new: dict | None = None,
) -> AuditLog:
    """Add an audit row to the current transaction (committed together with the change)."""
    user = ctx.user
    entry = AuditLog(
        occurred_at=utcnow(),
        user_id=user.id if user else None,
        username=user.username if user else "system",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=(label or "")[:200] or None,
        summary=summary,
        old_values=json.dumps(old, default=_jsonable) if old else None,
        new_values=json.dumps(new, default=_jsonable) if new else None,
        workstation=(ctx.workstation or "")[:100],
    )
    session.add(entry)
    return entry


class AuditService(BaseService):
    def search(
        self,
        text: str | None = None,
        entity_type: str | None = None,
        action: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 2000,
    ) -> list[AuditLog]:
        self.require(P.AUDIT_VIEW)
        q = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
        if entity_type:
            q = q.where(AuditLog.entity_type == entity_type)
        if action:
            q = q.where(AuditLog.action == action)
        if date_from:
            q = q.where(AuditLog.occurred_at >= datetime.combine(date_from, datetime.min.time()).astimezone())
        if date_to:
            q = q.where(AuditLog.occurred_at <= datetime.combine(date_to, datetime.max.time()).astimezone())
        if text:
            like = f"%{text.strip()}%"
            q = q.where(
                AuditLog.entity_label.ilike(like)
                | AuditLog.summary.ilike(like)
                | AuditLog.username.ilike(like)
                | AuditLog.new_values.ilike(like)
                | AuditLog.old_values.ilike(like)
            )
        with self.db.read_session() as s:
            return list(s.scalars(q))

    def for_entity(self, entity_type: str, entity_id: int) -> list[AuditLog]:
        with self.db.read_session() as s:
            return list(
                s.scalars(
                    select(AuditLog)
                    .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
                    .order_by(AuditLog.occurred_at.desc())
                )
            )

    def actions(self) -> list[str]:
        with self.db.read_session() as s:
            return sorted(s.scalars(select(AuditLog.action).distinct()))
