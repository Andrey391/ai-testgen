"""History of requirement analyses (the Requirements tab).

Every "Generate scenarios" is kept: the requirements it was given, the scenarios
designed from them (a person may edit, add and delete them) and, for each scenario,
the tests generated from it (in the Studio or by "Generate all", a pipeline run).
One document per analysis: data/projects/<id>/analyses/<analysis>.json.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from . import fs, projects

FIELDS = ("title", "type", "priority", "preconditions", "instructions", "expected_result", "gherkin")
TYPES = ("positive", "negative", "edge", "boundary", "accessibility", "security")
PRIORITIES = ("high", "medium", "low")
STALE = 3600     # a "running" analysis older than this was cut off (the studio restarted)


class Conflict(ValueError):
    pass


def _dir(pid: str):
    return projects.path(pid) / "analyses"


def _path(pid: str, aid: str):
    return _dir(pid) / f"{aid}.json"


def _id() -> str:
    return uuid.uuid4().hex[:10]


def create(pid: str, requirements: str, url: str = "", user: str = "") -> dict:
    first = next((line.strip(" #\t") for line in requirements.splitlines() if line.strip(" #\t")), "")
    a = {"id": _id(), "project_id": pid, "created": time.time(), "finished": None, "user": user,
         "title": first[:120], "requirements": requirements[:200_000], "url": url, "status": "running",
         "feature": "", "assumptions": [], "scenarios": [], "error": ""}
    fs.write_json(_path(pid, a["id"]), a, indent=1)
    return a


def get(aid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{10}", aid or ""):
        return None
    for f in fs.glob(projects.ROOT, f"*/analyses/{aid}.json"):
        return _stale(fs.read_json(f))
    return None


def _stale(a: dict) -> dict:
    if a["status"] == "running" and time.time() - a["created"] > STALE:
        a["status"], a["error"] = "error", a["error"] or "Генерация была прервана"
    return a


def _change(pid: str, aid: str, fn) -> dict:
    with fs.lock(_path(pid, aid)):
        a = fs.read_json(_path(pid, aid))
        out = fn(a)
        fs.write_json(_path(pid, aid), a, indent=1)
    return out if out is not None else a


def list_analyses(pid: str, limit: int = 50) -> list[dict]:
    out = []
    for _, text, _ in fs.documents(_dir(pid)):
        try:
            a = _stale(json.loads(text))
        except ValueError:
            continue
        ready = [s for s in a["scenarios"] if not s.get("pending")]
        out.append({k: a.get(k) for k in ("id", "created", "finished", "user", "title", "feature", "status", "error",
                                          "url")}
                   | {"scenarios": len(ready), "tests": sum(len(s.get("test_ids") or []) for s in ready)})
    return sorted(out, key=lambda x: x["created"], reverse=True)[:limit]


# ---------- the generation fills it in (scenarios.generate progress events) ----------

def _new_scenario(data: dict) -> dict:
    return {k: str(data.get(k) or "") for k in FIELDS} | {"id": _id(), "test_ids": []}


def set_plan(pid: str, aid: str, feature: str, assumptions: list[str], planned: list[dict]) -> list[dict]:
    def fn(a):
        a["feature"], a["assumptions"] = feature, list(assumptions)
        a["scenarios"] = [_new_scenario(s) | {"covers": s.get("covers", ""), "pending": True} for s in planned] + \
            [s for s in a["scenarios"] if not s.get("pending") and s.get("added")]
        return a["scenarios"]
    return _change(pid, aid, fn)


def set_batch(pid: str, aid: str, start: int, detailed: list[dict]) -> list[dict]:
    """Detailed scenarios from position `start` of the plan; one a person already edited stays."""
    def fn(a):
        out = []
        for k, d in enumerate(detailed):
            i = start + k
            if i < len(a["scenarios"]) and a["scenarios"][i].get("pending"):
                old = a["scenarios"][i]
                a["scenarios"][i] = {**{f: str(d.get(f) or "") for f in FIELDS}, "id": old["id"],
                                     "test_ids": old["test_ids"]}
                out.append(a["scenarios"][i])
        return out
    return _change(pid, aid, fn)


def finish(pid: str, aid: str, result: dict | None = None, error: str = "", status: str = "") -> dict:
    """Done (`result`: the final set fills what no batch filled) or stopped; plan entries never detailed go."""
    def fn(a):
        if result:
            a["feature"], a["assumptions"] = result["feature"], result["assumptions"]
            for i, s in enumerate(a["scenarios"]):
                if s.get("pending") and i < len(result["scenarios"]):
                    a["scenarios"][i] = {**{f: str(result["scenarios"][i].get(f) or "") for f in FIELDS},
                                         "id": s["id"], "test_ids": s["test_ids"]}
        a["scenarios"] = [s for s in a["scenarios"] if not s.get("pending")]
        a["status"] = status or ("error" if error else "done")
        a["error"], a["finished"] = error, time.time()
    return _change(pid, aid, fn)


# ---------- a person edits ----------

def clean_fields(data: dict) -> dict:
    out = {k: str(v)[:20_000] for k, v in data.items() if k in FIELDS and v is not None}
    if "type" in out and out["type"] not in TYPES:
        raise ValueError("Тип сценария: " + ", ".join(TYPES))
    if "priority" in out and out["priority"] not in PRIORITIES:
        raise ValueError("Приоритет сценария: high, medium или low")
    return out


def update_scenario(pid: str, aid: str, scid: str, data: dict) -> dict:
    fields = clean_fields(data)

    def fn(a):
        s = next((x for x in a["scenarios"] if x["id"] == scid), None)
        if s is None:
            raise KeyError(scid)
        if s.get("pending"):
            raise Conflict("Сценарий ещё детализируется")
        s.update(fields)
        return s
    return _change(pid, aid, fn)


def add_scenario(pid: str, aid: str, data: dict) -> dict:
    s = _new_scenario({"type": "positive", "priority": "medium"} | clean_fields(data)) | {"added": True}

    def fn(a):
        a["scenarios"].append(s)
        return s
    return _change(pid, aid, fn)


def delete_scenario(pid: str, aid: str, scid: str) -> bool:
    def fn(a):
        if a["status"] == "running":
            raise Conflict("Дождитесь окончания генерации сценариев")
        n = len(a["scenarios"])
        a["scenarios"] = [s for s in a["scenarios"] if s["id"] != scid]
        return len(a["scenarios"]) < n
    return _change(pid, aid, fn)


def delete(pid: str, aid: str) -> bool:
    if not fs.exists(_path(pid, aid)):
        return False
    fs.unlink(_path(pid, aid))
    return True


# ---------- tests made from scenarios ----------

def link_test(pid: str, aid: str, scid: str, test_id: str) -> None:
    if not fs.exists(_path(pid, aid)):
        return

    def fn(a):
        for s in a["scenarios"]:
            if s["id"] == scid and test_id not in s["test_ids"]:
                s["test_ids"].append(test_id)
    _change(pid, aid, fn)


def unlink_test(pid: str, test_id: str) -> None:
    for f, text, _ in fs.documents(_dir(pid)):
        if test_id in text:
            _change(pid, f.stem, lambda a: [s.update(test_ids=[t for t in s.get("test_ids") or [] if t != test_id])
                                            for s in a["scenarios"]])


def view(a: dict) -> dict:
    """The analysis with the tests of each scenario (name, status, last run); deleted tests drop out."""
    from . import storage
    out = dict(a)
    out["scenarios"] = []
    for s in a["scenarios"]:
        tests = []
        for tid in s.get("test_ids") or []:
            t = storage.load(tid)
            if t:
                last = t.get("last_run") or {}
                tests.append({"id": t["id"], "name": t["name"], "status": storage.status(t),
                              "last_run": {k: last.get(k) for k in ("status", "passed", "at")} if last else None})
        out["scenarios"].append({k: v for k, v in s.items() if k != "added"} | {"tests": tests})
    return out
