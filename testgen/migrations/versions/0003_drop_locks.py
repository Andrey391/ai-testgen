"""PostgreSQL only: the `locks` table of SQLite goes (locks are PostgreSQL advisory locks).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("locks")


def downgrade() -> None:
    op.create_table(
        "locks",
        sa.Column("name", sa.String(512), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("at", sa.Float, nullable=False),
    )
