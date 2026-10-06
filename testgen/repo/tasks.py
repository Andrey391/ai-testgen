"""Project tasks (tasks.py keeps the logic, this - the storage).

The `tasks` table: status, priority, assignee, due date and linked tests as columns, the task in `body`.
"""
from __future__ import annotations

from typing import Callable

from sqlalchemy import func, select

from .. import db
from . import held

Change = Callable[[dict | None], dict | None]


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

