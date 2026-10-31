"""project material lists

- project_materials: the quantity of a product a project requires (one row per project and
  product). Planning data only - it never changes stock and creates no movement. Reserved,
  Issued and Returned are always calculated from reservations and posted movements.
- ix_stock_movements_product_type_time: "last received / last issued" per product. Measured on
  100,000 products / 600,000 movements: sorting Materials by Last Received 734 ms -> see
  docs/MATERIALS.md.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31 10:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('project_materials',
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('required_qty', wms.db.types.Quantity(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.CheckConstraint('required_qty >= 0', name=op.f('ck_project_materials_required_not_negative')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_project_materials_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_project_materials_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_project_materials_site_id_sites'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_materials')),
    sa.UniqueConstraint('site_id', 'product_id', name=op.f('uq_project_materials_site_id'))
    )
    with op.batch_alter_table('project_materials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_project_materials_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_materials_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_materials_site_id'), ['site_id'], unique=False)
    op.create_index('ix_stock_movements_product_type_time', 'stock_movements',
                    ['product_id', 'movement_type', 'occurred_at'])


def downgrade() -> None:
    op.drop_index('ix_stock_movements_product_type_time', table_name='stock_movements')
    with op.batch_alter_table('project_materials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_materials_site_id'))
        batch_op.drop_index(batch_op.f('ix_project_materials_product_id'))
        batch_op.drop_index(batch_op.f('ix_project_materials_created_by_id'))
    op.drop_table('project_materials')
