"""add_pr_urls_jsonb

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-02-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add pr_urls JSONB column to investigations
    op.add_column(
        "investigations",
        sa.Column("pr_urls", JSONB(), nullable=True, server_default=sa.text("'[]'::jsonb")),
    )

    # Add pr_urls JSONB column to investigation_followups
    op.add_column(
        "investigation_followups",
        sa.Column("pr_urls", JSONB(), nullable=True, server_default=sa.text("'[]'::jsonb")),
    )

    # Backfill: copy existing pr_url values into pr_urls as single-element arrays
    op.execute(
        "UPDATE investigations SET pr_urls = jsonb_build_array(jsonb_build_object('pr_url', pr_url)) "
        "WHERE pr_url IS NOT NULL AND pr_url != '' AND (pr_urls IS NULL OR pr_urls = '[]'::jsonb)"
    )
    op.execute(
        "UPDATE investigation_followups SET pr_urls = jsonb_build_array(jsonb_build_object('pr_url', pr_url)) "
        "WHERE pr_url IS NOT NULL AND pr_url != '' AND (pr_urls IS NULL OR pr_urls = '[]'::jsonb)"
    )


def downgrade() -> None:
    op.drop_column("investigation_followups", "pr_urls")
    op.drop_column("investigations", "pr_urls")
