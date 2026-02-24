"""add_db_fields_to_service_team_mapping

Revision ID: 2c69dddceea8
Revises: c2d3e4f5a6b7
Create Date: 2026-02-22 08:23:07.665600

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2c69dddceea8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "service_team_mapping",
        sa.Column("database_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "service_team_mapping",
        sa.Column("dialect", sa.String(length=20), nullable=True),
    )

    # Seed known defaults for payment-service-sample
    from sqlalchemy import text

    conn = op.get_bind()
    conn.execute(
        text(
            "UPDATE service_team_mapping "
            "SET database_name = 'payments', "
            "    dialect = 'postgres', "
            "    description = :desc "
            "WHERE lower(service_name) = 'payment-service-sample';"
        ),
        {
            "desc": (
                "Payment Service Sample is a FastAPI-based microservice designed to handle core "
                "payment operations, backed by a SQLite database and integrated with Loki for "
                "structured logging. It provides service health monitoring, single and bulk payment "
                "processing, transaction refunds, and transaction detail retrieval. Additionally, it "
                "supports currency exchange rate lookups, tax calculation on given amounts, and "
                "account summary retrieval. The service follows a modular architecture with dedicated "
                "routers and service layers per domain, making it well-structured and maintainable."
            )
        },
    )


def downgrade() -> None:
    op.drop_column("service_team_mapping", "dialect")
    op.drop_column("service_team_mapping", "database_name")
