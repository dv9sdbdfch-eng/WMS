"""Production dashboard (v1.24): where every piece of every unit is - made, dispatched, on site.

Nothing is copied: the rows are the existing Project -> [Block -> Level ->] Unit structure, one row
per piece of a unit type (a unit with quantity 12 gives pieces #1..#12). The marks are rows of
``unit_completions`` (v1.23) with a ``stage`` and a ``piece_no``:

* a **Done** mark is a completed unit for the material norms too (``unit_norms.units_done``);
* **Dispatched** needs Done on the same piece, **On site** needs Dispatched;
* a mark is never edited or deleted: a wrong one is cancelled (kept in the history, not counted),
  a corrected date is "cancel + new mark" in one step, keeping the moment the piece was first
  recorded (so the norm the piece was made with does not change).

A Done batch recorded for the unit type as a whole ("6 done", no piece number) is shown on the
pieces without their own Done mark, in piece order, and cannot be changed from the dashboard.
Planning data only: nothing here changes stock or the movement ledger.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from wms.core.enums import OPEN_SITE_STATUSES, EntityType, SiteStatus
from wms.core.exceptions import NotFoundError, ValidationError
from wms.core.numbers import utcnow
from wms.core.permissions import P
from wms.db.models import Manager, ProjectBlock, ProjectLevel, ProjectUnit, Site, UnitCompletion
from wms.db.models.hierarchy import STAGE_DISPATCHED, STAGE_DONE, STAGE_ON_SITE, STAGES
from wms.services import audit
from wms.services.base import BaseService, clean_str

STAGE_LABELS = {STAGE_DONE: "Done", STAGE_DISPATCHED: "Dispatched", STAGE_ON_SITE: "On site"}
#: status of a piece = the furthest stage it reached
NOT_STARTED = "NOT_STARTED"
STATUS_LABELS = {NOT_STARTED: "Not started", **STAGE_LABELS}
STATUSES = (NOT_STARTED, *STAGES)
_PREVIOUS = {STAGE_DISPATCHED: STAGE_DONE, STAGE_ON_SITE: STAGE_DISPATCHED}
_NEXT = {STAGE_DONE: STAGE_DISPATCHED, STAGE_DISPATCHED: STAGE_ON_SITE}


@dataclass
class Mark:
    """One live mark on a piece. ``id`` None: shown from a batch for the whole unit type."""

    id: int | None
    stage: str
    on: date
    note: str | None = None
    user: str = ""
    batch: bool = False


@dataclass
class PieceRow:
    """One row of the dashboard: one piece of a unit type."""

    unit_id: int
    piece_no: int
    pieces: int                  # how many pieces the unit type has (its quantity)
    site_id: int
    project_number: str
    project_name: str
    manager_id: int | None
    manager: str
    block_id: int | None
    block: str
    level_id: int | None
    level: str
    unit_code: str | None
    unit_name: str
    marks: dict[str, Mark] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, int]:
        return self.unit_id, self.piece_no

    @property
    def project(self) -> str:
        return self.project_name or self.project_number

    @property
    def unit(self) -> str:
        """"Kitchen · K01", and "#3 of 12" when the unit type has more than one piece."""
        text = f"{self.unit_name} · {self.unit_code}" if self.unit_code else self.unit_name
        return f"{text}  #{self.piece_no} of {self.pieces}" if self.pieces > 1 else text

    @property
    def status(self) -> str:
        for stage in reversed(STAGES):
            if stage in self.marks:
                return stage
        return NOT_STARTED

    def date_of(self, stage: str) -> date | None:
        m = self.marks.get(stage)
        return m.on if m else None

    def can_mark(self, stage: str) -> bool:
        """The stage is empty and the one before it is marked."""
        prev = _PREVIOUS.get(stage)
        return stage not in self.marks and (prev is None or prev in self.marks)


@dataclass
class ProjectCard:
    site_id: int
    project_number: str
    name: str
    manager: str
    status: str
    total: int = 0
    done: int = 0          # pieces made (also those since dispatched / on site)
    dispatched: int = 0    # pieces dispatched (also those since on site)
    on_site: int = 0

    @property
    def title(self) -> str:
        return self.name or self.project_number


@dataclass
class Board:
    cards: list[ProjectCard]
    rows: list[PieceRow]

    @property
    def total(self) -> int:
        return len(self.rows)


@dataclass
class HistoryRow:
    stage: str
    on: date
    recorded_at: datetime
    user: str
    note: str | None
    cancelled: bool
    cancel_reason: str | None
    batch_qty: int | None = None


class UnitProgressService(BaseService):
    """The Production dashboard: project cards, one row per piece, Done / Dispatched / On site marks."""

    # ---- reading --------------------------------------------------------------------------------
    def board(self, include_closed: bool = False, site_ids: Iterable[int] | None = None) -> Board:
        """Every piece of every unit of the open projects (or all but archived ones), in tree order:
        project, block, level, unit, piece."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            q = (select(Site.id, Site.project_number, Site.name, Site.status, Site.manager_id,
                        Manager.name, Site.project_manager)
                 .outerjoin(Manager, Manager.id == Site.manager_id).order_by(Site.project_number))
            if include_closed:
                q = q.where(Site.status != SiteStatus.ARCHIVED.value)
            else:
                q = q.where(Site.status.in_(OPEN_SITE_STATUSES))
            if site_ids is not None:
                q = q.where(Site.id.in_(list(site_ids)))
            sites = {r[0]: r for r in s.execute(q)}
            if not sites:
                return Board([], [])
            ids = list(sites)
            blocks, levels, units = {}, {}, []
            for chunk in _chunks(ids):
                for b in s.execute(select(ProjectBlock.id, ProjectBlock.name, ProjectBlock.sort_order)
                                   .where(ProjectBlock.site_id.in_(chunk))):
                    blocks[b[0]] = b
                units += list(s.execute(
                    select(ProjectUnit.id, ProjectUnit.site_id, ProjectUnit.level_id, ProjectUnit.unit_code,
                           ProjectUnit.name, ProjectUnit.quantity, ProjectUnit.sort_order)
                    .where(ProjectUnit.site_id.in_(chunk))))
            for chunk in _chunks(list(blocks)):
                for lv in s.execute(select(ProjectLevel.id, ProjectLevel.block_id, ProjectLevel.name,
                                           ProjectLevel.sort_order).where(ProjectLevel.block_id.in_(chunk))):
                    levels[lv[0]] = lv
            marks = _live_marks(s, [u[0] for u in units])

        def order(u):
            site = sites[u[1]]
            lv = levels.get(u[2])
            bl = blocks.get(lv[1]) if lv else None
            # blocks first (block, level order), then the units directly in the project
            place = (0, bl[2], bl[0], lv[3], lv[0]) if lv else (1, 0, 0, 0, 0)
            return site[1], site[0], place, u[6], u[0]

        cards = {sid: ProjectCard(sid, r[1], r[2], r[5] or r[6] or "", r[3]) for sid, r in sites.items()}
        rows: list[PieceRow] = []
        for u in sorted(units, key=order):
            uid, sid, level_id, code, name, qty, _ = u
            site = sites[sid]
            lv = levels.get(level_id)
            bl = blocks.get(lv[1]) if lv else None
            pieces = int(qty)
            piece_marks, batches = marks.get(uid, ({}, []))
            # Done batches for the whole unit type fill the pieces without their own Done mark
            free = [n for n in range(1, pieces + 1) if STAGE_DONE not in piece_marks.get(n, {})]
            batch_done: dict[int, Mark] = {}
            for b_on, b_qty, b_note, b_user in batches:
                for _ in range(b_qty):
                    if free:
                        batch_done[free.pop(0)] = Mark(None, STAGE_DONE, b_on, b_note, b_user, batch=True)
            card = cards[sid]
            for n in range(1, pieces + 1):
                row = PieceRow(uid, n, pieces, sid, site[1], site[2], site[4], card.manager,
                               bl[0] if bl else None, bl[1] if bl else "", lv[0] if lv else None,
                               lv[2] if lv else "", code, name)
                row.marks = dict(piece_marks.get(n, {}))
                if STAGE_DONE not in row.marks and n in batch_done:
                    row.marks[STAGE_DONE] = batch_done[n]
                rows.append(row)
                card.total += 1
                card.done += STAGE_DONE in row.marks
                card.dispatched += STAGE_DISPATCHED in row.marks
                card.on_site += STAGE_ON_SITE in row.marks
        return Board([c for c in cards.values() if c.total], rows)

    def history(self, unit_id: int, piece_no: int) -> list[HistoryRow]:
        """Every mark of a piece (also cancelled ones) and the Done batches of its unit type."""
        self.require(P.SITES_VIEW)
        with self.db.read_session() as s:
            q = (select(UnitCompletion).where(UnitCompletion.unit_id == unit_id)
                 .where((UnitCompletion.piece_no == piece_no) | UnitCompletion.piece_no.is_(None))
                 .order_by(UnitCompletion.recorded_at, UnitCompletion.id))
            return [HistoryRow(r.stage, r.completed_on, r.recorded_at, r.user_name_snapshot or "", r.notes,
                               r.cancelled_at is not None, r.cancel_reason,
                               None if r.piece_no else int(r.quantity)) for r in s.scalars(q)]

    # ---- marking --------------------------------------------------------------------------------
    def mark(self, unit_id: int, piece_no: int, stage: str, on: date | None = None,
             note: str | None = None) -> Mark:
        """Mark one piece (today unless ``on`` is given)."""
        return self.mark_many([(unit_id, piece_no)], stage, on, note)[0]

    def mark_many(self, pieces: Iterable[tuple[int, int]], stage: str, on: date | None = None,
                  note: str | None = None, skip_done: bool = False) -> list[Mark]:
        """Mark several pieces in one go (all or none). With ``skip_done`` pieces that already have
        the stage, or cannot have it yet, are left out instead of stopping the whole mark."""
        self.require(P.SITES_EDIT)
        _check_stage(stage)
        when = on or date.today()
        if when > date.today():
            raise ValidationError("The date cannot be in the future.", "on")
        note = clean_str(note)
        out = []
        with self.db.session() as s:
            state = _PieceState(s)
            for unit_id, piece_no in dict.fromkeys(pieces):
                u, site = _unit(s, unit_id)
                problem = state.problem(u, piece_no, stage, when)
                if problem:
                    if skip_done:
                        continue
                    raise ValidationError(problem)
                row = self._add(s, u, piece_no, stage, when, note, utcnow())
                state.added(unit_id, piece_no, stage, when)
                audit.record(s, self.ctx, "UNIT_STAGE_MARKED", EntityType.PROJECT_UNIT.value, u.id,
                             f"{site.project_number} / {u.name}",
                             f"{_piece(u, piece_no)}: {STAGE_LABELS[stage]} on {when:%d/%m/%Y}",
                             new={"piece": piece_no, "stage": stage, "date": when.isoformat()})
                out.append(row)
            _flush(s)
            if stage == STAGE_DONE and out:  # units made may keep an older norm: Required follows
                _sync_required(s, self.ctx, {r.unit_id for r in out})
            return [_mark(r) for r in out]

    def change_date(self, mark_id: int, on: date, note: str | None = None) -> Mark:
        """Correct the date of a mark: the old mark is cancelled ("Date corrected") and a new one is
        recorded with the same recorded time, so the norm the piece was made with does not change."""
        self.require(P.SITES_EDIT)
        if on > date.today():
            raise ValidationError("The date cannot be in the future.", "on")
        with self.db.session() as s:
            old = _live(s, mark_id)
            if old.piece_no is None:
                raise ValidationError("This was recorded for several units together: change it in the unit's "
                                      "completed units list.")
            u, site = _unit(s, old.unit_id)
            if old.completed_on == on and (note is None or clean_str(note) == old.notes):
                return _mark(old)
            state = _PieceState(s, ignore_id=old.id)
            problem = state.date_problem(u.id, old.piece_no, old.stage, on)
            if problem:
                raise ValidationError(problem)
            self._cancel(s, old, f"Date corrected to {on:%d/%m/%Y}")
            s.flush()
            row = self._add(s, u, old.piece_no, old.stage, on, clean_str(note) if note is not None else old.notes,
                            old.recorded_at)
            audit.record(s, self.ctx, "UNIT_STAGE_DATE_CHANGED", EntityType.PROJECT_UNIT.value, u.id,
                         f"{site.project_number} / {u.name}",
                         f"{_piece(u, old.piece_no)}: {STAGE_LABELS[old.stage]} date "
                         f"{old.completed_on:%d/%m/%Y} -> {on:%d/%m/%Y}",
                         old={"date": old.completed_on.isoformat()}, new={"date": on.isoformat()})
            _flush(s)
            return _mark(row)

    def cancel(self, mark_id: int, reason: str) -> None:
        """Cancel a wrong mark. It stays in the history; a later stage must be cancelled first."""
        self.require(P.SITES_EDIT)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            row = _live(s, mark_id)
            if row.piece_no is None:
                raise ValidationError("This was recorded for several units together: cancel it in the unit's "
                                      "completed units list.")
            u, site = _unit(s, row.unit_id)
            nxt = _NEXT.get(row.stage)
            if nxt and s.scalar(select(UnitCompletion.id).where(
                    UnitCompletion.unit_id == row.unit_id, UnitCompletion.piece_no == row.piece_no,
                    UnitCompletion.stage == nxt, UnitCompletion.cancelled_at.is_(None))):
                raise ValidationError(f"{_piece(u, row.piece_no)} is already marked {STAGE_LABELS[nxt]}: "
                                      f"cancel that mark first.")
            self._cancel(s, row, reason)
            if row.stage == STAGE_DONE:
                s.flush()
                _sync_required(s, self.ctx, {row.unit_id})
            audit.record(s, self.ctx, "UNIT_STAGE_CANCELLED", EntityType.PROJECT_UNIT.value, u.id,
                         f"{site.project_number} / {u.name}",
                         f"{_piece(u, row.piece_no)}: {STAGE_LABELS[row.stage]} of {row.completed_on:%d/%m/%Y} "
                         f"cancelled: {reason}",
                         old={"piece": row.piece_no, "stage": row.stage, "date": row.completed_on.isoformat()})

    # ---- internals ------------------------------------------------------------------------------
    def _add(self, s: Session, u: ProjectUnit, piece_no: int, stage: str, when: date, note: str | None,
             recorded_at: datetime) -> UnitCompletion:
        user = self.ctx.user
        row = UnitCompletion(unit_id=u.id, stage=stage, piece_no=piece_no, quantity=1, completed_on=when,
                             recorded_at=recorded_at, notes=note, user_id=user.id if user else None,
                             user_name_snapshot=user.full_name if user else None)
        s.add(row)
        return row

    def _cancel(self, s: Session, row: UnitCompletion, reason: str) -> None:
        row.cancelled_at = utcnow()
        row.cancelled_by_id = self.ctx.user.id if self.ctx.user else None
        row.cancel_reason = reason


class _PieceState:
    """Live marks of the pieces touched in one transaction (for the stage and date rules)."""

    def __init__(self, s: Session, ignore_id: int | None = None):
        self.s = s
        self.ignore_id = ignore_id
        self.units: dict[int, tuple[dict[int, dict[str, Mark]], int]] = {}

    def _load(self, unit_id: int):
        if unit_id not in self.units:
            piece_marks, batches = _live_marks(self.s, [unit_id], self.ignore_id).get(unit_id, ({}, []))
            self.units[unit_id] = (piece_marks, sum(b[1] for b in batches))
        return self.units[unit_id]

    def marks(self, unit_id: int, piece_no: int) -> dict[str, Mark]:
        piece_marks, batch_done = self._load(unit_id)
        m = dict(piece_marks.get(piece_no, {}))
        if STAGE_DONE not in m and batch_done:
            # the batch covers the first pieces without their own Done mark
            free = [n for n in range(1, piece_no + 1)
                    if STAGE_DONE not in piece_marks.get(n, {})]
            if piece_no in free[:batch_done]:
                m[STAGE_DONE] = Mark(None, STAGE_DONE, date.min, batch=True)
        return m

    def problem(self, u: ProjectUnit, piece_no: int, stage: str, when: date) -> str | None:
        pieces = int(u.quantity)
        if not 1 <= piece_no <= pieces:
            return f"{u.name} has {pieces} piece(s): there is no piece #{piece_no}."
        m = self.marks(u.id, piece_no)
        if stage in m:
            return f"{_piece(u, piece_no)} is already marked {STAGE_LABELS[stage]}."
        return self.date_problem(u.id, piece_no, stage, when, u)

    def date_problem(self, unit_id: int, piece_no: int, stage: str, when: date,
                     u: ProjectUnit | None = None) -> str | None:
        m = self.marks(unit_id, piece_no)
        name = _piece(u, piece_no) if u is not None else f"Piece #{piece_no}"
        prev = _PREVIOUS.get(stage)
        if prev:
            if prev not in m:
                return f"{name} is not {STAGE_LABELS[prev]} yet: mark {STAGE_LABELS[prev]} first."
            if m[prev].on != date.min and when < m[prev].on:
                return (f"{STAGE_LABELS[stage]} ({when:%d/%m/%Y}) cannot be before {STAGE_LABELS[prev]} "
                        f"({m[prev].on:%d/%m/%Y}).")
        nxt = _NEXT.get(stage)
        if nxt and nxt in m and when > m[nxt].on:
            return (f"{STAGE_LABELS[stage]} ({when:%d/%m/%Y}) cannot be after {STAGE_LABELS[nxt]} "
                    f"({m[nxt].on:%d/%m/%Y}).")
        return None

    def added(self, unit_id: int, piece_no: int, stage: str, when: date) -> None:
        piece_marks, _ = self._load(unit_id)
        piece_marks.setdefault(piece_no, {})[stage] = Mark(0, stage, when)


def _live_marks(s: Session, unit_ids: list[int], ignore_id: int | None = None
                ) -> dict[int, tuple[dict[int, dict[str, Mark]], list[tuple[date, int, str | None, str]]]]:
    """{unit_id: ({piece_no: {stage: Mark}}, [Done batches (date, qty, note, user)])}."""
    out: dict = {}
    for chunk in _chunks(unit_ids):
        q = (select(UnitCompletion).where(UnitCompletion.unit_id.in_(chunk), UnitCompletion.cancelled_at.is_(None))
             .order_by(UnitCompletion.recorded_at, UnitCompletion.id))
        for r in s.scalars(q):
            if r.id == ignore_id:
                continue
            pieces, batches = out.setdefault(r.unit_id, ({}, []))
            if r.piece_no is None:
                if r.stage == STAGE_DONE:
                    batches.append((r.completed_on, int(r.quantity), r.notes, r.user_name_snapshot or ""))
            else:
                pieces.setdefault(r.piece_no, {})[r.stage] = _mark(r)
    return out


def _mark(r: UnitCompletion) -> Mark:
    return Mark(r.id, r.stage, r.completed_on, r.notes, r.user_name_snapshot or "")


def _live(s: Session, mark_id: int) -> UnitCompletion:
    row = s.get(UnitCompletion, mark_id)
    if row is None:
        raise NotFoundError("Mark not found.")
    if row.cancelled_at is not None:
        raise ValidationError("This mark is already cancelled.")
    return row


def _unit(s: Session, unit_id: int) -> tuple[ProjectUnit, Site]:
    from wms.services.project_tree import ProjectTreeService
    u = s.get(ProjectUnit, unit_id)
    if u is None:
        raise NotFoundError("Unit not found.")
    return u, ProjectTreeService._editable_site(s, u.site_id)


def _piece(u: ProjectUnit | None, piece_no: int) -> str:
    if u is None:
        return f"Piece #{piece_no}"
    return f"{u.name} #{piece_no}" if int(u.quantity) > 1 else u.name


def _check_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValidationError("Choose Done, Dispatched or On site.", "stage")


def _sync_required(s: Session, ctx, unit_ids: set[int]) -> None:
    from wms.services.project_demand import sync_project_lists
    sync_project_lists(s, ctx, set(s.scalars(select(ProjectUnit.site_id).where(ProjectUnit.id.in_(list(unit_ids))))))


def _flush(s: Session) -> None:
    try:
        s.flush()
    except IntegrityError as exc:  # two people marked the same piece at the same moment
        raise ValidationError("Someone has just marked this piece: press F5 to see the latest marks.") from exc


def _chunks(ids: list[int], size: int = 900):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]
