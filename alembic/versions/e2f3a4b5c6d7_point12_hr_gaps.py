"""Point 12 audit fix: HR gaps — Employee.name/department_id/warehouse_id,
a real employee<->user uniqueness constraint, and a dedicated hr.manage
permission split out of the previously-shared user.manage code (granted to
every role that already held user.manage, so access doesn't regress).

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("name", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), nullable=True))
    op.add_column("employees", sa.Column("warehouse_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("warehouses.id"), nullable=True))

    # Partial unique index: a login account may back at most one employee
    # record (NULLs excluded so most employees, who have no login, aren't
    # constrained against each other).
    op.create_index(
        "uq_employees_user_id", "employees", ["user_id"], unique=True, postgresql_where=sa.text("user_id IS NOT NULL")
    )

    conn = op.get_bind()
    permission_id = conn.execute(
        sa.text("insert into permissions (id, code) values (gen_random_uuid(), 'hr.manage') returning id")
    ).scalar_one()
    user_manage_id = conn.execute(sa.text("select id from permissions where code = 'user.manage'")).scalar_one_or_none()
    if user_manage_id is not None:
        conn.execute(
            sa.text(
                "insert into role_permissions (role_id, permission_id) "
                "select role_id, :new_perm from role_permissions where permission_id = :old_perm "
                "on conflict do nothing"
            ),
            {"new_perm": permission_id, "old_perm": user_manage_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    permission_id = conn.execute(sa.text("select id from permissions where code = 'hr.manage'")).scalar_one_or_none()
    if permission_id is not None:
        conn.execute(sa.text("delete from role_permissions where permission_id = :p"), {"p": permission_id})
        conn.execute(sa.text("delete from permissions where id = :p"), {"p": permission_id})

    op.drop_index("uq_employees_user_id", table_name="employees")
    op.drop_column("employees", "warehouse_id")
    op.drop_column("employees", "department_id")
    op.drop_column("employees", "name")
