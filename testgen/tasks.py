"""Project tasks: the team's work items (what to cover with tests, what to fix,
what to review), one JSON file per task under data/projects/<project id>/tasks/.

A task keeps a title, description, status, priority, assignee (a studio user),
due date and the ids of the tests that cover it. A test created in Studio from a
task is linked to it on save; deleting a test unlinks it (storage.delete).
"""
from __future__ import annotations

import datetime
import json
import re
import time
import uuid

from . import fs, projects

STATUSES = ["todo", "in_progress", "review", "done"]
OPEN = {"todo", "in_progress", "review"}
PRIORITIES = ["high", "medium", "low"]

def _dir(pid: str):
    return projects.path(pid) / "tasks"


def _file(pid: str, tid: str):
    if not re.fullmatch(r"[a-f0-9]{10}", tid or ""):
        raise ValueError("Bad task id")
    return _dir(pid) / f"{tid}.json"


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
        known = {p.stem for p in fs.glob(projects.path(pid) / "tests", "*.json")}
        ids = patch["test_ids"] if isinstance(patch["test_ids"], list) else []
        out["test_ids"] = list(dict.fromkeys(str(x) for x in ids if str(x) in known))
    return out


def _write(t: dict) -> dict:
    t["updated"] = time.time()
    f = _file(t["project_id"], t["id"])
    with fs.lock(f):
        fs.write_json(f, t)
    return t


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
    for p in fs.glob(projects.ROOT, f"*/tasks/{tid}.json"):
        with fs.reading(p):
            return fs.read_json(p)
    return None


def update(tid: str, patch: dict) -> dict | None:
    """Partial change under the lock (tasks are written by people and by test saves)."""
    t = load(tid)
    if t is None:
        return None
    with fs.lock(_file(t["project_id"], tid)):
        t = load(tid)
        if t is None:
            return None
        fields = _clean(patch, t["project_id"])
        if "status" in fields and fields["status"] != t["status"]:
            t["done_at"] = time.time() if fields["status"] == "done" else None
        t.update(fields)
        return _write(t)


def delete(tid: str) -> bool:
    t = load(tid)
    if not t:
        return False
    fs.unlink(_file(t["project_id"], tid))
    return True


def all_tasks(pid: str) -> list[dict]:
    out = []
    for _, text, _ in fs.documents(_dir(pid)):
        try:
            out.append(json.loads(text))
        except ValueError:
            continue
    return out


def _order(t: dict) -> tuple:
    # Open first; then by priority, the nearest due date, the newest.
    return (t["status"] == "done", PRIORITIES.index(t.get("priority", "medium")), t.get("due") or "9999",
            -t.get("created", 0))


def list_tasks(pid: str, status: str = "", assignee: str = "") -> list[dict]:
    """Tasks for the list: with the names of linked tests (deleted ones are dropped)."""
    names = {}
    for p, text, _ in fs.documents(projects.path(pid) / "tests"):
        try:
            names[p.stem] = json.loads(text).get("name", p.stem)
        except ValueError:
            continue
    today = datetime.date.today().isoformat()
    out = []
    for t in sorted(all_tasks(pid), key=_order):
        if status == "open" and t["status"] not in OPEN or status in STATUSES and t["status"] != status:
            continue
        if assignee and t.get("assignee") != assignee:
            continue
        tests = [{"id": x, "name": names[x]} for x in t.get("test_ids") or [] if x in names]
        out.append(t | {"tests": tests, "overdue": bool(t.get("due")) and t["status"] != "done" and t["due"] < today})
    return out


def counts(pid: str) -> dict:
    c = dict.fromkeys(STATUSES, 0)
    for t in all_tasks(pid):
        c[t["status"]] = c.get(t["status"], 0) + 1
    c["open"] = sum(c[s] for s in OPEN)
    return c


def link_test(tid: str, test_id: str) -> dict | None:
    """A test made for the task (Studio): link it; a task not started yet moves to work."""
    def change(t):
        ids = t.get("test_ids") or []
        return {"test_ids": ids + [test_id] if test_id not in ids else ids,
                **({"status": "in_progress"} if t["status"] == "todo" else {})}
    t = load(tid)
    if not t:
        return None
    with fs.lock(_file(t["project_id"], tid)):
        t = load(tid)
        return update(tid, change(t)) if t else None


def unlink_test(pid: str, test_id: str) -> None:
    for t in all_tasks(pid):
        if test_id in (t.get("test_ids") or []):
            with fs.lock(_file(pid, t["id"])):
                t = load(t["id"]) or t
                t["test_ids"] = [x for x in t.get("test_ids") or [] if x != test_id]
                _write(t)
