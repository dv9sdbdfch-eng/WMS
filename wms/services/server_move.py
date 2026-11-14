"""Moving the data of one PC (SQLite file) to the shared PostgreSQL server, with a full check.

Used once, on the day of the move (``StoreWMS.exe --move-to-server``):

1. the SQLite file is brought up to this version's schema (an automatic copy is kept first);
2. the server database must be empty: the program creates the tables and the protective triggers;
3. every table is copied in one transaction, row by row with the same ids, document numbers and
   dates; photos and scans go into the server too;
4. the copy is checked: the number of rows in every table, the quantity of every product in every
   bin, the ledger totals and the integrity check. Any difference rolls everything back, so the
   server is either a complete copy or still empty. The SQLite file itself is never changed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Integer, func, inspect, select, text

from wms.core.exceptions import ValidationError
from wms.db.base import Base
from wms.db.engine import Database

log = logging.getLogger(__name__)

BATCH = 2000


@dataclass
class MoveReport:
    tables: dict[str, int] = field(default_factory=dict)  # rows copied per table
    files: int = 0  # attachment contents put on the server
    missing_files: list[str] = field(default_factory=list)  # attachments with no file on this PC
    stock_rows: int = 0  # product/bin balances compared

    def summary(self) -> str:
        rows = sum(self.tables.values())
        text = (f"{rows} records in {len(self.tables)} tables copied and checked; {self.stock_rows} stock "
                f"balances match; {self.files} photo/scan file(s) moved to the server.")
        if self.missing_files:
            text += f" {len(self.missing_files)} attachment file(s) were already missing on this PC."
        return text


def move_to_server(sqlite_url: str, server_url: str, attachments_dir: Path,
                   progress: Callable[[str], None] | None = None) -> MoveReport:
    say = progress or (lambda m: log.info("%s", m))
    if not sqlite_url.startswith("sqlite"):
        raise ValidationError("The source must be this PC's own data file.")
    src = Database(sqlite_url)
    dst = Database(server_url)
    created = False
    try:
        if dst.dialect != "postgresql":
            raise ValidationError("The target must be the PostgreSQL server.")
        with dst.engine.connect() as c:
            existing = inspect(c).get_table_names()
        if existing:
            raise ValidationError("The server database is not empty. The move only fills a new, empty database "
                                  "(so nothing on the server is ever overwritten).")
        say("Checking this PC's data file...")
        src.migrate()  # same schema as this version (keeps a copy of the file first when it upgrades)
        say("Creating the tables on the server...")
        created = True
        dst.migrate()
        report = MoveReport()
        with src.engine.connect() as sc, dst.engine.begin() as dc:
            for table in Base.metadata.sorted_tables:
                if table.name == "attachment_files":
                    continue
                say(f"Copying {table.name}...")
                report.tables[table.name] = _copy_table(sc, dc, table)
            _reset_sequences(dc)
            say("Moving photos and scans...")
            _copy_files(sc, dc, attachments_dir, report)
            say("Checking the copy...")
            _verify(sc, dc, report)
        say("Done: " + report.summary())
        return report
    except BaseException:
        # the copy rolled back; drop the tables this run created too, so it can simply be run again
        # (a server database that was not empty to begin with is never touched)
        if created:
            try:
                _drop_all(dst)
            except Exception:  # noqa: BLE001
                log.exception("Could not empty the server database after a failed move")
        raise
    finally:
        src.dispose()
        dst.dispose()


def _copy_table(sc, dc, table) -> int:
    cols = [c for c in table.columns]
    pk = list(table.primary_key.columns)
    self_fks = [fk.parent for fk in table.foreign_keys if fk.column.table is table]
    q = select(*cols).order_by(*pk)
    later: list[dict] = []  # rows pointing at a parent inserted after them (e.g. a re-parented category)
    n = 0
    batch: list[dict] = []
    for row in sc.execute(q).mappings():
        r = dict(row)
        if self_fks and "id" in r:
            ahead = {c.name: r[c.name] for c in self_fks if r[c.name] is not None and r[c.name] > r["id"]}
            if ahead:
                later.append({"id": r["id"], **ahead})
                r.update(dict.fromkeys(ahead))
        batch.append(r)
        if len(batch) >= BATCH:
            dc.execute(table.insert(), batch)
            n += len(batch)
            batch = []
    if batch:
        dc.execute(table.insert(), batch)
        n += len(batch)
    for r in later:
        dc.execute(table.update().where(table.c.id == r["id"]).values({k: v for k, v in r.items() if k != "id"}))
    return n


def _reset_sequences(dc) -> None:
    """Rows were copied with their own ids: new rows on the server must continue after them."""
    for table in Base.metadata.sorted_tables:
        idc = table.c.get("id")
        if idc is None or not isinstance(idc.type, Integer) or not idc.primary_key:
            continue
        seq = dc.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}).scalar()
        if seq:
            dc.execute(text(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"))


def _copy_files(sc, dc, folder: Path, report: MoveReport) -> None:
    import hashlib

    from sqlalchemy.dialects.postgresql import insert

    from wms.core.enums import AttachmentKind
    from wms.core.numbers import utcnow
    from wms.db.models import Attachment, AttachmentFile
    from wms.services.attachments import make_thumbnail
    done: set[str] = set()
    for att in sc.execute(select(Attachment.stored_name, Attachment.sha256, Attachment.kind)).all():
        if att.sha256 in done:
            continue
        path = folder / att.stored_name
        if not path.is_file():
            report.missing_files.append(att.stored_name)
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != att.sha256:
            report.missing_files.append(att.stored_name)  # changed/damaged on disk: not what was attached
            continue
        thumb = make_thumbnail(path) if att.kind == AttachmentKind.PHOTO.value else None
        dc.execute(insert(AttachmentFile).values(sha256=att.sha256, size_bytes=len(data), data=data, thumb=thumb,
                                                 created_at=utcnow()).on_conflict_do_nothing())
        done.add(att.sha256)
        report.files += 1


def _verify(sc, dc, report: MoveReport) -> None:
    from wms.db.models import StockLevel, StockMovement
    problems = []
    for table in Base.metadata.sorted_tables:
        if table.name == "attachment_files":
            continue
        a = sc.execute(select(func.count()).select_from(table)).scalar()
        b = dc.execute(select(func.count()).select_from(table)).scalar()
        if a != b:
            problems.append(f"{table.name}: {a} rows on this PC, {b} on the server")
    lv = StockLevel.__table__
    q = select(lv.c.product_id, lv.c.bin_id, lv.c.quantity).order_by(lv.c.product_id, lv.c.bin_id)
    a, b = sc.execute(q).all(), dc.execute(q).all()
    report.stock_rows = len(a)
    if [tuple(r) for r in a] != [tuple(r) for r in b]:
        problems.append("stock balances per product and bin differ")
    mv = StockMovement.__table__
    q = select(func.count(), func.sum(mv.c.quantity), func.max(mv.c.id))
    if tuple(sc.execute(q).one()) != tuple(dc.execute(q).one()):
        problems.append("stock movement totals differ")
    if problems:
        raise ValidationError("The copy on the server does not match this PC's data, so it was undone:\n- "
                              + "\n- ".join(problems))


def _drop_all(db: Database) -> None:
    with db.engine.begin() as c:
        for name in inspect(c).get_table_names():
            c.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
