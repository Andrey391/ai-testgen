"""Suite runs: the regression run of a project - all tests, those with given tags,
or a given list - in ONE browser with up to run.parallel contexts at a time.

Every test goes through pipeline.run_and_record, so a suite run has the same
self-healing, re-runs of failures, analysis and history as a single run. The
suite's verdict ignores quarantined tests: they run and their results are kept,
but they do not fail the suite (and are reported as skipped in JUnit).

Suites are stored in data/projects/<id>/suites/<suite>.json; the studio API
(POST /api/projects/{id}/runs), the CLI (python -m testgen.run) and the MCP
server start them.

With a shared database (workqueue.py) a suite started from the studio is spread over the
workers: one worker logs in once (distribute), then every test is its own queue item that
any worker takes; item_done() records each result and the last one closes the suite.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from playwright.async_api import async_playwright

from . import fs, llm, notify, projects, runs, storage

LIVE: dict[str, dict] = {}
BLOCKING = ("failed", "error")


def _dir(pid: str):
    return projects.path(pid) / "suites"


def new(project: dict, tests: list[dict], tags: list[str] | None = None, trigger: str = "manual",
        user: str = "", browsers: list[str] | None = None, devices: list[str] | None = None) -> dict:
    """A suite run record: one item per test and (browser, device) of the project's matrix
    (run.browsers x run.devices; `browsers` narrows it, e.g. the CLI's --browser; `devices` replaces
    the project's devices for this run, e.g. a screen size "1366x768")."""
    from .pipeline import matrix
    combos = [c for c in matrix(project) if not browsers or c[0] in browsers] or \
        [(b, "") for b in browsers or ["chromium"]]
    if devices:
        combos = list(dict.fromkeys((b, "" if d == "desktop" else d) for b, _ in combos for d in devices))
    multi = len(combos) > 1
    s = {"id": uuid.uuid4().hex[:10], "project_id": project["id"], "project": project["name"],
         "tags": tags or [], "trigger": trigger, "user": user, "status": "running", "started": time.time(),
         "finished": None, "passed": None, "summary": {}, "usage": {},
         "matrix": [{"browser": b, "device": d} for b, d in combos],
         "items": [{"test_id": t["id"], "name": t["name"] + (f" [{', '.join(filter(None, (b, d)))}]" if multi else ""),
                    "test_name": t["name"], "browser": b, "device": d, "tags": t.get("tags") or [],
                    "quarantined": bool((t.get("quarantine") or {}).get("on")),
                    "status": "queued", "run_id": None, "error": "", "failed_step": "", "duration": None}
                   # an API test has no browser: it runs once, not per browser and device
                   for t in tests for b, d in (combos[:1] if t.get("layer") == "api" else combos)]}
    LIVE[s["id"]] = s
    save(s)
    return s


def _file(s: dict):
    return _dir(s["project_id"]) / f"{s['id']}.json"


def save(s: dict) -> None:
    s["saved"] = time.time()
    fs.write_json(_file(s), s, indent=1)


def summarize(s: dict) -> None:
    items = s["items"]
    count = {k: sum(i["status"] == k for i in items) for k in ("passed", "flaky", "failed", "error")}
    count["quarantined_failed"] = sum(i["status"] in BLOCKING and i["quarantined"] for i in items)
    count["total"] = len(items)
    s["summary"] = count
    s["passed"] = not any(i["status"] in BLOCKING and not i["quarantined"] for i in items)


async def run(project: dict, s: dict, tests: list[dict], headless: bool | None = None,
              parallel: int | None = None, on_item=None) -> dict:
    """Run the suite created with new(); `on_item(item)` is called when a test finishes.
    One browser per engine; with a login test the suite logs in once (per login) first."""
    from .pipeline import ensure_login_state, run_and_record, uses_login_state  # the pipeline imports the agent
    from .browser import DEVICES
    cfg = project["pipeline"]["run"]
    headless = cfg["headless"] if headless is None else headless
    gate = asyncio.Semaphore(max(1, min(parallel or cfg["parallel"], 8)))
    by_id = {t["id"]: t for t in tests}
    with llm.usage_scope() as usage:
        pw = await async_playwright().start()
        DEVICES.update(pw.devices)
        browsers: dict[str, object] = {}
        launching = asyncio.Lock()
        try:
            async def browser_for(engine: str):
                async with launching:
                    if engine not in browsers:
                        browsers[engine] = await getattr(pw, engine).launch(headless=headless)
                return browsers[engine]

            states: dict[str, dict | None] = {}
            first = s["items"][0] if s["items"] else None
            for t in tests:
                if uses_login_state(project, t):
                    creds = storage.credentials(t)
                    key = creds.get("username") or ""
                    if key not in states:
                        states[key] = await ensure_login_state(project, creds, browser=await browser_for(
                            first["browser"]), headless=headless, engine=first["browser"], fresh=True)

            async def one(item: dict) -> None:
                async with gate:
                    test = by_id[item["test_id"]]
                    item["status"] = "running"
                    started = time.time()
                    try:
                        r = runs.new(test, "suite", s["id"], s["user"])
                        item["run_id"] = r["id"]
                        state = states.get(storage.credentials(test).get("username") or "")
                        r = await run_and_record(project, test, headless, trigger="suite", suite_id=s["id"],
                                                 user=s["user"], browser=await browser_for(item["browser"]), run=r,
                                                 engine=item["browser"], device=item["device"], login_state=state)
                        failed = next((x for x in r["results"] if x["status"] == "failed"), None)
                        item.update(status=r["status"], error=(failed or {}).get("error") or r.get("error", ""),
                                    failed_step=(failed or {}).get("description", ""))
                    except Exception as e:
                        item.update(status="error", error=str(e).splitlines()[0][:300] if str(e) else type(e).__name__)
                    item["duration"] = round(time.time() - started, 1)
                    summarize(s)
                    save(s)
                    if on_item:
                        on_item(item)

            try:
                await asyncio.gather(*(one(i) for i in s["items"]))
            finally:
                for b in browsers.values():
                    await b.close()
        except Exception as e:
            s["error"] = str(e).splitlines()[0][:300]
            for i in s["items"]:
                if i["status"] in ("queued", "running"):
                    i["status"], i["error"] = "error", s["error"]
        finally:
            await pw.stop()
    s["usage"] = usage.as_dict()
    _close(project, s)
    return s


def _close(project: dict, s: dict) -> None:
    summarize(s)
    s["status"] = "passed" if s["passed"] else "failed"
    s["finished"] = time.time()
    LIVE.pop(s["id"], None)
    save(s)
    notify.event(project, "suite", suite=s)


# ---------- a suite spread over the workers (shared database) ----------

async def distribute(project: dict, s: dict, put, browser_for) -> None:
    """In a worker: log in once per login of the suite (the saved state is shared through the vault),
    then queue every test. `put(item_index, run)` queues one; `browser_for(engine)` is the worker's browser."""
    from .pipeline import ensure_login_state, uses_login_state
    done = set()
    for item in s["items"]:
        t = storage.load(item["test_id"])
        if t and uses_login_state(project, t):
            creds = storage.credentials(t)
            if (creds.get("username") or "") not in done:
                done.add(creds.get("username") or "")
                await ensure_login_state(project, creds, browser=await browser_for(item["browser"]), headless=True,
                                         engine=item["browser"], fresh=True)
    with fs.lock(_file(s)):
        s = get(s["id"]) or s
        for i, item in enumerate(s["items"]):
            t = storage.load(item["test_id"])
            if not t:
                item.update(status="error", error="Тест удалён")
                continue
            r = runs.new(t, "suite", s["id"], s["user"], live=False)
            item["run_id"] = r["id"]
            put(i, r)
        save(s)
    if all(i["status"] not in ("queued", "running") for i in s["items"]):
        _close(project, s)


def item_started(sid: str, index: int) -> None:
    s = get(sid)
    if not s:
        return
    with fs.lock(_file(s)):
        s = get(sid)
        if s and s["items"][index]["status"] == "queued":
            s["items"][index].update(status="running", started=time.time())
            save(s)


def item_done(project: dict, sid: str, index: int, run: dict | None, error: str = "") -> None:
    """A test of a spread suite finished (or its worker died): record it; the last one closes the suite."""
    s = get(sid)
    if not s:
        return
    with fs.lock(_file(s)):
        s = get(sid)
        if not s or s["status"] != "running":
            return
        item = s["items"][index]
        failed = next((x for x in (run or {}).get("results") or [] if x["status"] == "failed"), None)
        item.update(status=(run or {}).get("status") or "error",
                    error=error or (failed or {}).get("error") or (run or {}).get("error", ""),
                    failed_step=(failed or {}).get("description", ""),
                    duration=round(time.time() - (item.get("started") or s["started"]), 1))
        for k, v in ((run or {}).get("usage") or {}).items():
            if isinstance(v, (int, float)):
                s["usage"][k] = round(s["usage"].get(k, 0) + v, 6)
        summarize(s)
        if all(i["status"] not in ("queued", "running") for i in s["items"]):
            _close(project, s)
        else:
            save(s)


def get(sid: str) -> dict | None:
    if sid in LIVE:
        return LIVE[sid]
    if not re.fullmatch(r"[0-9a-f]{10}", sid or ""):
        return None
    for f in fs.glob(projects.ROOT, f"*/suites/{sid}.json"):
        s = fs.read_json(f)
        if s["status"] == "running" and _abandoned(s):
            s["status"], s["error"] = "error", "Студия была перезапущена во время прогона"
        return s
    return None


def _abandoned(s: dict) -> bool:
    from . import workqueue
    return not workqueue.active(s["id"]) and not any(workqueue.active(i["run_id"]) for i in s["items"]
                                                     if i.get("run_id") and i["status"] in ("queued", "running"))


def list_suites(pid: str, limit: int = 30) -> list[dict]:
    out = {s["id"]: s for s in LIVE.values() if s["project_id"] == pid}
    for f, text, _ in fs.documents(_dir(pid))[:limit]:
        if f.stem not in out:
            try:
                out[f.stem] = json.loads(text)
            except ValueError:
                continue
    return [{k: s.get(k) for k in ("id", "status", "started", "finished", "passed", "summary", "tags", "trigger",
                                   "user")} for s in sorted(out.values(), key=lambda s: s["started"], reverse=True)][:limit]
