"""Repositories: tables of their own for runs, tests, tasks and model spending (db.py) - columns for
filters and reports, the document itself in jsonb. Each module has a class `Sql` with the methods
the logic (runs.py, storage.py, tasks.py, llm.py) calls.

Everything else (projects, skills, suites, jobs, users...) is documents by path (fs.py); the files of
a run (screenshots, trace) and the versions of a test are files by path too.

Data of older versions kept in folders: `import_doc()` takes a file of that layout into its table and
`export_docs()` gives the tables back as files of that layout (db.py import-files / export-files,
migration 0003 and its downgrade).
"""
from __future__ import annotations

import contextlib
import json
from typing import Iterator

from sqlalchemy import select

from .. import db


@contextlib.contextmanager
def held(table, where: list):
    """A transaction with the row of `where` (None if there is none) held for a read-modify-write
    (SELECT ... FOR UPDATE). -> (connection, row). Write the row through this connection: another one
    would wait for it."""
    with db.engine().begin() as c:
        yield c, c.execute(select(table).where(*where).with_for_update()).first()


def drop_project(pid: str) -> None:
    """Rows of a deleted project (its files are removed by path)."""
    with db.engine().begin() as c:
        for t in (db.runs, db.tests, db.tasks, db.usage):
            c.execute(t.delete().where(t.c.project_id == pid))


def _parts(key: str) -> list[str] | None:
    """data/projects/<p>/<kind>/... -> its segments, else None."""
    parts = key.split("/")
    if len(parts) >= 5 and parts[0] == "data" and parts[1] == "projects" and parts[-1].endswith(".json"):
        return parts
    return None


def owns(key: str) -> bool:
    """Is this file of the old layout kept in a table?"""
    parts = _parts(key)
    if not parts:
        return False
    kind = parts[3]
    return (kind == "runs" and len(parts) == 6) or (kind in ("tests", "tasks", "usage") and len(parts) == 5)


def import_doc(c, key: str, text: str) -> bool:
    """A file of the old layout into its table. -> False if it is not one of theirs."""
    if not owns(key):
        return False
    from . import runs, tasks, tests, usage
    parts = _parts(key)
    pid, kind, name = parts[2], parts[3], parts[-1][:-len(".json")]
    if kind == "runs" and name == "index":
        return True                     # summaries come from the rows now
    try:
        doc = json.loads(text)
    except ValueError:
        return True
    if not isinstance(doc, dict):
        return True
    if kind == "runs":
        runs.Sql.put(c, doc | {"project_id": doc.get("project_id") or pid, "id": doc.get("id") or name})
    elif kind == "tests":
        tests.Sql.put(c, doc | {"project_id": pid, "id": doc.get("id") or name})
    elif kind == "tasks":
        tasks.Sql.put(c, doc | {"project_id": pid, "id": doc.get("id") or name})
    else:
        usage.Sql.put_month(c, pid, name, doc)
    return True


def import_text(key: str, text: str) -> bool:
    if not owns(key):
        return False
    with db.engine().begin() as c:
        return import_doc(c, key, text)


def export_docs(c, prefix: str = "") -> Iterator[tuple[str, str]]:
    """The tables as files of the old layout: (key, JSON text) under `prefix` ("data/projects/x")."""
    from . import runs, tasks, tests, usage
    for module in (tests, tasks, runs, usage):
        for key, doc in module.Sql.export(c):
            if not prefix or key.startswith(prefix.rstrip("/") + "/"):
                yield key, json.dumps(doc, ensure_ascii=False, indent=1)
