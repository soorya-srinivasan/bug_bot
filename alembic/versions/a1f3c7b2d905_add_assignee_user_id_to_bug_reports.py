"""add_assignee_user_id_to_bug_reports

Revision ID: a1f3c7b2d905
Revises: e7b3d8f2a91
Create Date: 2026-02-20 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c7b2d905'
down_revision: Union[str, None] = 'e7b3d8f2a91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'bug_reports',
        sa.Column('assignee_user_id', sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('bug_reports', 'assignee_user_id')
