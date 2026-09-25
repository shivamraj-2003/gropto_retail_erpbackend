"""grant admin the loyalty.configure permission

Revision ID: 2b1a9c3f7e5a
Revises: 35af731b18aa
Create Date: 2026-09-25 12:00:00.000000

Admin can already *request* price changes (product.update) that get gated by the
approval engine for anyone but Super Admin; loyalty/discount config changes were
missing the equivalent grant, which blocked Admin from even submitting a request.
submit_or_apply() already routes non-Super-Admin callers through the approval
engine — this migration only restores Admin's ability to call the endpoint at all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2b1a9c3f7e5a"
down_revision: Union[str, None] = "35af731b18aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    admin_role_id = conn.execute(sa.text("select id from roles where code = 'admin'")).scalar_one()
    permission_id = conn.execute(sa.text("select id from permissions where code = 'loyalty.configure'")).scalar_one()
    conn.execute(
        sa.text(
            "insert into role_permissions (role_id, permission_id) values (:r, :p) "
            "on conflict do nothing"
        ),
        {"r": admin_role_id, "p": permission_id},
    )


def downgrade() -> None:
    conn = op.get_bind()
    admin_role_id = conn.execute(sa.text("select id from roles where code = 'admin'")).scalar_one()
    permission_id = conn.execute(sa.text("select id from permissions where code = 'loyalty.configure'")).scalar_one()
    conn.execute(
        sa.text("delete from role_permissions where role_id = :r and permission_id = :p"),
        {"r": admin_role_id, "p": permission_id},
    )
