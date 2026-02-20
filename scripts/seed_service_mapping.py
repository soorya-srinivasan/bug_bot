"""Seed script: populate service_team_mapping with initial service data.

Usage:
    python scripts/seed_service_mapping.py

Set DATABASE_URL in your environment or .env before running.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://bugbot:bugbot@localhost:5433/bugbot")
# asyncpg uses postgresql:// not postgresql+asyncpg://
PG_DSN = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

SERVICES = [
    {
        "service_name": "Payment.API",
        "github_repo": "https://github.com/shopuptech/payment-api",
        "tech_stack": ".NET",
        "description": "Handles payment processing, Stripe integration, charge and refund lifecycle.",
    },
    {
        "service_name": "Bill.API",
        "github_repo": "https://github.com/shopuptech/bill-api",
        "tech_stack": ".NET",
        "description": "Manages billing cycles, invoice generation, and payment reconciliation.",
    },
    {
        "service_name": "Inventory.API",
        "github_repo": "https://github.com/shopuptech/inventory-api",
        "tech_stack": ".NET",
        "description": "Tracks product stock levels, warehouse operations, and stock adjustments.",
    },
    {
        "service_name": "Auth.Server",
        "github_repo": "https://github.com/shopuptech/auth-server",
        "tech_stack": ".NET",
        "description": "Handles user authentication, JWT issuance, SSO, and login/logout flows.",
    },
    {
        "service_name": "Subscription.API",
        "github_repo": "https://github.com/shopuptech/subscription-api",
        "tech_stack": ".NET",
        "description": "Manages subscription plans, renewals, upgrades, and cancellations.",
    },
    {
        "service_name": "Company.API",
        "github_repo": "https://github.com/shopuptech/company-api",
        "tech_stack": ".NET",
        "description": "Company profile management, settings, and multi-tenant onboarding.",
    },
    {
        "service_name": "vconnect",
        "github_repo": "https://github.com/shopuptech/vconnect",
        "tech_stack": "Ruby",
        "description": "Core Ruby on Rails application; business logic hub for vendor connectivity.",
    },
    {
        "service_name": "vconnect-aft",
        "github_repo": "https://github.com/shopuptech/vconnect",
        "tech_stack": "Ruby",
        "description": "Automated file transfer module within vconnect for EDI and bulk data exchange.",
    },
    {
        "service_name": "vconnect-audit",
        "github_repo": "https://github.com/shopuptech/vconnect",
        "tech_stack": "Ruby",
        "description": "Audit logging and compliance reporting module within vconnect.",
    },
    {
        "service_name": "payment-service-sample",
        "github_repo": "https://github.com/allen-anish-5006973/payment-service-sample",
        "tech_stack": "Python / FastAPI",
        "description": "A sample payment processing service for Bug Bot demo. Exposes endpoints for payment processing, refunds, batch payments, transaction lookup, exchange rates, and tax calculation.",
    },
]


async def seed():
    conn = await asyncpg.connect(dsn=PG_DSN)
    try:
        for svc in SERVICES:
            await conn.execute(
                """
                INSERT INTO service_team_mapping (id, service_name, github_repo, tech_stack, description)
                VALUES (gen_random_uuid(), $1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                svc["service_name"],
                svc["github_repo"],
                svc["tech_stack"],
                svc["description"],
            )
            # Also update description on existing rows (idempotent)
            await conn.execute(
                "UPDATE service_team_mapping SET description = $1 WHERE service_name = $2 AND description IS NULL",
                svc["description"],
                svc["service_name"],
            )
        print(f"Seeded {len(SERVICES)} services.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
