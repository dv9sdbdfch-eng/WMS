"""inventory control: stock reservations for projects, reorder level

- stock_reservations: quantity of a product set aside for a project (ACTIVE, FULFILLED,
  RELEASED). Not a stock movement. What is still reserved is worked out from the Goods Out
  lines that deliver each reservation (goods_issue_lines.reservation_id).
- products.reorder_level: reorder when Available (On Hand - Reserved) falls to this level.
- Number sequence RES-, permission reservations.edit (Administrator and Store Manager).

New columns are added with plain ALTER TABLE ADD COLUMN (no table rebuild), so the ledger's
foreign keys and the document triggers are untouched.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29 10:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _add_reference(table: str, column: str, target: str) -> None:
    """Nullable foreign key column without rebuilding the table."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        have = {r[1] for r in bind.exec_driver_sql(f"PRAGMA table_info({table})").all()}
        if column not in have:  # kept by an earlier downgrade (see _drop_reference)
            op.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER "
                       f"REFERENCES {target} (id) ON DELETE RESTRICT")
    else:
        op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_{table}_{column}_{target}", table, target, [column], ["id"],
                              ondelete="RESTRICT")
    op.create_index(f"ix_{table}_{column}", table, [column])


def _drop_reference(table: str, column: str, target: str) -> None:
    op.drop_index(f"ix_{table}_{column}", table_name=table)
    if op.get_bind().dialect.name == "sqlite":
        # Dropping the column would mean rebuilding a posted-document table (guarded by triggers,
        # with cascading lines). The emptied column is left in place instead; older versions
        # ignore it and upgrading again reuses it.
        op.execute(f"UPDATE {table} SET {column} = NULL")
    else:
        op.drop_constraint(f"fk_{table}_{column}_{target}", table, type_="foreignkey")
        op.drop_column(table, column)


def upgrade() -> None:
    op.create_table('stock_reservations',
    sa.Column('number', sa.String(length=30), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('required_by', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('released_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('released_by_id', sa.Integer(), nullable=True),
    sa.Column('release_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_number_snapshot', sa.String(length=50), nullable=True),
    sa.Column('site_name_snapshot', sa.String(length=200), nullable=True),
    sa.Column('client_token', sa.String(length=40), nullable=True),
    sa.CheckConstraint("status IN ('ACTIVE', 'FULFILLED', 'RELEASED')", name=op.f('ck_stock_reservations_status_valid')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_stock_reservations_qty_positive')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_stock_reservations_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_stock_reservations_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['released_by_id'], ['users.id'], name=op.f('fk_stock_reservations_released_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_stock_reservations_site_id_sites'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stock_reservations')),
    sa.UniqueConstraint('number', name=op.f('uq_stock_reservations_number'))
    )
    with op.batch_alter_table('stock_reservations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stock_reservations_client_token'), ['client_token'], unique=True)
        batch_op.create_index(batch_op.f('ix_stock_reservations_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_reservations_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_reservations_released_by_id'), ['released_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_reservations_site_id'), ['site_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stock_reservations_status'), ['status'], unique=False)

    _add_reference('goods_issue_lines', 'reservation_id', 'stock_reservations')
    op.add_column('products', sa.Column('reorder_level', wms.db.types.Quantity(), nullable=True))

    op.execute("INSERT INTO number_sequences (name, prefix, next_value, padding) "
               "SELECT 'RESERVATION', 'RES-', 1, 6 WHERE NOT EXISTS "
               "(SELECT 1 FROM number_sequences WHERE name = 'RESERVATION')")
    for role in ("ADMIN", "MANAGER"):
        op.execute("INSERT INTO role_permissions (role_id, permission) "
                   f"SELECT id, 'reservations.edit' FROM roles WHERE code = '{role}' AND NOT EXISTS "
                   "(SELECT 1 FROM role_permissions rp WHERE rp.role_id = roles.id "
                   "AND rp.permission = 'reservations.edit')")


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission = 'reservations.edit'")
    op.execute("DELETE FROM number_sequences WHERE name = 'RESERVATION'")
    op.execute("ALTER TABLE products DROP COLUMN reorder_level")
    _drop_reference('goods_issue_lines', 'reservation_id', 'stock_reservations')
    with op.batch_alter_table('stock_reservations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stock_reservations_status'))
        batch_op.drop_index(batch_op.f('ix_stock_reservations_site_id'))
        batch_op.drop_index(batch_op.f('ix_stock_reservations_released_by_id'))
        batch_op.drop_index(batch_op.f('ix_stock_reservations_product_id'))
        batch_op.drop_index(batch_op.f('ix_stock_reservations_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_stock_reservations_client_token'))

    op.drop_table('stock_reservations')
