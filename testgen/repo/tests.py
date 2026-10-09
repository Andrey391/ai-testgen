"""Saved tests (storage.py keeps the logic, this - the storage).

The `tests` table: columns for filters (status, role, tags, quarantine, the last run), the test itself
in `body`; a read-modify-write holds the row (SELECT ... FOR UPDATE).
"""
from __future__ import annotations

import re
from typing import Callable

from sqlalchemy import func, select

from .. import db
from . import held

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
            q = q.where(t.c.tags.overlap(list(tags)))
        with db.engine().connect() as c:
            return [dict(r.body) for r in c.execute(q)]

    @classmethod
    def names(cls, pid: str) -> dict[str, str]:
        with db.engine().connect() as c:
            return {r.id: r.name or r.id for r in c.execute(select(cls.t.c.id, cls.t.c.name)
                                                            .where(cls.t.c.project_id == pid))}

    @classmethod
    def data_refs(cls, pid: str) -> list[dict]:
        """Tests of the project that use or create records of the application model: {id, name, data_refs}."""
        t = cls.t
        refs = t.c.body["data_refs"]
        with db.engine().connect() as c:
            return [{"id": r.id, "name": r.name or r.id, "data_refs": list(r.refs or [])} for r in c.execute(
                select(t.c.id, t.c.name, refs.label("refs")).where(t.c.project_id == pid, t.c.body.has_key("data_refs")))]

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

