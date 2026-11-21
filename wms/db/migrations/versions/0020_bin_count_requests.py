"""bins to count (v1.34)

- bin_count_requests: a bin someone asked to be counted, e.g. because material on a picking list
  was not found there ("Set line to 0 (not found)" in Goods Out). Shown on the Dashboard as
  "Bins to Count" and closed automatically when an approved count includes the bin.

Only adds a table: stock, documents and history are not touched.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21 09:33:26
"""
import sqlalchemy as sa
from alembic import op

import wms.db.types

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bin_count_requests',
    sa.Column('bin_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('reason', sa.String(length=200), nullable=False),
    sa.Column('requested_by_id', sa.Integer(), nullable=False),
    sa.Column('requested_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('resolved_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('resolved_count_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['bin_id'], ['bins.id'], name=op.f('fk_bin_count_requests_bin_id_bins'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_bin_count_requests_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by_id'], ['users.id'], name=op.f('fk_bin_count_requests_requested_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['resolved_count_id'], ['stock_counts.id'], name=op.f('fk_bin_count_requests_resolved_count_id_stock_counts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bin_count_requests'))
    )
    with op.batch_alter_table('bin_count_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bin_count_requests_bin_id'), ['bin_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bin_count_requests_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bin_count_requests_requested_by_id'), ['requested_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bin_count_requests_resolved_at'), ['resolved_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_bin_count_requests_resolved_count_id'), ['resolved_count_id'], unique=False)


def downgrade() -> None:
    op.drop_table('bin_count_requests')
