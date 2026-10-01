"""The shared storage of the studio: documents, audit log, work queue, workers, owners, locks.

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docs",
        sa.Column("path", sa.String(512), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("body", sa.Text),
        sa.Column("data", sa.LargeBinary),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("updated", sa.Float, nullable=False),
    )
    op.create_index("ix_docs_name", "docs", ["name"])
    op.create_index("ix_docs_project_id", "docs", ["project_id"])
    op.create_index("ix_docs_updated", "docs", ["updated"])
    if op.get_bind().dialect.name == "postgresql":
        # Prefix searches (path LIKE 'data/projects/x/tests/%') use this index in any collation.
        op.execute("CREATE INDEX ix_docs_path_prefix ON docs (path varchar_pattern_ops)")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("ts", sa.Float, nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("rec", sa.Text, nullable=False),
    )
    op.create_index("ix_audit_log_month", "audit_log", ["month"])
    op.create_index("ix_audit_log_project_id", "audit_log", ["project_id"])

    op.create_table(
        "work",
        sa.Column("id", sa.String(24), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("ref", sa.String(64)),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("worker", sa.String(128)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("started", sa.Float),
        sa.Column("heartbeat", sa.Float),
        sa.Column("finished", sa.Float),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_work_status", "work", ["status"])
    op.create_index("ix_work_created", "work", ["created"])
    op.create_index("ix_work_project_id", "work", ["project_id"])
    op.create_index("ix_work_ref", "work", ["ref"])

    op.create_table(
        "workers",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("host", sa.String(255)),
        sa.Column("started", sa.Float, nullable=False),
        sa.Column("heartbeat", sa.Float, nullable=False),
        sa.Column("running", sa.Integer, nullable=False, server_default="0"),
        sa.Column("capacity", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_index("ix_workers_heartbeat", "workers", ["heartbeat"])

    op.create_table(
        "owners",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
    )

    op.create_table(
        "locks",
        sa.Column("name", sa.String(512), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("at", sa.Float, nullable=False),
    )


def downgrade() -> None:
    for t in ("locks", "owners", "workers", "work", "audit_log", "docs"):
        op.drop_table(t)
