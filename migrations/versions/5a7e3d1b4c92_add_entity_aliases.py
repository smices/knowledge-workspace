"""add reviewed entity aliases

Revision ID: 5a7e3d1b4c92
Revises: 4f3b9c9d8e21
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "5a7e3d1b4c92"
down_revision = "4f3b9c9d8e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entity_aliases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("document_version_id", sa.String(36), sa.ForeignKey("document_versions.id"), nullable=False),
        sa.Column("canonical", sa.String(255), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("source", sa.String(16), nullable=False, server_default="pattern"),
        sa.Column("created_by", sa.String(256)),
        sa.Column("approved_by", sa.String(256)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
        sa.UniqueConstraint("document_version_id", "canonical", "alias", name="uq_entity_alias_version_mapping"),
    )
    for column in ("tenant_id", "document_version_id", "canonical", "alias", "status"):
        op.create_index(op.f(f"ix_entity_aliases_{column}"), "entity_aliases", [column])


def downgrade() -> None:
    for column in ("status", "alias", "canonical", "document_version_id", "tenant_id"):
        op.drop_index(op.f(f"ix_entity_aliases_{column}"), table_name="entity_aliases")
    op.drop_table("entity_aliases")
