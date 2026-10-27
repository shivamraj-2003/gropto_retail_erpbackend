"""Paytm: each store's machine carries its own account details

Additive: four nullable columns on paytm_terminals. Empty means "use the server-wide defaults", so a setup made
before this migration keeps working unchanged.

Revision ID: f3c4d5e6a7b9
Revises: f2b3c4d5e6a8
"""

import sqlalchemy as sa
from alembic import op

revision = "f3c4d5e6a7b9"
down_revision = "f2b3c4d5e6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paytm_terminals", sa.Column("channel_id", sa.String()))
    op.add_column("paytm_terminals", sa.Column("merchant_key_enc", sa.Text()))
    op.add_column("paytm_terminals", sa.Column("environment", sa.String()))
    op.add_column("paytm_terminals", sa.Column("base_url", sa.String()))


def downgrade() -> None:
    for col in ("base_url", "environment", "merchant_key_enc", "channel_id"):
        op.drop_column("paytm_terminals", col)
