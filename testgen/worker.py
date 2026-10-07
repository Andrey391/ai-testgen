"""A worker: takes runs, suites, mutation checks and explorations from the queue (workqueue.py)
and does them in its browsers. Needs the shared database (TESTGEN_DATABASE_URL) and, for
screenshots and traces seen from other machines, S3 (fs.py).

    python -m testgen.worker          # TESTGEN_WORKER_CONCURRENCY items at a time (default 2)

Workers scale horizontally: start as many as the load needs, on any machines; each keeps one
browser per engine and a fresh context per test (the "one event loop for the browser" rule holds
inside the worker: everything runs on its own loop). The web server runs one too
(TESTGEN_EMBEDDED_WORKER=on, default) so that a single instance works without separate workers.

Also here: start_*() - what the web server and the studio's MCP server call to start such work:
queued with a shared database, on this process's browser loop (`submit`) otherwise.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
import traceback
import uuid

from . import explorer, fs, mutations, pipeline, projects, runs, storage, suite, workqueue
from .paths import utf8_console

HEARTBEAT = 10
POLL = 1.0


# ---------- starting work (web server, MCP server) ----------

def start_run(project: dict, test: dict, run: dict, submit, *, headless=None, trigger="manual", user="",
              browser: str = "", device: str | None = None) -> None:
    """`run` from runs.new(..., live=not workqueue.enabled())."""
    if workqueue.enabled():
        workqueue.put("run", {"project_id": project["id"], "test_id": test["id"], "run_id": run["id"],
                              "trigger": trigger, "user": user, "browser": browser, "device": device},
                      project["id"], run["id"])
    else:
        submit(pipeline.run_and_record(project, test, headless=headless, trigger=trigger, user=user, run=run,
                                       engine=browser, device=device))


def start_suite(project: dict, s: dict, tests: list[dict], submit, *, headless=None, parallel=None) -> None:
    if workqueue.enabled():
        workqueue.put("suite", {"project_id": project["id"], "suite_id": s["id"]}, project["id"], s["id"])
        suite.LIVE.pop(s["id"], None)       # workers write it; the web server reads the stored record
    else:
        submit(suite.run(project, s, tests, headless=headless, parallel=parallel))


def start_verify(project: dict, test: dict, submit) -> None:
    if workqueue.enabled():
        workqueue.put("verify", {"project_id": project["id"], "test_id": test["id"]}, project["id"], test["id"])
    else:
        submit(mutations.verify(project, test))


def start_explore(project: dict, state: dict, url: str, submit) -> None:
    """`state`: {"id", "project_id", "status": "running", "pages": [], "log": []}."""
    if workqueue.enabled():
        explorer.save(state | {"start": url, "at": time.time()})
        workqueue.put("explore", {"project_id": project["id"], "explore_id": state["id"], "url": url},
                      project["id"], state["id"])
        return
    explorer.LIVE[state["id"]] = state

    async def go():
        try:
            await explorer.explore(project, url, log=lambda text: state["log"].append(text), state=state, learn=True)
        finally:
            explorer.LIVE.pop(state["id"], None)
    submit(go())


# ---------- the worker ----------

class Worker:
    def __init__(self, concurrency: int = 2, name: str = ""):
        self.id = name or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.concurrency = max(1, concurrency)
        self.running: dict[str, asyncio.Task] = {}
        self.stopping = False
        self._pw = None
        self._browsers: dict[str, object] = {}
        self._launch = asyncio.Lock()

    async def browser(self, engine: str = "chromium"):
        """One browser per engine for all the worker's items; relaunched if it crashed."""
        engine = engine or "chromium"
        async with self._launch:
            b = self._browsers.get(engine)
            if b is None or not b.is_connected():
                if self._pw is None:
                    from playwright.async_api import async_playwright
                    self._pw = await async_playwright().start()
                    from .browser import DEVICES
                    DEVICES.update(self._pw.devices)
                headed = os.environ.get("TESTGEN_WORKER_HEADED", "off").lower() in ("on", "1", "true", "yes")
                b = self._browsers[engine] = await getattr(self._pw, engine).launch(headless=not headed)
            return b

    async def serve(self) -> None:
        workqueue.heartbeat(self.id, [], self.concurrency, socket.gethostname())
        last_beat = last_reap = 0.0
        try:
            while not self.stopping:
                now = time.time()
                if now - last_beat >= HEARTBEAT:
                    workqueue.heartbeat(self.id, list(self.running), self.concurrency, socket.gethostname())
                    last_beat = now
                if now - last_reap >= 30:
                    for item in workqueue.reap():
                        fail_item(item, "Воркер, выполнявший задание, перестал отвечать")
                    last_reap = now
                free = self.concurrency - len(self.running)
                if free > 0:
                    for item in workqueue.claim(self.id, free):
                        self.running[item["id"]] = asyncio.create_task(self._do(item))
                await asyncio.sleep(POLL)
        finally:
            for task in list(self.running.values()):
                await asyncio.wait([task], timeout=600)
            workqueue.leave(self.id)
            for b in self._browsers.values():
                try:
                    await b.close()
                except Exception:
                    pass
            if self._pw:
                await self._pw.stop()

    async def _do(self, item: dict) -> None:
        try:
            await execute(item, self)
            workqueue.finish(item["id"])
        except Exception as e:
            traceback.print_exc()
            workqueue.finish(item["id"], f"{type(e).__name__}: {e}")
            fail_item(item, str(e) or type(e).__name__)
        finally:
            self.running.pop(item["id"], None)


async def execute(item: dict, w: Worker) -> None:
    p = item["payload"]
    project = projects.get(p["project_id"])
    if not project:
        raise ValueError("Проект удалён")
    kind = item["kind"]
    if kind == "run":
        test = storage.load(p["test_id"])
        run = runs.get(p["run_id"])
        if not test or not run:
            raise ValueError("Тест или прогон удалён")
        runs.LIVE[run["id"]] = run
        if p.get("suite_id"):
            suite.item_started(p["suite_id"], p["index"])
        r = await pipeline.run_and_record(project, test, headless=True, trigger=p.get("trigger", "manual"),
                                          suite_id=p.get("suite_id", ""), user=p.get("user", ""),
                                          browser=await w.browser(p.get("browser") or "chromium"), run=run,
                                          engine=p.get("browser") or "", device=p.get("device"))
        if p.get("suite_id"):
            suite.item_done(project, p["suite_id"], p["index"], r)
    elif kind == "suite":
        s = suite.get(p["suite_id"])
        if not s:
            raise ValueError("Прогон набора удалён")

        def put(index: int, run: dict) -> None:
            it = s["items"][index]
            workqueue.put("run", {"project_id": project["id"], "test_id": it["test_id"], "run_id": run["id"],
                                  "trigger": "suite", "user": s["user"], "browser": it["browser"],
                                  "device": it["device"], "suite_id": s["id"], "index": index},
                          project["id"], run["id"])
        await suite.distribute(project, s, put, w.browser)
    elif kind == "verify":
        test = storage.load(p["test_id"])
        if not test:
            raise ValueError("Тест удалён")
        await mutations.verify(project, test)
    elif kind == "explore":
        state = explorer.get(project["id"], p["explore_id"]) or {"id": p["explore_id"], "project_id": project["id"]}
        state.setdefault("log", [])
        explorer.LIVE[state["id"]] = state
        saved = [0.0]

        def log(text: str) -> None:
            state["log"].append(text)
            if time.time() - saved[0] > 2:        # the web server of another process shows the progress
                explorer.save(state)
                saved[0] = time.time()
        try:
            await explorer.explore(project, p.get("url", ""), log=log, state=state, learn=True)
        finally:
            explorer.LIVE.pop(state["id"], None)
    else:
        raise ValueError(f"Неизвестный вид задания: {kind}")


def fail_item(item: dict, error: str) -> None:
    """An item that will not finish: its run, suite, check or exploration gets the error."""
    p = item.get("payload") or {}
    project = projects.get(p.get("project_id", ""))
    try:
        if item["kind"] == "run":
            run = runs.get(p.get("run_id", ""))
            if run and run["status"] == "running":
                run.update(status="error", passed=False, error=error)
                runs.finish(run)
            if p.get("suite_id") and project:
                suite.item_done(project, p["suite_id"], p["index"], run, error=error)
        elif item["kind"] == "suite" and project:
            s = suite.get(p.get("suite_id", ""))
            if s and s["status"] == "running":
                for i in s["items"]:
                    if i["status"] in ("queued", "running") and not i.get("run_id"):
                        i.update(status="error", error=error)
                s["error"] = error
                suite._close(project, s)
        elif item["kind"] == "verify":
            storage.update(p.get("test_id", ""), lambda t: t.update(verify={"status": "error", "error": error,
                                                                            "at": time.time()}))
        elif item["kind"] == "explore" and project:
            state = explorer.get(project["id"], p.get("explore_id", ""))
            if state and state.get("status") == "running":
                state.update(status="error", error=error)
                explorer.save(state)
    except Exception:
        traceback.print_exc()


def main() -> None:
    utf8_console()
    try:
        fs.db.check()
    except fs.db.NotConfigured as e:
        sys.exit(f"Воркер не запущен: {e}")
    if not workqueue.enabled():
        sys.exit("Очередь выключена (TESTGEN_QUEUE=off): воркеру нечего делать")
    w = Worker(int(os.environ.get("TESTGEN_WORKER_CONCURRENCY", "2")))
    port = os.environ.get("TESTGEN_METRICS_PORT", "").strip()
    if port:
        from . import monitoring
        monitoring.serve_worker(int(port))
    loop = asyncio.new_event_loop()

    def stop(*_):
        w.stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass
    print(f"Воркер {w.id}: {w.concurrency} задания одновременно; база {fs.db.engine().url.render_as_string()}")
    loop.run_until_complete(w.serve())


if __name__ == "__main__":
    main()
