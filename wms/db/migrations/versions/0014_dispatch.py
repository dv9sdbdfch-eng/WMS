"""dispatch of finished products with an outgoing Delivery Note

- dispatches / dispatch_lines: finished products sent to a customer or project. The dispatch
  number (DEL-000001) is the Delivery Note number. Protected like the other stock documents
  (never deleted, posted lines never change, a reversed one stays reversed).
- number sequence DISPATCH (prefix DEL-).

Existing data is not touched.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-10 01:00:00
"""
import sqlalchemy as sa
from alembic import op

import wms.db.types

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('dispatches',
    sa.Column('dispatch_number', sa.String(length=30), nullable=False),
    sa.Column('dispatch_date', sa.Date(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('customer', sa.String(length=200), nullable=True),
    sa.Column('delivery_address', sa.Text(), nullable=True),
    sa.Column('contact', sa.String(length=200), nullable=True),
    sa.Column('vehicle', sa.String(length=120), nullable=True),
    sa.Column('reference', sa.String(length=80), nullable=True),
    sa.Column('dispatched_by', sa.String(length=120), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('reversed_by_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_number_snapshot', sa.String(length=50), nullable=True),
    sa.Column('site_name_snapshot', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('reversed_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('reversal_reason', sa.Text(), nullable=True),
    sa.Column('client_token', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(['reversed_by_id'], ['users.id'], name=op.f('fk_dispatches_reversed_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_dispatches_site_id_sites'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_dispatches_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dispatches')),
    sa.UniqueConstraint('dispatch_number', name=op.f('uq_dispatches_dispatch_number'))
    )
    op.create_index(op.f('ix_dispatches_client_token'), 'dispatches', ['client_token'], unique=True)
    op.create_index(op.f('ix_dispatches_dispatch_date'), 'dispatches', ['dispatch_date'], unique=False)
    op.create_index(op.f('ix_dispatches_reference'), 'dispatches', ['reference'], unique=False)
    op.create_index(op.f('ix_dispatches_reversed_by_id'), 'dispatches', ['reversed_by_id'], unique=False)
    op.create_index(op.f('ix_dispatches_site_id'), 'dispatches', ['site_id'], unique=False)
    op.create_index(op.f('ix_dispatches_status'), 'dispatches', ['status'], unique=False)
    op.create_index(op.f('ix_dispatches_user_id'), 'dispatches', ['user_id'], unique=False)
    op.create_table('dispatch_lines',
    sa.Column('dispatch_id', sa.Integer(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('bin_id', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('movement_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['bin_id'], ['bins.id'], name=op.f('fk_dispatch_lines_bin_id_bins'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['dispatch_id'], ['dispatches.id'], name=op.f('fk_dispatch_lines_dispatch_id_dispatches'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['movement_id'], ['stock_movements.id'], name=op.f('fk_dispatch_lines_movement_id_stock_movements'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_dispatch_lines_product_id_products'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dispatch_lines'))
    )
    op.create_index(op.f('ix_dispatch_lines_bin_id'), 'dispatch_lines', ['bin_id'], unique=False)
    op.create_index(op.f('ix_dispatch_lines_dispatch_id'), 'dispatch_lines', ['dispatch_id'], unique=False)
    op.create_index(op.f('ix_dispatch_lines_movement_id'), 'dispatch_lines', ['movement_id'], unique=False)
    op.create_index(op.f('ix_dispatch_lines_product_id'), 'dispatch_lines', ['product_id'], unique=False)

    from wms.db.engine import dispatch_trigger_sql
    for stmt in dispatch_trigger_sql(op.get_bind().dialect.name):
        op.execute(stmt)
    op.execute("INSERT INTO number_sequences (name, prefix, next_value, padding) "
               "SELECT 'DISPATCH', 'DEL-', 1, 6 WHERE NOT EXISTS "
               "(SELECT 1 FROM number_sequences WHERE name = 'DISPATCH')")


def downgrade() -> None:
    op.drop_table('dispatch_lines')
    op.drop_table('dispatches')
    op.execute("DELETE FROM number_sequences WHERE name = 'DISPATCH'")
