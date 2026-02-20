"""merge heads

Revision ID: b55e9dba7821
Revises: 65b225a8d12a
Create Date: 2026-02-20 17:41:28.879569

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b55e9dba7821'
down_revision: Union[str, None] = '65b225a8d12a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
