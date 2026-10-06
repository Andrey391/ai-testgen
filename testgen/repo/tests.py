"""Saved tests (storage.py keeps the logic, this - the storage).

Files:  data/projects/<id>/tests/<test>.json; a read-modify-write under fs.lock
Sql:    the `tests` table: columns for filters (status, role, tags, quarantine, the last run), the
        test itself in `body`; a read-modify-write holds the row (SELECT ... FOR UPDATE)
"""
from __future__ import annotations

import json
import re
from typing import Callable

from sqlalchemy import func, select, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text

from .. import db, fs, projects
from . import held, sql

Change = Callable[[dict | None], dict | None]       # the stored test (None: none yet) -> the test to store


def safe(test_id: str) -> str:
    return re.sub(r"[^\w-]+", "_", test_id.strip()) or "_"


def status(test: dict) -> str:
    return test.get("status") or "ready"


def _matches(t: dict, ids, tags, ready: bool, modules: bool) -> bool:
    if not modules and t.get("role") == "module":
        return False
    if ids is not None:
        return t["id"] in ids
    if ready and status(t) != "ready":
        return False
    return not tags or bool(set(t.get("tags") or []) & set(tags))


class Files:
    @staticmethod
    def file(pid: str, tid: str):
        return projects.path(pid) / "tests" / f"{safe(tid)}.json"

    @staticmethod
    def locate(tid: str) -> str | None:
        p = next(iter(fs.glob(projects.ROOT, f"*/tests/{safe(tid)}.json")), None)
        return p.parent.parent.name if p else None

    @classmethod
    def get(cls, tid: str) -> dict | None:
        pid = cls.locate(tid)
        if pid is None:
            return None
        f = cls.file(pid, tid)
        with fs.reading(f):
            return fs.read_json(f)

    @classmethod
    def write(cls, pid: str, tid: str, change: Change) -> dict | None:
        f = cls.file(pid, tid)
        with fs.lock(f):
            try:
                old = fs.read_json(f)
            except ValueError:
                old = None
            new = change(old)
            if new is not None:
                fs.write_json(f, new)
            return new

    @staticmethod
    def all(pid: str) -> list[dict]:
        out = []
        for _, text, _ in fs.documents(projects.path(pid) / "tests"):
            try:
                out.append(json.loads(text))
            except ValueError:
                continue
        return out

    @classmethod
    def query(cls, pid: str, ids=None, tags=None, ready: bool = False, modules: bool = True) -> list[dict]:
        return [t for t in cls.all(pid) if _matches(t, ids, tags, ready, modules)]

    @classmethod
    def names(cls, pid: str) -> dict[str, str]:
        return {t["id"]: t.get("name", t["id"]) for t in cls.all(pid) if "id" in t}

    @staticmethod
    def counts() -> dict[str, int]:
        out: dict[str, int] = {}
        for p in fs.glob(projects.ROOT, "*/tests/*.json"):
            pid = p.parent.parent.name
            out[pid] = out.get(pid, 0) + 1
        return out

    @classmethod
    def delete(cls, pid: str, tid: str) -> None:
        fs.unlink(cls.file(pid, tid))


class Sql:
    t = db.tests

    @classmethod
    def row(cls, test: dict) -> dict:
        return {"project_id": test["project_id"], "id": test["id"], "name": str(test.get("name") or ""),
                "status": status(test), "role": test.get("role") or "", "tags": list(test.get("tags") or []),
                "quarantined": bool((test.get("quarantine") or {}).get("on")),
                "last_status": (test.get("last_run") or {}).get("status"),
                "updated": float(test.get("updated") or 0), "updated_by": test.get("updated_by") or "",
                "body": test}

    @classmethod
    def put(cls, c, test: dict) -> None:
        db.upsert(c, cls.t, cls.row(test))

    @classmethod
    def locate(cls, tid: str) -> str | None:
        with db.engine().connect() as c:
            return c.execute(select(cls.t.c.project_id).where(cls.t.c.id == tid).limit(1)).scalar()

    @classmethod
    def get(cls, tid: str) -> dict | None:
        with db.engine().connect() as c:
            r = c.execute(select(cls.t.c.body).where(cls.t.c.id == tid).limit(1)).first()
        return dict(r.body) if r else None

    @classmethod
    def write(cls, pid: str, tid: str, change: Change) -> dict | None:
        t = cls.t
        with held(t, [t.c.project_id == pid, t.c.id == tid]) as (c, r):
            new = change(dict(r.body) if r else None)
            if new is not None:
                cls.put(c, new | {"project_id": pid, "id": tid})
            return new

    @classmethod
    def all(cls, pid: str) -> list[dict]:
        return cls.query(pid)

    @classmethod
    def query(cls, pid: str, ids=None, tags=None, ready: bool = False, modules: bool = True) -> list[dict]:
        """Filters in SQL: role, status, ids, tags."""
        t = cls.t
        q = select(t.c.body).where(t.c.project_id == pid).order_by(t.c.updated.desc())
        if not modules:
            q = q.where(t.c.role != "module")
        if ids is not None:
            q = q.where(t.c.id.in_(list(ids)))
        elif ready:
            q = q.where(t.c.status == "ready")
        if ids is None and tags:
            q = q.where(type_coerce(t.c.tags, ARRAY(Text())).overlap(list(tags)))
        with db.engine().connect() as c:
            return [dict(r.body) for r in c.execute(q)]

    @classmethod
    def names(cls, pid: str) -> dict[str, str]:
        with db.engine().connect() as c:
            return {r.id: r.name or r.id for r in c.execute(select(cls.t.c.id, cls.t.c.name)
                                                            .where(cls.t.c.project_id == pid))}

    @classmethod
    def counts(cls) -> dict[str, int]:
        with db.engine().connect() as c:
            return dict(c.execute(select(cls.t.c.project_id, func.count()).group_by(cls.t.c.project_id)).all())

    @classmethod
    def delete(cls, pid: str, tid: str) -> None:
        with db.engine().begin() as c:
            c.execute(cls.t.delete().where(cls.t.c.project_id == pid, cls.t.c.id == tid))

    @classmethod
    def export(cls, c):
        for r in c.execute(select(cls.t.c.project_id, cls.t.c.id, cls.t.c.body)):
            yield f"data/projects/{r.project_id}/tests/{safe(r.id)}.json", r.body


def backend():
    return Sql if sql() else Files
