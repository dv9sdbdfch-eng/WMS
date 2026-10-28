"""Gap-free document numbers (GO-000001, ADJ-000001 ...)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from wms.db.models import NumberSequence


def next_number(session: Session, name: str) -> str:
    seq = session.scalar(select(NumberSequence).where(NumberSequence.name == name).with_for_update())
    if seq is None:
        raise RuntimeError(f"Number sequence {name!r} is not configured.")
    value = seq.next_value
    seq.next_value = value + 1
    session.flush()
    return f"{seq.prefix}{value:0{seq.padding}d}"
