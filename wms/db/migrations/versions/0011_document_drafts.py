"""document drafts

- document_drafts: unfinished forms (a Goods In with many lines) saved to finish later. Only the
  form's content as JSON; never stock, never the ledger. Posting the form deletes its draft.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-31 20:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('document_drafts',
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('data', sa.Text(), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_document_drafts_created_by_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_drafts'))
    )
    with op.batch_alter_table('document_drafts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_drafts_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_drafts_kind'), ['kind'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('document_drafts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_drafts_kind'))
        batch_op.drop_index(batch_op.f('ix_document_drafts_created_by_id'))
    op.drop_table('document_drafts')
