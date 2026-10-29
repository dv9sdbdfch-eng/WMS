"""Write docs/DATABASE_SCHEMA.md from the ORM models, so the document always matches the code.

    python -m tools.schema_doc

The test suite checks that the committed file is up to date (tests/test_docs.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from wms.db import models  # noqa: F401  (registers tables)
from wms.db.base import Base
from wms.db.engine import APPEND_ONLY_TABLES, DOCUMENT_TABLES, LEDGER_APPEND_ONLY_TABLES, LINE_TABLES

OUT = Path(__file__).resolve().parents[1] / "docs" / "DATABASE_SCHEMA.md"

HEADER = """# Database schema

Generated from the models by `python -m tools.schema_doc` - do not edit by hand.
Quantities are stored as integer thousandths (`Quantity`), money as integer 1/10,000 GBP (`Money`),
times in UTC. See docs/STOCK_LEDGER.md for how stock is recorded and docs/ARCHITECTURE.md for the
layers that use these tables.

Protected tables (database triggers, see `wms.db.engine`):

* Append-only (no UPDATE, no DELETE): {append}
* Never deleted; a posted line never changes: {docs}

"""


def _type(col) -> str:
    t = type(col.type).__name__
    length = getattr(col.type, "length", None)
    return f"{t}({length})" if length else t


def render() -> str:
    append = ", ".join(f"`{t}`" for t in (*APPEND_ONLY_TABLES, *LEDGER_APPEND_ONLY_TABLES))
    docs = ", ".join(f"`{t}`" for t in (*DOCUMENT_TABLES, *LINE_TABLES, "stock_count_lines"))
    out = [HEADER.format(append=append, docs=docs)]
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        out.append(f"## {name}\n\n| Column | Type | Null | Key / reference |\n|---|---|---|---|\n")
        for col in table.columns:
            keys = []
            if col.primary_key:
                keys.append("PK")
            keys += [f"FK {fk.target_fullname}" for fk in col.foreign_keys]
            if col.unique:
                keys.append("UNIQUE")
            if col.index:
                keys.append("indexed")
            out.append(f"| {col.name} | {_type(col)} | {'yes' if col.nullable else 'no'} | {', '.join(keys)} |\n")
        extras = []
        for c in sorted(table.constraints, key=lambda c: str(c.name)):
            if isinstance(c, UniqueConstraint) and len(c.columns) > 0 and not (
                    len(c.columns) == 1 and next(iter(c.columns)).unique):
                extras.append(f"UNIQUE ({', '.join(x.name for x in c.columns)})")
            elif isinstance(c, CheckConstraint):
                extras.append(f"CHECK {c.sqltext}")
        for ix in sorted(table.indexes, key=lambda i: str(i.name)):
            where = ix.dialect_options["sqlite"]["where"]
            if len(ix.columns) > 1 or ix.unique or where is not None:
                extras.append(f"{'UNIQUE ' if ix.unique else ''}INDEX {ix.name} "
                              f"({', '.join(x.name for x in ix.columns)}){f' WHERE {where}' if where is not None else ''}")
        if extras:
            out.append("\n" + "\n".join(f"* {e}" for e in extras) + "\n")
        out.append("\n")
    return "".join(out).rstrip() + "\n"


def main() -> int:
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
