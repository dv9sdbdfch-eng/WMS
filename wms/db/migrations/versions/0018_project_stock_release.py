"""return from project stock to the main warehouse stock

- project_stock_releases: material given back from a project's stock (what was set aside for it)
  to the main stock, with number (PSR-000001), quantity, reason, who and when. Append-only.
- number sequence PROJECT_RELEASE (prefix PSR-).

Nothing else changes: stock levels, the ledger and documents are not touched.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-14 14:00:00
"""
import sqlalchemy as sa
from alembic import op

import wms.db.types

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('project_stock_releases',
    sa.Column('number', sa.String(length=30), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('user_name_snapshot', sa.String(length=120), nullable=True),
    sa.Column('released_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_number_snapshot', sa.String(length=50), nullable=True),
    sa.Column('site_name_snapshot', sa.String(length=200), nullable=True),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_project_stock_releases_qty_positive')),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_project_stock_releases_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_project_stock_releases_site_id_sites'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_project_stock_releases_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_stock_releases'))
    )
    op.create_index(op.f('ix_project_stock_releases_number'), 'project_stock_releases', ['number'], unique=False)
    op.create_index(op.f('ix_project_stock_releases_product_id'), 'project_stock_releases', ['product_id'], unique=False)
    op.create_index(op.f('ix_project_stock_releases_released_at'), 'project_stock_releases', ['released_at'], unique=False)
    op.create_index(op.f('ix_project_stock_releases_site_id'), 'project_stock_releases', ['site_id'], unique=False)
    op.create_index(op.f('ix_project_stock_releases_user_id'), 'project_stock_releases', ['user_id'], unique=False)
    op.execute("INSERT INTO number_sequences (name, prefix, next_value, padding) "
               "SELECT 'PROJECT_RELEASE', 'PSR-', 1, 6 WHERE NOT EXISTS "
               "(SELECT 1 FROM number_sequences WHERE name = 'PROJECT_RELEASE')")


def downgrade() -> None:
    op.execute("DELETE FROM number_sequences WHERE name = 'PROJECT_RELEASE'")
    op.drop_table('project_stock_releases')
