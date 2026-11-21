"""Drawings on projects and units: revision and status of an attached file

- attachments.revision: the drawing's revision as written on it ("C", "P2"); empty for photos and scans.
- attachments.doc_status: PRELIMINARY / APPROVED / SUPERSEDED (empty for photos and scans).
- attachments.superseded_by_id: the newer revision that replaced this file (the old one stays,
  marked Superseded, so it is never lost).

Added with plain ALTER TABLE ADD COLUMN (no table rebuild). Existing attachments keep empty values;
nothing is deleted or rewritten.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-21 22:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

FK = 'fk_attachments_superseded_by_id_attachments'
CK = 'ck_attachments_doc_status_valid'
STATUS_CHECK = "doc_status IS NULL OR doc_status IN ('PRELIMINARY', 'APPROVED', 'SUPERSEDED')"


def upgrade() -> None:
    bind = op.get_bind()
    have = {c["name"] for c in sa.inspect(bind).get_columns('attachments')}
    sqlite = bind.dialect.name == "sqlite"
    if 'revision' not in have:
        op.add_column('attachments', sa.Column('revision', sa.String(length=20), nullable=True))
    if 'doc_status' not in have:
        if sqlite:
            op.execute(f"ALTER TABLE attachments ADD COLUMN doc_status VARCHAR(20) CONSTRAINT {CK} CHECK ({STATUS_CHECK})")
        else:
            op.add_column('attachments', sa.Column('doc_status', sa.String(length=20), nullable=True))
            op.create_check_constraint(CK, 'attachments', STATUS_CHECK)
    if 'superseded_by_id' not in have:
        if sqlite:
            op.execute("ALTER TABLE attachments ADD COLUMN superseded_by_id INTEGER "
                       f"CONSTRAINT {FK} REFERENCES attachments (id) ON DELETE RESTRICT")
        else:
            op.add_column('attachments', sa.Column('superseded_by_id', sa.Integer(), nullable=True))
            op.create_foreign_key(FK, 'attachments', 'attachments', ['superseded_by_id'], ['id'], ondelete='RESTRICT')
    op.create_index(op.f('ix_attachments_superseded_by_id'), 'attachments', ['superseded_by_id'], unique=False)


def downgrade() -> None:
    # The columns stay (SQLite cannot drop a column with a constraint without rebuilding the
    # table); upgrading again reuses them.
    op.drop_index(op.f('ix_attachments_superseded_by_id'), table_name='attachments')
