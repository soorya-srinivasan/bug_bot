"""rename_service_groups_to_teams

Revision ID: e7b3d8f2a91
Revises: df206ce5d01
Create Date: 2026-02-20 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7b3d8f2a91'
down_revision: Union[str, None] = 'df206ce5d01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename table service_groups to teams
    op.rename_table('service_groups', 'teams')

    # 2. service_team_mapping: drop FK, rename group_id -> team_id, add FK to teams
    op.drop_constraint(
        'fk_service_team_mapping_group_id',
        'service_team_mapping',
        type_='foreignkey',
    )
    op.alter_column(
        'service_team_mapping',
        'group_id',
        new_column_name='team_id',
    )
    op.create_foreign_key(
        'fk_service_team_mapping_team_id',
        'service_team_mapping', 'teams',
        ['team_id'], ['id'],
        ondelete='SET NULL',
    )
    op.drop_index('idx_service_team_mapping_group_id', table_name='service_team_mapping')
    op.create_index('idx_service_team_mapping_team_id', 'service_team_mapping', ['team_id'])

    # 3. oncall_schedules: drop FK and indexes, rename group_id -> team_id, add FK and new indexes
    # PostgreSQL names unnamed FKs as {table}_{column}_fkey
    op.drop_constraint(
        'oncall_schedules_group_id_fkey',
        'oncall_schedules',
        type_='foreignkey',
    )
    op.drop_index('idx_oncall_schedules_group_end', table_name='oncall_schedules')
    op.drop_index('idx_oncall_schedules_group_start', table_name='oncall_schedules')
    op.alter_column(
        'oncall_schedules',
        'group_id',
        new_column_name='team_id',
    )
    op.create_foreign_key(
        'fk_oncall_schedules_team_id',
        'oncall_schedules', 'teams',
        ['team_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_index('idx_oncall_schedules_team_start', 'oncall_schedules', ['team_id', 'start_date'])
    op.create_index('idx_oncall_schedules_team_end', 'oncall_schedules', ['team_id', 'end_date'])

    # 4. oncall_history: drop FK and indexes, rename group_id -> team_id, add FK and new indexes
    op.drop_index('idx_oncall_history_group_created', table_name='oncall_history')
    op.drop_index('idx_oncall_history_group_effective', table_name='oncall_history')
    op.drop_constraint(
        'oncall_history_group_id_fkey',
        'oncall_history',
        type_='foreignkey',
    )
    op.alter_column(
        'oncall_history',
        'group_id',
        new_column_name='team_id',
    )
    op.create_foreign_key(
        'fk_oncall_history_team_id',
        'oncall_history', 'teams',
        ['team_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_index('idx_oncall_history_team_effective', 'oncall_history', ['team_id', 'effective_date'])
    op.create_index('idx_oncall_history_team_created', 'oncall_history', ['team_id', 'created_at'])


def downgrade() -> None:
    # 4. oncall_history: reverse
    op.drop_index('idx_oncall_history_team_created', table_name='oncall_history')
    op.drop_index('idx_oncall_history_team_effective', table_name='oncall_history')
    op.drop_constraint('fk_oncall_history_team_id', 'oncall_history', type_='foreignkey')
    op.alter_column('oncall_history', 'team_id', new_column_name='group_id')
    op.create_foreign_key(
        'oncall_history_group_id_fkey',
        'oncall_history', 'teams',
        ['group_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_index('idx_oncall_history_group_effective', 'oncall_history', ['group_id', 'effective_date'])
    op.create_index('idx_oncall_history_group_created', 'oncall_history', ['group_id', 'created_at'])

    # 3. oncall_schedules: reverse
    op.drop_index('idx_oncall_schedules_team_end', table_name='oncall_schedules')
    op.drop_index('idx_oncall_schedules_team_start', table_name='oncall_schedules')
    op.drop_constraint('fk_oncall_schedules_team_id', 'oncall_schedules', type_='foreignkey')
    op.alter_column('oncall_schedules', 'team_id', new_column_name='group_id')
    op.create_foreign_key(
        'oncall_schedules_group_id_fkey',
        'oncall_schedules', 'teams',
        ['group_id'], ['id'],
        ondelete='CASCADE',
    )
    op.create_index('idx_oncall_schedules_group_start', 'oncall_schedules', ['group_id', 'start_date'])
    op.create_index('idx_oncall_schedules_group_end', 'oncall_schedules', ['group_id', 'end_date'])

    # 2. service_team_mapping: reverse
    op.drop_index('idx_service_team_mapping_team_id', table_name='service_team_mapping')
    op.drop_constraint('fk_service_team_mapping_team_id', 'service_team_mapping', type_='foreignkey')
    op.alter_column('service_team_mapping', 'team_id', new_column_name='group_id')
    op.create_foreign_key(
        'fk_service_team_mapping_group_id',
        'service_team_mapping', 'teams',
        ['group_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('idx_service_team_mapping_group_id', 'service_team_mapping', ['group_id'])

    # 1. Rename table teams back to service_groups
    op.rename_table('teams', 'service_groups')
