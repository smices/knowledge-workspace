"""add ingestion generations, tenant revisions, and durable dead letters

Revision ID: 7b1d9f4e6a20
Revises: 5a7e3d1b4c92
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b1d9f4e6a20"
down_revision: Union[str, Sequence[str], None] = "5a7e3d1b4c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("knowledge_revision", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingestion_jobs", sa.Column("generation", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "ingestion_dead_letters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("document_version_id", sa.String(length=36), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("event_id", "event_type", "tenant_id", "document_id", "document_version_id", "trace_id"):
        op.create_index(op.f(f"ix_ingestion_dead_letters_{column}"), "ingestion_dead_letters", [column])


def downgrade() -> None:
    for column in ("trace_id", "document_version_id", "document_id", "tenant_id", "event_type", "event_id"):
        op.drop_index(op.f(f"ix_ingestion_dead_letters_{column}"), table_name="ingestion_dead_letters")
    op.drop_table("ingestion_dead_letters")
    op.drop_column("ingestion_jobs", "generation")
    op.drop_column("tenants", "knowledge_revision")
