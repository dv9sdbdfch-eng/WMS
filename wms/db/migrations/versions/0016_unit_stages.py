"""production dashboard: Done / Dispatched / On site marks per piece of a unit

unit_completions (v1.23) gets two columns:

- stage: DONE (default - every existing row is a DONE batch), DISPATCHED or ON_SITE;
- piece_no: which piece of the unit type (1..quantity) a mark from the Production dashboard is
  for. Empty for batches recorded for the unit type as a whole.

plus a unique index "one live mark per piece and stage" (cancelled marks are kept as history).
The table is rebuilt (SQLite cannot add CHECK constraints otherwise); its rows are copied as they
are. Stock, the ledger and documents are not touched.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-14 00:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

_WHERE = "cancelled_at IS NULL AND piece_no IS NOT NULL"


def _recreate() -> str:
    # SQLite must rebuild the table to add CHECK constraints. PostgreSQL alters it in place: a
    # rebuilt copy would get a new id counter starting at 1 and the next new row would clash.
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    with op.batch_alter_table('unit_completions', schema=None, recreate=_recreate()) as batch_op:
        batch_op.add_column(sa.Column('stage', sa.String(length=12), nullable=False, server_default='DONE'))
        batch_op.add_column(sa.Column('piece_no', sa.Integer(), nullable=True))
        batch_op.create_check_constraint(op.f('ck_unit_completions_stage_valid'), "stage IN ('DONE', 'DISPATCHED', 'ON_SITE')")
        batch_op.create_check_constraint(op.f('ck_unit_completions_piece_no_valid'), "piece_no IS NULL OR piece_no >= 1")
    op.create_index('ux_unit_completions_piece_stage', 'unit_completions', ['unit_id', 'piece_no', 'stage'],
                    unique=True, sqlite_where=sa.text(_WHERE), postgresql_where=sa.text(_WHERE))


def downgrade() -> None:
    op.drop_index('ux_unit_completions_piece_stage', table_name='unit_completions')
    # marks of the later stages have no meaning without the column: drop them, keep DONE rows
    op.execute("DELETE FROM unit_completions WHERE stage <> 'DONE'")
    with op.batch_alter_table('unit_completions', schema=None, recreate=_recreate()) as batch_op:
        batch_op.drop_constraint(op.f('ck_unit_completions_piece_no_valid'), type_='check')
        batch_op.drop_constraint(op.f('ck_unit_completions_stage_valid'), type_='check')
        batch_op.drop_column('piece_no')
        batch_op.drop_column('stage')
