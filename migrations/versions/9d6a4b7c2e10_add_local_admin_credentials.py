"""add installation local administrator credentials

Revision ID: 9d6a4b7c2e10
Revises: 6a2d8f51c904
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9d6a4b7c2e10"
down_revision: Union[str, Sequence[str], None] = "6a2d8f51c904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "local_admin_credentials",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("principal_id", sa.String(length=256), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("principal_id"),
        sa.UniqueConstraint("username"),
    )


def downgrade() -> None:
    op.drop_table("local_admin_credentials")
