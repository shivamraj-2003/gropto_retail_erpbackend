"""Purchase lines carry the vendor bill's own columns (batch, free/reject/leakage qty, discount, GST code, cess, surcharge, net)

Additive. Old rows keep working: every new column is nullable or defaults to 0, and a line with no GST code or net amount
is read the way it always was (quantity x cost, GST at the product's rate). The cost column is widened from 2 to 4
decimals (vendors quote rates like 6.857); widening never changes a stored value.

Revision ID: f4d5e6f7a8ba
Revises: f3c4d5e6a7b9
"""

import sqlalchemy as sa
from alembic import op

revision = "f4d5e6f7a8ba"
down_revision = "f3c4d5e6a7b9"
branch_labels = None
depends_on = None

_NEW = [
    ("bill_serial", sa.String(), None),
    ("mrp", sa.Numeric(12, 2), None),
    ("batch_no", sa.String(), None),
    ("hsn_code", sa.String(), None),
    ("free_qty", sa.Numeric(12, 3), "0"),
    ("reject_qty", sa.Numeric(12, 3), "0"),
    ("hl_qty", sa.Numeric(12, 3), "0"),
    ("rate_unit", sa.String(), None),
    ("discount_amount", sa.Numeric(12, 2), "0"),
    ("gst_rate", sa.Numeric(5, 2), None),
    ("gst_amount", sa.Numeric(12, 2), None),
    ("cess_amount", sa.Numeric(12, 2), "0"),
    ("surcharge_rate", sa.Numeric(5, 2), "0"),
    ("surcharge_amount", sa.Numeric(12, 2), "0"),
    ("net_amount", sa.Numeric(12, 2), None),
]


def upgrade() -> None:
    op.alter_column("purchase_items", "unit_cost", existing_type=sa.Numeric(12, 2), type_=sa.Numeric(12, 4), existing_nullable=False)
    for name, kind, default in _NEW:
        op.add_column("purchase_items", sa.Column(name, kind, server_default=default))


def downgrade() -> None:
    for name, _kind, _default in reversed(_NEW):
        op.drop_column("purchase_items", name)
    op.alter_column("purchase_items", "unit_cost", existing_type=sa.Numeric(12, 4), type_=sa.Numeric(12, 2), existing_nullable=False)
