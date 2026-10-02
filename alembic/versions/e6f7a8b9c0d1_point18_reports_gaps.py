"""Point 18 audit fix: operational/compliance audit checklist — the one
Point-18 report with no table at all anywhere (DocumentChecklistItem is
HR-only joining/exit paperwork; audit_log is the immutable change trail;
neither is this). Seeds five real default checklist items so the report
isn't empty by construction on a fresh deploy.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

DEFAULT_ITEMS = [
    ("Store opened on time", "opening", "daily"),
    ("Cash drawer opening float counted", "opening", "daily"),
    ("Chiller/freezer temperature logged", "hygiene", "daily"),
    ("Floor and billing area cleaned", "hygiene", "daily"),
    ("Fire extinguisher and emergency exit checked", "safety", "weekly"),
]


def upgrade() -> None:
    op.create_table(
        "operational_checklist_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("frequency", sa.String(), nullable=False, server_default="daily"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "store_checklist_completions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("checklist_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("operational_checklist_items.id"), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("checklist_item_id", "store_id", "business_date", name="uq_store_checklist_completion"),
    )
    op.create_index("ix_store_checklist_completions_store_date", "store_checklist_completions", ["store_id", "business_date"])

    conn = op.get_bind()
    for name, category, frequency in DEFAULT_ITEMS:
        conn.execute(
            sa.text(
                "insert into operational_checklist_items (id, name, category, frequency) "
                "values (gen_random_uuid(), :name, :category, :frequency)"
            ),
            {"name": name, "category": category, "frequency": frequency},
        )


def downgrade() -> None:
    op.drop_index("ix_store_checklist_completions_store_date", table_name="store_checklist_completions")
    op.drop_table("store_checklist_completions")
    op.drop_table("operational_checklist_items")
