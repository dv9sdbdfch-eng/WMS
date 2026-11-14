"""Goods In for a project: the delivery becomes that project's stock

- delivery_notes.site_id (+ site number / name snapshots): the project a delivery is for.
- stock_reservations.delivery_note_id: the delivery that set the stock aside for the project.

Columns are added with plain ALTER TABLE ADD COLUMN (no table rebuild), so documents, the ledger
and their protective triggers are untouched. Existing deliveries and reservations stay as they are
(no project / reserved by hand).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-14 10:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def _add_fk_column(table: str, column: str, target: str, fk_name: str) -> None:
    bind = op.get_bind()
    if column in {c["name"] for c in sa.inspect(bind).get_columns(table)}:
        return  # left behind by a downgrade that could not drop it
    if bind.dialect.name == "sqlite":
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER "
                   f"CONSTRAINT {fk_name} REFERENCES {target} (id) ON DELETE RESTRICT")
    else:
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(fk_name, table, target, [column], ['id'], ondelete='RESTRICT')


def _add_column(table: str, column: str, sql_type: str) -> None:
    if column not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
        op.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def upgrade() -> None:
    _add_fk_column('delivery_notes', 'site_id', 'sites', 'fk_delivery_notes_site_id_sites')
    _add_column('delivery_notes', 'site_number_snapshot', 'VARCHAR(50)')
    _add_column('delivery_notes', 'site_name_snapshot', 'VARCHAR(200)')
    op.create_index(op.f('ix_delivery_notes_site_id'), 'delivery_notes', ['site_id'], unique=False)
    _add_fk_column('stock_reservations', 'delivery_note_id', 'delivery_notes',
                   'fk_stock_reservations_delivery_note_id_delivery_notes')
    op.create_index(op.f('ix_stock_reservations_delivery_note_id'), 'stock_reservations', ['delivery_note_id'],
                    unique=False)


def downgrade() -> None:
    # The columns stay (SQLite cannot drop a column that has a foreign key without rebuilding a
    # protected table); upgrading again reuses them.
    op.drop_index(op.f('ix_stock_reservations_delivery_note_id'), table_name='stock_reservations')
    op.drop_index(op.f('ix_delivery_notes_site_id'), table_name='delivery_notes')
