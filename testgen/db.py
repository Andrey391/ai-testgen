"""The studio's database: PostgreSQL through SQLAlchemy - the only place where its data lives, for one
instance as for several instances of the studio and any number of workers.

    TESTGEN_DATABASE_URL   postgresql+psycopg://testgen:***@127.0.0.1:5432/testgen   (required)

Without it the studio, workers and command-line tools do not start (NotConfigured).

Tables (migrations in testgen/migrations, Alembic; `python -m testgen.db upgrade`, also run at
start under a lock unless TESTGEN_DB_MIGRATE=off):
    runs       run history (runs.py through repo/runs.py): a row per run, columns for filters and
               reports, the whole record in `report` (jsonb)
    tests      saved tests (storage.py, repo/tests.py): columns for filters, the test in `body` (jsonb)
    tasks      the team's tasks (tasks.py, repo/tasks.py)
    usage      language model spending: a row per request (llm.py, repo/usage.py)
    docs       everything else by path: JSON documents (projects, suites, jobs, users...), text
               (skills, notes), secrets (encrypted, vault.py) and binary files when there is no S3;
               for files in S3 only their size and time. `body` is JSON text: reports may use body::jsonb.
    audit_log  the audit log (audit.py), its hash chain in insertion order
    work       the queue of runs, suites, mutation checks and explorations (workqueue.py)
    workers    workers alive: heartbeat, running items
    owners     which instance of the studio holds a live Studio session or pipeline job
Locks between processes are PostgreSQL advisory locks (fs.lock).

Data of older versions kept in folders: `python -m testgen.db import-files <data> <secrets>` copies
them into the database (and S3); `export-files <dir>` writes the database out as folders of the same
layout (a copy to read; pg_dump is the backup to restore).
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import (BigInteger, Boolean, Column, Float, Index, Integer, LargeBinary, MetaData, String, Table,
                        Text, create_engine, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine

metadata = MetaData()
Doc = JSONB()
Strings = ARRAY(Text())

docs = Table(
    "docs", metadata,
    Column("path", String(512), primary_key=True),
    Column("name", String(255), nullable=False, index=True),
    Column("project_id", String(64), index=True),
    Column("kind", String(8), nullable=False),          # json | text | bin | s3
    Column("body", Text),
    Column("data", LargeBinary),
    Column("size", BigInteger, nullable=False, default=0),
    Column("updated", Float, nullable=False, index=True),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("month", String(7), nullable=False, index=True),
    Column("ts", Float, nullable=False),
    Column("project_id", String(64), index=True),
    Column("rec", Text, nullable=False),
)

work = Table(
    "work", metadata,
    Column("id", String(24), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("project_id", String(64), index=True),
    Column("ref", String(64), index=True),               # the run, suite, test or exploration it is about
    Column("payload", Text, nullable=False),
    Column("status", String(12), nullable=False, index=True),   # queued | running | done | failed
    Column("worker", String(128)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("created", Float, nullable=False, index=True),
    Column("started", Float),
    Column("heartbeat", Float),
    Column("finished", Float),
    Column("error", Text),
)

workers = Table(
    "workers", metadata,
    Column("id", String(128), primary_key=True),
    Column("host", String(255)),
    Column("started", Float, nullable=False),
    Column("heartbeat", Float, nullable=False, index=True),
    Column("running", Integer, nullable=False, default=0),
    Column("capacity", Integer, nullable=False, default=1),
)

owners = Table(
    "owners", metadata,
    Column("kind", String(16), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("url", String(512), nullable=False),
    Column("updated", Float, nullable=False),
)

runs = Table(
    "runs", metadata,
    Column("id", String(16), primary_key=True),
    Column("project_id", String(64), nullable=False),
    Column("test_id", String(128), nullable=False),
    Column("status", String(12), nullable=False, index=True),    # running | passed | flaky | failed | error
    Column("trigger", String(16)),
    Column("suite_id", String(32), index=True),
    Column("started_by", String(64)),
    Column("started", Float, nullable=False, index=True),
    Column("finished", Float),                                   # NULL while it runs
    Column("duration", Float),
    Column("passed", Boolean),
    Column("flaky", Boolean, nullable=False, default=False),
    Column("quarantined", Boolean, nullable=False, default=False),
    Column("healed", Integer, nullable=False, default=0),
    Column("proposals", Integer, nullable=False, default=0),
    Column("outcomes", Doc),                                     # pass/fail of each attempt
    Column("report", Doc, nullable=False),
    Index("ix_runs_test", "project_id", "test_id", "finished"),
)

tests = Table(
    "tests", metadata,
    Column("project_id", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("name", Text, nullable=False, default=""),
    Column("status", String(12), nullable=False, default="ready"),   # draft | review | ready
    Column("role", String(16), nullable=False, default=""),          # "" | login | module
    Column("tags", Strings),
    Column("quarantined", Boolean, nullable=False, default=False),
    Column("last_status", String(12)),
    Column("updated", Float, nullable=False),
    Column("updated_by", String(64)),
    Column("body", Doc, nullable=False),
    Index("ix_tests_id", "id"),
)

tasks = Table(
    "tasks", metadata,
    Column("id", String(16), primary_key=True),
    Column("project_id", String(64), nullable=False, index=True),
    Column("title", Text, nullable=False, default=""),
    Column("status", String(12), nullable=False),                  # todo | in_progress | review | done
    Column("priority", String(8), nullable=False),
    Column("assignee", String(64), nullable=False, default=""),
    Column("due", String(10), nullable=False, default=""),
    Column("created", Float, nullable=False),
    Column("updated", Float, nullable=False),
    Column("done_at", Float),
    Column("test_ids", Strings),
    Column("body", Doc, nullable=False),
)

usage = Table(
    "usage", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("project_id", String(64), nullable=False),
    Column("month", String(7), nullable=False),
    Column("at", Float, nullable=False),
    Column("stage", String(32), nullable=False),
    Column("model", String(128), nullable=False),
    Column("requests", Integer, nullable=False, default=1),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("cache_creation_input_tokens", BigInteger, nullable=False, default=0),
    Column("cache_read_input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Index("ix_usage_project_month", "project_id", "month"),
)

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_engine: tuple[str, Engine] | None = None
_init = threading.Lock()


class NotConfigured(RuntimeError):
    """No TESTGEN_DATABASE_URL, or not a PostgreSQL one."""


def url() -> str:
    u = os.environ.get("TESTGEN_DATABASE_URL", "").strip()
    if not u:
        raise NotConfigured("Задайте TESTGEN_DATABASE_URL — адрес базы PostgreSQL, например "
                            "postgresql+psycopg://testgen:пароль@127.0.0.1:5432/testgen: данные студии хранятся только в ней")
    if not u.startswith("postgresql"):
        raise NotConfigured("TESTGEN_DATABASE_URL: студия хранит данные только в PostgreSQL (адрес postgresql+psycopg://...)")
    return u


def engine() -> Engine:
    """The engine for TESTGEN_DATABASE_URL, created (and migrated) on first use."""
    global _engine
    u = url()
    if _engine and _engine[0] == u:
        return _engine[1]
    with _init:
        if _engine and _engine[0] == u:
            return _engine[1]
        e = create_engine(u, pool_pre_ping=True, pool_size=int(os.environ.get("TESTGEN_DB_POOL", "10")),
                          max_overflow=20)
        if os.environ.get("TESTGEN_DB_MIGRATE", "on").lower() not in ("off", "0", "false", "no"):
            upgrade(e)
        if _engine:
            _engine[1].dispose()        # another database (tests switch them): its pooled connections go
        _engine = (u, e)
        return e


def upsert(conn, table: Table, values: dict) -> None:
    """INSERT, or UPDATE the row with the same primary key."""
    from sqlalchemy.dialects.postgresql import insert
    keys = [c.name for c in table.primary_key.columns]
    rest = {k: v for k, v in values.items() if k not in keys}
    conn.execute(insert(table).values(**values).on_conflict_do_update(index_elements=keys, set_=rest))


def lock_id(name: str) -> int:
    """A stable signed 64-bit number for pg_advisory_lock."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def upgrade(e: Engine | None = None, revision: str = "head", down: bool = False) -> None:
    """Alembic migrations up to `revision` (the newest by default; `down` - back to it); one process
    at a time (a PostgreSQL advisory lock)."""
    e = e or engine()
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", str(e.url.render_as_string(hide_password=False)).replace("%", "%%"))
    with e.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_id("testgen:migrations")})
        try:
            cfg.attributes["connection"] = conn
            (command.downgrade if down else command.upgrade)(cfg, revision)
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_id("testgen:migrations")})
            conn.commit()


def check() -> None:
    """At the start of the studio, a worker or a command-line tool: the database answers (and is
    migrated), secrets can be encrypted. -> NotConfigured with what to fix."""
    from sqlalchemy.exc import SQLAlchemyError
    from . import vault
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        raise NotConfigured(f"PostgreSQL по адресу TESTGEN_DATABASE_URL не отвечает: {e.__class__.__name__}: "
                            f"{str(e.orig if hasattr(e, 'orig') else e).strip()[:300]}")
    try:
        vault.check()
    except vault.VaultError as e:
        raise NotConfigured(str(e))


def ping() -> bool:
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _cli(argv: list[str]) -> None:
    from . import fs, vault
    from .paths import DATA, SECRETS, utf8_console
    utf8_console()
    cmd, *rest = argv or ["help"]
    try:
        url()
    except NotConfigured as e:
        sys.exit(str(e))
    if cmd == "upgrade":
        upgrade(revision=rest[0] if rest else "head")
        print("База данных обновлена до " + (f"версии {rest[0]}" if rest else "последней версии"))
    elif cmd == "downgrade" and rest:
        os.environ["TESTGEN_DB_MIGRATE"] = "off"
        upgrade(revision=rest[0], down=True)
        print(f"База данных возвращена к версии {rest[0]}")
    elif cmd == "import-files" and rest:
        try:
            key = vault.secret_key()
        except vault.VaultError as e:
            sys.exit(str(e))
        data, secrets = Path(rest[0]), Path(rest[1]) if len(rest) > 1 else None
        if not data.is_dir() or secrets is not None and not secrets.is_dir():
            sys.exit("Нет такой папки: " + (str(data) if not data.is_dir() else str(secrets)))
        started = time.time()
        n = fs.import_tree(data, DATA) + (fs.import_tree(secrets, SECRETS) if secrets else 0)
        sealed = vault.DbVault(SECRETS).reseal(key, key)      # plain secrets of the old folder: encrypted now
        print(f"Перенесено файлов: {n} (секретов зашифровано: {sealed}) за {time.time() - started:.0f} с")
    elif cmd == "export-files" and rest:
        for d in ("data", "secrets"):
            (Path(rest[0]) / d).mkdir(parents=True, exist_ok=True)     # import-files takes both back
        n =fs.export_tree(DATA, Path(rest[0]) / "data") + fs.export_tree(SECRETS, Path(rest[0]) / "secrets")
        print(f"Выгружено файлов: {n} в {rest[0]}")
    else:
        print(__doc__ + "\nCommands: upgrade [revision] | downgrade <revision> | import-files <data_dir> [secrets_dir]"
                        " | export-files <dir>")


if __name__ == "__main__":
    _cli(sys.argv[1:])
