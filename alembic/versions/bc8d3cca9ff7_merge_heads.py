"""merge_heads

Revision ID: bc8d3cca9ff7
Revises: 2c69dddceea8, e1f2a3b4c5d6
Create Date: 2026-02-24 14:21:44.013148

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc8d3cca9ff7'
down_revision: Union[str, None] = ('2c69dddceea8', 'e1f2a3b4c5d6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
