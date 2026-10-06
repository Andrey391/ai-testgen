"""Tables of their own for runs, tests, tasks and model spending; their documents move out of `docs`.

Before: data/projects/<p>/runs/<test>/<run>.json and index.json (rewritten whole on every run),
tests/<t>.json, tasks/<t>.json, usage/<YYYY-MM>.json - rows of `docs` by path. After: rows of
`runs`, `tests`, `tasks`, `usage` with columns for filters and reports (repo/). The downgrade
writes them back as documents.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""
from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

Doc = JSONB()
Strings = ARRAY(sa.Text())
BATCH = 500


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("test_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("trigger", sa.String(16)),
        sa.Column("suite_id", sa.String(32)),
        sa.Column("started_by", sa.String(64)),
        sa.Column("started", sa.Float, nullable=False),
        sa.Column("finished", sa.Float),
        sa.Column("duration", sa.Float),
        sa.Column("passed", sa.Boolean),
        sa.Column("flaky", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("quarantined", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("healed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("proposals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("outcomes", Doc),
        sa.Column("report", Doc, nullable=False),
    )
    op.create_index("ix_runs_test", "runs", ["project_id", "test_id", "finished"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_suite_id", "runs", ["suite_id"])
    op.create_index("ix_runs_started", "runs", ["started"])

    op.create_table(
        "tests",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="ready"),
        sa.Column("role", sa.String(16), nullable=False, server_default=""),
        sa.Column("tags", Strings),
        sa.Column("quarantined", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_status", sa.String(12)),
        sa.Column("updated", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(64)),
        sa.Column("body", Doc, nullable=False),
    )
    op.create_index("ix_tests_id", "tests", ["id"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("priority", sa.String(8), nullable=False),
        sa.Column("assignee", sa.String(64), nullable=False, server_default=""),
        sa.Column("due", sa.String(10), nullable=False, server_default=""),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("updated", sa.Float, nullable=False),
        sa.Column("done_at", sa.Float),
        sa.Column("test_ids", Strings),
        sa.Column("body", Doc, nullable=False),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])

    op.create_table(
        "usage",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("at", sa.Float, nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("requests", sa.Integer, nullable=False, server_default="1"),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cache_creation_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cache_read_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.create_index("ix_usage_project_month", "usage", ["project_id", "month"])

    _move_documents_in(op.get_bind())


def _move_documents_in(c) -> None:
    """Documents of the old layout -> rows of the new tables (repo.import_doc), then out of `docs`."""
    from testgen import db, repo
    d = db.docs
    paths = [p for (p,) in c.execute(sa.select(d.c.path).where(d.c.path.like("data/projects/%"),
                                                                d.c.kind == "json")) if repo.owns(p)]
    for i in range(0, len(paths), BATCH):           # bodies batch by batch: runs may take gigabytes
        batch = paths[i:i + BATCH]
        for path, body in c.execute(sa.select(d.c.path, d.c.body).where(d.c.path.in_(batch))).all():
            repo.import_doc(c, path, body or "")
        c.execute(d.delete().where(d.c.path.in_(batch)))


def downgrade() -> None:
    from testgen import db, repo
    c = op.get_bind()
    now = time.time()
    for path, text in list(repo.export_docs(c)):
        parts = path.split("/")
        db.upsert(c, db.docs, {"path": path, "name": parts[-1][:255], "project_id": parts[2][:64], "kind": "json",
                               "body": text, "data": None, "size": len(text.encode()), "updated": now})
    for t in ("usage", "tasks", "tests", "runs"):
        op.drop_table(t)
