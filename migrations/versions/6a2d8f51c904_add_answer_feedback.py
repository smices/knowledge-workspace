"""add answer feedback

Revision ID: 6a2d8f51c904
Revises: e80b3b5f2d95
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6a2d8f51c904"
down_revision: Union[str, Sequence[str], None] = "e80b3b5f2d95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("answer_id", sa.String(length=36), nullable=False),
        sa.Column("liked", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "subject", "answer_id", name="uq_answer_feedback_actor"),
    )
    op.create_index(op.f("ix_answer_feedback_answer_id"), "answer_feedback", ["answer_id"])
    op.create_index(op.f("ix_answer_feedback_subject"), "answer_feedback", ["subject"])
    op.create_index(op.f("ix_answer_feedback_tenant_id"), "answer_feedback", ["tenant_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_answer_feedback_tenant_id"), table_name="answer_feedback")
    op.drop_index(op.f("ix_answer_feedback_subject"), table_name="answer_feedback")
    op.drop_index(op.f("ix_answer_feedback_answer_id"), table_name="answer_feedback")
    op.drop_table("answer_feedback")
