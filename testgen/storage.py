"""Saved tests: one JSON file per test under data/projects/<project id>/tests/.

Besides the steps a test keeps: tags (suite filters), quarantine, heal proposals
waiting for review, the mutation testing result ("verify"), the last run summary,
the authoring cost, its status (draft -> review -> ready: only ready tests join the
regression suite) and comments on steps. Runs, visual baselines, recorded traffic and
versions (data/projects/<id>/history/<test>/<n>.json: kept whenever the steps, the
data blocks, the name or the URL change) live in their own folders and are removed
with the test.

Tests are written from the web server and from the browser worker (runs) at the
same time - and with a shared database from other instances and workers: use `update()`
for partial changes, it re-reads the test under a lock (fs.lock).
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import time
import uuid

from . import fs, projects, runs, tasks, traffic, vault

TAG = re.compile(r"[\w.-]{1,40}")
# Who changes tests (the server sets it per request): version history and the audit log.
ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="")
# What a version of a test is: a change here keeps the previous state in the history.
VERSIONED = ("name", "url", "scenario", "steps", "before", "after")
MAX_VERSIONS = 50


def _safe(test_id: str) -> str:
    return re.sub(r"[^\w-]+", "_", test_id.strip()) or "_"


def _history_dir(pid: str, tid: str):
    return projects.path(pid) / "history" / _safe(tid)


def _file(pid: str, tid: str):
    return projects.path(pid) / "tests" / f"{_safe(tid)}.json"


def save(test: dict) -> dict:
    test.setdefault("id", uuid.uuid4().hex[:10])
    test["updated"] = time.time()
    f = _file(test["project_id"], test["id"])
    with fs.lock(f):
        try:
            old = fs.read_json(f)
        except ValueError:
            old = None
        if old and any(old.get(k) != test.get(k) for k in VERSIONED):
            _keep_version(old)
        test["updated_by"] = ACTOR.get() or test.get("updated_by", "")
        fs.write_json(f, test)
    return test


def _versions(h) -> list:
    return [p for p in fs.glob(h, "*.json") if p.stem.isdigit()]


def _keep_version(old: dict) -> None:
    h = _history_dir(old["project_id"], old["id"])
    n = max((int(p.stem) for p in _versions(h)), default=0) + 1
    snap = {k: old.get(k) for k in VERSIONED} | {"version": n, "at": old.get("updated") or time.time(),
                                                  "by": old.get("updated_by", "")}
    fs.write_json(h / f"{n}.json", snap, indent=1)
    for p in sorted(_versions(h), key=lambda p: int(p.stem))[:-MAX_VERSIONS]:
        fs.unlink(p)


def versions(test: dict) -> list[dict]:
    """Earlier versions of a test, newest first (without their steps)."""
    h = _history_dir(test["project_id"], test["id"])
    out = []
    for p in sorted(_versions(h), key=lambda p: -int(p.stem)):
        v = fs.read_json(p)
        out.append({"version": v["version"], "at": v["at"], "by": v.get("by", ""), "name": v.get("name"),
                    "steps": len(v.get("steps") or [])})
    return out


def version(test: dict, n: int) -> dict | None:
    return fs.read_json(_history_dir(test["project_id"], test["id"]) / f"{int(n)}.json")


def diff(old: dict, new: dict) -> list[dict]:
    """Step-level difference between two versions: added / removed / changed (by step id)."""
    a = {s["id"]: s for s in old.get("steps") or []}
    b = {s["id"]: s for s in new.get("steps") or []}
    out = []
    for s in new.get("steps") or []:
        if s["id"] not in a:
            out.append({"change": "added", "step": s})
        else:
            fields = [k for k in ("action", "description", "value", "locator") if a[s["id"]].get(k) != s.get(k)]
            if fields:
                out.append({"change": "changed", "step": s, "was": a[s["id"]], "fields": fields})
    out += [{"change": "removed", "step": s} for s in old.get("steps") or [] if s["id"] not in b]
    for k in ("name", "url", "scenario"):
        if old.get(k) != new.get(k):
            out.append({"change": "field", "field": k, "was": old.get(k), "now": new.get(k)})
    if (old.get("before") or []) != (new.get("before") or []) or (old.get("after") or []) != (new.get("after") or []):
        out.append({"change": "field", "field": "data", "was": "", "now": ""})
    return out


def restore(test_id: str, n: int) -> dict | None:
    """Roll back to version n (the current state becomes a version itself)."""
    def change(t: dict) -> None:
        v = version(t, n)
        if not v:
            raise KeyError(n)
        t.update({k: v.get(k) for k in VERSIONED if k in v})
    return update(test_id, change)


def _find(test_id: str):
    return next(iter(fs.glob(projects.ROOT, f"*/tests/{_safe(test_id)}.json")), None)


def load(test_id: str) -> dict | None:
    p = _find(test_id)
    if p is None:
        return None
    with fs.reading(p):
        return fs.read_json(p)


def update(test_id: str, change) -> dict | None:
    """Re-read the test, apply `change(test)` and save - atomically w.r.t. other updates."""
    p = _find(test_id)
    if p is None:
        return None
    with fs.lock(p):
        t = fs.read_json(p)
        if t is None:
            return None
        change(t)
        return save(t)


def delete(test_id: str) -> bool:
    for p in fs.glob(projects.ROOT, f"*/tests/{_safe(test_id)}.json"):
        pid = p.parent.parent.name
        fs.unlink(p)
        vault.delete(projects.secrets_kind(pid), f"test-{test_id}")
        runs.delete_test(pid, test_id)
        traffic.delete(pid, test_id)
        tasks.unlink_test(pid, test_id)
        fs.rmtree(projects.path(pid) / "baselines" / _safe(test_id))
        fs.rmtree(_history_dir(pid, test_id))
        return True
    return False


def normalize_tags(tags) -> list[str]:
    out = []
    for t in tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split():
        t = str(t).strip().lower()
        if TAG.fullmatch(t) and t not in out:
            out.append(t)
    return out


def all_tests(project_id: str) -> list[dict]:
    out = []
    for _, text, _ in fs.documents(projects.path(project_id) / "tests"):
        try:
            out.append(json.loads(text))
        except ValueError:
            continue
    return out


STATUSES = ("draft", "review", "ready")     # a test from the agent goes to regression after a person's review


def status(test: dict) -> str:
    """draft (written by the agent) -> review -> ready (in the regression suite). Older tests: ready."""
    return test.get("status") or "ready"


def select(project_id: str, tags: list[str] | None = None, test_ids: list[str] | None = None,
           include_drafts: bool = False) -> list[dict]:
    """Tests of a suite run: the given ids, else those with any of `tags`, else all. Modules never run on
    their own; drafts and tests under review join only when asked (or named by id)."""
    tests = [t for t in all_tests(project_id) if t.get("role") != "module"]
    if test_ids:
        return [t for t in tests if t["id"] in test_ids]
    if not include_drafts:
        tests = [t for t in tests if status(t) == "ready"]
    if tags:
        return [t for t in tests if set(t.get("tags") or []) & set(tags)]
    return tests


def list_tests(project_id: str, tag: str = "") -> list[dict]:
    out = []
    for t in all_tests(project_id):
        if tag and tag not in (t.get("tags") or []):
            continue
        hist = runs.history(project_id, t["id"])
        out.append({k: t.get(k) for k in ("id", "project_id", "name", "url", "scenario", "updated",
                                          "last_run", "external", "engine", "quarantine", "verify",
                                          "authoring_usage")}
                   | {"steps": len(t.get("steps", [])), "tags": t.get("tags") or [], "role": t.get("role") or "",
                      "status": status(t), "comments": len(t.get("comments") or []),
                      "data_steps": len(t.get("before") or []) + len(t.get("after") or []),
                      "proposals": len(t.get("heal_proposals") or []),
                      "recent": hist[-10:], "flip_rate": runs.flip_rate(hist),
                      "has_traffic": traffic.exists(project_id, t["id"])})
    return out


# ---------- login for the application under test ----------

def credentials(test: dict) -> dict:
    """Test's own login, else the project's, else TESTGEN_USERNAME / TESTGEN_PASSWORD /
    TESTGEN_TOTP_SECRET (the same variables the exported code reads)."""
    src = own_credentials(test) or projects.app_credentials(test["project_id"])
    c = {}
    for key in ("username", "password", "totp_secret"):
        c[key] = src.get(key) or os.environ.get(f"TESTGEN_{key.upper()}", "")
    return {k: v for k, v in c.items() if v}


def own_credentials(test: dict) -> dict:
    return vault.load(projects.secrets_kind(test["project_id"]), f"test-{test['id']}") or {}


def set_own_credentials(test: dict, c: dict) -> None:
    c = {k: v for k, v in c.items() if v}
    kind, key = projects.secrets_kind(test["project_id"]), f"test-{test['id']}"
    if c:
        vault.save(kind, key, c)
    else:
        vault.delete(kind, key)
