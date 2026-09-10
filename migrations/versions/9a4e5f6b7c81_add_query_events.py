"""add query event telemetry table

Revision ID: 9a4e5f6b7c81
Revises: 8d3e5f7a2b41
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a4e5f6b7c81"
down_revision: Union[str, Sequence[str], None] = "8d3e5f7a2b41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "query_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("tenant_id", "subject", "created_at"):
        op.create_index(op.f(f"ix_query_events_{column}"), "query_events", [column])


def downgrade() -> None:
    for column in ("created_at", "subject", "tenant_id"):
        op.drop_index(op.f(f"ix_query_events_{column}"), table_name="query_events")
    op.drop_table("query_events")
