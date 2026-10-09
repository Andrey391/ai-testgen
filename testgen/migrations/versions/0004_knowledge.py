"""The application model of a project in tables of its own.

Before: data/projects/<p>/knowledge.json - one document rewritten whole on every record a test or an
analysis found, with limits on the number of entities and data. After: a row of `km_items` per entity,
role, stand data record, fact and pending update (columns to search and filter), the rest of the model
in `km_meta`. A document of the old layout is moved into the tables when the model is first read
(knowledge.get), so `db import-files` keeps working; the downgrade writes the tables back as documents.

The search of the model by name uses the pg_trgm index when the extension can be created (it is a
trusted extension: the owner of the database may create it); without it the same queries work slower.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09
"""
from __future__ import annotations

import json
import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "km_items",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(12), primary_key=True),
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("pos", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("entity", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default=""),
        sa.Column("body", JSONB(), nullable=False),
    )
    op.create_index("ix_km_items_list", "km_items", ["project_id", "kind", "pos"])
    op.create_table(
        "km_meta",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("updated", sa.Float),
        sa.Column("body", JSONB(), nullable=False),
    )
    c = op.get_bind()
    try:
        with c.begin_nested():
            c.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            c.execute(sa.text("CREATE INDEX ix_km_items_name ON km_items USING gin (name gin_trgm_ops)"))
    except sa.exc.DBAPIError:
        pass                    # no pg_trgm here: the search goes without the index


def downgrade() -> None:
    from testgen import db
    from testgen.repo import knowledge
    c = op.get_bind()
    now = time.time()
    for path, doc in list(knowledge.Sql.export(c)):
        text = json.dumps(doc, ensure_ascii=False, indent=1)
        db.upsert(c, db.docs, {"path": path, "name": path.split("/")[-1], "project_id": path.split("/")[2][:64],
                               "kind": "json", "body": text, "data": None, "size": len(text.encode()), "updated": now})
    op.drop_table("km_items")
    op.drop_table("km_meta")
