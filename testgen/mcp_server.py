"""The studio as an MCP server: IDE agents
generate, run and export tests and read failures without opening the studio.

Two ways to connect (see docs/mcp.md):
- HTTP, served by the running studio: http://127.0.0.1:8765/mcp with the header
  "Authorization: Bearer <token>". Generations started this way are ordinary
  Studio sessions: people can watch them live in the browser.
- stdio: `python -m testgen.mcp_server` with the token in TESTGEN_TOKEN. The
  process does the browser work itself, on the same data folder.

A token (Проект → Доступ из IDE, or `python -m testgen.auth token <user>`)
carries its user's rights. Nothing here deletes data or touches the commands of
MCP connections, so admin rights are never involved; the authoring agent keeps
all its rules (no irreversible actions, the password never reaches the LLM).
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from . import auth, exporters, pipeline, projects, runs, storage, suite, traffic
from .agent import StudioSession

CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_user", default="")
MAX_WAIT = 1800

INSTRUCTIONS = """AI Test Generator: end-to-end UI tests written by an AI agent in a real browser.
Typical flow: list_projects -> generate_test(project, scenario in plain language) -> the test is saved when the
agent verified the scenario -> run_test / run_suite -> list_failures -> export_test (pytest-playwright code) or
get_trace (Playwright trace of a failed run). Long operations accept wait_seconds; when it runs out they return
the current status with an id to poll (get_generation / get_run / get_suite)."""


_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Background work in this process (stdio mode); the set keeps tasks from being collected."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


@dataclass
class Backend:
    """How tools reach the browser: in the studio everything runs on its worker loop."""
    submit: Callable[[Any], Any] = _spawn
    sessions: dict = field(default_factory=dict)
    studio_url: str = ""


def _user() -> str:
    return CURRENT_USER.get() or auth.ANONYMOUS


def _project(ref: str) -> dict:
    p = projects.get(ref)
    if p:
        return p
    for item in projects.list_projects():
        if item["name"].strip().lower() == (ref or "").strip().lower():
            return projects.get(item["id"])
    raise ValueError(f"Проект не найден: {ref}. Список: list_projects")


def _test(ref: str, project: str = "") -> dict:
    t = storage.load(ref)
    if t:
        return t
    pids = [_project(project)["id"]] if project else [p["id"] for p in projects.list_projects()]
    for pid in pids:
        for t in storage.all_tests(pid):
            if t["name"].strip().lower() == (ref or "").strip().lower():
                return t
    raise ValueError(f"Тест не найден: {ref}")


async def _until(done: Callable[[], bool], wait_seconds: float) -> None:
    end = time.time() + max(0, min(wait_seconds, MAX_WAIT))
    while not done() and time.time() < end:
        await asyncio.sleep(1)


def _run_summary(run: dict, backend: Backend) -> dict:
    out = {k: run.get(k) for k in ("id", "test_id", "test_name", "status", "started", "finished", "healed",
                                   "proposals", "analysis", "error", "quarantined")}
    out["steps"] = [f"{r['status']}: {r['description']}" + (f" — {r['error']}" if r.get("error") else "")
                    for r in run.get("results") or []]
    out["events"] = [e["text"] for e in (run.get("events") or [])[:15]]
    if run.get("trace"):
        out["trace"] = f"{backend.studio_url}/api/runs/{run['id']}/files/{run['trace']}" if backend.studio_url \
            else run["trace"]
    return out


def _session_summary(s: StudioSession) -> dict:
    return {"session_id": s.id, "status": s.status, "finish_status": s.finish_status, "summary": s.summary,
            "test_id": s.test_id, "steps": [f"{st['status']}: {st['description']}" for st in s.steps],
            "last_messages": [m["text"] for m in s.chat[-5:]], "usage": s.usage.as_dict()}


def _finished(s: StudioSession) -> bool:
    return s.status in ("done", "error", "closed") or (
        not s.autopilot and s.status in ("idle", "awaiting_approval") and bool(s.messages))


def build(backend: Backend | None = None) -> FastMCP:
    backend = backend or Backend()
    mcp = FastMCP("ai-testgen", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  streamable_http_path="/mcp")

    @mcp.tool(description="Projects of the studio: id, name, application URL, number of tests.")
    async def list_projects() -> list[dict]:
        return projects.list_projects()

    @mcp.tool(description="Saved tests of a project (name or id), optionally only those with a tag: steps, "
                          "tags, last run, quarantine, mutation score.")
    async def list_tests(project: str, tag: str = "") -> list[dict]:
        return [{k: t[k] for k in ("id", "name", "url", "steps", "tags", "last_run", "quarantine", "verify")}
                for t in storage.list_tests(_project(project)["id"], tag)]

    @mcp.tool(description="Generate a UI test: the studio's agent carries out the scenario (plain language) in a "
                          "real browser in Auto-Pilot and records the steps. The test is saved when the agent "
                          "verified the scenario (finish 'passed' with an assertion). Waits up to wait_seconds, "
                          "then returns the status; poll with get_generation.")
    async def generate_test(project: str, scenario: str, name: str = "", url: str = "",
                            wait_seconds: int = 300) -> dict:
        p = _project(project)
        url = url or p.get("base_url", "")
        if not url:
            raise ValueError("Не указан URL приложения (в вызове или в настройках проекта)")
        a = p["pipeline"]["authoring"]
        s = StudioSession(p, (name or scenario)[:80], url if "://" in url else "https://" + url, scenario,
                          headless=a["headless"], credentials=projects.app_credentials(p["id"]))
        backend.sessions[s.id] = s

        async def boot():
            try:
                await s.start()
            except Exception as e:
                s.status = "error"
                s.chat.append({"role": "system", "text": str(e)})
                return
            if s.status != "error":
                s.set_autopilot(True)

        backend.submit(boot())
        return await _generation(s, wait_seconds)

    async def _generation(s: StudioSession, wait_seconds: float) -> dict:
        await _until(lambda: _finished(s), wait_seconds)
        if s.status == "done" and s.finish_status == "passed" and not s.test_id:
            test, warnings = s.save()
            out = _session_summary(s) | {"saved": True, "warnings": warnings}
        else:
            out = _session_summary(s) | {"saved": bool(s.test_id)}
        if backend.studio_url:
            out["studio"] = backend.studio_url
        return out

    @mcp.tool(description="Status of a generation started with generate_test; waits up to wait_seconds.")
    async def get_generation(session_id: str, wait_seconds: int = 0) -> dict:
        s = backend.sessions.get(session_id)
        if not s:
            raise ValueError("Сессия не найдена (студия перезапускалась?)")
        return await _generation(s, wait_seconds)

    @mcp.tool(description="Run a saved test (id or name) with self-healing and failure analysis; waits up to "
                          "wait_seconds for the result.")
    async def run_test(test: str, project: str = "", wait_seconds: int = 300) -> dict:
        t = _test(test, project)
        p = projects.get(t["project_id"])
        run = runs.new(t, "mcp", user=_user())
        backend.submit(pipeline.run_and_record(p, t, trigger="mcp", user=_user(), run=run))
        rid = run["id"]
        await _until(lambda: (runs.get(rid) or {}).get("status") != "running", wait_seconds)
        return _run_summary(runs.get(rid) or run, backend)

    @mcp.tool(description="A run by id: status, steps, analysis, browser events, trace link.")
    async def get_run(run_id: str, wait_seconds: int = 0) -> dict:
        await _until(lambda: (runs.get(run_id) or {}).get("status") != "running", wait_seconds)
        run = runs.get(run_id)
        if not run:
            raise ValueError("Прогон не найден")
        return _run_summary(run, backend)

    @mcp.tool(description="Run the project's tests (all, or those with any of `tags`) in parallel, like CI; "
                          "quarantined tests do not fail the suite. Waits up to wait_seconds.")
    async def run_suite(project: str, tags: list[str] | None = None, wait_seconds: int = 600) -> dict:
        p = _project(project)
        tags = storage.normalize_tags(tags or [])
        tests = storage.select(p["id"], tags=tags)
        if not tests:
            raise ValueError("Нет тестов для прогона")
        s = suite.new(p, tests, tags=tags, trigger="mcp", user=_user())
        backend.submit(suite.run(p, s, tests))
        return await get_suite(s["id"], wait_seconds)

    @mcp.tool(description="A suite run by id: verdict, counts and the result of every test.")
    async def get_suite(suite_id: str, wait_seconds: int = 0) -> dict:
        await _until(lambda: (suite.get(suite_id) or {}).get("status") != "running", wait_seconds)
        s = suite.get(suite_id)
        if not s:
            raise ValueError("Прогон набора не найден")
        return {k: s.get(k) for k in ("id", "status", "passed", "summary", "tags", "started", "finished", "items")}

    @mcp.tool(description="Failing tests of a project (last run failed, errored or flaky) with the failed step, "
                          "the error and the AI verdict: product bug, test issue, environment, flaky.")
    async def list_failures(project: str, include_flaky: bool = True) -> list[dict]:
        out = []
        for t in storage.all_tests(_project(project)["id"]):
            lr = t.get("last_run") or {}
            status = lr.get("status") or ("passed" if lr.get("passed") else "failed" if lr else "")
            if status in ("failed", "error") or (include_flaky and status == "flaky"):
                out.append({"test_id": t["id"], "name": t["name"], "status": status, "at": lr.get("at"),
                            "run_id": lr.get("run_id"), "failed_step": lr.get("failed_step"),
                            "error": lr.get("error"), "analysis": lr.get("analysis"),
                            "quarantined": bool((t.get("quarantine") or {}).get("on"))})
        return sorted(out, key=lambda x: x["at"] or 0, reverse=True)

    @mcp.tool(description="Export a saved test: 'playwright' (pytest-playwright code with fallback locators), "
                          "'gherkin' (.feature) or 'api' (pytest + httpx tests from the recorded requests).")
    async def export_test(test: str, format: str = "playwright", project: str = "") -> str:
        t = _test(test, project)
        p = projects.get(t["project_id"]) or {"name": "", "pipeline": projects.normalize_pipeline(None)}
        if format == "gherkin":
            return exporters.to_gherkin(t | {"project": p.get("name", "")})
        if format == "api":
            return exporters.to_api_tests(t, traffic.load(t["project_id"], t["id"]))
        return exporters.to_playwright(t, p["pipeline"]["run"]["a11y_impact"])

    @mcp.tool(description="Playwright trace of a run (recorded for failed runs by default): file path, download "
                          "link and the command to open it in Trace Viewer.")
    async def get_trace(run_id: str) -> dict:
        run = runs.get(run_id)
        if not run:
            raise ValueError("Прогон не найден")
        names = [a["trace"] for a in run.get("attempts") or [] if a.get("trace")] or \
                ([run["trace"]] if run.get("trace") else [])
        if not names:
            raise ValueError("У этого прогона нет trace (настройка «Проект → Процесс → Прогон → Trace»)")
        out = []
        for n in names:
            f = runs.file(run, n)
            if f:
                out.append({"file": str(f), "size": f.stat().st_size, "open": f'playwright show-trace "{f}"',
                            "download": f"{backend.studio_url}/api/runs/{run_id}/files/{n}" if backend.studio_url
                            else ""})
        return {"run_id": run_id, "status": run["status"], "traces": out}

    return mcp


def main() -> None:
    """stdio server for IDEs: python -m testgen.mcp_server (TESTGEN_TOKEN = the user's API token)."""
    user = auth.user_for_api_token(os.environ.get("TESTGEN_TOKEN")) if auth.ENABLED else auth.ANONYMOUS
    if not user:
        sys.exit("Set TESTGEN_TOKEN to an API token of a studio user (Проект → Доступ из IDE, "
                 "or python -m testgen.auth token <user>)")
    CURRENT_USER.set(user)
    build(Backend(studio_url=os.environ.get("TESTGEN_STUDIO_URL", "").rstrip("/"))).run("stdio")


if __name__ == "__main__":
    main()
