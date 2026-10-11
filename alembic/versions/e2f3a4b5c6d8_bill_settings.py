"""bill_settings

Revision ID: e2f3a4b5c6d8
Revises: d1e2f3a4b5c7
Create Date: 2026-10-11 12:00:00.000000

The wording printed on every bill moves out of the app code into the database, per store with a company-wide default.
The default row starts with the details from the current printed bill; they can be changed on the Bill & Receipt screen.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2f3a4b5c6d8"
down_revision: Union[str, None] = "d1e2f3a4b5c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bill_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id", ondelete="CASCADE"), unique=True),
        sa.Column("heading", sa.String()),
        sa.Column("brand_name", sa.String()),
        sa.Column("legal_name", sa.String()),
        sa.Column("address", sa.Text()),
        sa.Column("gstin", sa.String()),
        sa.Column("fssai_no", sa.String()),
        sa.Column("cin_no", sa.String()),
        sa.Column("phone", sa.String()),
        sa.Column("website", sa.String()),
        sa.Column("footer_text", sa.Text()),
        sa.Column("terms_text", sa.Text()),
        sa.Column("sign_off", sa.String()),
        sa.Column("thanks_text", sa.String()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # only one company-wide default row may exist (a unique column alone allows many nulls)
    op.execute("create unique index uq_bill_settings_default on bill_settings ((store_id is null)) where store_id is null")
    op.execute(
        """
        insert into bill_settings (heading, brand_name, legal_name, address, gstin, fssai_no, cin_no, phone, website,
                                   footer_text, terms_text, sign_off, thanks_text)
        values ('TAX INVOICE/RECEIPT', 'GROPTO MART', 'GROPTO TECHNOLOGIES PVT LTD',
                'SHOP NO:- B-236, TRANS YAMUNA COLONY , RAMBAGH AGRA', '09AAJCG6999A1Z5', '12723001000868',
                'U52322UP2022PTC162250', '7902096353', 'https://www.gropto.com/',
                E'For Latest Offer / Update Please whatsapp at 7902096353\nhttps://www.gropto.com/\nFollow us on Facebook / Instagram\nNow We Are Available For Home Delivery, Call Or Whatsapp @ 7902096353',
                E'1. MRP (Inclusive of all taxes)\n2. All EXCHANGE / Complaints Must be Supported By Invoice\n3. Any Replacement after 15 days for bill date will not be accepted\n4. All Dispute Subject To Jurisdiction Agra Only.',
                'For "GROPTO MART"', '*****Thanks for your visit *****')
        """
    )


def downgrade() -> None:
    op.drop_table("bill_settings")
