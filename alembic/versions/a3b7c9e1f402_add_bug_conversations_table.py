"""add_bug_conversations_table

Revision ID: a3b7c9e1f402
Revises: 9dfeb93f9938
Create Date: 2026-02-19 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a3b7c9e1f402'
down_revision: Union[str, None] = '9dfeb93f9938'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bug_conversations',
        sa.Column('id', sa.UUID(), nullable=False, default=sa.text('gen_random_uuid()')),
        sa.Column('bug_id', sa.String(length=50), sa.ForeignKey('bug_reports.bug_id'), nullable=False),
        sa.Column('channel', sa.String(length=20), nullable=True),
        sa.Column('sender_type', sa.String(length=20), nullable=False),
        sa.Column('sender_id', sa.String(length=50), nullable=True),
        sa.Column('message_text', sa.Text(), nullable=True),
        sa.Column('message_type', sa.String(length=30), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_bug_conversations_bug_id', 'bug_conversations', ['bug_id'])
    op.create_index('idx_bug_conversations_message_type', 'bug_conversations', ['message_type'])


def downgrade() -> None:
    op.drop_index('idx_bug_conversations_message_type', table_name='bug_conversations')
    op.drop_index('idx_bug_conversations_bug_id', table_name='bug_conversations')
    op.drop_table('bug_conversations')
