"""add_attachments_to_bug_reports

Revision ID: b5d2e8f1a390
Revises: a3b7c9e1f402
Create Date: 2026-02-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b5d2e8f1a390'
down_revision: Union[str, None] = 'a3b7c9e1f402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'bug_reports',
        sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('bug_reports', 'attachments')
