"""add local users and principal session versions

Revision ID: 8c2d4e6f1a30
Revises: 7b1d9f4e6a20
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c2d4e6f1a30"
down_revision: Union[str, Sequence[str], None] = "7b1d9f4e6a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "principals",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "local_user_credentials",
        sa.Column("principal_id", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("principal_id"),
        sa.UniqueConstraint("tenant_id", "username", name="uq_local_users_tenant_username"),
    )
    op.create_index(
        op.f("ix_local_user_credentials_tenant_id"),
        "local_user_credentials",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_local_user_credentials_tenant_id"), table_name="local_user_credentials")
    op.drop_table("local_user_credentials")
    op.drop_column("principals", "session_version")
