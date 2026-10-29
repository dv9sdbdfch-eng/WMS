"""purchasing: purchase orders, partial deliveries, delivery discrepancies

- purchase_orders / purchase_order_lines: what was ordered from a supplier (DRAFT, ORDERED,
  PARTIALLY_RECEIVED, RECEIVED, CANCELLED). Received / outstanding quantities are not stored:
  they are worked out from the posted delivery note lines that point at each order line.
- delivery_notes.purchase_order_id and delivery_note_lines.purchase_order_line_id: a delivery
  note can deliver (part of) an order. Older delivery notes keep their free-text PO reference.
- delivery_discrepancies: short / over / damaged / rejected / wrong goods, with photos as
  attachments; open ones are shown on the dashboard.
- Number sequences PO- and DSC-, permissions purchasing.view (every role) and purchasing.edit
  (Administrator and Store Manager).

The two new delivery note columns are added with plain ALTER TABLE ADD COLUMN (no table rebuild),
so the ledger's foreign keys and the document triggers are untouched.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29 09:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0007'
down_revision = '0006'
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
        # Dropping the column would mean rebuilding the delivery note tables (posted documents,
        # guarded by triggers, with cascading lines). The emptied column is left in place instead;
        # older versions ignore it and upgrading again reuses it.
        op.execute(f"UPDATE {table} SET {column} = NULL")
    else:
        op.drop_constraint(f"fk_{table}_{column}_{target}", table, type_="foreignkey")
        op.drop_column(table, column)


def upgrade() -> None:
    op.create_table('purchase_orders',
    sa.Column('po_number', sa.String(length=30), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('supplier_name_snapshot', sa.String(length=200), nullable=True),
    sa.Column('site_id', sa.Integer(), nullable=True),
    sa.Column('order_date', sa.Date(), nullable=False),
    sa.Column('expected_date', sa.Date(), nullable=True),
    sa.Column('supplier_reference', sa.String(length=80), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('ordered_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('closed_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('closed_by_id', sa.Integer(), nullable=True),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('site_number_snapshot', sa.String(length=50), nullable=True),
    sa.Column('site_name_snapshot', sa.String(length=200), nullable=True),
    sa.Column('client_token', sa.String(length=40), nullable=True),
    sa.CheckConstraint("status IN ('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED')", name=op.f('ck_purchase_orders_status_valid')),
    sa.ForeignKeyConstraint(['closed_by_id'], ['users.id'], name=op.f('fk_purchase_orders_closed_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_purchase_orders_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_purchase_orders_site_id_sites'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_purchase_orders_supplier_id_suppliers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_purchase_orders')),
    sa.UniqueConstraint('po_number', name=op.f('uq_purchase_orders_po_number'))
    )
    with op.batch_alter_table('purchase_orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_purchase_orders_client_token'), ['client_token'], unique=True)
        batch_op.create_index(batch_op.f('ix_purchase_orders_closed_by_id'), ['closed_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_expected_date'), ['expected_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_order_date'), ['order_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_site_id'), ['site_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_orders_supplier_id'), ['supplier_id'], unique=False)

    op.create_table('purchase_order_lines',
    sa.Column('purchase_order_id', sa.Integer(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('unit_cost', wms.db.types.Money(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_purchase_order_lines_qty_positive')),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_purchase_order_lines_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name=op.f('fk_purchase_order_lines_purchase_order_id_purchase_orders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_purchase_order_lines')),
    sa.UniqueConstraint('purchase_order_id', 'line_no', name=op.f('uq_purchase_order_lines_purchase_order_id'))
    )
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_purchase_order_lines_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_purchase_order_lines_purchase_order_id'), ['purchase_order_id'], unique=False)

    op.create_table('delivery_discrepancies',
    sa.Column('number', sa.String(length=30), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('supplier_name_snapshot', sa.String(length=200), nullable=True),
    sa.Column('purchase_order_id', sa.Integer(), nullable=True),
    sa.Column('purchase_order_line_id', sa.Integer(), nullable=True),
    sa.Column('delivery_note_id', sa.Integer(), nullable=True),
    sa.Column('product_id', sa.Integer(), nullable=True),
    sa.Column('reason_code', sa.String(length=30), nullable=False),
    sa.Column('expected_qty', wms.db.types.Quantity(), nullable=True),
    sa.Column('received_qty', wms.db.types.Quantity(), nullable=True),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('is_automatic', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('resolved_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('resolved_by_id', sa.Integer(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('client_token', sa.String(length=40), nullable=True),
    sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name=op.f('ck_delivery_discrepancies_status_valid')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_delivery_discrepancies_qty_not_negative')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_delivery_discrepancies_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['delivery_note_id'], ['delivery_notes.id'], name=op.f('fk_delivery_discrepancies_delivery_note_id_delivery_notes'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_delivery_discrepancies_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name=op.f('fk_delivery_discrepancies_purchase_order_id_purchase_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['purchase_order_line_id'], ['purchase_order_lines.id'], name=op.f('fk_delivery_discrepancies_purchase_order_line_id_purchase_order_lines'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reason_code'], ['reason_codes.code'], name=op.f('fk_delivery_discrepancies_reason_code_reason_codes'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['resolved_by_id'], ['users.id'], name=op.f('fk_delivery_discrepancies_resolved_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_delivery_discrepancies_supplier_id_suppliers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_delivery_discrepancies')),
    sa.UniqueConstraint('number', name=op.f('uq_delivery_discrepancies_number'))
    )
    with op.batch_alter_table('delivery_discrepancies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_client_token'), ['client_token'], unique=True)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_delivery_note_id'), ['delivery_note_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_purchase_order_id'), ['purchase_order_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_purchase_order_line_id'), ['purchase_order_line_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_reason_code'), ['reason_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_resolved_by_id'), ['resolved_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_delivery_discrepancies_supplier_id'), ['supplier_id'], unique=False)

    _add_reference('delivery_note_lines', 'purchase_order_line_id', 'purchase_order_lines')
    _add_reference('delivery_notes', 'purchase_order_id', 'purchase_orders')

    op.execute("INSERT INTO number_sequences (name, prefix, next_value, padding) "
               "SELECT 'PURCHASE_ORDER', 'PO-', 1, 6 WHERE NOT EXISTS "
               "(SELECT 1 FROM number_sequences WHERE name = 'PURCHASE_ORDER')")
    op.execute("INSERT INTO number_sequences (name, prefix, next_value, padding) "
               "SELECT 'DISCREPANCY', 'DSC-', 1, 6 WHERE NOT EXISTS "
               "(SELECT 1 FROM number_sequences WHERE name = 'DISCREPANCY')")
    for perm, roles in (("purchasing.view", ("ADMIN", "MANAGER", "OPERATIVE", "VIEWER")),
                        ("purchasing.edit", ("ADMIN", "MANAGER"))):
        for role in roles:
            op.execute("INSERT INTO role_permissions (role_id, permission) "
                       f"SELECT id, '{perm}' FROM roles WHERE code = '{role}' AND NOT EXISTS "
                       "(SELECT 1 FROM role_permissions rp WHERE rp.role_id = roles.id "
                       f"AND rp.permission = '{perm}')")
    # ### end Alembic commands ###


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions WHERE permission IN ('purchasing.view', 'purchasing.edit')")
    op.execute("DELETE FROM number_sequences WHERE name IN ('PURCHASE_ORDER', 'DISCREPANCY')")
    _drop_reference('delivery_notes', 'purchase_order_id', 'purchase_orders')
    _drop_reference('delivery_note_lines', 'purchase_order_line_id', 'purchase_order_lines')

    with op.batch_alter_table('delivery_discrepancies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_supplier_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_status'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_resolved_by_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_reason_code'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_purchase_order_line_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_purchase_order_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_product_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_delivery_note_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_delivery_discrepancies_client_token'))

    op.drop_table('delivery_discrepancies')
    with op.batch_alter_table('purchase_order_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_purchase_order_lines_purchase_order_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_order_lines_product_id'))

    op.drop_table('purchase_order_lines')
    with op.batch_alter_table('purchase_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_purchase_orders_supplier_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_status'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_site_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_order_date'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_expected_date'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_closed_by_id'))
        batch_op.drop_index(batch_op.f('ix_purchase_orders_client_token'))

    op.drop_table('purchase_orders')
    # ### end Alembic commands ###

