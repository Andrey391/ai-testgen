"""Saved tests (storage.py keeps the logic, this - the storage).

The `tests` table: columns for filters (status, role, tags, quarantine, the last run), the test itself
in `body`; a read-modify-write holds the row (SELECT ... FOR UPDATE).
"""
from __future__ import annotations

import re
from typing import Callable

from sqlalchemy import case, func, select

from .. import db
from . import held

Change = Callable[[dict | None], dict | None]       # the stored test (None: none yet) -> the test to store
LIST_COUNTS = ("steps", "before", "after", "comments")    # Sql.listing: lengths instead of the arrays


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
    def listing(cls, pid: str, *, ids=None, tag: str = "", q: str = "", status: str = "", layer: str = "",
                health: str = "") -> list[dict]:
        """The test list with its filters in SQL: text in the name, address or id, status, layer (`ui`/`api`),
        tag, ids (the tests of a task) and health that the columns know (`failing`, `quarantined`, `weak`,
        `heal`; `flaky` needs the run history - storage.list_tests)."""
        t = cls.t
        w = [t.c.project_id == pid]
        if ids is not None:
            w.append(t.c.id.in_(list(ids)))
        if tag:
            w.append(t.c.tags.overlap([tag]))
        if q:
            like = "%" + "".join("\\" + ch if ch in "\\%_" else ch for ch in q.strip()) + "%"
            w.append(t.c.name.ilike(like) | t.c.body["url"].astext.ilike(like) | t.c.id.ilike(like))
        if status:
            w.append(t.c.status == status)
        if layer:
            lay = func.coalesce(t.c.body["layer"].astext, "ui")
            w.append(lay == layer if layer == "api" else lay != "api")
        if health == "failing":
            w.append(t.c.last_status.in_(["failed", "error"]))
        elif health == "quarantined":
            w.append(t.c.quarantined.is_(True))
        elif health == "weak":
            w.append(t.c.body["verify"]["weak"].as_boolean().is_(True))
        elif health == "heal":
            w.append(cls._proposals() > 0)
        elif health == "outdated":
            w.append(cls._outdated() > 0)
        # The list needs only how many steps, data steps and comments a test has: the arrays stay in the database
        body = t.c.body
        for k in LIST_COUNTS:
            body = body.op("-", return_type=t.c.body.type)(k)
        counts = [cls._length(k).label(k) for k in LIST_COUNTS]
        with db.engine().connect() as c:
            return [dict(r.body) | {"counts": {k: getattr(r, k) for k in LIST_COUNTS}}
                    for r in c.execute(select(body.label("body"), *counts).where(*w).order_by(t.c.updated.desc()))]

    @classmethod
    def _length(cls, key: str):
        v = cls.t.c.body[key]
        return case((func.jsonb_typeof(v) == "array", func.jsonb_array_length(v)), else_=0)

    @classmethod
    def _proposals(cls):
        props = cls.t.c.body["heal_proposals"]
        # SQL does not promise to check the type first: the length only of an array
        return case((func.jsonb_typeof(props) == "array", func.jsonb_array_length(props)), else_=0)

    @classmethod
    def _outdated(cls):
        marks = cls.t.c.body["outdated"]
        return case((func.jsonb_typeof(marks) == "array", func.jsonb_array_length(marks)), else_=0)

    @classmethod
    def outdated(cls, pid: str) -> list[dict]:
        """Tests of the project marked outdated (what they were made from changed): {id, name, count, since,
        author}."""
        t = cls.t
        n = cls._outdated()
        author = t.c.body["author"].astext.label("author")
        marks = t.c.body["outdated"].label("marks")
        with db.engine().connect() as c:
            return [{"id": r.id, "name": r.name or r.id, "count": r.n,
                     "since": min((m.get("at") or 0 for m in r.marks or []), default=r.updated),
                     "author": r.author or r.updated_by or ""} for r in c.execute(
                select(t.c.id, t.c.name, t.c.updated, t.c.updated_by, author, marks, n.label("n")).where(
                    t.c.project_id == pid, n > 0))]

    @classmethod
    def names(cls, pid: str) -> dict[str, str]:
        with db.engine().connect() as c:
            return {r.id: r.name or r.id for r in c.execute(select(cls.t.c.id, cls.t.c.name)
                                                            .where(cls.t.c.project_id == pid))}

    @classmethod
    def brief(cls, pid: str) -> dict[str, dict]:
        """{test id: {name, status, last_status}} of a project, from the columns (no steps)."""
        t = cls.t
        with db.engine().connect() as c:
            return {r.id: {"name": r.name or r.id, "status": r.status, "last_status": r.last_status}
                    for r in c.execute(select(t.c.id, t.c.name, t.c.status, t.c.last_status)
                                       .where(t.c.project_id == pid))}

    @classmethod
    def data_refs(cls, pid: str) -> list[dict]:
        """Tests of the project that use or create records of the application model: {id, name, data_refs}."""
        t = cls.t
        refs = t.c.body["data_refs"]
        with db.engine().connect() as c:
            return [{"id": r.id, "name": r.name or r.id, "data_refs": list(r.refs or [])} for r in c.execute(
                select(t.c.id, t.c.name, refs.label("refs")).where(t.c.project_id == pid, t.c.body.has_key("data_refs")))]

    @classmethod
    def healing(cls, pid: str) -> list[dict]:
        """Tests of the project with self-healing proposals waiting for a person: {id, name, count, updated, author}."""
        t = cls.t
        n = cls._proposals()
        author = t.c.body["author"].astext.label("author")
        with db.engine().connect() as c:
            return [{"id": r.id, "name": r.name or r.id, "count": r.n, "updated": r.updated,
                     "author": r.author or r.updated_by or ""} for r in c.execute(
                select(t.c.id, t.c.name, t.c.updated, t.c.updated_by, author, n.label("n")).where(
                    t.c.project_id == pid, n > 0))]

    @classmethod
    def under_review(cls, pid: str) -> list[dict]:
        """Tests of the project with the status `review`: {id, name, updated, author} - the author reviews them
        (whoever saved the test last when it has none)."""
        t = cls.t
        author = t.c.body["author"].astext.label("author")
        with db.engine().connect() as c:
            return [{"id": r.id, "name": r.name or r.id, "updated": r.updated, "author": r.author or r.updated_by or ""}
                    for r in c.execute(select(t.c.id, t.c.name, t.c.updated, t.c.updated_by, author).where(
                        t.c.project_id == pid, t.c.status == "review"))]

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

