"""Stock counts made from an Excel stock import (v1.38)

- stock_counts.kind: COUNT (a physical count: the counted quantity replaces the system one) or
  IMPORT_ADD (an Excel import: each line's quantity is ADDED to the bin when approved).
- stock_counts.source_sha256: the imported file's fingerprint, so the same file is not added twice.

Added with plain ALTER TABLE ADD COLUMN. Existing counts become COUNT; nothing is deleted or rewritten.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-22 01:30:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None

CK = 'ck_stock_counts_kind_valid'
KIND_CHECK = "kind IN ('COUNT', 'IMPORT_ADD')"


def upgrade() -> None:
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns('stock_counts')}
    if 'kind' not in have:
        if bind.dialect.name == "sqlite":
            op.execute(f"ALTER TABLE stock_counts ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'COUNT' "
                       f"CONSTRAINT {CK} CHECK ({KIND_CHECK})")
        else:
            op.add_column('stock_counts', sa.Column('kind', sa.String(length=20), nullable=False,
                                                    server_default='COUNT'))
            op.create_check_constraint(CK, 'stock_counts', KIND_CHECK)
    if 'source_sha256' not in have:
        op.add_column('stock_counts', sa.Column('source_sha256', sa.String(length=64), nullable=True))
    op.create_index(op.f('ix_stock_counts_source_sha256'), 'stock_counts', ['source_sha256'], unique=False)


def downgrade() -> None:
    # The columns stay (SQLite cannot drop a column with a constraint without rebuilding the
    # table); upgrading again reuses them.
    op.drop_index(op.f('ix_stock_counts_source_sha256'), table_name='stock_counts')
