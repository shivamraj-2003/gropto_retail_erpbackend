"""activate_pending_devices

Data-only migration: login dropped the device pending/approval gate (it's
email+password only now), so any device left in 'pending' from before this
change is normalized to 'active' — it would already log in fine either way
since the app no longer checks for 'pending', this just keeps the Devices
screen and any status_filter query consistent with what's actually true.

Revision ID: 31080c1bcb04
Revises: 9203d53d334d
Create Date: 2026-09-28 22:58:46.104427

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '31080c1bcb04'
down_revision: Union[str, None] = '9203d53d334d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("update devices set status = 'active' where status = 'pending'")


def downgrade() -> None:
    pass
