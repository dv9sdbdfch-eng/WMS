"""attachment content and server backup log in the database (shared server)

- attachment_files: the bytes of each attached photo or scan, one row per distinct content
  (sha256). Filled on the shared PostgreSQL server, so every PC in every building sees the same
  files without a network share; each PC keeps a local copy as a cache. The local SQLite file
  keeps using the attachments folder and leaves this table empty.

- server_backups: one row per backup made by the server's scheduled script, so every PC shows
  when the last good copy was made (empty with the local SQLite file).

Nothing else changes: attachments, stock and documents are not touched.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-14 18:00:00
"""
import sqlalchemy as sa
from alembic import op

import wms.db.types

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('attachment_files',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('thumb', sa.LargeBinary(), nullable=True),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('sha256', name=op.f('pk_attachment_files'))
    )
    op.create_table('server_backups',
    sa.Column('finished_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('file_name', sa.String(length=300), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_server_backups'))
    )
    op.create_index(op.f('ix_server_backups_finished_at'), 'server_backups', ['finished_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_server_backups_finished_at'), table_name='server_backups')
    op.drop_table('server_backups')
    op.drop_table('attachment_files')
