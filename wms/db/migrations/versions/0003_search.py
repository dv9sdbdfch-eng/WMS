"""search: product brand, saved and recent searches

- products.brand: searchable / filterable brand (Egger, Blum, Hettich...).
- saved_searches: named filter combinations per user ("Low Stock - Egger").
- recent_searches: each user's last few searches.
No new search indexes: measured on 10,000 products / 100,000 movements, every filter column already
has one, "contains" searches cannot use a B-tree index, and the candidate composite indexes gained
nothing over up-to-date planner statistics (see docs/SEARCH.md and Database.refresh_statistics).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28 22:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('brand', sa.String(length=100), nullable=True))

    op.create_table('saved_searches',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('criteria', sa.Text(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_saved_searches_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_saved_searches')),
    sa.UniqueConstraint('user_id', 'name', name=op.f('uq_saved_searches_user_id'))
    )
    with op.batch_alter_table('saved_searches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_saved_searches_user_id'), ['user_id'], unique=False)

    op.create_table('recent_searches',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('text', sa.String(length=120), nullable=False),
    sa.Column('searched_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_recent_searches_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recent_searches')),
    sa.UniqueConstraint('user_id', 'text', name=op.f('uq_recent_searches_user_id'))
    )
    with op.batch_alter_table('recent_searches', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recent_searches_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('recent_searches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recent_searches_user_id'))
    op.drop_table('recent_searches')
    with op.batch_alter_table('saved_searches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_saved_searches_user_id'))
    op.drop_table('saved_searches')
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('brand')
