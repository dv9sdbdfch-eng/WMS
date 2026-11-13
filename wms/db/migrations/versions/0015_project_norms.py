"""projects: duplicate-safe names, norm history per unit material, completed units

- sites.name_key: the project name as compared for duplicates (case, spaces and punctuation
  ignored). Plain ALTER TABLE ADD COLUMN + index, filled for every existing project. Projects that
  already share a name keep working; the check applies to new names only.
- unit_material_norms: every value a unit material's norm (quantity per unit) has had, when, who
  and whether it applied to ALL units or only to FUTURE ones. Each existing line gets its first
  row (its current norm, dated when the line was added).
- unit_completions: how many units of a unit type were made / installed and when (cancellable,
  never edited).

Stock, the ledger, documents and Required figures are not touched.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-13 23:30:00
"""
import re

import sqlalchemy as sa
from alembic import op

import wms.db.types

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def _key(name):  # frozen copy of wms.services.sites.project_name_key
    return re.sub(r"[\W_]+", " ", (name or "").casefold()).strip() or None


def upgrade() -> None:
    bind = op.get_bind()
    if "name_key" not in {c["name"] for c in sa.inspect(bind).get_columns("sites")}:
        op.execute("ALTER TABLE sites ADD COLUMN name_key VARCHAR(200)")
    op.create_index(op.f('ix_sites_name_key'), 'sites', ['name_key'], unique=False)
    for sid, name in bind.execute(sa.text("SELECT id, name FROM sites")).fetchall():
        bind.execute(sa.text("UPDATE sites SET name_key = :k WHERE id = :i"), {"k": _key(name), "i": sid})

    op.create_table('unit_material_norms',
    sa.Column('unit_material_id', sa.Integer(), nullable=False),
    sa.Column('quantity_per_unit', wms.db.types.Quantity(), nullable=False),
    sa.Column('previous_qty', wms.db.types.Quantity(), nullable=True),
    sa.Column('applies_to', sa.String(length=10), nullable=False),
    sa.Column('changed_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('changed_by_id', sa.Integer(), nullable=True),
    sa.Column('changed_by_name_snapshot', sa.String(length=120), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.CheckConstraint("applies_to IN ('ALL', 'FUTURE')", name=op.f('ck_unit_material_norms_applies_to_valid')),
    sa.CheckConstraint('quantity_per_unit >= 0', name=op.f('ck_unit_material_norms_qty_not_negative')),
    sa.ForeignKeyConstraint(['changed_by_id'], ['users.id'], name=op.f('fk_unit_material_norms_changed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['unit_material_id'], ['unit_materials.id'], name=op.f('fk_unit_material_norms_unit_material_id_unit_materials'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unit_material_norms'))
    )
    op.create_index(op.f('ix_unit_material_norms_changed_by_id'), 'unit_material_norms', ['changed_by_id'], unique=False)
    op.create_index('ix_unit_material_norms_line_time', 'unit_material_norms', ['unit_material_id', 'changed_at'], unique=False)

    op.create_table('unit_completions',
    sa.Column('unit_id', sa.Integer(), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('completed_on', sa.Date(), nullable=False),
    sa.Column('recorded_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('user_name_snapshot', sa.String(length=120), nullable=True),
    sa.Column('cancelled_at', wms.db.types.UTCDateTime(), nullable=True),
    sa.Column('cancelled_by_id', sa.Integer(), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_unit_completions_qty_positive')),
    sa.ForeignKeyConstraint(['cancelled_by_id'], ['users.id'], name=op.f('fk_unit_completions_cancelled_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['unit_id'], ['project_units.id'], name=op.f('fk_unit_completions_unit_id_project_units'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_unit_completions_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unit_completions'))
    )
    op.create_index(op.f('ix_unit_completions_cancelled_by_id'), 'unit_completions', ['cancelled_by_id'], unique=False)
    op.create_index(op.f('ix_unit_completions_completed_on'), 'unit_completions', ['completed_on'], unique=False)
    op.create_index(op.f('ix_unit_completions_user_id'), 'unit_completions', ['user_id'], unique=False)
    op.create_index('ix_unit_completions_unit_time', 'unit_completions', ['unit_id', 'recorded_at'], unique=False)

    # first norm row of every existing line: its current norm, from when the line was added
    bind.execute(sa.text(
        "INSERT INTO unit_material_norms (unit_material_id, quantity_per_unit, previous_qty, applies_to, changed_at, "
        "reason, changed_by_id, changed_by_name_snapshot) "
        "SELECT id, quantity_per_unit, NULL, 'ALL', created_at, 'Norm when the line was added', created_by_id, NULL "
        "FROM unit_materials"))


def downgrade() -> None:
    op.drop_index('ix_unit_completions_unit_time', table_name='unit_completions')
    op.drop_index(op.f('ix_unit_completions_user_id'), table_name='unit_completions')
    op.drop_index(op.f('ix_unit_completions_completed_on'), table_name='unit_completions')
    op.drop_index(op.f('ix_unit_completions_cancelled_by_id'), table_name='unit_completions')
    op.drop_table('unit_completions')
    op.drop_index('ix_unit_material_norms_line_time', table_name='unit_material_norms')
    op.drop_index(op.f('ix_unit_material_norms_changed_by_id'), table_name='unit_material_norms')
    op.drop_table('unit_material_norms')
    op.drop_index(op.f('ix_sites_name_key'), table_name='sites')
    # sites.name_key stays (SQLite cannot drop a column of a table with foreign keys without a
    # rebuild); upgrading again reuses it.
