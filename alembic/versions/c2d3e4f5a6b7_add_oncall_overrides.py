"""add oncall_overrides table

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-02-21 14:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'oncall_overrides',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('team_id', sa.UUID(), nullable=False),
        sa.Column('override_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('substitute_engineer_slack_id', sa.String(length=20), nullable=False),
        sa.Column('original_engineer_slack_id', sa.String(length=20), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_oncall_overrides_team_date', 'oncall_overrides', ['team_id', 'override_date'])


def downgrade() -> None:
    op.drop_index('idx_oncall_overrides_team_date', table_name='oncall_overrides')
    op.drop_table('oncall_overrides')
