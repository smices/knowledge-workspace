"""allow audit resources to use full principal identifiers

Revision ID: 8d3e5f7a2b41
Revises: 8c2d4e6f1a30
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8d3e5f7a2b41"
down_revision: Union[str, Sequence[str], None] = "8c2d4e6f1a30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "audit_events",
        "resource_id",
        existing_type=sa.String(length=36),
        type_=sa.String(length=256),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "audit_events",
        "resource_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=36),
        existing_nullable=True,
    )
