"""A numeric item code for every product, and the head a voucher line's account belongs to.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
"""

import sqlalchemy as sa
from alembic import op

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Item code: a short number staff can type instead of the name or the SKU. Numbers start at 100001.
    op.execute("create sequence if not exists product_item_code_seq start 100001")
    op.add_column("products", sa.Column("item_code", sa.BigInteger(), nullable=True))
    op.execute(
        """update products p set item_code = s.n
           from (select id, 100000 + row_number() over (order by created_at, sku) as n from products) s
           where p.id = s.id"""
    )
    op.execute("select setval('product_item_code_seq', greatest((select coalesce(max(item_code), 100000) from products), 100000))")
    op.alter_column("products", "item_code", server_default=sa.text("nextval('product_item_code_seq')"), nullable=False)
    op.create_unique_constraint("uq_products_item_code", "products", ["item_code"])
    # every product goes to the tills again, now with its code
    op.execute("update products set revision = nextval('global_revision_seq'), updated_at = now()")
    op.add_column("finance_voucher_lines", sa.Column("account_group", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("finance_voucher_lines", "account_group")
    op.drop_constraint("uq_products_item_code", "products", type_="unique")
    op.drop_column("products", "item_code")
    op.execute("drop sequence if exists product_item_code_seq")
