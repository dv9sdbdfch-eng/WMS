"""Drafts of long documents (UI refactor spec 40): save a half-entered Goods In and finish it later.

A draft is the form's content only. Saving, opening or deleting one never changes stock, the
movement ledger or a posted document - only posting the form does, through the normal Goods In
service, after which the draft is deleted. Drafts are shared: another operator can finish one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select

from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import utcnow
from wms.core.permissions import P
from wms.db.models import DocumentDraft
from wms.services import audit
from wms.services.base import BaseService, clean_str

KINDS = {"GOODS_IN": P.GOODS_IN_POST}


@dataclass
class DraftInfo:
    id: int
    kind: str
    title: str
    created_by: str
    updated_at: datetime
    lines: int


class DraftService(BaseService):
    def _check(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValidationError(f"Unknown draft type {kind}.")
        self.require(KINDS[kind])

    def save(self, kind: str, title: str, data: dict, draft_id: int | None = None) -> DraftInfo:
        """Create or overwrite a draft. ``data`` must be JSON-serialisable (ids, texts, numbers as text)."""
        self._check(kind)
        title = clean_str(title, "title", required=True, max_len=160)
        text = json.dumps(data, default=str)
        with self.db.session() as s:
            d = s.get(DocumentDraft, draft_id) if draft_id else None
            if draft_id and (d is None or d.kind != kind):
                d = None  # it was posted or deleted meanwhile: keep the work as a new draft
            if d is None:
                d = DocumentDraft(kind=kind, title=title, data=text, created_by_id=self.user.id)
                s.add(d)
                action = "DRAFT_SAVED"
            else:
                d.title, d.data, d.updated_at = title, text, utcnow()
                action = "DRAFT_UPDATED"
            s.flush()
            audit.record(s, self.ctx, action, "DRAFT", d.id, title, f"Draft saved: {title} (no stock change)")
            return self._info(d)

    def list(self, kind: str) -> list[DraftInfo]:
        self._check(kind)
        with self.db.read_session() as s:
            return [self._info(d) for d in s.scalars(select(DocumentDraft).where(DocumentDraft.kind == kind)
                                                     .order_by(DocumentDraft.updated_at.desc()))]

    def load(self, draft_id: int) -> tuple[DraftInfo, dict]:
        with self.db.read_session() as s:
            d = s.get(DocumentDraft, draft_id)
            if d is None:
                raise NotFoundError("That draft no longer exists (it may have been posted or deleted).")
            self._check(d.kind)
            return self._info(d), json.loads(d.data)

    def delete(self, draft_id: int, reason: str = "deleted") -> None:
        with self.db.session() as s:
            d = s.get(DocumentDraft, draft_id)
            if d is None:
                return
            self._check(d.kind)
            audit.record(s, self.ctx, "DRAFT_DELETED", "DRAFT", d.id, d.title, f"Draft {reason}: {d.title}")
            s.execute(delete(DocumentDraft).where(DocumentDraft.id == draft_id))

    @staticmethod
    def _info(d: DocumentDraft) -> DraftInfo:
        try:
            lines = len(json.loads(d.data).get("lines") or [])
        except (ValueError, AttributeError):
            lines = 0
        return DraftInfo(d.id, d.kind, d.title, d.created_by.full_name if d.created_by else "", d.updated_at, lines)
