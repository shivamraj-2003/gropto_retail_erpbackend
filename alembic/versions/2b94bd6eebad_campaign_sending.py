"""campaign_sending

Revision ID: 2b94bd6eebad
Revises: b2b03aaf2dbc
Create Date: 2026-09-28 23:28:26.921867

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b94bd6eebad'
down_revision: Union[str, None] = 'b2b03aaf2dbc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate also flagged 8 indexes as "removed" (idx_approvals_status_store,
    # idx_audit_entity, ix_audit_log_created_at, ix_audit_log_entity,
    # idx_movements_product_store, idx_products_name_trgm, idx_products_revision,
    # idx_sales_store_date) — same false positives as the two prior migrations in
    # this session: created via raw SQL in earlier migrations, not declared as
    # SQLAlchemy Index() objects, so the model diff doesn't see them. Left
    # untouched here; only the campaign-sending changes below are real.
    op.create_table(
        'campaign_recipients',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('campaign_id', sa.UUID(), nullable=False),
        sa.Column('customer_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('provider_message_id', sa.String(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_campaign_recipients_campaign', 'campaign_recipients', ['campaign_id'])
    op.add_column('campaigns', sa.Column('template_name', sa.String(), nullable=True))
    op.add_column('campaigns', sa.Column('sent_count', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('campaigns', sa.Column('failed_count', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('campaigns', 'failed_count')
    op.drop_column('campaigns', 'sent_count')
    op.drop_column('campaigns', 'template_name')
    op.drop_index('idx_campaign_recipients_campaign', table_name='campaign_recipients')
    op.drop_table('campaign_recipients')
