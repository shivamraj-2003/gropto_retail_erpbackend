"""drop_unused_device_activation_code

Revision ID: b2b03aaf2dbc
Revises: 31080c1bcb04
Create Date: 2026-09-28 23:02:46.852881

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2b03aaf2dbc'
down_revision: Union[str, None] = '31080c1bcb04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also flagged 8 indexes as "removed" (idx_approvals_status_store,
    # idx_audit_entity, ix_audit_log_created_at, ix_audit_log_entity,
    # idx_movements_product_store, idx_products_name_trgm, idx_products_revision,
    # idx_sales_store_date) — false positives, since they were created via raw SQL
    # in earlier migrations rather than declared as SQLAlchemy Index() objects, so
    # the model diff doesn't see them. Deliberately left untouched here; only the
    # actually-unused column is dropped.
    op.drop_column('devices', 'activation_code')


def downgrade() -> None:
    op.add_column('devices', sa.Column('activation_code', sa.VARCHAR(), autoincrement=False, nullable=True))
