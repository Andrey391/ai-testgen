"""Saved tests: one JSON file per test under data/projects/<project id>/tests/."""
from __future__ import annotations

import json
import os
import re
import time
import uuid

from . import projects, vault


def _safe(test_id: str) -> str:
    return re.sub(r"[^\w-]+", "_", test_id.strip()) or "_"


def save(test: dict) -> dict:
    test.setdefault("id", uuid.uuid4().hex[:10])
    test["updated"] = time.time()
    d = projects.path(test["project_id"]) / "tests"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{_safe(test['id'])}.json").write_text(json.dumps(test, ensure_ascii=False, indent=2), "utf-8")
    return test


def load(test_id: str) -> dict | None:
    for p in projects.ROOT.glob(f"*/tests/{_safe(test_id)}.json"):
        return json.loads(p.read_text("utf-8"))
    return None


def delete(test_id: str) -> bool:
    for p in projects.ROOT.glob(f"*/tests/{_safe(test_id)}.json"):
        pid = p.parent.parent.name
        p.unlink()
        vault.delete(projects.secrets_kind(pid), f"test-{test_id}")
        return True
    return False


def list_tests(project_id: str) -> list[dict]:
    out = []
    files = (projects.path(project_id) / "tests").glob("*.json")
    for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        t = json.loads(p.read_text("utf-8"))
        out.append({k: t.get(k) for k in ("id", "project_id", "name", "url", "scenario", "updated",
                                          "last_run", "external", "engine")}
                   | {"steps": len(t.get("steps", []))})
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
