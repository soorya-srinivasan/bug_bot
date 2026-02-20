"""add_oncall_scheduling

Revision ID: df206ce5d01
Revises: d4f1a2b3c5e6
Create Date: 2026-02-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'df206ce5d01'
down_revision: Union[str, None] = 'd4f1a2b3c5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add rotation fields to service_groups
    op.add_column('service_groups', sa.Column('rotation_enabled', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('service_groups', sa.Column('rotation_type', sa.String(length=20), nullable=True))
    op.add_column('service_groups', sa.Column('rotation_order', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('service_groups', sa.Column('rotation_start_date', sa.Date(), nullable=True))
    op.add_column('service_groups', sa.Column('current_rotation_index', sa.Integer(), nullable=True))

    # Create oncall_schedules table
    op.create_table(
        'oncall_schedules',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('group_id', sa.UUID(), nullable=False),
        sa.Column('engineer_slack_id', sa.String(length=20), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('schedule_type', sa.String(length=10), nullable=False),
        sa.Column('days_of_week', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_by', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['group_id'], ['service_groups.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_oncall_schedules_group_start', 'oncall_schedules', ['group_id', 'start_date'])
    op.create_index('idx_oncall_schedules_group_end', 'oncall_schedules', ['group_id', 'end_date'])

    # Create oncall_history table
    op.create_table(
        'oncall_history',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('group_id', sa.UUID(), nullable=False),
        sa.Column('engineer_slack_id', sa.String(length=20), nullable=False),
        sa.Column('previous_engineer_slack_id', sa.String(length=20), nullable=True),
        sa.Column('change_type', sa.String(length=20), nullable=False),
        sa.Column('change_reason', sa.Text(), nullable=True),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('changed_by', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['group_id'], ['service_groups.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_oncall_history_group_effective', 'oncall_history', ['group_id', 'effective_date'])
    op.create_index('idx_oncall_history_group_created', 'oncall_history', ['group_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_oncall_history_group_created', table_name='oncall_history')
    op.drop_index('idx_oncall_history_group_effective', table_name='oncall_history')
    op.drop_table('oncall_history')
    
    op.drop_index('idx_oncall_schedules_group_end', table_name='oncall_schedules')
    op.drop_index('idx_oncall_schedules_group_start', table_name='oncall_schedules')
    op.drop_table('oncall_schedules')
    
    op.drop_column('service_groups', 'current_rotation_index')
    op.drop_column('service_groups', 'rotation_start_date')
    op.drop_column('service_groups', 'rotation_order')
    op.drop_column('service_groups', 'rotation_type')
    op.drop_column('service_groups', 'rotation_enabled')
