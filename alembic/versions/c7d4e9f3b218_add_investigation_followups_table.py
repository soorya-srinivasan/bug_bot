"""add_investigation_followups_and_messages_tables

Revision ID: c7d4e9f3b218
Revises: 60b46cd533cf
Create Date: 2026-02-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision: str = 'c7d4e9f3b218'
down_revision: Union[str, None] = '60b46cd533cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'investigation_followups',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('bug_id', sa.String(50), sa.ForeignKey('bug_reports.bug_id'), nullable=False),
        sa.Column('trigger_state', sa.String(20), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('fix_type', sa.String(20), nullable=False),
        sa.Column('summary', sa.Text, nullable=False),
        sa.Column('confidence', sa.Float, nullable=False, server_default='0.0'),
        sa.Column('root_cause', sa.Text, nullable=True),
        sa.Column('pr_url', sa.String(500), nullable=True),
        sa.Column('recommended_actions', JSONB, server_default='[]'),
        sa.Column('relevant_services', JSONB, server_default='[]'),
        sa.Column('cost_usd', sa.Float, nullable=True),
        sa.Column('duration_ms', sa.Integer, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_investigation_followups_bug_id', 'investigation_followups', ['bug_id'])

    op.create_table(
        'investigation_messages',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('bug_id', sa.String(50), sa.ForeignKey('bug_reports.bug_id'), nullable=False),
        sa.Column('investigation_id', UUID(as_uuid=True), sa.ForeignKey('investigations.id'), nullable=True),
        sa.Column('followup_id', UUID(as_uuid=True), sa.ForeignKey('investigation_followups.id'), nullable=True),
        sa.Column('sequence', sa.Integer, nullable=False),
        sa.Column('message_type', sa.String(30), nullable=False),
        sa.Column('content', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_investigation_messages_bug_id', 'investigation_messages', ['bug_id'])
    op.create_index('idx_investigation_messages_investigation_id', 'investigation_messages', ['investigation_id'])
    op.create_index('idx_investigation_messages_followup_id', 'investigation_messages', ['followup_id'])

    # Migrate existing conversation_history JSONB data into investigation_messages.
    # Each array element becomes a row; only entries with non-empty text are migrated.
    op.execute("""
        INSERT INTO investigation_messages (id, bug_id, investigation_id, sequence, message_type, content, created_at)
        SELECT
            gen_random_uuid(),
            i.bug_id,
            i.id,
            (elem_row.seq - 1),
            COALESCE(elem_row.elem->>'type', 'unknown'),
            elem_row.elem->>'text',
            i.created_at
        FROM investigations i,
        LATERAL jsonb_array_elements(i.conversation_history) WITH ORDINALITY AS elem_row(elem, seq)
        WHERE i.conversation_history IS NOT NULL
          AND jsonb_typeof(i.conversation_history) = 'array'
          AND COALESCE(TRIM(elem_row.elem->>'text'), '') <> ''
    """)

    op.drop_column('investigations', 'conversation_history')


def downgrade() -> None:
    op.add_column('investigations', sa.Column('conversation_history', JSONB, nullable=True))

    op.drop_index('idx_investigation_messages_followup_id', table_name='investigation_messages')
    op.drop_index('idx_investigation_messages_investigation_id', table_name='investigation_messages')
    op.drop_index('idx_investigation_messages_bug_id', table_name='investigation_messages')
    op.drop_table('investigation_messages')
    op.drop_index('idx_investigation_followups_bug_id', table_name='investigation_followups')
    op.drop_table('investigation_followups')
