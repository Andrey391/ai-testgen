"""Projects: each has its own tests, MCP connections, skills and generation
pipeline settings.

data/projects/<id>/project.json   settings (no secrets)
data/projects/<id>/tests/*.json   saved tests (storage.py)
data/projects/<id>/skills/*.md    project skills (skills.py)
data/projects/<id>/jobs/*.json    pipeline runs (pipeline.py)
data/projects/<id>/tasks/*.json   the team's tasks: status, assignee, linked tests (tasks.py)
data/projects/<id>/runs/          run history of each test (runs.py)
data/projects/<id>/suites/*.json  suite runs: all or tagged tests (suite.py)
data/projects/<id>/baselines/     visual check baselines (checks.py)
data/projects/<id>/traffic/*.har  requests recorded while authoring (traffic.py)
data/projects/<id>/explore/       site maps built by the Planner (explorer.py)
secrets/projects/<id>/            tokens of MCP connections, login for the app under test,
                                  API key of the model connection (llm.json)

With a shared database the same paths are keys of its rows (fs.py).
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from pathlib import Path

from . import fs, vault
from .llm import EFFORTS
from .paths import DATA

ROOT = DATA / "projects"

SCENARIO_TYPES = ["positive", "negative", "edge", "boundary", "accessibility", "security"]

# The model connection (llm.py): no model is built in, the project chooses one.
# "effort" empty = the model's own default; "prices": model -> [$ input, $ output]
# per million tokens, for cost estimates; "models" / "check": the last connection check.
DEFAULT_LLM = {"model": "", "effort": "", "base_url": "", "prices": {}, "models": [], "check": None}

# The generation process. Every stage can be switched off or tuned; "skills" are
# names from skills.py, "model" / "effort" empty = the project's model settings.
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
        "prompt": "full",          # full | compact (weaker models, with examples)
        "screenshots": "always",   # always | on_request (the agent calls `look`) | never
        "device": "",              # record in a device profile (e.g. "iPhone 13"); empty = desktop
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
        "login_once": True,        # with a login test: other tests start logged in (saved state)
        "browsers": ["chromium"],  # chromium | firefox | webkit: suites run every test in each
        "devices": [],             # Playwright device profiles ("iPhone 13", "Pixel 7"); empty = desktop
        "locale": "",              # browser locale, e.g. ru-RU (empty = en-US)
        "timezone": "",            # e.g. Europe/Moscow (empty = the machine's)
        "manual_minutes": 5,       # time to run one test case by hand: saved hours on the dashboard
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
    "budget": {                    # spending limits of language models, 0 = none (llm.py)
        "session": 0.0,            # one Studio session
        "job": 0.0,                # one pipeline run
        "month": 0.0,              # the project per calendar month
        "currency": "USD",         # USD | RUB
    },
}
# Ranges of numbers that are not the usual 1..200 / 0..100.
RANGES = {("budget", "session"): (0, 1e7), ("budget", "job"): (0, 1e7), ("budget", "month"): (0, 1e8)}


def _valid_id(pid: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]{4,32}", pid or ""))


def path(pid: str) -> Path:
    if not _valid_id(pid):
        raise ValueError("Bad project id")
    return ROOT / pid


def secrets_kind(pid: str) -> str:
    return f"projects/{pid}"


LANGUAGES = {"ru": "Russian", "en": "English"}


def language_rule(project: dict | None) -> str:
    """A prompt line fixing the language of generated texts, "" when the project leaves it to the input."""
    lang = LANGUAGES.get((project or {}).get("language") or "")
    return f"\n- Write every description, title and text you produce in {lang}." if lang else ""


def normalize_pipeline(p: dict | None) -> dict:
    """Stored settings merged over the defaults; unknown keys dropped, values checked."""
    out = copy.deepcopy(DEFAULT_PIPELINE)
    for stage, defaults in out.items():
        given = (p or {}).get(stage) or {}
        for key, default in defaults.items():
            if key not in given:
                continue
            v = given[key]
            low, high = RANGES.get((stage, key), (1, 200) if isinstance(default, int) else (0.0, 100.0))
            if isinstance(default, bool):
                defaults[key] = bool(v)
            elif isinstance(default, int):
                try:
                    defaults[key] = int(max(low, min(int(v), high)))
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, float):
                try:
                    defaults[key] = float(max(low, min(float(v), high)))
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
    a["prompt"] = a["prompt"] if a["prompt"] in ("full", "compact") else "full"
    a["screenshots"] = a["screenshots"] if a["screenshots"] in ("always", "on_request", "never") else "always"
    out["budget"]["currency"] = out["budget"]["currency"] if out["budget"]["currency"] in ("USD", "RUB") else "USD"
    r["heal_mode"] = r["heal_mode"] if r["heal_mode"] in ("review", "auto") else "review"
    r["trace"] = r["trace"] if r["trace"] in ("always", "failed", "off") else "failed"
    r["a11y_impact"] = r["a11y_impact"] if r["a11y_impact"] in ("minor", "moderate", "serious",
                                                                  "critical") else "serious"
    r["parallel"] = min(r["parallel"], 8)
    r["browsers"] = [b for b in dict.fromkeys(r["browsers"]) if b in ("chromium", "firefox", "webkit")] or ["chromium"]
    r["devices"] = list(dict.fromkeys(d.strip() for d in r["devices"]))[:5]
    r["locale"] = r["locale"] if re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2})?", r["locale"]) else ""
    r["timezone"] = r["timezone"] if re.fullmatch(r"[A-Za-z_]+(/[A-Za-z_+-]+)*", r["timezone"]) else ""
    r["flaky_threshold"] = min(r["flaky_threshold"], 100)
    v["mutants"] = min(v["mutants"], 10)
    out["explore"]["max_pages"] = min(out["explore"]["max_pages"], 100)
    out["explore"]["max_depth"] = min(out["explore"]["max_depth"], 5)
    for stage in out.values():
        if "effort" in stage and stage["effort"] not in EFFORTS:
            stage["effort"] = ""
    return out


def normalize_llm(d: dict | None) -> dict:
    out = copy.deepcopy(DEFAULT_LLM)
    d = d or {}
    out["model"] = str(d.get("model") or "").strip()
    out["effort"] = d.get("effort") if d.get("effort") in EFFORTS else ""
    url = str(d.get("base_url") or "").strip().rstrip("/")
    out["base_url"] = url if re.match(r"https?://", url) else ""
    for model, price in (d.get("prices") or {}).items():
        try:
            inp, outp = (max(0.0, float(x)) for x in price)
        except (TypeError, ValueError):
            continue
        if str(model).strip() and (inp or outp):
            out["prices"][str(model).strip()] = [inp, outp]
    if isinstance(d.get("models"), list):
        out["models"] = [m for m in d["models"] if isinstance(m, dict) and m.get("id")]
    if isinstance(d.get("check"), dict):
        out["check"] = d["check"]
    return out


def _read(pid: str) -> dict | None:
    p = fs.read_json(path(pid) / "project.json")
    if p is None:
        return None
    p["pipeline"] = normalize_pipeline(p.get("pipeline"))
    p["llm"] = normalize_llm(p.get("llm"))
    p.setdefault("connections", [])
    return p


def get(pid: str) -> dict | None:
    try:
        return _read(pid)
    except ValueError:
        return None


def save(p: dict) -> dict:
    p["updated"] = time.time()
    fs.write_json(path(p["id"]) / "project.json", p)
    return p


def create(name: str, description: str = "", base_url: str = "", owner: str = "") -> dict:
    """With an owner (a signed-in user) the project is visible to its members only;
    without one (CLI, TESTGEN_AUTH=off, the old layout) it is open to every user."""
    name = name.strip()
    if not name:
        raise ValueError("Укажите название проекта")
    if any(p["name"].lower() == name.lower() for p in list_projects()):
        raise ValueError("Проект с таким названием уже есть")
    # New projects: a Russian-speaking browser in Moscow time (most sites under test are Russian);
    # older projects keep en-US, as they were recorded.
    pipeline = normalize_pipeline({"run": {"locale": "ru-RU", "timezone": "Europe/Moscow"}})
    return save({"id": uuid.uuid4().hex[:10], "name": name, "description": description.strip(),
                 "base_url": base_url.strip(), "created": time.time(), "connections": [],
                 "pipeline": pipeline, "llm": normalize_llm(None), "visibility": "members" if owner else "open",
                 "members": {owner: "owner"} if owner else {}})


def set_access(pid: str, visibility: str, members: dict) -> dict:
    """Members {user: role} (validated by access.normalize_members) and visibility: members | open."""
    p = get(pid)
    if not p:
        raise KeyError(pid)
    p["visibility"] = visibility if visibility in ("members", "open") else "members"
    p["members"] = members
    return save(p)


def update(pid: str, patch: dict) -> dict:
    p = get(pid)
    if not p:
        raise KeyError(pid)
    for key in ("name", "description", "base_url"):
        if key in patch:
            p[key] = str(patch[key] or "").strip()
    if "language" in patch:
        # Language of step descriptions, scenarios and Gherkin: "" = as the scenario / requirements are written.
        p["language"] = patch["language"] if patch["language"] in ("", "ru", "en") else ""
    if not p["name"]:
        raise ValueError("Укажите название проекта")
    if "pipeline" in patch:
        p["pipeline"] = normalize_pipeline(patch["pipeline"])
    return save(p)


def delete(pid: str) -> bool:
    d = path(pid)
    if not fs.exists(d / "project.json"):
        return False
    fs.rmtree(d)
    vault.delete_all(secrets_kind(pid))
    return True


def list_projects() -> list[dict]:
    out = []
    for f in sorted(fs.glob(ROOT, "*/project.json")):
        p = fs.read_json(f)
        out.append({"id": p["id"], "name": p["name"], "description": p.get("description", ""),
                    "base_url": p.get("base_url", ""),
                    "tests": len(fs.glob(f.parent / "tests", "*.json"))})
    return sorted(out, key=lambda p: p["name"].lower())


# ---------- login for the application under test (project default) ----------

def app_credentials(pid: str) -> dict:
    return vault.load(secrets_kind(pid), "app") or {}


def set_app_credentials(pid: str, username: str, password: str = "", totp_secret: str | None = None) -> None:
    """Empty password keeps the saved one; totp_secret: None keeps, "" removes the 2FA secret."""
    c = app_credentials(pid)
    c["username"] = username.strip()
    if password:
        c["password"] = password
    if totp_secret is not None:
        c["totp_secret"] = re.sub(r"\s+", "", totp_secret).upper()
    c = {k: v for k, v in c.items() if v}
    if c:
        vault.save(secrets_kind(pid), "app", c)
    else:
        vault.delete(secrets_kind(pid), "app")


# ---------- model connection ----------

def llm_key(pid: str) -> str:
    return (vault.load(secrets_kind(pid), "llm") or {}).get("api_key", "")


def llm_settings(pid: str) -> dict:
    """Settings for llm.Model: project.json "llm" plus the API key (empty = ANTHROPIC_API_KEY)."""
    p = get(pid) if pid else None
    if not p:
        return normalize_llm(None) | {"api_key": ""}
    return p["llm"] | {"api_key": llm_key(pid)}


def update_llm(pid: str, patch: dict, api_key: str = "") -> dict:
    """`patch`: model / effort / base_url / prices; `api_key` empty keeps the saved one.
    A new key or address makes the last check stale."""
    p = get(pid)
    if not p:
        raise KeyError(pid)
    cur = p["llm"]
    new = normalize_llm(cur | {k: v for k, v in patch.items() if k in ("model", "effort", "base_url", "prices")})
    if api_key:
        vault.save(secrets_kind(pid), "llm", {"api_key": api_key.strip()})
    if api_key or new["base_url"] != cur["base_url"]:
        new["check"], new["models"] = None, []
    p["llm"] = new
    return save(p)


def clear_llm_key(pid: str) -> None:
    vault.delete(secrets_kind(pid), "llm")
    p = get(pid)
    if p:
        p["llm"]["check"], p["llm"]["models"] = None, []
        save(p)


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
                fs.write_json(path(pid) / "tests" / f.name, t)
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
