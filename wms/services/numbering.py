"""Gap-free document numbers (GO-000001, ADJ-000001 ...)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.db.models import NumberSequence


def next_number(session: Session, name: str) -> str:
    seq = session.scalar(select(NumberSequence).where(NumberSequence.name == name).with_for_update()
                        .execution_options(populate_existing=True))
    if seq is None:
        raise RuntimeError(f"Number sequence {name!r} is not configured.")
    value = seq.next_value
    seq.next_value = value + 1
    session.flush()
    return f"{seq.prefix}{value:0{seq.padding}d}"


def next_numbers(session: Session, name: str, count: int) -> list[str]:
    """``count`` consecutive numbers in one locked step (bulk posting: one round trip, not one per line)."""
    if count <= 0:
        return []
    seq = session.scalar(select(NumberSequence).where(NumberSequence.name == name).with_for_update()
                        .execution_options(populate_existing=True))
    if seq is None:
        raise RuntimeError(f"Number sequence {name!r} is not configured.")
    first = seq.next_value
    seq.next_value = first + count
    session.flush()
    return [f"{seq.prefix}{v:0{seq.padding}d}" for v in range(first, first + count)]
