"""project hierarchy: managers, project units, unit materials

- managers: people who look after projects (name, contact, notes, active, order).
- sites.manager_id: optional link from a project to its manager. Added with plain ALTER TABLE ADD
  COLUMN (no table rebuild): stock, documents, the ledger and its triggers are untouched.
  ON DELETE SET NULL - removing a manager never removes a project.
- project_units: unit types a project needs (Kitchen K01 x 24), ordered.
- unit_materials: materials of one unit - a link to the existing product plus the quantity one
  unit needs. Total required (unit qty x qty per unit) is calculated, never stored.

Existing data: every different "Project manager" name already typed on projects becomes a manager
and those projects are linked to it, so the Projects tree starts filled in. Nothing else changes.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01 19:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('managers',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('email', sa.String(length=200), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_managers'))
    )
    op.create_index('ix_managers_name_upper', 'managers', [sa.text('upper(name)')], unique=False)

    bind = op.get_bind()
    if "manager_id" in {c["name"] for c in sa.inspect(bind).get_columns("sites")}:
        pass  # left behind by a downgrade that could not drop it (see downgrade)
    elif bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE sites ADD COLUMN manager_id INTEGER "
                   "CONSTRAINT fk_sites_manager_id_managers REFERENCES managers (id) ON DELETE SET NULL")
    else:
        op.add_column('sites', sa.Column('manager_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_sites_manager_id_managers', 'sites', 'managers', ['manager_id'], ['id'],
                              ondelete='SET NULL')
    op.create_index('ix_sites_manager_id', 'sites', ['manager_id'], unique=False)

    op.create_table('project_units',
    sa.Column('site_id', sa.Integer(), nullable=False),
    sa.Column('unit_code', sa.String(length=50), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('quantity', wms.db.types.Quantity(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_project_units_quantity_not_negative')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_project_units_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name=op.f('fk_project_units_site_id_sites'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_units'))
    )
    op.create_index('ix_project_units_created_by_id', 'project_units', ['created_by_id'], unique=False)
    op.create_index('ix_project_units_name', 'project_units', ['name'], unique=False)
    op.create_index('ix_project_units_site_id', 'project_units', ['site_id'], unique=False)
    op.create_index('ix_project_units_unit_code', 'project_units', ['unit_code'], unique=False)
    op.create_index('ix_project_units_site_order', 'project_units', ['site_id', 'sort_order'], unique=False)

    op.create_table('unit_materials',
    sa.Column('unit_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('quantity_per_unit', wms.db.types.Quantity(), nullable=False),
    sa.Column('vpk_code', sa.String(length=80), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', wms.db.types.UTCDateTime(), nullable=False),
    sa.CheckConstraint('quantity_per_unit >= 0', name=op.f('ck_unit_materials_qty_not_negative')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_unit_materials_created_by_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_unit_materials_product_id_products'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['unit_id'], ['project_units.id'], name=op.f('fk_unit_materials_unit_id_project_units'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_unit_materials'))
    )
    op.create_index('ix_unit_materials_created_by_id', 'unit_materials', ['created_by_id'], unique=False)
    op.create_index('ix_unit_materials_product_id', 'unit_materials', ['product_id'], unique=False)
    op.create_index('ix_unit_materials_unit_id', 'unit_materials', ['unit_id'], unique=False)
    op.create_index('ix_unit_materials_vpk_code', 'unit_materials', ['vpk_code'], unique=False)
    op.create_index('ix_unit_materials_unit_order', 'unit_materials', ['unit_id', 'sort_order'], unique=False)

    _managers_from_existing_names()


def _managers_from_existing_names() -> None:
    """Each different 'Project manager' name typed on a project becomes one manager (names that
    differ only in case or spacing are one person) and the projects are linked to it."""
    from wms.core.numbers import utcnow
    from wms.db.types import UTCDateTime

    bind = op.get_bind()
    sites = bind.execute(sa.text("SELECT id, project_manager FROM sites WHERE project_manager IS NOT NULL")).all()
    names: dict[str, str] = {}
    for _sid, raw in sites:
        name = " ".join(str(raw).split())[:120]
        if name:
            names.setdefault(name.upper(), name)
    now = utcnow()
    ids: dict[str, int] = {}
    for order, key in enumerate(sorted(names), start=1):
        bind.execute(sa.text("INSERT INTO managers (name, sort_order, is_active, created_at, updated_at) "
                             "VALUES (:n, :o, :a, :t, :t)").bindparams(sa.bindparam("t", type_=UTCDateTime()),
                                                                       sa.bindparam("a", type_=sa.Boolean())),
                     {"n": names[key], "o": order, "a": True, "t": now})
        ids[key] = bind.execute(sa.text("SELECT max(id) FROM managers")).scalar()
    for sid, raw in sites:
        key = " ".join(str(raw).split())[:120].upper()
        if key in ids:
            bind.execute(sa.text("UPDATE sites SET manager_id = :m, project_manager = :n WHERE id = :i"),
                         {"m": ids[key], "n": names[key], "i": sid})


def downgrade() -> None:
    op.drop_index('ix_unit_materials_unit_order', table_name='unit_materials')
    op.drop_index('ix_unit_materials_vpk_code', table_name='unit_materials')
    op.drop_index('ix_unit_materials_unit_id', table_name='unit_materials')
    op.drop_index('ix_unit_materials_product_id', table_name='unit_materials')
    op.drop_index('ix_unit_materials_created_by_id', table_name='unit_materials')
    op.drop_table('unit_materials')
    op.drop_index('ix_project_units_site_order', table_name='project_units')
    op.drop_index('ix_project_units_unit_code', table_name='project_units')
    op.drop_index('ix_project_units_site_id', table_name='project_units')
    op.drop_index('ix_project_units_name', table_name='project_units')
    op.drop_index('ix_project_units_created_by_id', table_name='project_units')
    op.drop_table('project_units')
    op.drop_index('ix_sites_manager_id', table_name='sites')
    bind = op.get_bind()
    op.execute("UPDATE sites SET manager_id = NULL")
    if bind.dialect.name == "sqlite":
        # A database created new (not upgraded) declares the link as a table constraint, which
        # SQLite cannot drop without rebuilding the table. The empty column is then left in place
        # (harmless; upgrade reuses it) rather than rebuilding a table other records point at.
        try:
            with bind.begin_nested():
                bind.exec_driver_sql("ALTER TABLE sites DROP COLUMN manager_id")
        except sa.exc.OperationalError:
            pass
    else:
        op.drop_constraint('fk_sites_manager_id_managers', 'sites', type_='foreignkey')
        op.drop_column('sites', 'manager_id')
    op.drop_index('ix_managers_name_upper', table_name='managers')
    op.drop_table('managers')
