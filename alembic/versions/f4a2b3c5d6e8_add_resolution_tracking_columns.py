"""add resolution tracking columns

Revision ID: f4a2b3c5d6e8
Revises: e9f1a2b3c4d7
Create Date: 2026-02-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4a2b3c5d6e8"
down_revision: Union[str, None] = "e9f1a2b3c4d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bug_reports", sa.Column("resolution_type", sa.String(30), nullable=True))
    op.add_column("bug_reports", sa.Column("closure_reason", sa.Text(), nullable=True))
    op.add_column("bug_reports", sa.Column("fix_provided", sa.Text(), nullable=True))
    op.create_index("idx_bug_reports_resolution_type", "bug_reports", ["resolution_type"])


def downgrade() -> None:
    op.drop_index("idx_bug_reports_resolution_type", table_name="bug_reports")
    op.drop_column("bug_reports", "fix_provided")
    op.drop_column("bug_reports", "closure_reason")
    op.drop_column("bug_reports", "resolution_type")
