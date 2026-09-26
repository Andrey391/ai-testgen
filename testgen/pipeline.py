"""The full, configurable test generation process of a project:

    requirements (Jira / Confluence via MCP, .md, text)
      -> scenarios (test design by Claude + skills; manual or automatic selection)
      -> authoring (agent in a real browser: built-in Playwright or Playwright MCP)
      -> run (replay with self-healing, failure analysis)
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

from . import llm, projects, publisher, runner, scenarios, sources, storage
from .agent import StudioSession, has_assertion

JOBS: dict[str, "Job"] = {}
AUTHORING_TIMEOUT = 2 * 3600   # guarded mode waits for a human this long at most


def _jobs_dir(pid: str):
    return projects.path(pid) / "jobs"


async def run_and_record(project: dict, test: dict, headless: bool | None = None, on_progress=None,
                         log=None) -> dict:
    """Run a saved test with the project's "run" settings, save the result (and healed
    locators) into the test, and report it to Zephyr if publishing is set up."""
    cfg = project["pipeline"]["run"]
    report = await runner.run_test(test, headless=cfg["headless"] if headless is None else headless,
                                   on_progress=on_progress, credentials=storage.credentials(test), cfg=cfg)
    failed = next((r for r in report["results"] if r["status"] == "failed"), None)
    test["last_run"] = {"at": time.time(), "passed": report["passed"], "healed": report["healed"],
                        "failed_step": failed["description"] if failed else "",
                        "error": failed["error"] if failed else "", "analysis": report.get("analysis")}
    storage.save(test)
    pub = project["pipeline"]["publish"]
    if pub["enabled"] and pub["report_runs"] and (test.get("external") or {}).get("zephyr"):
        try:
            res = await publisher.report_run(project, test, report, log)
            report["zephyr"] = res
        except Exception as e:
            report["zephyr"] = {"status": "failed", "summary": _err(e)}
    return report


def _err(e: Exception) -> str:
    if isinstance(e, (publisher.PublishError, sources.SourceError)) or type(e).__name__ == "McpError":
        return str(e)
    return llm.api_error_text(e)


class Job:
    def __init__(self, project: dict, links: list[str], text: str, url: str, sessions: dict,
                 user: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.project_id = project["id"]
        self.links = [l.strip() for l in links if l.strip()]
        self.text, self.url, self.user = text.strip(), url.strip() or project.get("base_url", ""), user
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
            "id", "project_id", "links", "url", "user", "status", "stage", "created", "finished", "log",
            "requirements", "feature", "assumptions", "scenarios", "items", "error")}

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
                "run": None, "publish": None, "error": ""}

    # ---------- the process ----------

    async def run(self) -> None:
        try:
            await self._run()
            self.status = "cancelled" if self._cancel else "done"
        except Exception as e:
            self.status, self.error = "error", _err(e)
            self._log(self.error, "error")
        finally:
            self.finished = time.time()
            self.stage = ""
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
        if not parts:
            raise ValueError("Нет требований: укажите ссылки или текст")
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

        # 3-5. Each scenario: author -> run -> publish
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
        self.sessions[s.id] = s
        self._session = s
        item["session_id"] = s.id
        self._log(f"«{sc['title']}»: генерация теста ({'Auto-Pilot' if a['autopilot'] else 'с подтверждением шагов'})")
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
                    source=", ".join(self.links), gherkin_scenario=sc.get("gherkin", ""))
        storage.save(test)
        s.test_id = item["test_id"] = test["id"]
        self._log(f"«{sc['title']}»: тест сохранён, шагов {len(test['steps'])}")
        await s.close()
        self.sessions.pop(s.id, None)
        item["session_id"] = None

        report = None
        if cfg["run"]["enabled"]:
            self.stage = item["status"] = "running"
            self._log(f"«{sc['title']}»: прогон")
            report = await run_and_record(project, test, log=self._log)
            item["run"] = {"passed": report["passed"], "healed": report["healed"],
                           "analysis": report.get("analysis")}
            self._log(f"«{sc['title']}»: прогон {'успешен' if report['passed'] else 'упал'}"
                      + (f", самолечение: {report['healed']}" if report["healed"] else ""),
                      "info" if report["passed"] else "warn")

        if cfg["publish"]["enabled"]:
            self.stage = item["status"] = "publishing"
            self._log(f"«{sc['title']}»: публикация в Zephyr")
            try:
                res = await publisher.publish_test(project, test, self._log)
                storage.save(test)
                item["publish"] = res
                if res.get("status") == "ok" and report and cfg["publish"]["report_runs"]:
                    run_res = await publisher.report_run(project, test, report, self._log)
                    item["publish"]["execution"] = run_res.get("key", "")
                self._log(f"«{sc['title']}»: {res.get('key') or ''} {res.get('summary', '')}".strip(),
                          "info" if res.get("status") == "ok" else "warn")
            except Exception as e:
                item["publish"] = {"status": "failed", "summary": _err(e)}
                self._log(f"«{sc['title']}»: публикация не удалась: {_err(e)}", "error")
        item["status"] = "done"

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
                                   "user")} | {"items": len(j.get("items") or []),
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
