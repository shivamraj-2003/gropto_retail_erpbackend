"""default stock reason codes

The Adjust Stock screen reads its reason list from reason_codes_master, which started empty, so the list showed nothing to pick.
This adds the usual reasons once; anything already there (same category and code) is left alone.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""
import sqlalchemy as sa
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None

# (category, code, description, needs a manager's approval when a cashier or staff member adjusts)
DEFAULTS = [
    ("stock_adjustment", "count_correction", "Stock count was wrong: correct the quantity", False),
    ("stock_adjustment", "found_stock", "Extra stock found on the shelf or in the store room", False),
    ("stock_adjustment", "opening_balance", "Opening balance correction", True),
    ("damage", "damaged", "Damaged goods, cannot be sold", False),
    ("damage", "leakage_breakage", "Leakage or breakage in the store", False),
    ("expiry", "expired", "Expired goods taken off the shelf", False),
    ("wastage", "wastage", "Spoiled or wasted (fresh items, cut loose items)", False),
    ("shrinkage", "theft_loss", "Theft or unexplained loss", True),
    ("shrinkage", "sample_internal_use", "Used as a sample or for the shop's own use", True),
]


def upgrade() -> None:
    conn = op.get_bind()
    for category, code, description, needs_approval in DEFAULTS:
        conn.execute(
            sa.text(
                """
                insert into reason_codes_master (id, category, code, description, requires_approval, is_active)
                select gen_random_uuid(), :category, :code, :description, :approval, true
                where not exists (select 1 from reason_codes_master where category = :category and code = :code)
                """
            ),
            {"category": category, "code": code, "description": description, "approval": needs_approval},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for category, code, _, _ in DEFAULTS:
        conn.execute(sa.text("delete from reason_codes_master where category = :c and code = :k"), {"c": category, "k": code})
