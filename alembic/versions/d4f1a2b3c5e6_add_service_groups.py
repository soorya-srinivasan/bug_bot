"""add_service_groups

Revision ID: d4f1a2b3c5e6
Revises: b5d2e8f1a390
Create Date: 2026-02-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4f1a2b3c5e6'
down_revision: Union[str, None] = 'b5d2e8f1a390'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_groups',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('slack_group_id', sa.String(length=30), nullable=False),
        sa.Column('oncall_engineer', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slack_group_id'),
    )

    op.add_column('service_team_mapping', sa.Column('service_owner', sa.String(length=20), nullable=True))
    op.add_column('service_team_mapping', sa.Column('group_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_service_team_mapping_group_id',
        'service_team_mapping', 'service_groups',
        ['group_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('idx_service_team_mapping_group_id', 'service_team_mapping', ['group_id'])


def downgrade() -> None:
    op.drop_index('idx_service_team_mapping_group_id', table_name='service_team_mapping')
    op.drop_constraint('fk_service_team_mapping_group_id', 'service_team_mapping', type_='foreignkey')
    op.drop_column('service_team_mapping', 'group_id')
    op.drop_column('service_team_mapping', 'service_owner')
    op.drop_table('service_groups')
