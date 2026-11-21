"""Production for a project uses the project's stock

- production_lines.reservation_id: the project stock row (stock_reservations) a consumed material
  came out of, like goods_issue_lines.reservation_id for Goods Out.

Added with plain ALTER TABLE ADD COLUMN (no table rebuild): documents, the ledger and their
protective triggers are untouched. Existing production lines keep an empty value.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-21 15:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

FK = 'fk_production_lines_reservation_id_stock_reservations'


def upgrade() -> None:
    bind = op.get_bind()
    if 'reservation_id' not in {c["name"] for c in sa.inspect(bind).get_columns('production_lines')}:
        if bind.dialect.name == "sqlite":
            op.execute("ALTER TABLE production_lines ADD COLUMN reservation_id INTEGER "
                       f"CONSTRAINT {FK} REFERENCES stock_reservations (id) ON DELETE RESTRICT")
        else:
            op.add_column('production_lines', sa.Column('reservation_id', sa.Integer(), nullable=True))
            op.create_foreign_key(FK, 'production_lines', 'stock_reservations', ['reservation_id'], ['id'],
                                  ondelete='RESTRICT')
    op.create_index(op.f('ix_production_lines_reservation_id'), 'production_lines', ['reservation_id'],
                    unique=False)


def downgrade() -> None:
    # The column stays (SQLite cannot drop a column with a foreign key without rebuilding the
    # table); upgrading again reuses it.
    op.drop_index(op.f('ix_production_lines_reservation_id'), table_name='production_lines')
