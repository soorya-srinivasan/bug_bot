"""merge heads

Revision ID: 65b225a8d12a
Revises: e7b3d8f2a91, f3a1c9d7e052
Create Date: 2026-02-20 17:41:26.263132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65b225a8d12a'
down_revision: Union[str, None] = ('e7b3d8f2a91', 'f3a1c9d7e052')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
