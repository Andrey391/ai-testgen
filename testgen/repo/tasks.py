"""Project tasks (tasks.py keeps the logic, this - the storage).

Files:  data/projects/<id>/tasks/<task>.json; a read-modify-write under fs.lock
Sql:    the `tasks` table: status, priority, assignee, due date and linked tests as columns
"""
from __future__ import annotations

import json
from typing import Callable

from sqlalchemy import func, select

from .. import db, fs, projects
from . import held, sql

Change = Callable[[dict | None], dict | None]


class Files:
    @staticmethod
    def file(pid: str, tid: str):
        return projects.path(pid) / "tasks" / f"{tid}.json"

    @staticmethod
    def get(tid: str) -> dict | None:
        for p in fs.glob(projects.ROOT, f"*/tasks/{tid}.json"):
            with fs.reading(p):
                return fs.read_json(p)
        return None

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
    def all(pid: str, status: list[str] | None = None, assignee: str = "") -> list[dict]:
        out = []
        for _, text, _ in fs.documents(projects.path(pid) / "tasks"):
            try:
                t = json.loads(text)
            except ValueError:
                continue
            if (status is None or t.get("status") in status) and (not assignee or t.get("assignee") == assignee):
                out.append(t)
        return out

    @classmethod
    def counts(cls, pid: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for t in cls.all(pid):
            c[t["status"]] = c.get(t["status"], 0) + 1
        return c

    @classmethod
    def with_test(cls, pid: str, test_id: str) -> list[str]:
        return [t["id"] for t in cls.all(pid) if test_id in (t.get("test_ids") or [])]

    @classmethod
    def delete(cls, pid: str, tid: str) -> None:
        fs.unlink(cls.file(pid, tid))


class Sql:
    t = db.tasks

    @classmethod
    def row(cls, task: dict) -> dict:
        return {"id": task["id"], "project_id": task["project_id"], "title": task.get("title") or "",
                "status": task.get("status") or "todo", "priority": task.get("priority") or "medium",
                "assignee": task.get("assignee") or "", "due": task.get("due") or "",
                "created": float(task.get("created") or 0), "updated": float(task.get("updated") or 0),
                "done_at": task.get("done_at"), "test_ids": list(task.get("test_ids") or []), "body": task}

    @classmethod
    def put(cls, c, task: dict) -> None:
        db.upsert(c, cls.t, cls.row(task))

    @classmethod
    def get(cls, tid: str) -> dict | None:
        with db.engine().connect() as c:
            r = c.execute(select(cls.t.c.body).where(cls.t.c.id == tid)).first()
        return dict(r.body) if r else None

    @classmethod
    def write(cls, pid: str, tid: str, change: Change) -> dict | None:
        t = cls.t
        with held(t, [t.c.id == tid]) as (c, r):
            if r is not None and r.body.get("project_id") != pid:
                return None
            new = change(dict(r.body) if r else None)
            if new is not None:
                cls.put(c, new | {"project_id": pid, "id": tid})
            return new

    @classmethod
    def all(cls, pid: str, status: list[str] | None = None, assignee: str = "") -> list[dict]:
        t = cls.t
        q = select(t.c.body).where(t.c.project_id == pid).order_by(t.c.updated.desc())
        if status is not None:
            q = q.where(t.c.status.in_(status))
        if assignee:
            q = q.where(t.c.assignee == assignee)
        with db.engine().connect() as c:
            return [dict(r.body) for r in c.execute(q)]

    @classmethod
    def counts(cls, pid: str) -> dict[str, int]:
        t = cls.t
        with db.engine().connect() as c:
            return dict(c.execute(select(t.c.status, func.count()).where(t.c.project_id == pid)
                                  .group_by(t.c.status)).all())

    @classmethod
    def with_test(cls, pid: str, test_id: str) -> list[str]:
        t = cls.t
        with db.engine().connect() as c:
            rows = c.execute(select(t.c.id, t.c.test_ids).where(t.c.project_id == pid)).all()
        return [r.id for r in rows if test_id in (r.test_ids or [])]

    @classmethod
    def delete(cls, pid: str, tid: str) -> None:
        with db.engine().begin() as c:
            c.execute(cls.t.delete().where(cls.t.c.project_id == pid, cls.t.c.id == tid))

    @classmethod
    def export(cls, c):
        for r in c.execute(select(cls.t.c.project_id, cls.t.c.id, cls.t.c.body)):
            yield f"data/projects/{r.project_id}/tasks/{r.id}.json", r.body


def backend():
    return Sql if sql() else Files
