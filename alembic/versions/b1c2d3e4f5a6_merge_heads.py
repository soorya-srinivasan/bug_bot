"""merge heads

Revision ID: b1c2d3e4f5a6
Revises: a9f2e1d3b047, f4a2b3c5d6e8
Create Date: 2026-02-21 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = ('a9f2e1d3b047', 'f4a2b3c5d6e8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
