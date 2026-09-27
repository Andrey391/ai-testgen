"""Suite runs: the regression run of a project - all tests, those with given tags,
or a given list - in ONE browser with up to run.parallel contexts at a time.

Every test goes through pipeline.run_and_record, so a suite run has the same
self-healing, re-runs of failures, analysis and history as a single run. The
suite's verdict ignores quarantined tests: they run and their results are kept,
but they do not fail the suite (and are reported as skipped in JUnit).

Suites are stored in data/projects/<id>/suites/<suite>.json; the studio API
(POST /api/projects/{id}/runs), the CLI (python -m testgen.run) and the MCP
server start them.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from playwright.async_api import async_playwright

from . import llm, projects, runs

LIVE: dict[str, dict] = {}
BLOCKING = ("failed", "error")


def _dir(pid: str):
    return projects.path(pid) / "suites"


def new(project: dict, tests: list[dict], tags: list[str] | None = None, trigger: str = "manual",
        user: str = "") -> dict:
    s = {"id": uuid.uuid4().hex[:10], "project_id": project["id"], "project": project["name"],
         "tags": tags or [], "trigger": trigger, "user": user, "status": "running", "started": time.time(),
         "finished": None, "passed": None, "summary": {}, "usage": {},
         "items": [{"test_id": t["id"], "name": t["name"], "tags": t.get("tags") or [],
                    "quarantined": bool((t.get("quarantine") or {}).get("on")),
                    "status": "queued", "run_id": None, "error": "", "failed_step": "", "duration": None}
                   for t in tests]}
    LIVE[s["id"]] = s
    save(s)
    return s


def save(s: dict) -> None:
    d = _dir(s["project_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{s['id']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf-8")


def summarize(s: dict) -> None:
    items = s["items"]
    count = {k: sum(i["status"] == k for i in items) for k in ("passed", "flaky", "failed", "error")}
    count["quarantined_failed"] = sum(i["status"] in BLOCKING and i["quarantined"] for i in items)
    count["total"] = len(items)
    s["summary"] = count
    s["passed"] = not any(i["status"] in BLOCKING and not i["quarantined"] for i in items)


async def run(project: dict, s: dict, tests: list[dict], headless: bool | None = None,
              parallel: int | None = None, on_item=None) -> dict:
    """Run the suite created with new(); `on_item(item)` is called when a test finishes."""
    from .pipeline import run_and_record    # the pipeline imports the agent; keep this module light
    cfg = project["pipeline"]["run"]
    headless = cfg["headless"] if headless is None else headless
    gate = asyncio.Semaphore(max(1, min(parallel or cfg["parallel"], 8)))
    by_id = {t["id"]: t for t in tests}
    with llm.usage_scope() as usage:
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=headless)

            async def one(item: dict) -> None:
                async with gate:
                    test = by_id[item["test_id"]]
                    item["status"] = "running"
                    started = time.time()
                    try:
                        r = runs.new(test, "suite", s["id"], s["user"])
                        item["run_id"] = r["id"]
                        r = await run_and_record(project, test, headless, trigger="suite", suite_id=s["id"],
                                                 user=s["user"], browser=browser, run=r)
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
                await browser.close()
        except Exception as e:
            s["error"] = str(e).splitlines()[0][:300]
            for i in s["items"]:
                if i["status"] in ("queued", "running"):
                    i["status"], i["error"] = "error", s["error"]
        finally:
            await pw.stop()
    s["usage"] = usage.as_dict()
    summarize(s)
    s["status"] = "passed" if s["passed"] else "failed"
    s["finished"] = time.time()
    LIVE.pop(s["id"], None)
    save(s)
    return s


def get(sid: str) -> dict | None:
    if sid in LIVE:
        return LIVE[sid]
    if not re.fullmatch(r"[0-9a-f]{10}", sid or ""):
        return None
    for f in projects.ROOT.glob(f"*/suites/{sid}.json"):
        s = json.loads(f.read_text("utf-8"))
        if s["status"] == "running":
            s["status"], s["error"] = "error", "Студия была перезапущена во время прогона"
        return s
    return None


def list_suites(pid: str, limit: int = 30) -> list[dict]:
    d = _dir(pid)
    out = {s["id"]: s for s in LIVE.values() if s["project_id"] == pid}
    if d.exists():
        for f in sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]:
            if f.stem not in out:
                try:
                    out[f.stem] = json.loads(f.read_text("utf-8"))
                except ValueError:
                    continue
    return [{k: s.get(k) for k in ("id", "status", "started", "finished", "passed", "summary", "tags", "trigger",
                                   "user")} for s in sorted(out.values(), key=lambda s: s["started"], reverse=True)][:limit]
