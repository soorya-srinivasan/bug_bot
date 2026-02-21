"""merge heads

Revision ID: 7d4399b0b6d6
Revises: c7d4e9f3b218, f8a2b3c4d5e6
Create Date: 2026-02-21 10:03:48.815641

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d4399b0b6d6'
down_revision: Union[str, None] = ('c7d4e9f3b218', 'f8a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
