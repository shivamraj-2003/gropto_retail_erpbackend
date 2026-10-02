"""Point 15 audit fix: master-data governance gaps. Adds is_active (deactivate
support) to companies/clusters/departments/reason_codes_master/
chart_of_accounts/regions — none of these could previously be retired once
created. Adds the unique constraints every one of these catalogue tables was
missing except Store/Warehouse/Vendor (companies.gstin/pan — partial, nulls
allowed; clusters.region_code; departments.code; payment_modes_master.code;
reason_codes_master (category, code); chart_of_accounts.account_code).
Confirmed no existing duplicate rows before adding any of these constraints.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("clusters", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("departments", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("reason_codes_master", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("chart_of_accounts", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("regions", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))

    op.create_index(
        "uq_companies_gstin", "companies", ["gstin"], unique=True, postgresql_where=sa.text("gstin IS NOT NULL")
    )
    op.create_index(
        "uq_companies_pan", "companies", ["pan"], unique=True, postgresql_where=sa.text("pan IS NOT NULL")
    )
    op.create_unique_constraint("uq_clusters_region_code", "clusters", ["region_code"])
    op.create_unique_constraint("uq_departments_code", "departments", ["code"])
    op.create_unique_constraint("uq_payment_modes_master_code", "payment_modes_master", ["code"])
    op.create_unique_constraint("uq_reason_codes_master_category_code", "reason_codes_master", ["category", "code"])
    op.create_unique_constraint("uq_chart_of_accounts_account_code", "chart_of_accounts", ["account_code"])


def downgrade() -> None:
    op.drop_constraint("uq_chart_of_accounts_account_code", "chart_of_accounts", type_="unique")
    op.drop_constraint("uq_reason_codes_master_category_code", "reason_codes_master", type_="unique")
    op.drop_constraint("uq_payment_modes_master_code", "payment_modes_master", type_="unique")
    op.drop_constraint("uq_departments_code", "departments", type_="unique")
    op.drop_constraint("uq_clusters_region_code", "clusters", type_="unique")
    op.drop_index("uq_companies_pan", table_name="companies")
    op.drop_index("uq_companies_gstin", table_name="companies")

    op.drop_column("regions", "is_active")
    op.drop_column("chart_of_accounts", "is_active")
    op.drop_column("reason_codes_master", "is_active")
    op.drop_column("departments", "is_active")
    op.drop_column("clusters", "is_active")
    op.drop_column("companies", "is_active")
