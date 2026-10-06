"""Without the `locks` table: it held locks between processes on SQLite; PostgreSQL uses advisory locks.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
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
