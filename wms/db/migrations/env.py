"""Alembic environment. Runs against the connection handed over by Database.migrate()."""

from alembic import context
from sqlalchemy import create_engine

from wms.db import models  # noqa: F401
from wms.db.base import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.begin() as conn:
        _run(conn)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs table rebuilds for ALTER
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run()
