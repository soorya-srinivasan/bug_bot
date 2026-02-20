"""merge heads

Revision ID: 60b46cd533cf
Revises: a1f3c7b2d905, b55e9dba7821
Create Date: 2026-02-20 19:25:31.841643

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '60b46cd533cf'
down_revision: Union[str, None] = ('a1f3c7b2d905', 'b55e9dba7821')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
