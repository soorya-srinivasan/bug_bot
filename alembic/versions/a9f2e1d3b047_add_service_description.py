"""add_service_description

Revision ID: a9f2e1d3b047
Revises: 60b46cd533cf
Create Date: 2026-02-20 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'a9f2e1d3b047'
down_revision: Union[str, None] = '60b46cd533cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('service_team_mapping', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('service_team_mapping', 'description')
