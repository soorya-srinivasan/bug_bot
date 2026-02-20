"""add_investigation_findings_table

Revision ID: f3a1c9d7e052
Revises: b5d2e8f1a390
Create Date: 2026-02-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f3a1c9d7e052'
down_revision: Union[str, None] = 'b5d2e8f1a390'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'investigation_findings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('bug_id', sa.String(50), sa.ForeignKey('bug_reports.bug_id'), nullable=False),
        sa.Column('category', sa.String(50), nullable=False),
        sa.Column('finding', sa.Text, nullable=False),
        sa.Column('severity', sa.String(10), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index('idx_investigation_findings_bug_id', 'investigation_findings', ['bug_id'])
    op.create_index('idx_investigation_findings_category', 'investigation_findings', ['category'])


def downgrade() -> None:
    op.drop_table('investigation_findings')
