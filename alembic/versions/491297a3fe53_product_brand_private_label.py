"""product_brand_private_label

Revision ID: 491297a3fe53
Revises: 60bd1d1ce669
Create Date: 2026-09-30 16:25:12.090827

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '491297a3fe53'
down_revision: Union[str, None] = '60bd1d1ce669'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('brand', sa.String(), nullable=True))
    op.add_column('products', sa.Column('is_private_label', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    # NOTE: autogenerate flagged idx_approvals_status_store, idx_audit_entity,
    # ix_audit_log_created_at, ix_audit_log_entity, idx_campaign_recipients_campaign,
    # idx_movements_product_store, idx_products_name_trgm, idx_products_revision and
    # idx_sales_store_date as "removed" — known false positives this session
    # (raw-SQL-created indexes not declared as SQLAlchemy Index() objects, so
    # the model diff doesn't see them). None are touched here.


def downgrade() -> None:
    op.drop_column('products', 'is_private_label')
    op.drop_column('products', 'brand')
