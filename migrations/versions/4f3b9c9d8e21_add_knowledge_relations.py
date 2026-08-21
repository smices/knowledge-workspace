"""add durable evidence-backed knowledge relations

Revision ID: 4f3b9c9d8e21
Revises: 9d6a4b7c2e10
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4f3b9c9d8e21"
down_revision: Union[str, Sequence[str], None] = "9d6a4b7c2e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_relations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_version_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("relation", sa.String(length=128), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_version_id", "chunk_index", "source", "target", "relation", name="uq_knowledge_relation_evidence"),
    )
    op.create_index(op.f("ix_knowledge_relations_tenant_id"), "knowledge_relations", ["tenant_id"])
    op.create_index(op.f("ix_knowledge_relations_document_version_id"), "knowledge_relations", ["document_version_id"])
    op.create_index(op.f("ix_knowledge_relations_source"), "knowledge_relations", ["source"])
    op.create_index(op.f("ix_knowledge_relations_target"), "knowledge_relations", ["target"])
    op.create_index(op.f("ix_knowledge_relations_relation"), "knowledge_relations", ["relation"])


def downgrade() -> None:
    for column in ("relation", "target", "source", "document_version_id", "tenant_id"):
        op.drop_index(op.f(f"ix_knowledge_relations_{column}"), table_name="knowledge_relations")
    op.drop_table("knowledge_relations")
