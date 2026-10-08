"""product_barcodes_transfer_docs

Revision ID: d1e2f3a4b5c7
Revises: c0d1e2f3a4b6
Create Date: 2026-10-09 12:00:00.000000

A product can carry several barcodes (the main one stays on products.barcode;
the others live in product_barcodes). Transfers become proper documents: a
running TR number and an optional note.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e2f3a4b5c7"
down_revision: Union[str, None] = "c0d1e2f3a4b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_barcodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("barcode", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_product_barcodes_product", "product_barcodes", ["product_id"])

    op.execute("create sequence if not exists transfer_no_seq")
    op.add_column("transfers", sa.Column("transfer_no", sa.BigInteger(), server_default=sa.text("nextval('transfer_no_seq')"), nullable=False))
    op.create_index("idx_transfers_transfer_no", "transfers", ["transfer_no"], unique=True)
    op.add_column("transfers", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("transfers", "note")
    op.drop_index("idx_transfers_transfer_no", table_name="transfers")
    op.drop_column("transfers", "transfer_no")
    op.execute("drop sequence if exists transfer_no_seq")
    op.drop_index("idx_product_barcodes_product", table_name="product_barcodes")
    op.drop_table("product_barcodes")
