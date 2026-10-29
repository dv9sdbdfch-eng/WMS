"""Configurable reason codes (Damaged, Missing, Stocktake Correction, ...).

Adjustments, reversals, returns and delivery discrepancies store the ``code``; the label shown
on screen comes from this table, so a code can be relabelled without touching history. Codes are
never deleted once created - an unwanted one is retired (inactive) and disappears from pickers.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wms.core.enums import ReasonUse
from wms.core.exceptions import DuplicateError, NotFoundError, ValidationError
from wms.core.permissions import P
from wms.db.models import ReasonCode
from wms.services import audit
from wms.services.base import BaseService, clean_str

USE_COLUMNS = {
    ReasonUse.ADJUSTMENT: "for_adjustment",
    ReasonUse.REVERSAL: "for_reversal",
    ReasonUse.RETURN: "for_return",
    ReasonUse.DISCREPANCY: "for_discrepancy",
}
FIELDS = ("code", "label", "for_adjustment", "for_reversal", "for_return", "for_discrepancy", "sort_order",
          "is_active")


def require_reason(s: Session, code: str | None, use: ReasonUse) -> ReasonCode:
    """The active reason code ``code`` if it may be used for ``use``; otherwise a clear error."""
    rc = s.scalar(select(ReasonCode).where(ReasonCode.code == (code or "").strip().upper())) if code else None
    if rc is None:
        raise ValidationError("Choose a reason.", "reason_code")
    if not rc.is_active:
        raise ValidationError(f"Reason '{rc.label}' has been retired - choose another reason.", "reason_code")
    if not getattr(rc, USE_COLUMNS[use]):
        raise ValidationError(f"Reason '{rc.label}' cannot be used for {use.label.lower()}s.", "reason_code")
    return rc


def reason_label(s: Session, code: str | None) -> str:
    if not code:
        return ""
    label = s.scalar(select(ReasonCode.label).where(ReasonCode.code == code))
    return label or code.replace("_", " ").title()


class ReasonCodeService(BaseService):
    def list(self, use: ReasonUse | str | None = None, include_inactive: bool = False) -> list[ReasonCode]:
        q = select(ReasonCode).order_by(ReasonCode.sort_order, ReasonCode.label)
        if use:
            q = q.where(getattr(ReasonCode, USE_COLUMNS[ReasonUse(use)]).is_(True))
        if not include_inactive:
            q = q.where(ReasonCode.is_active.is_(True))
        with self.db.read_session() as s:
            return list(s.scalars(q))

    def choices(self, use: ReasonUse | str) -> list[tuple[str, str]]:
        """(code, label) pairs for a picker."""
        return [(r.code, r.label) for r in self.list(use)]

    def label(self, code: str | None) -> str:
        with self.db.read_session() as s:
            return reason_label(s, code)

    def save(self, reason_id: int | None, data: dict) -> ReasonCode:
        self.require(P.SETTINGS_MANAGE)
        label = clean_str(data.get("label"), "label", required=True, max_len=100)
        uses = {col: bool(data.get(col)) for col in USE_COLUMNS.values()}
        if not any(uses.values()):
            raise ValidationError("Tick at least one place where this reason can be used.")
        with self.db.session() as s:
            if reason_id:
                rc = s.get(ReasonCode, reason_id)
                if rc is None:
                    raise NotFoundError("Reason code not found.")
                old = audit.snapshot(rc, FIELDS)
                if rc.is_system and not data.get("is_active", True):
                    raise ValidationError(f"'{rc.label}' is used by the system and cannot be retired.")
            else:
                code = re.sub(r"[^A-Z0-9]+", "_", (data.get("code") or label).strip().upper()).strip("_")[:30]
                if not code:
                    raise ValidationError("Enter a code.", "code")
                if s.scalar(select(ReasonCode.id).where(func.upper(ReasonCode.code) == code)):
                    raise DuplicateError(f"Reason code '{code}' already exists.")
                rc = ReasonCode(code=code, is_system=False)
                s.add(rc)
                old = {}
            rc.label = label
            for col, val in uses.items():
                setattr(rc, col, val)
            rc.sort_order = int(data.get("sort_order") or rc.sort_order or 100)
            rc.is_active = bool(data.get("is_active", True))
            s.flush()
            o, n = audit.diff(old, audit.snapshot(rc, FIELDS))
            if n:
                audit.record(s, self.ctx, "REASON_CODE_UPDATED" if reason_id else "REASON_CODE_CREATED",
                             "SETTING", rc.id, rc.code, old=o, new=n)
            return rc
