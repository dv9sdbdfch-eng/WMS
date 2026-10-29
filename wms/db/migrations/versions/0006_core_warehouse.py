"""core warehouse: bin barcodes, return reason codes

- bins.barcode: what a scanner reads from the bin label (unique). Empty means the label carries
  the bin's full code, which is unique already, so existing bins need no change.
- stock_returns.reason_code: reason code for returns (Returned from Project, Damaged, Supplier
  Return...). Returns posted before this version keep their free-text reason only.

Columns are added with plain ALTER TABLE ADD COLUMN (no table rebuild), so the ledger's foreign
keys and the document triggers are untouched.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('bins', sa.Column('barcode', sa.String(length=80), nullable=True))
    op.create_index('uq_bins_barcode', 'bins', ['barcode'], unique=True)
    op.add_column('stock_returns', sa.Column('reason_code', sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_index('uq_bins_barcode', table_name='bins')
    op.execute("ALTER TABLE bins DROP COLUMN barcode")
    op.execute("ALTER TABLE stock_returns DROP COLUMN reason_code")
