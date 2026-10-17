"""vendor_details

Revision ID: e6a7b8c9d0e2
Revises: e5f6a7b8c9d1
Create Date: 2026-10-17 12:00:00.000000

A vendor keeps what a proper purchase bill needs: legal name, PAN, CIN, FSSAI licence, full address, contact person.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6a7b8c9d0e2"
down_revision: Union[str, None] = "e5f6a7b8c9d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ["legal_name", "pan_number", "cin_number", "fssai_number", "address_line1", "address_line2", "city", "state", "pincode", "contact_person", "alt_phone"]


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("vendors", sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("vendors", name)
