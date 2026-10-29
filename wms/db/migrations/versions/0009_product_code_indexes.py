"""case-insensitive indexes on product code and barcode

Duplicate checks, scans and the Excel import look products up by UPPER(code) / UPPER(barcode).
Without an index on the expression every lookup reads the whole products table, which made a
10,000-row Excel import take minutes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29 14:00:00
"""
from alembic import op


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_products_code_upper ON products (upper(internal_code))")
    op.execute("CREATE INDEX IF NOT EXISTS ix_products_barcode_upper ON products (upper(barcode))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_products_barcode_upper")
    op.execute("DROP INDEX IF EXISTS ix_products_code_upper")
