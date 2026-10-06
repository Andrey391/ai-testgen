"""Saved tests: one JSON file per test under data/projects/<project id>/tests/, or a row of the
`tests` table with a shared database (repo/tests.py).

Besides the steps a test keeps: tags (suite filters), quarantine, heal proposals
waiting for review, the mutation testing result ("verify"), the last run summary,
the authoring cost, its status (draft -> review -> ready: only ready tests join the
regression suite) and comments on steps. Runs, visual baselines, recorded traffic and
versions (data/projects/<id>/history/<test>/<n>.json: kept whenever the steps, the
data blocks, the name or the URL change) live in their own folders and are removed
with the test.

Tests are written from the web server and from the browser worker (runs) at the
same time - and with a shared database from other instances and workers: use `update()`
for partial changes, it re-reads the test under a lock (fs.lock; the row held with SELECT ... FOR
UPDATE in PostgreSQL).
"""
from __future__ import annotations

import contextvars
import copy
import os
import re
import time
import uuid

from . import fs, projects, runs, tasks, traffic, vault
from .repo import tests as repo

TAG = re.compile(r"[\w.-]{1,40}")
# Who changes tests (the server sets it per request): version history and the audit log.
ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("actor", default="")
# What a version of a test is: a change here keeps the previous state in the history.
VERSIONED = ("name", "url", "scenario", "steps", "before", "after")
MAX_VERSIONS = 50


_safe = repo.safe


def _history_dir(pid: str, tid: str):
    return projects.path(pid) / "history" / _safe(tid)


def _stamp(old: dict | None, test: dict) -> dict:
    """The test about to replace `old`: a version of `old` kept if it changed, who and when."""
    test["updated"] = time.time()
    if old and any(old.get(k) != test.get(k) for k in VERSIONED):
        _keep_version(old)
    test["updated_by"] = ACTOR.get() or test.get("updated_by", "")
    return test


def save(test: dict) -> dict:
    test.setdefault("id", uuid.uuid4().hex[:10])
    repo.backend().write(test["project_id"], test["id"], lambda old: _stamp(old, test))
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


def load(test_id: str) -> dict | None:
    return repo.backend().get(test_id)


def update(test_id: str, change) -> dict | None:
    """Re-read the test, apply `change(test)` and save - atomically w.r.t. other updates."""
    pid = repo.backend().locate(test_id)
    if pid is None:
        return None

    def apply(old: dict | None) -> dict | None:
        if old is None:
            return None
        t = copy.deepcopy(old)          # `old` stays as it was: the version kept if the test changes
        change(t)
        return _stamp(old, t)
    return repo.backend().write(pid, test_id, apply)


def delete(test_id: str) -> bool:
    pid = repo.backend().locate(test_id)
    if pid is None:
        return False
    repo.backend().delete(pid, test_id)
    vault.delete(projects.secrets_kind(pid), f"test-{test_id}")
    runs.delete_test(pid, test_id)
    traffic.delete(pid, test_id)
    tasks.unlink_test(pid, test_id)
    fs.rmtree(projects.path(pid) / "baselines" / _safe(test_id))
    fs.rmtree(_history_dir(pid, test_id))
    return True


def normalize_tags(tags) -> list[str]:
    out = []
    for t in tags if isinstance(tags, list) else str(tags or "").replace(",", " ").split():
        t = str(t).strip().lower()
        if TAG.fullmatch(t) and t not in out:
            out.append(t)
    return out


def all_tests(project_id: str) -> list[dict]:
    return repo.backend().all(project_id)


def names(project_id: str) -> dict[str, str]:
    """{test id: name} of a project (without reading the steps from the database)."""
    return repo.backend().names(project_id)


def counts() -> dict[str, int]:
    """{project id: number of tests}."""
    return repo.backend().counts()


STATUSES = ("draft", "review", "ready")     # a test from the agent goes to regression after a person's review
status = repo.status        # draft (written by the agent) -> review -> ready (in the regression suite). Older: ready


def select(project_id: str, tags: list[str] | None = None, test_ids: list[str] | None = None,
           include_drafts: bool = False) -> list[dict]:
    """Tests of a suite run: the given ids, else those with any of `tags`, else all. Modules never run on
    their own; drafts and tests under review join only when asked (or named by id)."""
    return repo.backend().query(project_id, ids=list(test_ids) if test_ids else None, tags=tags or None,
                                ready=not include_drafts, modules=False)


def list_tests(project_id: str, tag: str = "") -> list[dict]:
    tests = repo.backend().query(project_id, tags=[tag]) if tag else all_tests(project_id)
    histories = runs.histories(project_id, [t["id"] for t in tests])
    out = []
    for t in tests:
        hist = histories.get(t["id"]) or []
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
