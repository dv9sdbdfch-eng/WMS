"""project material lists follow the project units automatically

- project_materials.units_qty: how much of Required came from the project's units (Project Tree).
  From now on every change to units, quantities or unit materials moves Required by exactly that
  change, so figures typed or imported by hand are kept and nobody has to press
  "Update Material List" any more.

Existing data: on every row the current unit demand is taken as already counted (nothing on the
list changes). Materials that the units of an open project need but that are not on its list yet
are added with Required = what the units need. Stock and the ledger are not touched.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-07 23:00:00
"""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

CLOSED = ("COMPLETED", "CANCELLED", "ARCHIVED")


def upgrade() -> None:
    bind = op.get_bind()
    if "units_qty" not in {c["name"] for c in sa.inspect(bind).get_columns("project_materials")}:
        # plain ADD COLUMN: no table rebuild
        op.execute("ALTER TABLE project_materials ADD COLUMN units_qty INTEGER NOT NULL DEFAULT 0")
    rows = bind.execute(sa.text(
        "SELECT pu.site_id, um.product_id, s.status, SUM(pu.quantity * um.quantity_per_unit) "
        "FROM unit_materials um JOIN project_units pu ON pu.id = um.unit_id JOIN sites s ON s.id = pu.site_id "
        "GROUP BY pu.site_id, um.product_id, s.status")).fetchall()
    now = datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")
    for site_id, product_id, status, raw in rows:
        # thousandths x thousandths -> thousandths, rounded half up like the screens do
        qty = int((Decimal(int(raw or 0)) / 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        found = bind.execute(sa.text("SELECT id FROM project_materials WHERE site_id = :s AND product_id = :p"),
                             {"s": site_id, "p": product_id}).scalar()
        if found is not None:
            bind.execute(sa.text("UPDATE project_materials SET units_qty = :q WHERE id = :i"),
                         {"q": qty, "i": found})
        elif status not in CLOSED and qty > 0:
            bind.execute(sa.text(
                "INSERT INTO project_materials (site_id, product_id, required_qty, units_qty, notes, created_at, "
                "updated_at) VALUES (:s, :p, :q, :q, NULL, :t, :t)"), {"s": site_id, "p": product_id, "q": qty,
                                                                     "t": now})


def downgrade() -> None:
    with op.batch_alter_table('project_materials', schema=None) as batch_op:
        batch_op.drop_column('units_qty')
