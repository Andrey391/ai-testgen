"""The full, configurable test generation process of a project:

    requirements (Jira / Confluence via MCP, .md, text, or the Planner's site map)
      -> scenarios (test design by Claude + skills; manual or automatic selection)
      -> authoring (agent in a real browser: built-in Playwright or Playwright MCP)
      -> run (replay with self-healing, re-run of failures, failure analysis)
      -> verify (mutation testing of the assertions; weak ones are strengthened by the agent)
      -> publish (test case and run result to Zephyr via MCP)

Each stage is switched on/off and tuned in the project's pipeline settings
(projects.DEFAULT_PIPELINE). A job processes scenarios one by one, so the first
tests are ready long before the last ones; every authoring session is also a
regular Studio session, so a person can watch it live or take over.

Jobs run on the browser worker loop. Their state is written to
data/projects/<id>/jobs/<job id>.json.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from urllib.parse import urlparse

from . import explorer, llm, mutations, projects, publisher, runner, runs, scenarios, sources, storage
from .agent import StudioSession, has_assertion

JOBS: dict[str, "Job"] = {}
AUTHORING_TIMEOUT = 2 * 3600   # guarded mode waits for a human this long at most


def _jobs_dir(pid: str):
    return projects.path(pid) / "jobs"


async def run_and_record(project: dict, test: dict, headless: bool | None = None, on_progress=None,
                         log=None, *, trigger: str = "manual", suite_id: str = "", user: str = "",
                         browser=None, run: dict | None = None) -> dict:
    """Run a saved test with the project's "run" settings and record the result.

    One attempt, and a second one if it failed (run.retry_failed): failed then passed
    is "flaky". A failure is analysed by Claude with the test's history. The run goes
    to the history (runs.py); the test gets its last run summary, heal proposals
    (review mode) or healed locators (auto mode), and quarantine if auto-quarantine
    is on and the test flips too often. Reported to Zephyr if publishing is set up.

    Returns the run record; `run` may be one created beforehand with runs.new().
    """
    cfg = project["pipeline"]["run"]
    headless = cfg["headless"] if headless is None else headless
    run = run or runs.new(test, trigger, suite_id, user)
    creds = storage.credentials(test)
    files = runs.files_dir(run)
    attempts: list[dict] = []

    async def progress(report):
        run["results"], run["attempt"] = report["results"], report["attempt"]
        if on_progress:
            await on_progress(run)

    with llm.usage_scope() as usage:
        try:
            attempts.append(await runner.run_test(test, headless, progress, creds, cfg, browser=browser,
                                                  run_dir=files, attempt=1))
            if not attempts[0]["passed"] and cfg["retry_failed"]:
                if log:
                    log(f"«{test['name']}»: повторный запуск после падения")
                attempts.append(await runner.run_test(test, headless, progress, creds, cfg, browser=browser,
                                                      run_dir=files, attempt=2))
            first, final = attempts[0], attempts[-1]
            run.update(attempts=[{k: v for k, v in a.items() if k != "proposals"} for a in attempts],
                       results=final["results"], events=final["events"], passed=final["passed"],
                       flaky=not first["passed"] and final["passed"],
                       healed=sum(a["healed"] for a in attempts),
                       trace=next((a["trace"] for a in attempts if a["trace"] and not a["passed"]),
                                  final["trace"]))
            run["status"] = "flaky" if run["flaky"] else "passed" if final["passed"] else "failed"
            if not first["passed"] and cfg["analyze_failures"]:
                try:
                    run["analysis"] = await runner.analyze(
                        test, first, cfg, project["id"], runs.history(project["id"], test["id"]),
                        retry=attempts[1] if len(attempts) > 1 else None)
                except Exception as e:
                    run["analysis"] = {"verdict": "unknown", "summary": llm.api_error_text(e), "suggestion": ""}
        except Exception as e:
            run.update(status="error", passed=False, error=_err(e))
    run["usage"] = usage.as_dict()

    proposals = {}
    for a in attempts:
        for p in a.get("proposals", []):
            proposals[p["step_id"]] = p | {"run_id": run["id"]}
    run["proposals"] = len(proposals)
    runs.finish(run, keep=cfg["keep_runs"])
    _record(project, test, run, attempts, proposals)

    pub = project["pipeline"]["publish"]
    if pub["enabled"] and pub["report_runs"] and (test.get("external") or {}).get("zephyr") \
            and run["status"] != "error":
        try:
            run["zephyr"] = await publisher.report_run(project, test, run, log)
        except Exception as e:
            run["zephyr"] = {"status": "failed", "summary": _err(e)}
        runs.save(run)
    return run


def _record(project: dict, test: dict, run: dict, attempts: list[dict], proposals: dict) -> None:
    """Write the run's outcome into the saved test (re-read under a lock: the test may
    have been edited meanwhile) and into the caller's copy."""
    cfg = project["pipeline"]["run"]
    healed_ids = {r["id"] for a in attempts for r in a["results"] if r.get("healed")}
    healed = {s["id"]: s["locator"] for s in test["steps"] if s["id"] in healed_ids}
    failed = next((r for r in run["results"] if r["status"] == "failed"), None)
    paths = sorted({urlparse(r["url"]).path for a in attempts for r in a["results"] if r.get("url")})
    rate = runs.flip_rate(runs.history(project["id"], test["id"]))

    def change(t: dict) -> None:
        if cfg["heal_mode"] == "auto":
            for s in t["steps"]:
                if s["id"] in healed:
                    s["locator"], s["healed"] = healed[s["id"]], True
        elif proposals:
            keep = [p for p in t.get("heal_proposals") or [] if p["step_id"] not in proposals]
            t["heal_proposals"] = keep + list(proposals.values())
        t["last_run"] = {"at": time.time(), "run_id": run["id"], "status": run["status"],
                         "passed": bool(run["passed"]), "flaky": run["flaky"], "healed": run["healed"],
                         "failed_step": failed["description"] if failed else "",
                         "error": (failed["error"] if failed else "") or run.get("error", ""),
                         "analysis": run.get("analysis"), "paths": paths}
        q = t.get("quarantine") or {}
        if cfg["auto_quarantine"] and not q.get("on") and rate is not None and rate * 100 >= cfg["flaky_threshold"]:
            t["quarantine"] = {"on": True, "by": "auto", "at": time.time(),
                               "reason": f"Автоматически: результат меняется в {rate:.0%} запусков"}

    fresh = storage.update(test["id"], change)
    if fresh:
        test.clear()
        test.update(fresh)


def _err(e: Exception) -> str:
    if isinstance(e, (publisher.PublishError, sources.SourceError, ValueError)) or type(e).__name__ == "McpError":
        return str(e)
    return llm.api_error_text(e)


class Job:
    def __init__(self, project: dict, links: list[str], text: str, url: str, sessions: dict,
                 user: str = "", explore: bool = False):
        self.id = uuid.uuid4().hex[:10]
        self.project_id = project["id"]
        self.links = [l.strip() for l in links if l.strip()]
        self.text, self.url, self.user = text.strip(), url.strip() or project.get("base_url", ""), user
        self.explore = explore
        self.sessions = sessions
        self.status = "running"     # running | awaiting_selection | done | error | cancelled
        self.stage = "requirements"
        self.created = time.time()
        self.finished = None
        self.log: list[dict] = []
        self.requirements: list[dict] = []
        self.feature, self.assumptions = "", []
        self.scenarios: list[dict] = []
        self.items: list[dict] = []
        self.error = ""
        self.usage: dict = {}
        self._selected = asyncio.Event()
        self._cancel = False
        self._session: StudioSession | None = None

    # ---------- control (call on the worker loop) ----------

    def select(self, indices: list[int]) -> None:
        if self.status != "awaiting_selection":
            return
        self.items = [self._item(self.scenarios[i]) for i in sorted(set(indices))
                      if 0 <= i < len(self.scenarios)]
        self._selected.set()

    def cancel(self) -> None:
        self._cancel = True
        self._selected.set()
        if self._session:
            self._session.autopilot = False

    # ---------- state ----------

    def state(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "project_id", "links", "url", "user", "explore", "status", "stage", "created", "finished",
            "log", "requirements", "feature", "assumptions", "scenarios", "items", "error", "usage")}

    def save(self) -> None:
        d = _jobs_dir(self.project_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.id}.json").write_text(json.dumps(self.state(), ensure_ascii=False, indent=1), "utf-8")

    def _log(self, text: str, level: str = "info") -> None:
        self.log.append({"at": time.time(), "level": level, "text": text})
        self.save()

    @staticmethod
    def _item(sc: dict) -> dict:
        return {"title": sc["title"], "priority": sc.get("priority", ""), "type": sc.get("type", ""),
                "status": "queued", "session_id": None, "test_id": None, "summary": "",
                "run": None, "verify": None, "publish": None, "error": ""}

    # ---------- the process ----------

    async def run(self) -> None:
        with llm.usage_scope() as usage:
            try:
                await self._run()
                self.status = "cancelled" if self._cancel else "done"
            except Exception as e:
                self.status, self.error = "error", _err(e)
                self._log(self.error, "error")
            finally:
                self.finished = time.time()
                self.stage = ""
                self.usage = usage.as_dict()
                self._log({"done": "Конвейер завершён", "cancelled": "Конвейер остановлен"}.get(
                    self.status, "Конвейер завершился с ошибкой"), "info" if self.status != "error" else "error")

    async def _run(self) -> None:
        project = projects.get(self.project_id)
        cfg = project["pipeline"]

        # 1. Requirements
        parts = []
        for link in self.links:
            self._log(f"Загрузка требований: {link}")
            r = await sources.fetch(link, project)
            self.requirements.append({"source": link, "title": r["title"], "chars": len(r["text"])})
            parts.append(r["text"] if r["text"].startswith("#") else f"# {r['title']}\n\n{r['text']}")
        if self.text:
            self.requirements.append({"source": "текст", "title": self.text.split("\n")[0][:80],
                                      "chars": len(self.text)})
            parts.append(self.text)
        if self.explore:
            self._log(f"Planner: исследование сайта {self.url}")
            m = await explorer.explore(project, self.url, log=self._log)
            if m["status"] != "done":
                raise ValueError(f"Исследование сайта не удалось: {m['error']}")
            text = explorer.to_requirements(m)
            self.requirements.append({"source": "Planner", "title": f"Карта сайта: страниц {len(m['pages'])}",
                                      "chars": len(text)})
            parts.append(text)
        if not parts:
            raise ValueError("Нет требований: укажите ссылки, текст или включите исследование сайта")
        requirements = "\n\n---\n\n".join(parts)

        # 2. Scenarios
        self.stage = "scenarios"
        if cfg["scenarios"]["enabled"]:
            self._log("Проектирование сценариев…")
            res = await scenarios.generate(requirements, self.url, project=project, log=self._log)
            self.feature, self.assumptions = res.feature, res.assumptions
            self.scenarios = [s.model_dump() for s in res.scenarios]
            self._log(f"Сценариев: {len(self.scenarios)}")
            mode = cfg["scenarios"]["select"]
            if not self.scenarios:
                self._log("По этим требованиям сценариев не получилось", "warn")
                return
            if mode == "manual":
                self.status = "awaiting_selection"
                self._log("Выберите сценарии для генерации тестов")
                await self._selected.wait()
                self.status = "running"
            else:
                chosen = [s for s in self.scenarios if mode == "all" or s["priority"] == "high"]
                self.items = [self._item(s) for s in chosen]
        else:
            self.scenarios = [{"title": self.requirements[0]["title"] or "Сценарий", "type": "", "priority": "",
                               "preconditions": "", "instructions": requirements, "expected_result": "",
                               "gherkin": ""}]
            self.items = [self._item(self.scenarios[0])]
        if self._cancel:
            return
        self._log(f"В работу взято сценариев: {len(self.items)}")

        # 3-6. Each scenario: author -> run -> verify -> publish
        by_title = {s["title"]: s for s in self.scenarios}
        for item in self.items:
            if self._cancel:
                item["status"] = "cancelled"
                continue
            try:
                await self._process(project, item, by_title[item["title"]])
            except Exception as e:
                item["status"], item["error"] = "error", _err(e)
                self._log(f"«{item['title']}»: {item['error']}", "error")
            self.save()

    async def _author(self, project: dict, item: dict, s: StudioSession) -> str:
        """Run an authoring session to its end -> done | error | stalled | timeout | cancelled."""
        a = project["pipeline"]["authoring"]
        self.sessions[s.id] = s
        self._session = s
        item["session_id"] = s.id
        try:
            await s.start()
        except Exception as e:
            s.status = "error"
            s.chat.append({"role": "system", "text": _err(e)})
            raise
        if a["autopilot"]:
            s.set_autopilot(True)
        outcome = await self._wait(s, a["autopilot"])
        self._session = None
        return outcome

    async def _process(self, project: dict, item: dict, sc: dict) -> None:
        cfg = project["pipeline"]
        self.stage = "authoring"
        item["status"] = "authoring"
        scenario = sc["instructions"]
        if sc.get("preconditions"):
            scenario = f"Предусловия: {sc['preconditions']}\n{scenario}"
        if sc.get("expected_result"):
            scenario += f"\nОжидаемый результат: {sc['expected_result']}"
        if not self.url:
            raise ValueError("Не указан URL приложения (в запуске или в настройках проекта)")
        a = cfg["authoring"]
        s = StudioSession(project, sc["title"], self.url, scenario, headless=a["headless"],
                          credentials=projects.app_credentials(project["id"]))
        self._log(f"«{sc['title']}»: генерация теста ({'Auto-Pilot' if a['autopilot'] else 'с подтверждением шагов'})")
        outcome = await self._author(project, item, s)
        item["summary"] = s.summary
        if outcome != "done":
            item["status"] = "needs_attention" if outcome != "cancelled" else "cancelled"
            item["error"] = {"error": "Ошибка агента — откройте сессию в Studio",
                             "stalled": "Агент ждёт человека — откройте сессию в Studio",
                             "timeout": "Истекло время ожидания"}.get(outcome, "")
            self._log(f"«{sc['title']}»: {item['error'] or 'остановлено'}", "warn")
            return

        test = s.to_test()
        # Only a scenario the agent completed and verified becomes a test: otherwise the
        # saved test would pass on replay without checking anything. The session stays
        # open in Studio so a person can finish it and save by hand.
        problem = {"failed": "Агент нашёл расхождение с ожидаемым результатом (возможный дефект)",
                   "blocked": "Агент не смог пройти сценарий"}.get(s.finish_status, "")
        if not problem and not has_assertion(test["steps"]):
            problem = "В тесте нет ни одной проверки"
        if problem:
            item["status"], item["error"] = "needs_attention", f"{problem} — откройте сессию в Studio"
            self._log(f"«{sc['title']}»: {problem}", "warn")
            return

        test.update(priority=sc.get("priority", ""), scenario_type=sc.get("type", ""),
                    source=", ".join(self.links) or ("Planner" if self.explore else ""),
                    gherkin_scenario=sc.get("gherkin", ""))
        storage.save(test)
        s.save_artifacts(test)
        s.test_id = item["test_id"] = test["id"]
        self._log(f"«{sc['title']}»: тест сохранён, шагов {len(test['steps'])}")
        await s.close()
        self.sessions.pop(s.id, None)
        item["session_id"] = None

        run = None
        if cfg["run"]["enabled"]:
            self.stage = item["status"] = "running"
            self._log(f"«{sc['title']}»: прогон")
            run = await run_and_record(project, test, log=self._log, trigger="pipeline", user=self.user)
            item["run"] = {"passed": run["passed"], "status": run["status"], "healed": run["healed"],
                           "analysis": run.get("analysis"), "run_id": run["id"]}
            self._log(f"«{sc['title']}»: прогон {({'passed': 'успешен', 'flaky': 'нестабилен (прошёл со второй попытки)'}).get(run['status'], 'упал')}"
                      + (f", самолечение: {run['healed']}" if run["healed"] else ""),
                      "info" if run["status"] == "passed" else "warn")

        if cfg["verify"]["enabled"] and (run is None or run["passed"]):
            self.stage = item["status"] = "verifying"
            await self._verify(project, item, test)

        if cfg["publish"]["enabled"]:
            self.stage = item["status"] = "publishing"
            self._log(f"«{sc['title']}»: публикация в Zephyr")
            try:
                res = await publisher.publish_test(project, test, self._log)
                storage.update(test["id"], lambda t: t.update(external=test.get("external")))
                item["publish"] = res
                if res.get("status") == "ok" and run and cfg["publish"]["report_runs"]:
                    run_res = await publisher.report_run(project, test, run, self._log)
                    item["publish"]["execution"] = run_res.get("key", "")
                self._log(f"«{sc['title']}»: {res.get('key') or ''} {res.get('summary', '')}".strip(),
                          "info" if res.get("status") == "ok" else "warn")
            except Exception as e:
                item["publish"] = {"status": "failed", "summary": _err(e)}
                self._log(f"«{sc['title']}»: публикация не удалась: {_err(e)}", "error")
        item["status"] = "done"

    async def _verify(self, project: dict, item: dict, test: dict) -> None:
        """Mutation testing; weak assertions are strengthened by the agent once."""
        name = test["name"]
        self._log(f"«{name}»: проверка качества теста (мутации)")
        res = await mutations.verify(project, test, log=self._log)
        item["verify"] = _verify_summary(res)
        if res["status"] != "done":
            self._log(f"«{name}»: проверка качества не выполнена: {res.get('error', '')}", "warn")
            return
        self._log(f"«{name}»: обнаружено мутантов {res['killed']} из {res['total']}",
                  "info" if not res["weak"] else "warn")
        if not res["weak"] or not project["pipeline"]["verify"]["improve"] or self._cancel:
            return
        self._log(f"«{name}»: слабые проверки — агент добавляет проверки")
        a = project["pipeline"]["authoring"]
        s = StudioSession(project, name, test["url"], test.get("scenario", ""), headless=a["headless"],
                          credentials=storage.credentials(test), base_steps=test["steps"],
                          task=mutations.improvement_task(res))
        s.test_id = test["id"]
        outcome = await self._author(project, item, s)
        if outcome != "done" or s.finish_status != "passed":
            self._log(f"«{name}»: агент не усилил проверки — откройте сессию в Studio", "warn")
            return
        steps = s.to_test()["steps"]
        storage.update(test["id"], lambda t: t.update(steps=steps))
        test["steps"] = steps
        await s.close()
        self.sessions.pop(s.id, None)
        item["session_id"] = None
        res = await mutations.verify(project, test, log=self._log)
        item["verify"] = _verify_summary(res) | {"improved": True}
        if res["status"] == "done":
            self._log(f"«{name}»: после усиления обнаружено мутантов {res['killed']} из {res['total']}",
                      "info" if not res["weak"] else "warn")

    async def _wait(self, s: StudioSession, autopilot: bool) -> str:
        """-> done | error | stalled | timeout | cancelled"""
        start = time.time()
        while True:
            await asyncio.sleep(1)
            if self._cancel:
                return "cancelled"
            if s.status == "done":
                return "done"
            if s.status in ("error", "closed"):
                return "error"
            if autopilot and not s.autopilot and s.status in ("idle", "awaiting_approval"):
                return "stalled"   # asked the user something or hit the step limit
            if time.time() - start > AUTHORING_TIMEOUT:
                return "timeout"


def _verify_summary(res: dict) -> dict:
    return {k: res.get(k) for k in ("status", "killed", "total", "score", "weak", "error")}


def list_jobs(pid: str, limit: int = 30) -> list[dict]:
    out = {j.id: j.state() for j in JOBS.values() if j.project_id == pid}
    d = _jobs_dir(pid)
    if d.exists():
        for f in sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]:
            if f.stem not in out:
                try:
                    out[f.stem] = json.loads(f.read_text("utf-8"))
                except ValueError:
                    continue
    jobs = sorted(out.values(), key=lambda j: j["created"], reverse=True)[:limit]
    return [{k: j.get(k) for k in ("id", "status", "stage", "created", "finished", "links", "feature",
                                   "user", "explore")} | {"items": len(j.get("items") or []),
                                                          "done": sum(1 for i in j.get("items") or []
                                                                      if i["status"] == "done")} for j in jobs]


def get_job(jid: str) -> dict | None:
    if jid in JOBS:
        return JOBS[jid].state()
    if not re.fullmatch(r"[0-9a-f]{10}", jid):
        return None
    for f in projects.ROOT.glob(f"*/jobs/{jid}.json"):
        j = json.loads(f.read_text("utf-8"))
        if j["status"] in ("running", "awaiting_selection"):
            j["status"], j["error"] = "error", "Студия была перезапущена во время работы конвейера"
        return j
    return None
