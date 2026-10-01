"""The shared database (stage 5.5): PostgreSQL through SQLAlchemy, so that several instances of the
studio and any number of workers use the same data. SQLite works too - for one instance and for
the studio's own tests.

    TESTGEN_DATABASE_URL   postgresql+psycopg://testgen:***@db:5432/testgen   (or sqlite:///path/testgen.db)

Without it the studio keeps everything in files, as before (fs.py).

Tables (migrations in testgen/migrations, Alembic; `python -m testgen.db upgrade`, also run at
start under a lock unless TESTGEN_DB_MIGRATE=off):
    docs       the studio's files by path: JSON documents (projects, tests, tasks, runs, suites, jobs,
               users...), text (skills, notes) and binary files when there is no S3; for files in S3
               only their size and time. `body` is JSON text: reports may use body::jsonb.
    audit_log  the audit log (audit.py), its hash chain in insertion order
    work       the queue of runs, suites, mutation checks and explorations (workqueue.py)
    workers    workers alive: heartbeat, running items
    owners     which instance of the studio holds a live Studio session or pipeline job
    locks      locks between processes on SQLite (PostgreSQL uses advisory locks)

Moving an existing installation: `python -m testgen.db import-files` copies data/ and secrets/
into the database (and S3); `export-files` writes them back to folders (backups, leaving).
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import (BigInteger, Column, Float, Integer, LargeBinary, MetaData, String, Table, Text, create_engine,
                        event, text)
from sqlalchemy.engine import Engine

metadata = MetaData()

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

locks = Table(
    "locks", metadata,
    Column("name", String(512), primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("at", Float, nullable=False),
)

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
LOCK_STALE = 120          # seconds: a lock row older than this was left by a dead process (SQLite)
_engine: tuple[str, Engine] | None = None
_init = threading.Lock()


def url() -> str:
    return os.environ.get("TESTGEN_DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(url())


def is_postgres(e: Engine | None = None) -> bool:
    return (e or engine()).dialect.name == "postgresql"


def engine() -> Engine:
    """The engine for TESTGEN_DATABASE_URL, created (and migrated) on first use."""
    global _engine
    u = url()
    if _engine and _engine[0] == u:
        return _engine[1]
    with _init:
        if _engine and _engine[0] == u:
            return _engine[1]
        if u.startswith("sqlite"):
            e = create_engine(u, connect_args={"timeout": 60, "check_same_thread": False})

            @event.listens_for(e, "connect")
            def _sqlite_pragmas(conn, _record):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")      # readers do not wait for the writer
                cur.execute("PRAGMA busy_timeout=60000")
                cur.close()
        else:
            e = create_engine(u, pool_pre_ping=True, pool_size=int(os.environ.get("TESTGEN_DB_POOL", "10")),
                              max_overflow=20)
        if os.environ.get("TESTGEN_DB_MIGRATE", "on").lower() not in ("off", "0", "false", "no"):
            upgrade(e)
        _engine = (u, e)
        return e


def lock_id(name: str) -> int:
    """A stable signed 64-bit number for pg_advisory_lock."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def upgrade(e: Engine | None = None) -> None:
    """Alembic migrations up to the newest; one process at a time (a PostgreSQL advisory lock,
    a lock file next to a SQLite database)."""
    e = e or engine()
    if e.dialect.name == "sqlite" and e.url.database:
        # Processes starting together on one SQLite file: one migrates, the others wait for it.
        from .filelock import locked
        with locked(Path(e.url.database + ".migrate.lock")):
            return _upgrade(e)
    return _upgrade(e)


def _upgrade(e: Engine) -> None:
    from alembic import command
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", str(e.url.render_as_string(hide_password=False)).replace("%", "%%"))
    with e.connect() as conn:
        if is_postgres(e):
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_id("testgen:migrations")})
        try:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            conn.commit()
        finally:
            if is_postgres(e):
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_id("testgen:migrations")})
                conn.commit()


def ping() -> bool:
    try:
        with engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _cli(argv: list[str]) -> None:
    from . import fs
    from .paths import DATA, SECRETS
    cmd, *rest = argv or ["help"]
    if not enabled():
        sys.exit("Задайте TESTGEN_DATABASE_URL")
    if cmd == "upgrade":
        upgrade()
        print("База данных обновлена до последней версии")
    elif cmd == "import-files":
        from . import vault
        key = os.environ.get(vault.KEY_ENV)
        if not key:
            sys.exit(f"Задайте {vault.KEY_ENV}: секреты в общей базе хранятся только зашифрованными")
        data = Path(rest[0]) if rest else DATA
        secrets = Path(rest[1]) if len(rest) > 1 else SECRETS
        started = time.time()
        n = fs.import_tree(data, DATA) + fs.import_tree(secrets, SECRETS)
        sealed = vault.FileVault(SECRETS).reseal(key, key)      # plain secrets of the old folder: encrypted now
        print(f"Перенесено файлов: {n} (секретов зашифровано: {sealed}) за {time.time() - started:.0f} с")
    elif cmd == "export-files" and rest:
        n = fs.export_tree(DATA, Path(rest[0]) / "data") + fs.export_tree(SECRETS, Path(rest[0]) / "secrets")
        print(f"Выгружено файлов: {n} в {rest[0]}")
    else:
        print(__doc__ + "\nCommands: upgrade | import-files [data_dir [secrets_dir]] | export-files <dir>")


if __name__ == "__main__":
    _cli(sys.argv[1:])
