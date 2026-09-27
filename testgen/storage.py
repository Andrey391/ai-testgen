"""Saved tests: one JSON file per test under data/projects/<project id>/tests/.

Besides the steps a test keeps: tags (suite filters), quarantine, heal proposals
waiting for review, the mutation testing result ("verify"), the last run summary
and the authoring cost. Runs, visual baselines and recorded traffic live in their
own folders (runs.py, checks.py, traffic.py) and are removed with the test.

Tests are written from the web server and from the browser worker (runs) at the
same time: use `update()` for partial changes, it re-reads the file under a lock.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid

from . import projects, runs, traffic, vault

_lock = threading.RLock()
TAG = re.compile(r"[\w.-]{1,40}")


def _safe(test_id: str) -> str:
    return re.sub(r"[^\w-]+", "_", test_id.strip()) or "_"


def save(test: dict) -> dict:
    test.setdefault("id", uuid.uuid4().hex[:10])
    test["updated"] = time.time()
    d = projects.path(test["project_id"]) / "tests"
    d.mkdir(parents=True, exist_ok=True)
    with _lock:
        (d / f"{_safe(test['id'])}.json").write_text(json.dumps(test, ensure_ascii=False, indent=2), "utf-8")
    return test


def load(test_id: str) -> dict | None:
    for p in projects.ROOT.glob(f"*/tests/{_safe(test_id)}.json"):
        with _lock:
            return json.loads(p.read_text("utf-8"))
    return None


def update(test_id: str, change) -> dict | None:
    """Re-read the test, apply `change(test)` and save - atomically w.r.t. other updates."""
    with _lock:
        t = load(test_id)
        if t is None:
            return None
        change(t)
        return save(t)


def delete(test_id: str) -> bool:
    for p in projects.ROOT.glob(f"*/tests/{_safe(test_id)}.json"):
        pid = p.parent.parent.name
        p.unlink()
        vault.delete(projects.secrets_kind(pid), f"test-{test_id}")
        runs.delete_test(pid, test_id)
        traffic.delete(pid, test_id)
        shutil.rmtree(projects.path(pid) / "baselines" / _safe(test_id), ignore_errors=True)
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
    d = projects.path(project_id) / "tests"
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except ValueError:
            continue
    return out


def select(project_id: str, tags: list[str] | None = None, test_ids: list[str] | None = None) -> list[dict]:
    """Tests of a suite run: the given ids, else those with any of `tags`, else all."""
    tests = all_tests(project_id)
    if test_ids:
        return [t for t in tests if t["id"] in test_ids]
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
                   | {"steps": len(t.get("steps", [])), "tags": t.get("tags") or [],
                      "proposals": len(t.get("heal_proposals") or []),
                      "recent": hist[-10:], "flip_rate": runs.flip_rate(hist),
                      "has_traffic": traffic.exists(project_id, t["id"])})
    return out


# ---------- login for the application under test ----------

def credentials(test: dict) -> dict:
    """Test's own login, else the project's, else TESTGEN_USERNAME / TESTGEN_PASSWORD
    (the same variables the exported code reads)."""
    src = own_credentials(test) or projects.app_credentials(test["project_id"])
    c = {}
    for key in ("username", "password"):
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
