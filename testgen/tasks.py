"""Project tasks: the team's work items (what to cover with tests, what to fix,
what to review), one JSON file per task under data/projects/<project id>/tasks/, or a row of
the `tasks` table with a shared database (repo/tasks.py).

A task keeps a title, description, status, priority, assignee (a studio user),
due date and the ids of the tests that cover it. A test created in Studio from a
task is linked to it on save; deleting a test unlinks it (storage.delete).
"""
from __future__ import annotations

import datetime
import re
import time
import uuid

from .repo import tasks as repo

STATUSES = ["todo", "in_progress", "review", "done"]
OPEN = {"todo", "in_progress", "review"}
PRIORITIES = ["high", "medium", "low"]

def _check_id(tid: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{10}", tid or ""):
        raise ValueError("Bad task id")
    return tid


def _due(v) -> str:
    v = str(v or "").strip()
    if not v:
        return ""
    try:
        return datetime.date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError("Срок — дата в формате ГГГГ-ММ-ДД")


def _clean(patch: dict, pid: str) -> dict:
    """Checked fields of a create/update request; unknown keys are dropped."""
    out = {}
    if "title" in patch:
        out["title"] = str(patch["title"] or "").strip()[:200]
        if not out["title"]:
            raise ValueError("Укажите название задачи")
    if "description" in patch:
        out["description"] = str(patch["description"] or "").strip()
    if "status" in patch:
        if patch["status"] not in STATUSES:
            raise ValueError(f"Статус — один из: {', '.join(STATUSES)}")
        out["status"] = patch["status"]
    if "priority" in patch:
        if patch["priority"] not in PRIORITIES:
            raise ValueError(f"Приоритет — один из: {', '.join(PRIORITIES)}")
        out["priority"] = patch["priority"]
    if "assignee" in patch:
        out["assignee"] = str(patch["assignee"] or "").strip()[:64]
    if "due" in patch:
        out["due"] = _due(patch["due"])
    if "test_ids" in patch:
        from . import storage
        known = set(storage.names(pid))
        ids = patch["test_ids"] if isinstance(patch["test_ids"], list) else []
        out["test_ids"] = list(dict.fromkeys(str(x) for x in ids if str(x) in known))
    return out


def _write(t: dict) -> dict:
    t["updated"] = time.time()
    repo.Sql.write(t["project_id"], _check_id(t["id"]), lambda old: t)
    return t


def _change(tid: str, change) -> dict | None:
    """Re-read the task and store `change(task)` (a dict of fields) under the lock."""
    t = load(tid)
    if t is None:
        return None

    def apply(old: dict | None) -> dict | None:
        if old is None:
            return None
        new = old | change(old)
        new["updated"] = time.time()
        return new
    return repo.Sql.write(t["project_id"], tid, apply)


def create(pid: str, data: dict, user: str = "") -> dict:
    fields = _clean({"title": data.get("title", "")} | data, pid)
    t = {"id": uuid.uuid4().hex[:10], "project_id": pid, "title": "", "description": "", "status": "todo",
         "priority": "medium", "assignee": "", "due": "", "test_ids": [], "created": time.time(),
         "created_by": user or ""} | fields
    if t["status"] == "done":
        t["done_at"] = time.time()
    return _write(t)


def load(tid: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{10}", tid or ""):
        return None
    return repo.Sql.get(tid)


def update(tid: str, patch: dict) -> dict | None:
    """Partial change under the lock (tasks are written by people and by test saves)."""
    t = load(tid)
    if t is None:
        return None
    fields = _clean(patch, t["project_id"])

    def change(t: dict) -> dict:
        out = dict(fields)
        if "status" in fields and fields["status"] != t["status"]:
            out["done_at"] = time.time() if fields["status"] == "done" else None
        return out
    return _change(tid, change)


def delete(tid: str) -> bool:
    t = load(tid)
    if not t:
        return False
    repo.Sql.delete(t["project_id"], tid)
    return True


def all_tasks(pid: str) -> list[dict]:
    return repo.Sql.all(pid)


def _order(t: dict) -> tuple:
    # Open first; then by priority, the nearest due date, the newest.
    return (t["status"] == "done", PRIORITIES.index(t.get("priority", "medium")), t.get("due") or "9999",
            -t.get("created", 0))


def list_tasks(pid: str, status: str = "", assignee: str = "") -> list[dict]:
    """Tasks for the list: with the names of linked tests (deleted ones are dropped)."""
    from . import storage
    names = storage.names(pid)
    today = datetime.date.today().isoformat()
    only = sorted(OPEN) if status == "open" else [status] if status in STATUSES else None
    out = []
    for t in sorted(repo.Sql.all(pid, only, assignee), key=_order):
        tests = [{"id": x, "name": names[x]} for x in t.get("test_ids") or [] if x in names]
        out.append(t | {"tests": tests, "overdue": bool(t.get("due")) and t["status"] != "done" and t["due"] < today})
    return out


def counts(pid: str) -> dict:
    c = dict.fromkeys(STATUSES, 0) | repo.Sql.counts(pid)
    c["open"] = sum(c[s] for s in OPEN)
    return c


def link_test(tid: str, test_id: str) -> dict | None:
    """A test made for the task (Studio): link it; a task not started yet moves to work."""
    def change(t):
        ids = t.get("test_ids") or []
        return {"test_ids": ids + [test_id] if test_id not in ids else ids,
                **({"status": "in_progress", "done_at": None} if t["status"] == "todo" else {})}
    return _change(tid, change)


def unlink_test(pid: str, test_id: str) -> None:
    for tid in repo.Sql.with_test(pid, test_id):
        _change(tid, lambda t: {"test_ids": [x for x in t.get("test_ids") or [] if x != test_id]})
