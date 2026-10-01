"""stores_cluster_fk

Revision ID: 7c3e9a1f2b44
Revises: 491297a3fe53
Create Date: 2026-10-01 00:00:00.000000

Point 2 architecture audit finding: `clusters.regional_manager_id` existed
with no Store FK anywhere pointing at it, so a Regional/Cluster Manager's
`Cluster` assignment was write-only data — real store scoping went entirely
through the generic `user_stores` table instead. This adds the missing
`stores.cluster_id` FK so a Cluster can finally have real member stores,
and `app/api/v1/auth.py`'s `_load_user_store_ids` now unions in every store
belonging to a cluster the user manages.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '7c3e9a1f2b44'
down_revision: Union[str, None] = '491297a3fe53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id"), nullable=True))
    op.create_index("ix_stores_cluster_id", "stores", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_stores_cluster_id", table_name="stores")
    op.drop_column("stores", "cluster_id")
