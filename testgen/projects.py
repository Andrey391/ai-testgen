"""Projects: each has its own tests, MCP connections, skills and generation
pipeline settings.

data/projects/<id>/project.json   settings (no secrets)
data/projects/<id>/tests/*.json   saved tests (storage.py)
data/projects/<id>/skills/*.md    project skills (skills.py)
data/projects/<id>/jobs/*.json    pipeline runs (pipeline.py)
data/projects/<id>/runs/          run history of each test (runs.py)
data/projects/<id>/suites/*.json  suite runs: all or tagged tests (suite.py)
data/projects/<id>/baselines/     visual check baselines (checks.py)
data/projects/<id>/traffic/*.har  requests recorded while authoring (traffic.py)
data/projects/<id>/explore/       site maps built by the Planner (explorer.py)
secrets/projects/<id>/            tokens of MCP connections, login for the app under test
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import vault
from .llm import EFFORTS

# TESTGEN_DATA_DIR moves the data folder, e.g. to a directory versioned with the
# application so CI runs the same tests (python -m testgen.run).
DATA = Path(os.environ.get("TESTGEN_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
ROOT = DATA / "projects"

SCENARIO_TYPES = ["positive", "negative", "edge", "boundary", "accessibility", "security"]

# The generation process. Every stage can be switched off or tuned; "skills" are
# names from skills.py, "model" / "effort" empty = global defaults.
DEFAULT_PIPELINE = {
    "requirements": {
        "connection": "",          # Atlassian connection id; empty = the first one
    },
    "explore": {                   # Planner: crawl the site instead of (or besides) requirements
        "max_pages": 20,
        "max_depth": 2,
        "login_test": "",          # saved test replayed first to log in (e.g. "Login")
        "headless": True,
    },
    "scenarios": {
        "enabled": True,
        "skills": ["test-design"],
        "types": list(SCENARIO_TYPES),
        "select": "manual",        # manual | all | high (only high-priority scenarios)
        "model": "", "effort": "",
    },
    "authoring": {
        "engine": "builtin",       # builtin | playwright-mcp
        "browser_connection": "",  # Playwright MCP connection id (engine playwright-mcp)
        "tool_connections": [],    # connections whose read-only tools the agent may use
        "skills": ["ui-test-authoring"],
        "autopilot": True,
        "headless": True,
        "max_steps": 40,
        "model": "", "effort": "",
    },
    "run": {
        "enabled": True,
        "self_heal": True,
        "heal_mode": "review",     # review: a person accepts healed locators | auto: rewritten at once
        "analyze_failures": True,
        "headless": True,
        "retry_failed": True,      # re-run a failed test once: passed the second time = flaky
        "trace": "failed",         # Playwright trace: always | failed | off
        "keep_runs": 30,           # run history per test
        "parallel": 2,             # tests at once in a suite run
        "auto_quarantine": False,  # quarantine a test whose flip rate reaches flaky_threshold
        "flaky_threshold": 30,     # percent
        "a11y_impact": "serious",  # assert_accessible fails from this impact: minor|moderate|serious|critical
        "visual_threshold": 1.0,   # assert_screenshot: percent of differing pixels allowed
        "skills": ["test-run-analysis"],
        "model": "", "effort": "",
    },
    "verify": {                    # mutation testing of a new test's assertions (mutations.py)
        "enabled": False,
        "mutants": 5,
        "improve": True,           # weak assertions: the agent adds checks, then verify again
        "headless": True,
    },
    "publish": {
        "enabled": False,
        "connection": "",          # Zephyr (or other test management) connection id
        "skills": ["zephyr-publish"],
        "report_runs": True,
        "run_skills": ["zephyr-report-run"],
        "folder": "",
        "model": "", "effort": "",
    },
}


def _valid_id(pid: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]{4,32}", pid or ""))


def path(pid: str) -> Path:
    if not _valid_id(pid):
        raise ValueError("Bad project id")
    return ROOT / pid


def secrets_kind(pid: str) -> str:
    return f"projects/{pid}"


def normalize_pipeline(p: dict | None) -> dict:
    """Stored settings merged over the defaults; unknown keys dropped, values checked."""
    out = copy.deepcopy(DEFAULT_PIPELINE)
    for stage, defaults in out.items():
        given = (p or {}).get(stage) or {}
        for key, default in defaults.items():
            if key not in given:
                continue
            v = given[key]
            if isinstance(default, bool):
                defaults[key] = bool(v)
            elif isinstance(default, int):
                try:
                    defaults[key] = max(1, min(int(v), 200))
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, float):
                try:
                    defaults[key] = max(0.0, min(float(v), 100.0))
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, list):
                defaults[key] = [str(x) for x in v if str(x).strip()] if isinstance(v, list) else default
            else:
                defaults[key] = str(v or "").strip()
    s, a, r, v = out["scenarios"], out["authoring"], out["run"], out["verify"]
    s["types"] = [t for t in s["types"] if t in SCENARIO_TYPES] or list(SCENARIO_TYPES)
    s["select"] = s["select"] if s["select"] in ("manual", "all", "high") else "manual"
    a["engine"] = a["engine"] if a["engine"] in ("builtin", "playwright-mcp") else "builtin"
    r["heal_mode"] = r["heal_mode"] if r["heal_mode"] in ("review", "auto") else "review"
    r["trace"] = r["trace"] if r["trace"] in ("always", "failed", "off") else "failed"
    r["a11y_impact"] = r["a11y_impact"] if r["a11y_impact"] in ("minor", "moderate", "serious",
                                                                  "critical") else "serious"
    r["parallel"] = min(r["parallel"], 8)
    r["flaky_threshold"] = min(r["flaky_threshold"], 100)
    v["mutants"] = min(v["mutants"], 10)
    out["explore"]["max_pages"] = min(out["explore"]["max_pages"], 100)
    out["explore"]["max_depth"] = min(out["explore"]["max_depth"], 5)
    for stage in out.values():
        if "effort" in stage and stage["effort"] not in EFFORTS:
            stage["effort"] = ""
    return out


def _read(pid: str) -> dict | None:
    f = path(pid) / "project.json"
    if not f.exists():
        return None
    p = json.loads(f.read_text("utf-8"))
    p["pipeline"] = normalize_pipeline(p.get("pipeline"))
    p.setdefault("connections", [])
    return p


def get(pid: str) -> dict | None:
    try:
        return _read(pid)
    except ValueError:
        return None


def save(p: dict) -> dict:
    p["updated"] = time.time()
    d = path(p["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(json.dumps(p, ensure_ascii=False, indent=2), "utf-8")
    return p


def create(name: str, description: str = "", base_url: str = "") -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Укажите название проекта")
    if any(p["name"].lower() == name.lower() for p in list_projects()):
        raise ValueError("Проект с таким названием уже есть")
    return save({"id": uuid.uuid4().hex[:10], "name": name, "description": description.strip(),
                 "base_url": base_url.strip(), "created": time.time(), "connections": [],
                 "pipeline": normalize_pipeline(None)})


def update(pid: str, patch: dict) -> dict:
    p = get(pid)
    if not p:
        raise KeyError(pid)
    for key in ("name", "description", "base_url"):
        if key in patch:
            p[key] = str(patch[key] or "").strip()
    if not p["name"]:
        raise ValueError("Укажите название проекта")
    if "pipeline" in patch:
        p["pipeline"] = normalize_pipeline(patch["pipeline"])
    return save(p)


def delete(pid: str) -> bool:
    d = path(pid)
    if not d.exists():
        return False
    shutil.rmtree(d)
    shutil.rmtree(vault.SECRETS / "projects" / pid, ignore_errors=True)
    return True


def list_projects() -> list[dict]:
    out = []
    for f in sorted(ROOT.glob("*/project.json")):
        p = json.loads(f.read_text("utf-8"))
        out.append({"id": p["id"], "name": p["name"], "description": p.get("description", ""),
                    "base_url": p.get("base_url", ""),
                    "tests": len(list((f.parent / "tests").glob("*.json")))})
    return sorted(out, key=lambda p: p["name"].lower())


# ---------- login for the application under test (project default) ----------

def app_credentials(pid: str) -> dict:
    return vault.load(secrets_kind(pid), "app") or {}


def set_app_credentials(pid: str, username: str, password: str = "") -> None:
    """Empty password keeps the saved one."""
    c = app_credentials(pid)
    c["username"] = username.strip()
    if password:
        c["password"] = password
    c = {k: v for k, v in c.items() if v}
    if c:
        vault.save(secrets_kind(pid), "app", c)
    else:
        vault.delete(secrets_kind(pid), "app")


# ---------- first start ----------

def ensure_default() -> None:
    """Move tests from the old layout (data/<project name>/<id>.json) into projects.
    With no projects at all the studio opens the new project wizard."""
    by_name = {p["name"].lower(): p["id"] for p in list_projects()}
    if DATA.exists():
        for old in DATA.iterdir():
            if not old.is_dir() or old.name == "projects":
                continue
            for f in old.glob("*.json"):
                t = json.loads(f.read_text("utf-8"))
                name = str(t.get("project") or old.name)
                if name.lower() not in by_name:
                    by_name[name.lower()] = create(name)["id"]
                pid = by_name[name.lower()]
                t["project_id"] = pid
                dest = path(pid) / "tests"
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f.name).write_text(json.dumps(t, ensure_ascii=False, indent=2), "utf-8")
                # Old per-test login moves with the test.
                creds = vault.load("sites", t.get("id", ""))
                if creds:
                    vault.save(secrets_kind(pid), f"test-{t['id']}", creds)
                    vault.delete("sites", t["id"])
                f.unlink()
            try:
                old.rmdir()
            except OSError:
                pass
