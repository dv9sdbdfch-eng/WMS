"""history: snapshots of names on every transaction, archived projects

- stock_movements: product code/name, from/to bin, project, supplier and user *as they were*
  when the movement was posted. Renaming or removing a bin, project, product or supplier later
  no longer changes what history shows.
- delivery_notes / goods_issues / stock_returns / production_orders: the supplier / project
  name at posting time, for document lists and print-outs.
- Existing rows are filled from the current names (the best record available at upgrade time).
- Site status ARCHIVED is new; no schema change is needed for it.

The ledger and posted documents are protected by triggers that refuse UPDATE. They are lifted
only for the one-off backfill inside this migration's transaction and put back straight after.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 00:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

MOVEMENT_COLUMNS = (
    ("product_code_snapshot", 50),
    ("product_name_snapshot", 200),
    ("from_bin_name_snapshot", 450),
    ("to_bin_name_snapshot", 450),
    ("site_number_snapshot", 50),
    ("site_name_snapshot", 200),
    ("supplier_name_snapshot", 200),
    ("user_name_snapshot", 120),
)
SITE_COLUMNS = (("site_number_snapshot", 50), ("site_name_snapshot", 200))
DOCUMENT_COLUMNS = {
    "delivery_notes": (("supplier_name_snapshot", 200),),
    "goods_issues": SITE_COLUMNS,
    "stock_returns": SITE_COLUMNS + (("supplier_name_snapshot", 200),),
    "production_orders": SITE_COLUMNS,
}


def _all_columns():
    yield "stock_movements", MOVEMENT_COLUMNS
    yield from DOCUMENT_COLUMNS.items()


def _sqlite_trigger_names() -> list[str]:
    from wms.db.engine import integrity_trigger_sql

    return [stmt.split()[5] for stmt in integrity_trigger_sql("sqlite")]  # CREATE TRIGGER IF NOT EXISTS <name>


def _lift_guards(dialect: str) -> None:
    if dialect == "sqlite":
        for name in _sqlite_trigger_names():
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
    elif dialect == "postgresql":
        op.execute("ALTER TABLE stock_movements DISABLE TRIGGER trg_stock_movements_append_only")


def _restore_guards(dialect: str) -> None:
    from wms.db.engine import integrity_trigger_sql

    if dialect == "postgresql":
        op.execute("ALTER TABLE stock_movements ENABLE TRIGGER trg_stock_movements_append_only")
    else:
        for stmt in integrity_trigger_sql(dialect):
            op.execute(stmt)


def _site(col: str, table: str) -> str:
    return f"(SELECT sites.{col} FROM sites WHERE sites.id = {table}.site_id)"


def _supplier(table: str) -> str:
    return f"(SELECT suppliers.name FROM suppliers WHERE suppliers.id = {table}.supplier_id)"


def upgrade() -> None:
    for table, cols in _all_columns():
        for name, length in cols:
            op.add_column(table, sa.Column(name, sa.String(length=length), nullable=True))

    dialect = op.get_bind().dialect.name
    _lift_guards(dialect)
    m = "stock_movements"
    op.execute(
        f"UPDATE {m} SET "
        f"product_code_snapshot = (SELECT products.internal_code FROM products WHERE products.id = {m}.product_id), "
        f"product_name_snapshot = (SELECT products.name FROM products WHERE products.id = {m}.product_id), "
        f"from_bin_name_snapshot = (SELECT bins.full_code FROM bins WHERE bins.id = {m}.from_bin_id), "
        f"to_bin_name_snapshot = (SELECT bins.full_code FROM bins WHERE bins.id = {m}.to_bin_id), "
        f"site_number_snapshot = {_site('project_number', m)}, "
        f"site_name_snapshot = {_site('name', m)}, "
        f"supplier_name_snapshot = {_supplier(m)}, "
        f"user_name_snapshot = (SELECT users.full_name FROM users WHERE users.id = {m}.user_id)"
    )
    op.execute(f"UPDATE delivery_notes SET supplier_name_snapshot = {_supplier('delivery_notes')}")
    for table in ("goods_issues", "stock_returns", "production_orders"):
        op.execute(f"UPDATE {table} SET site_number_snapshot = {_site('project_number', table)}, "
                   f"site_name_snapshot = {_site('name', table)}")
    op.execute(f"UPDATE stock_returns SET supplier_name_snapshot = {_supplier('stock_returns')}")
    _restore_guards(dialect)


def downgrade() -> None:
    op.execute("UPDATE sites SET status = 'COMPLETED' WHERE status = 'ARCHIVED'")
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table, cols in _all_columns():
        for name, _length in cols:
            if sqlite:
                # Native DROP COLUMN (SQLite 3.35+): a batch table rebuild would trip the ledger's
                # foreign keys and append-only triggers.
                op.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
            else:
                op.drop_column(table, name)
