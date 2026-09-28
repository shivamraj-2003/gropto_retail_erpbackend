"""password_reset_otp_and_gst_inclusive

Revision ID: d0f72fd50650
Revises: 2b94bd6eebad
Create Date: 2026-09-29 00:02:41.905938

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0f72fd50650'
down_revision: Union[str, None] = '2b94bd6eebad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate flagged 9 indexes as "removed" — false positives, same as
    # every prior migration in this session: 8 were created via raw SQL in
    # earlier migrations, and idx_campaign_recipients_campaign (from this
    # session's campaign_sending migration) was created via op.create_index
    # but never declared as a SQLAlchemy Index() object on the model — either
    # way the model diff doesn't see them, and none are touched here.
    #
    # devices.activation_code is a genuine leftover: it was supposed to drop
    # in an earlier migration this session, whose DDL silently didn't commit
    # (discovered by checking the live schema directly) — cleaned up here.
    op.create_table(
        'password_reset_otps',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.drop_column('devices', 'activation_code')
    op.add_column('products', sa.Column('hsn_code', sa.String(), nullable=True))
    op.add_column('sale_items', sa.Column('hsn_code_snapshot', sa.String(), nullable=True))
    op.add_column('sale_items', sa.Column('taxable_value', sa.Numeric(precision=12, scale=2), server_default=sa.text('0'), nullable=False))
    op.add_column('sale_items', sa.Column('cgst_amount', sa.Numeric(precision=12, scale=2), server_default=sa.text('0'), nullable=False))
    op.add_column('sale_items', sa.Column('sgst_amount', sa.Numeric(precision=12, scale=2), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('sale_items', 'sgst_amount')
    op.drop_column('sale_items', 'cgst_amount')
    op.drop_column('sale_items', 'taxable_value')
    op.drop_column('sale_items', 'hsn_code_snapshot')
    op.drop_column('products', 'hsn_code')
    op.add_column('devices', sa.Column('activation_code', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.drop_table('password_reset_otps')
