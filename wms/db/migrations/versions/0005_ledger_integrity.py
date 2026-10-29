"""ledger integrity: reason codes, reversal records, stocktake adjustment type

- reason_codes: configurable reasons for adjustments, reversals, returns and delivery
  discrepancies (Damaged, Missing, Stocktake Correction, ...). Seeded with the codes the
  application already used, so existing adjustments keep their labels.
- movement_reversals: who reversed which movement, when and why (append-only). Filled for
  reversals made before this upgrade from the ledger itself (reason = the note on the reversal).
- stock_movements: a movement can be reversed once only (unique index), a reversal must mirror
  its original and cannot be reversed again (triggers, see wms.db.engine.ledger_trigger_sql).
- Movement type STOCKTAKE_ADJUSTMENT is new; no schema change is needed for it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29 02:00:00
"""
from alembic import op
import sqlalchemy as sa
import wms.db.types


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

# code, label, adjustment, reversal, return, discrepancy, system, sort
REASON_CODES = (
    ("DAMAGED", "Damaged", 1, 1, 1, 1, 0, 10),
    ("MISSING", "Missing", 1, 0, 0, 1, 0, 20),
    ("STOCK_COUNT", "Stocktake Correction", 1, 0, 0, 0, 1, 30),
    ("INCORRECT_DELIVERY", "Incorrect Delivery", 0, 1, 1, 1, 0, 40),
    ("OVER_DELIVERED", "Over-delivered", 0, 0, 0, 1, 0, 45),
    ("REJECTED", "Rejected", 0, 0, 1, 1, 0, 46),
    ("RETURNED_FROM_PROJECT", "Returned from Project", 0, 0, 1, 0, 0, 50),
    ("SUPPLIER_RETURN", "Supplier Return", 0, 0, 1, 0, 0, 60),
    ("SCRAP", "Scrap", 1, 0, 0, 0, 0, 70),
    ("LOST", "Lost", 1, 0, 0, 0, 0, 75),
    ("FOUND", "Found", 1, 0, 0, 0, 0, 76),
    ("DATA_ENTRY_ERROR", "Data Entry Correction", 1, 1, 0, 0, 1, 80),
    ("POSTED_TWICE", "Posted Twice", 0, 1, 0, 0, 0, 85),
    ("OPENING_BALANCE", "Opening Balance", 1, 0, 0, 0, 1, 90),
    ("OTHER", "Other", 1, 1, 1, 1, 1, 999),
)


def upgrade() -> None:
    reason_codes = op.create_table(
        'reason_codes',
        sa.Column('code', sa.String(length=30), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('for_adjustment', sa.Boolean(), nullable=False),
        sa.Column('for_reversal', sa.Boolean(), nullable=False),
        sa.Column('for_return', sa.Boolean(), nullable=False),
        sa.Column('for_discrepancy', sa.Boolean(), nullable=False),
        sa.Column('is_system', sa.Boolean(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_reason_codes')),
        sa.UniqueConstraint('code', name=op.f('uq_reason_codes_code')),
    )
    op.bulk_insert(reason_codes, [
        {"code": c, "label": l, "for_adjustment": bool(a), "for_reversal": bool(r), "for_return": bool(rt),
         "for_discrepancy": bool(d), "is_system": bool(sy), "sort_order": so, "is_active": True}
        for c, l, a, r, rt, d, sy, so in REASON_CODES])

    op.create_table(
        'movement_reversals',
        sa.Column('original_movement_id', sa.Integer(), nullable=False),
        sa.Column('reversal_movement_id', sa.Integer(), nullable=False),
        sa.Column('reason_code', sa.String(length=30), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('reversed_by_id', sa.Integer(), nullable=False),
        sa.Column('reversed_by_name_snapshot', sa.String(length=120), nullable=True),
        sa.Column('reversed_at', wms.db.types.UTCDateTime(), nullable=False),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.CheckConstraint('original_movement_id <> reversal_movement_id',
                           name=op.f('ck_movement_reversals_not_self')),
        sa.ForeignKeyConstraint(['original_movement_id'], ['stock_movements.id'], ondelete='RESTRICT',
                                name=op.f('fk_movement_reversals_original_movement_id_stock_movements')),
        sa.ForeignKeyConstraint(['reversal_movement_id'], ['stock_movements.id'], ondelete='RESTRICT',
                                name=op.f('fk_movement_reversals_reversal_movement_id_stock_movements')),
        sa.ForeignKeyConstraint(['reason_code'], ['reason_codes.code'], ondelete='RESTRICT',
                                name=op.f('fk_movement_reversals_reason_code_reason_codes')),
        sa.ForeignKeyConstraint(['reversed_by_id'], ['users.id'], ondelete='RESTRICT',
                                name=op.f('fk_movement_reversals_reversed_by_id_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_movement_reversals')),
        sa.UniqueConstraint('original_movement_id', name=op.f('uq_movement_reversals_original_movement_id')),
        sa.UniqueConstraint('reversal_movement_id', name=op.f('uq_movement_reversals_reversal_movement_id')),
    )
    op.create_index(op.f('ix_movement_reversals_reason_code'), 'movement_reversals', ['reason_code'])
    op.create_index(op.f('ix_movement_reversals_reversed_by_id'), 'movement_reversals', ['reversed_by_id'])

    # Reversals made before this upgrade: the ledger already links them; record who/when/why.
    op.execute(
        "INSERT INTO movement_reversals (original_movement_id, reversal_movement_id, reason_code, reason, "
        "reversed_by_id, reversed_by_name_snapshot, reversed_at) "
        "SELECT r.reversal_of_id, r.id, 'OTHER', "
        "CASE WHEN r.notes LIKE 'REVERSAL: %' THEN substr(r.notes, 11) ELSE coalesce(r.notes, 'Reversed') END, "
        "r.user_id, r.user_name_snapshot, r.recorded_at "
        "FROM stock_movements r WHERE r.reversal_of_id IS NOT NULL ORDER BY r.id")

    op.execute("CREATE UNIQUE INDEX uq_stock_movements_reversal_of ON stock_movements (reversal_of_id) "
               "WHERE reversal_of_id IS NOT NULL")

    from wms.db.engine import ledger_trigger_sql
    for stmt in ledger_trigger_sql(op.get_bind().dialect.name):
        op.execute(stmt)


def downgrade() -> None:
    for name in ("trg_stock_movements_reversal_of_reversal", "trg_stock_movements_reversal_mirror",
                 "trg_movement_reversals_no_update", "trg_movement_reversals_no_delete"):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS uq_stock_movements_reversal_of")
    op.drop_table('movement_reversals')
    op.drop_table('reason_codes')
