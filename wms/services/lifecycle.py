"""Removing master records (bins, products, projects, suppliers) without losing history.

Warehouse users only ever see "Delete". The application then picks the safe behaviour:

* a record that was **never used** (no transactions, nothing pointing at it) is deleted
  permanently;
* a record **with history** is *removed from active use* (archived): it disappears from
  selectors and cannot take new transactions, but every past transaction still shows it -
  each stock movement keeps its own snapshot of the names (see ``StockMovement``);
* a record that is **still in use** (stock on hand, an unfinished stocktake) cannot be removed
  at all; the user is told exactly what to clear first.

Every removal and restore is written to the audit trail: who, when, the reason and whether the
record was deleted permanently or archived.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wms.core.enums import CountStatus
from wms.core.exceptions import ValidationError
from wms.db.models import AuditLog, StockCount, StockCountLine
from wms.services import audit
from wms.services.base import AppContext, clean_str

DELETED = "Deleted permanently"
ARCHIVED = "Removed from active use"


@dataclass
class RemovalPlan:
    """What "Delete" will do to one record. Built by the owning service's ``removal_plan``."""

    kind: str  # "Bin", "Product", "Project", "Supplier" - as the user knows it
    entity_type: str  # EntityType value, for the audit trail
    entity_id: int
    name: str  # "B04", "H3311 Oak Panel", "P-1001 - Surrey County Hall"
    history: int = 0  # historical stock transactions
    references: int = 0  # other records that point at it (documents, counts, attachments...)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    already_archived: bool = False
    archive_only: bool = False  # "Active" unticked on the edit form: always archive, never delete

    @property
    def allowed(self) -> bool:
        return not self.blockers and not self.already_archived

    @property
    def permanent(self) -> bool:
        return not self.archive_only and self.history == 0 and self.references == 0

    @property
    def action(self) -> str:
        """Button text: the user sees what will really happen."""
        return "Delete permanently" if self.permanent else "Remove from active use"

    @property
    def outcome(self) -> str:
        return DELETED if self.permanent else ARCHIVED

    @property
    def message(self) -> str:
        """The confirmation (or refusal) shown before anything changes."""
        who = f"{self.kind} {self.name}"
        if self.already_archived:
            return f"{who} has already been removed from active use."
        if self.blockers:
            return f"{who} cannot be removed yet:\n\n" + "\n".join(f"• {b}" for b in self.blockers)
        where = "active warehouse operations" if self.kind == "Bin" else "active use"
        if self.permanent:
            text = f"{who} has never been used. It will be deleted permanently."
        elif self.history:
            n = f"{self.history:,} historical transaction{'' if self.history == 1 else 's'}"
            text = (f"{who} has {n}. It will be removed from {where}, but its historical transaction data "
                    f"will be preserved.")
        else:
            text = (f"{who} is referred to by other records. It will be removed from {where}; those records "
                    f"keep showing it.")
        if self.notes:
            text += "\n\n" + "\n".join(self.notes)
        return text + "\n\nContinue?"

    def check(self) -> None:
        """Raise the refusal as a normal validation error (re-checked inside the removal itself)."""
        if not self.allowed:
            raise ValidationError(self.message)


def count(s: Session, model_col, *conds) -> int:
    return s.scalar(select(func.count(model_col)).where(*conds)) or 0


def unfinished_counts(s: Session, *line_conds) -> list[str]:
    """Numbers of stocktakes still open or awaiting approval that include matching lines."""
    return list(s.scalars(
        select(StockCount.count_number).where(
            StockCount.status.in_((CountStatus.OPEN.value, CountStatus.SUBMITTED.value)),
            StockCount.id.in_(select(StockCountLine.count_id).where(*line_conds)))
        .order_by(StockCount.count_number)))


def stocktake_blocker(numbers: list[str]) -> str | None:
    if not numbers:
        return None
    return (f"It is on stocktake {', '.join(numbers)}, which is not finished. "
            f"Approve or cancel the count first.")


def record_removal(s: Session, ctx: AppContext, plan: RemovalPlan, prefix: str, reason: str | None,
                   label: str | None = None) -> None:
    audit.record(
        s, ctx, f"{prefix}_{'DELETED' if plan.permanent else 'ARCHIVED'}", plan.entity_type, plan.entity_id,
        label or plan.name, f"{plan.outcome}." + (f" Reason: {reason}" if reason else ""),
        new={"removal": plan.outcome, "reason": reason, "historical_transactions": plan.history},
    )


def record_restore(s: Session, ctx: AppContext, entity_type: str, entity_id: int, label: str, prefix: str,
                   reason: str | None) -> None:
    audit.record(s, ctx, f"{prefix}_RESTORED", entity_type, entity_id, label,
                 "Restored to active use." + (f" Reason: {reason}" if reason else ""),
                 new={"restored": True, "reason": reason})


def reason_text(reason: str | None) -> str | None:
    return clean_str(reason, "reason", max_len=500)


def last_removal(s: Session, entity_type: str, entity_id: int) -> AuditLog | None:
    """The audit entry that took this record out of active use (for "Archived on ... by ...")."""
    return s.scalar(
        select(AuditLog).where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id,
                               AuditLog.action.like("%\\_ARCHIVED", escape="\\"))
        .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(1))


def attachment_count(s, entity_type: str, entity_id: int) -> int:
    """Files attached to a record: a record with documents is archived, never deleted with them."""
    from wms.db.models import Attachment
    return count(s, Attachment.id, Attachment.entity_type == entity_type, Attachment.entity_id == entity_id,
                 Attachment.is_deleted.is_(False))
