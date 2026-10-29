"""Documentation and schema stay in step with the code."""

from __future__ import annotations

from sqlalchemy import text

from tools.schema_doc import OUT, render


def test_database_schema_document_is_up_to_date():
    assert OUT.read_text(encoding="utf-8") == render(), "run: python -m tools.schema_doc"


def _objects(db) -> dict[str, set[str]]:
    with db.engine.connect() as c:
        rows = c.execute(text("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")).all()
    out: dict[str, set[str]] = {}
    for kind, name in rows:
        out.setdefault(kind, set()).add(name)
    return out


def test_upgraded_database_has_the_same_tables_indexes_and_guards_as_a_new_one(tmp_path):
    from alembic import command
    from alembic.config import Config

    from wms.db.engine import MIGRATIONS_DIR, Database
    new = Database(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    new.migrate()
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0001")
    old = Database(url)
    old.migrate()
    a, b = _objects(new), _objects(old)
    for kind in ("table", "index", "trigger"):
        assert a[kind] == b[kind], (kind, a[kind] ^ b[kind])
    new.dispose()
    old.dispose()


def test_screens_never_talk_to_the_database_directly():
    """UI -> services -> database: no SQL or sessions in screen code (keeps PostgreSQL migration simple)."""
    from pathlib import Path
    ui = Path(__file__).resolve().parents[1] / "wms" / "ui"
    offenders = [p.name for p in ui.rglob("*.py") if "sqlalchemy" in p.read_text(encoding="utf-8")
                 or "sqlite3" in p.read_text(encoding="utf-8")]
    assert offenders == []
