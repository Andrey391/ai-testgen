"""The full, configurable test generation process of a project:

    requirements (Jira / Confluence via MCP, .md, text, or the Planner's site map)
      -> scenarios (test design by the LLM + skills; manual or automatic selection)
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

from . import (analyses, audit, explorer, fs, knowledge, llm, mcp_hub, mutations, notify, projects, publisher, reuse,
               runner, runs, scenarios, sources, storage, validation, vault)
from . import agent
from .agent import StudioSession, has_assertion

JOBS: dict[str, "Job"] = {}
AUTHORING_TIMEOUT = 2 * 3600   # guarded mode waits for a human this long at most


def _jobs_dir(pid: str):
    return projects.path(pid) / "jobs"


# ---------- log in once: the saved state of the project's login test ----------

def login_test(pid: str) -> dict | None:
    """The project's test with the role "login" (Тесты → «Сделать тестом входа»)."""
    return next((t for t in storage.all_tests(pid) if t.get("role") == "login"), None)


def _state_key(creds: dict) -> str:
    return "state-" + (creds.get("username") or "default")


def load_state(pid: str, creds: dict) -> dict | None:
    return vault.load(projects.secrets_kind(pid), _state_key(creds))


def save_state(pid: str, creds: dict, state: dict) -> None:
    """The context state (cookies, localStorage) after the login test: a secret, kept in secrets/."""
    vault.save(projects.secrets_kind(pid), _state_key(creds), state)


def uses_login_state(project: dict, test: dict) -> bool:
    lt = login_test(project["id"])
    # An API test signs in with the cookies of the login test only when the project's API uses cookies.
    if test.get("layer") == "api" and projects.api_settings(project)["auth"] != "cookies":
        return False
    return bool(project["pipeline"]["run"].get("login_once") and lt and lt["id"] != test["id"]
                and test.get("role") != "module")


async def ensure_login_state(project: dict, creds: dict, *, browser=None, headless: bool = True, engine: str = "",
                             device: str = "", fresh: bool = False, log=None) -> dict | None:
    """The saved login of `creds`, running the login test when there is none (or `fresh`)."""
    lt = login_test(project["id"])
    if not lt:
        return None
    if not fresh:
        state = load_state(project["id"], creds)
        if state:
            return state
    if log:
        log(f"Вход: тест «{lt['name']}»")
    cfg = project["pipeline"]["run"] | {"trace": "off", "analyze_failures": False}
    rep = await runner.run_test(lt, headless, credentials=creds, cfg=cfg, browser=browser, capture_state=True,
                                engine=engine or "chromium", device=device, base_url=project.get("base_url", ""))
    if not rep["passed"] or not rep.get("storage_state"):
        return None
    save_state(project["id"], creds, rep["storage_state"])
    return rep["storage_state"]


def logged_out(report: dict, project: dict) -> bool:
    """Did the test land on the login page or get 401 (the saved login expired)?"""
    lt = login_test(project["id"])
    if any(e.get("status") == 401 for e in report.get("events") or []):
        return True
    if not lt:
        return False
    login_path = urlparse(lt.get("url", "")).path.rstrip("/")
    last = next((r.get("url") for r in reversed(report["results"]) if r.get("url")), "")
    return bool(login_path) and urlparse(last).path.rstrip("/") == login_path


def matrix(project: dict) -> list[tuple[str, str]]:
    """(browser, device) combinations of the project's runs: run.browsers x run.devices."""
    cfg = project["pipeline"]["run"]
    devices = [("" if d == "desktop" else d) for d in cfg.get("devices") or [""]]
    return [(b, d) for b in cfg.get("browsers") or ["chromium"] for d in devices]


async def run_and_record(project: dict, test: dict, headless: bool | None = None, on_progress=None,
                         log=None, *, trigger: str = "manual", suite_id: str = "", user: str = "",
                         browser=None, run: dict | None = None, engine: str = "", device: str | None = None,
                         login_state: dict | None = None) -> dict:
    """Run a saved test with the project's "run" settings and record the result.

    One attempt, and a second one if it failed (run.retry_failed): failed then passed
    is "flaky". A failure is analysed by the model with the test's history. The run goes
    to the history (runs.py); the test gets its last run summary, heal proposals
    (review mode) or healed locators (auto mode), and quarantine if auto-quarantine
    is on and the test flips too often. Reported to Zephyr if publishing is set up.

    With a login test in the project (run.login_once) the test starts logged in: the saved
    state (`login_state`, or the stored one, or a fresh run of the login test). If it lands
    on the login page or gets 401, it logs in again once and repeats the attempt.
    `engine` / `device`: the browser and device of this run (default: the first of the project's).

    Returns the run record; `run` may be one created beforehand with runs.new().
    """
    cfg = project["pipeline"]["run"]
    headless = cfg["headless"] if headless is None else headless
    first_combo = matrix(project)[0]
    engine = engine or first_combo[0]
    device = first_combo[1] if device is None else device
    run = run or runs.new(test, trigger, suite_id, user)
    if test.get("layer") == "api":
        run.update(browser="api", device="")        # no browser: requests to the API
    else:
        run.update(browser=engine, device=device)
    creds = storage.credentials(test)
    files = runs.files_dir(run)
    attempts: list[dict] = []
    is_login = (login_test(project["id"]) or {}).get("id") == test["id"]
    base_url = project.get("base_url", "")

    async def progress(report):
        run["results"], run["attempt"] = report["results"], report["attempt"]
        if time.time() - run.get("saved", 0) > 1:
            runs.save(run)            # the web server of another process polls the stored record
        if on_progress:
            await on_progress(run)

    async def attempt(n: int, state: dict | None) -> dict:
        return await runner.run_test(test, headless, progress, creds, cfg, browser=browser, run_dir=files,
                                     attempt=n, engine=engine, device=device, storage_state=state,
                                     capture_state=is_login, base_url=base_url)

    with llm.usage_scope() as usage:
        try:
            state = None
            if uses_login_state(project, test):
                state = login_state or await ensure_login_state(project, creds, browser=browser, headless=headless,
                                                                engine=engine, device=device, log=log)
            attempts.append(await attempt(1, state))
            if not attempts[0]["passed"] and state is not None and logged_out(attempts[0], project):
                if log:
                    log(f"«{test['name']}»: сохранённый вход устарел — повторный вход")
                state = await ensure_login_state(project, creds, browser=browser, headless=headless, engine=engine,
                                                 device=device, fresh=True, log=log)
                attempts[0] = await attempt(1, state)
                run["relogin"] = True
            if is_login and attempts[0]["passed"] and attempts[0].get("storage_state"):
                save_state(project["id"], creds, attempts[0].pop("storage_state"))
            if not attempts[0]["passed"] and cfg["retry_failed"]:
                if log:
                    log(f"«{test['name']}»: повторный запуск после падения")
                attempts.append(await attempt(2, state))
            for a in attempts:
                a.pop("storage_state", None)
            first, final = attempts[0], attempts[-1]
            run.update(attempts=[{k: v for k, v in a.items() if k not in ("proposals", "module_heals")}
                                 for a in attempts],
                       before=final.get("before") or [], after=final.get("after") or [],
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

    proposals, module_proposals = {}, {}
    for a in attempts:
        for p in a.get("proposals", []):
            if p.get("module"):
                module_proposals.setdefault(p["test_id"], {})[p["step_id"]] = p | {"run_id": run["id"]}
            else:
                proposals[p["step_id"]] = p | {"run_id": run["id"]}
    run["proposals"] = len(proposals) + sum(len(x) for x in module_proposals.values())
    # The test first: whoever sees the run finished also sees its outcome in the test.
    _record(project, test, run, attempts, proposals)
    _record_modules(project, attempts, module_proposals)
    runs.finish(run, keep=cfg["keep_runs"])

    pub = project["pipeline"]["publish"]
    if pub["enabled"] and pub["report_runs"] and publisher.external(test, project).get("key") \
            and run["status"] != "error":
        try:
            run["tms"] = await publisher.report_run(project, test, run, log)
        except Exception as e:
            run["tms"] = {"status": "failed", "summary": _err(e)}
        run["tms"]["system"] = publisher.title_of(project)
        runs.save(run)
    if run.get("proposals"):
        notify.event(project, "proposals", test=test, run=run)
    return run


def _record(project: dict, test: dict, run: dict, attempts: list[dict], proposals: dict) -> None:
    """Write the run's outcome into the saved test (re-read under a lock: the test may
    have been edited meanwhile) and into the caller's copy."""
    cfg = project["pipeline"]["run"]
    healed_ids = {r["id"] for a in attempts for r in a["results"] if r.get("healed")}
    healed = {s["id"]: s["locator"] for s in test["steps"] if s["id"] in healed_ids and s["action"] != "use_module"}
    failed = next((r for r in run["results"] if r["status"] == "failed"), None)
    paths = sorted({urlparse(r["url"]).path for a in attempts for r in a["results"] if r.get("url")})
    # The run is not in the history yet (it is finished after the test is written): its outcomes go last.
    history = [h for h in runs.history(project["id"], test["id"]) if h.get("id") != run["id"]]
    outcomes = [bool(a.get("passed")) for a in run.get("attempts") or []] or (
        [bool(run["passed"])] if run.get("passed") is not None else [])
    rate = runs.flip_rate(history + [{"outcomes": outcomes}])

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
    if cfg["heal_mode"] == "auto" and healed:
        # Without a human review: the security team wants to see these.
        audit.record("heal.auto", user="system", project_id=project["id"], target={"tid": test["id"]},
                     details={"run_id": run["id"], "steps": sorted(healed)}, via="system")


def _record_modules(project: dict, attempts: list[dict], proposals: dict[str, dict]) -> None:
    """Healing inside a module goes into the module test: every test that uses it gets the fix."""
    healed: dict[str, list] = {}
    for a in attempts:
        healed.update(a.get("module_heals") or {})
    for mid in set(healed) | set(proposals):
        def change(t: dict, mid=mid) -> None:
            if mid in healed and project["pipeline"]["run"]["heal_mode"] == "auto":
                fresh = {s["id"]: s for s in healed[mid]}
                for s in t["steps"]:
                    if s["id"] in fresh and fresh[s["id"]].get("healed"):
                        s["locator"], s["healed"] = fresh[s["id"]]["locator"], True
            if proposals.get(mid):
                keep = [p for p in t.get("heal_proposals") or [] if p["step_id"] not in proposals[mid]]
                t["heal_proposals"] = keep + list(proposals[mid].values())
        storage.update(mid, change)


def _err(e: Exception) -> str:
    if isinstance(e, (publisher.PublishError, sources.SourceError, ValueError)) or type(e).__name__ == "McpError":
        return str(e)
    return llm.api_error_text(e)


class Job:
    def __init__(self, project: dict, links: list[str], text: str, url: str, sessions: dict,
                 user: str = "", explore: bool = False, cases: dict | None = None,
                 scenarios: list[dict] | None = None, feature: str = "", design: dict | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.project_id = project["id"]
        self.links = [l.strip() for l in links if l.strip()]
        self.text, self.url, self.user = text.strip(), url.strip() or project.get("base_url", ""), user
        self.explore = explore
        # Manual test cases to automate: {"connection": id, "ids": [...]} (no ids = every case, Test IT)
        self.cases = cases or None
        # Scenarios prepared (and edited) in the Requirements tab: no requirements or test design stage.
        self.given = [dict(s) for s in scenarios or []]
        # This run's choice for test design: kinds of checks, layers, techniques (scenarios.settings)
        self.design = {k: list(v) for k, v in (design or {}).items() if k in ("types", "layers", "techniques")}
        self.validation: dict | None = None     # the specification checked against the documentation standard
        self.sessions = sessions
        # running | awaiting_model | awaiting_selection | awaiting_reuse | done | error | cancelled
        self.status = "running"
        self.stage = "requirements"
        self.created = time.time()
        self.finished = None
        self.log: list[dict] = []
        self.requirements: list[dict] = []
        self.feature, self.assumptions = feature.strip(), []
        self.scenarios: list[dict] = []
        self.items: list[dict] = []
        self.error = ""
        self.usage: dict = {}
        self._selected = asyncio.Event()
        self._wake = asyncio.Event()        # the application model may have been confirmed
        self._cancel = False
        self._session: StudioSession | None = None

    # ---------- control (call on the worker loop) ----------

    def select(self, indices: list[int]) -> None:
        if self.status != "awaiting_selection":
            return
        self.items = [self._item(self.scenarios[i]) for i in sorted(set(indices))
                      if 0 <= i < len(self.scenarios)]
        self._selected.set()

    def model_confirmed(self) -> None:
        self._wake.set()

    def session_saved(self, sid: str, test: dict) -> None:
        """A person finished in Studio the session of an item and saved its test (call on the worker loop)."""
        if _saved_item(self.state(), sid, test):
            self._log(f"Тест «{test['name']}» сохранён в Studio: сценарий готов")

    def decide(self, decisions: dict[int, str]) -> None:
        """What to do with scenarios similar to earlier ones (reuse.DECISIONS), by item index."""
        for i, d in decisions.items():
            if 0 <= i < len(self.items) and self.items[i].get("match") and d in reuse.DECISIONS:
                self.items[i]["match"]["decision"] = d
                sc = self.given[i] if self.given and len(self.given) == len(self.items) else {}
                if sc.get("analysis_id"):       # the Requirements tab shows the same decision
                    try:
                        analyses.update_scenario(self.project_id, sc["analysis_id"], sc["scenario_id"], {"decision": d})
                    except (KeyError, ValueError):
                        pass
        self.save()
        self._wake.set()

    def cancel(self) -> None:
        self._cancel = True
        self._selected.set()
        self._wake.set()
        if self._session:
            self._session.autopilot = False

    # ---------- state ----------

    def state(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "project_id", "links", "text", "url", "user", "explore", "cases", "status", "stage", "created",
            "finished", "log", "requirements", "feature", "assumptions", "given", "scenarios", "items", "error", "usage",
            "design", "validation")}

    @classmethod
    def resumed(cls, project: dict, j: dict, sessions: dict, user: str = "") -> "Job":
        """A run interrupted by a restart of the studio (or stopped, or failed) that goes on from where
        it stopped: the requirements and scenarios it has are kept, finished items are not redone, an
        item's authoring continues its session from the checkpoint (agent.restored)."""
        job = cls(project, j.get("links") or [], j.get("text") or "", j.get("url") or "", sessions,
                  user=j.get("user") or user, explore=j.get("explore", False), cases=j.get("cases"),
                  design=j.get("design"))
        job.id, job.created = j["id"], j["created"]
        for k in ("log", "feature", "assumptions", "given", "scenarios", "items", "validation"):
            setattr(job, k, j.get(k) or getattr(job, k))
        job.requirements = j.get("requirements") or [] if job.scenarios else []
        for item in job.items:
            if item["status"] not in DONE_ITEMS:
                item["status"], item["error"] = "queued", ""
        job._log(f"Конвейер продолжен ({user or 'студия'}): готовые сценарии не повторяются")
        return job

    @classmethod
    def retried(cls, project: dict, j: dict, sessions: dict, indices: list[int], user: str = "") -> "Job":
        """The run goes on (as resumed()) and the given items whose authoring failed start over: a fresh
        session, no error; the caller has closed their old sessions."""
        job = cls.resumed(project, j, sessions, user)
        for i in indices:
            item = job.items[i]
            item.update(status="queued", error="", session_id=None, summary="", bug=False)
        job._log(f"Перезапуск генерации ({user or 'студия'}): сценариев {len(indices)} — "
                 + ", ".join(f"«{job.items[i]['title']}»" for i in indices[:10]) + ("…" if len(indices) > 10 else ""))
        return job

    def save(self) -> None:
        fs.write_json(_jobs_dir(self.project_id) / f"{self.id}.json", self.state(), indent=1)

    def _log(self, text: str, level: str = "info") -> None:
        self.log.append({"at": time.time(), "level": level, "text": text})
        self.save()

    @staticmethod
    def _item(sc: dict) -> dict:
        return {"title": sc["title"], "priority": sc.get("priority", ""), "type": sc.get("type", ""),
                "layer": sc.get("layer") or "ui", "match": reuse.clean(sc.get("match")),
                "status": "queued", "session_id": None, "test_id": None, "summary": "",
                "run": None, "verify": None, "publish": None, "error": ""}

    # ---------- the process ----------

    async def run(self) -> None:
        budget = (projects.get(self.project_id) or {}).get("pipeline", {}).get("budget") or {}
        with llm.usage_scope(budget.get("job") or 0, budget.get("currency") or "USD", "запуска конвейера",
                             on_warn=lambda text: self._log(text, "warn")) as usage:
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
        # A resumed run already has its scenarios (and maybe the chosen ones): those stages are not redone.
        if not self.scenarios:
            if self.given:
                # 0. Ready scenarios from the Requirements tab: every one of them goes to authoring.
                self.stage = "scenarios"
                self.scenarios = self.given
                self.requirements.append({"source": "Требования", "title": f"Готовые сценарии: {len(self.given)}",
                                          "chars": 0})
                self.items = [self._item(s) for s in self.scenarios]
            elif self.cases:
                # 0. Manual test cases to automate: they ARE the scenarios (no test design needed).
                await self._import_cases(project)
            else:
                await self._design(project, cfg)
            if not self.scenarios:
                return
        if self._cancel:
            return
        if cfg["requirements"].get("confirm_model"):
            await self._await_model()
            if self._cancel:
                return
        if not self.items:
            await self._select(cfg["scenarios"]["select"])
            if self._cancel:
                return
        await self._await_reuse()
        if self._cancel:
            return
        self._log(f"В работу взято {'кейсов' if self.cases else 'сценариев'}: {len(self.items)}")

        # 3-6. Each scenario: author -> run -> verify -> publish
        if self.given:
            pairs = zip(self.items, self.scenarios)
        else:
            by_title = {s["title"]: s for s in self.scenarios}
            pairs = ((item, by_title[item["title"]]) for item in self.items)
        await self._process_all(project, pairs)

    async def _design(self, project: dict, cfg: dict) -> None:
        """Requirements -> scenarios (or one scenario of the whole text when the stage is off)."""
        self.requirements = []
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
            spec = "\n\n---\n\n".join(parts)       # the model learns from the map itself (learn=True)
            m = await explorer.explore(project, self.url, log=self._log, learn=True)
            if m["status"] != "done":
                raise ValueError(f"Исследование сайта не удалось: {m['error']}")
            text = explorer.to_requirements(m)
            self.requirements.append({"source": "Planner", "title": f"Карта сайта: страниц {len(m['pages'])}",
                                      "chars": len(text)})
            parts.append(text)
        if not parts:
            raise ValueError("Нет требований: укажите ссылки, текст или включите исследование сайта")
        requirements = "\n\n---\n\n".join(parts)

        # The specification against the documentation standard, and what it says about the application
        if cfg["requirements"]["validate"]:
            self._log("Проверка ТЗ на соответствие стандарту документации…")
            try:
                self.validation = await validation.validate(project, requirements)
                self._log(f"Проверка ТЗ: {self.validation['score']}/100, замечаний {len(self.validation['findings'])}"
                          f" — {self.validation['summary']}", "info" if self.validation["verdict"] == "ready" else "warn")
            except Exception as e:
                self._log(f"Проверка ТЗ не удалась: {_err(e)}", "warn")
        learn = None
        if cfg["requirements"].get("learn_model") and (self.links or self.text):
            learn = asyncio.create_task(knowledge.extract(project, spec if self.explore else requirements, self.user))

        # 2. Scenarios
        self.stage = "scenarios"
        if cfg["scenarios"]["enabled"]:
            self._log("Проектирование сценариев…")
            res = await scenarios.generate(requirements, self.url, project=project, log=self._log,
                                           cfg=scenarios.settings(project, **self.design))
            self.feature, self.assumptions = res.feature, res.assumptions
            self.scenarios = [s.model_dump() for s in res.scenarios]
            self._log(f"Сценариев: {len(self.scenarios)}")
            if not self.scenarios:
                self._log("По этим требованиям сценариев не получилось", "warn")
        else:
            self.scenarios = [{"title": self.requirements[0]["title"] or "Сценарий", "type": "", "priority": "",
                               "preconditions": "", "instructions": requirements, "expected_result": "",
                               "gherkin": ""}]
            self.items = [self._item(self.scenarios[0])]
        if learn:
            try:
                doc = await learn
                self._log(f"Модель приложения обновлена: сущностей {len(doc['entities'])}, ролей {len(doc['roles'])}")
            except Exception as e:
                self._log(f"Модель приложения не обновлена: {_err(e)}", "warn")
        self.save()

    async def _process_all(self, project: dict, pairs) -> None:
        """(item, scenario) one after another; an error of one scenario does not stop the others."""
        for item, sc in pairs:
            if item["status"] in DONE_ITEMS:
                continue
            if self._cancel:
                item["status"] = "cancelled"
                continue
            try:
                await self._process(project, item, sc)
            except Exception as e:
                item["status"], item["error"] = "error", _err(e)
                self._log(f"«{item['title']}»: {item['error']}", "error")
            if item.get("test_id") and sc.get("analysis_id"):
                analyses.link_test(self.project_id, sc["analysis_id"], sc["scenario_id"], item["test_id"])
            self.save()

    async def _import_cases(self, project: dict) -> None:
        conn = mcp_hub.find(project, cid=self.cases.get("connection", ""))
        if not conn:
            raise ValueError("Подключение системы управления тестами для импорта кейсов не найдено")
        ids = [str(i) for i in self.cases.get("ids") or []]
        self._log(f"Импорт ручных кейсов из «{conn['name']}»" + (f": {', '.join(ids[:20])}" if ids else " (все кейсы)"))
        self.scenarios = await publisher.import_cases(project, conn, ids, log=self._log)
        self.requirements.append({"source": conn["name"], "title": f"Ручные кейсы: {len(self.scenarios)}",
                                  "chars": 0})
        self.feature = f"Автоматизация ручных кейсов ({conn['name']})"
        self._log(f"Кейсов для автоматизации: {len(self.scenarios)}")
        if not self.scenarios:
            self._log("Кейсы не найдены", "warn")

    async def _await_model(self) -> None:
        """Tests are generated only from a confirmed lifecycle of the system: entities, dependencies,
        lifecycles, roles and their capabilities (knowledge.confirm). The run waits for a person; a
        confirmation made anywhere (the job, "Проект → Тестовые данные", another instance) lets it go on."""
        if knowledge.is_confirmed(self.project_id):
            return
        self.status = "awaiting_model"
        state = knowledge.confirmation(knowledge.get(self.project_id))
        self._log("Подтвердите жизненный цикл системы перед генерацией тестов: сущности, зависимости, статусы, роли "
                  "и их возможности («Проект → Тестовые данные»)"
                  + (" — модель изменилась после подтверждения" if state["state"] == "changed" else "")
                  + (f". Не хватает: {'; '.join(state['missing'])}" if state["missing"] else ""), "warn")
        while not self._cancel and not knowledge.is_confirmed(self.project_id):
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), MODEL_POLL)
            except asyncio.TimeoutError:
                pass
        if not self._cancel:
            self.status = "running"
            self._log("Жизненный цикл системы подтверждён: генерация тестов")

    def _undecided(self) -> list[dict]:
        return [it for it in self.items if it["status"] not in DONE_ITEMS and reuse.pending(it.get("match"))]

    async def _await_reuse(self) -> None:
        """A scenario similar to an earlier test or scenario waits for a person: create a new test, reuse
        the earlier one or refine it. A decision made in the Requirements tab counts as well."""
        if not self._undecided():
            return
        self.status = "awaiting_reuse"
        self._log(f"Похожи на созданные ранее: {len(self._undecided())} — решите для каждого: создать новый тест, "
                  "переиспользовать или доработать прежний", "warn")
        while not self._cancel and self._undecided():
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), MODEL_POLL)
            except asyncio.TimeoutError:
                self._decisions_from_analyses()
        if not self._cancel:
            self.status = "running"
            self._log("Решения по похожим сценариям приняты: " + ", ".join(
                f"«{it['title']}» — {reuse.DECISION_TITLES[it['match']['decision']]}"
                for it in self.items if it.get("match") and it["match"].get("decision")))
            self.save()

    def _decisions_from_analyses(self) -> None:
        if not self.given or len(self.given) != len(self.items):
            return
        for it, sc in zip(self.items, self.given):
            if reuse.pending(it.get("match")) and sc.get("analysis_id"):
                a = analyses.get(sc["analysis_id"]) or {}
                s = next((x for x in a.get("scenarios") or [] if x["id"] == sc.get("scenario_id")), None)
                if s and (s.get("match") or {}).get("decision"):
                    it["match"]["decision"] = s["match"]["decision"]

    async def _select(self, mode: str) -> None:
        if mode == "manual":
            self.status = "awaiting_selection"
            self._log("Выберите сценарии для генерации тестов")
            await self._selected.wait()
            self.status = "running"
        else:
            chosen = [s for s in self.scenarios if mode == "all" or s["priority"] == "high"]
            self.items = [self._item(s) for s in chosen]

    async def _author(self, project: dict, item: dict, s: StudioSession, started: bool = False) -> str:
        """Run an authoring session to its end -> done | error | stalled | timeout | cancelled.
        `started`: a person already continues it in Studio - just wait for it."""
        a = project["pipeline"]["authoring"]
        s.origin = {"job": self.id}
        self.sessions[s.id] = s
        self._session = s
        item["session_id"] = s.id
        self.save()
        if not started:
            s.starts_in_autopilot = bool(a["autopilot"])
            try:
                await s.start()
            except Exception as e:
                s.status = "error"
                s.chat.append({"role": "system", "text": _err(e)})
                s.checkpoint()
                raise
        auto = a["autopilot"] and not started
        if auto and s.status != "done":
            s.set_autopilot(True)
        outcome = await self._wait(s, auto)
        self._session = None
        return outcome

    async def _process(self, project: dict, item: dict, sc: dict) -> None:
        cfg = project["pipeline"]
        # A resumed item whose test is saved goes on with the stages it has not passed.
        test = storage.load(item["test_id"]) if item.get("test_id") else None
        match = item.get("match") or {}
        earlier = reuse.test_of(project["id"], match) if not test else None
        if test:
            item["session_id"] = None
        elif match.get("decision") == "reuse" and match["kind"] == "scenario":
            item["status"], item["summary"] = "done", f"Сценарий уже есть: «{match['title']}» — новый тест не создан"
            self._log(f"«{sc['title']}»: {item['summary']}")
            return
        elif match.get("decision") == "reuse" and earlier:
            test = earlier
            item["test_id"], item["summary"] = test["id"], f"Переиспользован тест «{test['name']}»"
            self._log(f"«{sc['title']}»: {item['summary']}")
        elif match.get("decision") == "refine" and earlier:
            test = await self._refine(project, item, sc, earlier)
            if not test:
                return
        else:
            test = await self._create_test(project, item, sc)
            if not test:
                return

        run = None
        if cfg["run"]["enabled"] and item.get("run"):
            run = runs.get(item["run"].get("run_id") or "")
        elif cfg["run"]["enabled"]:
            self.stage = item["status"] = "running"
            self._log(f"«{sc['title']}»: прогон")
            run = await run_and_record(project, test, log=self._log, trigger="pipeline", user=self.user)
            item["run"] = {"passed": run["passed"], "status": run["status"], "healed": run["healed"],
                           "analysis": run.get("analysis"), "run_id": run["id"]}
            self._log(f"«{sc['title']}»: прогон {({'passed': 'успешен', 'flaky': 'нестабилен (прошёл со второй попытки)'}).get(run['status'], 'упал')}"
                      + (f", самолечение: {run['healed']}" if run["healed"] else ""),
                      "info" if run["status"] == "passed" else "warn")
            self.save()
        passed = item["run"]["passed"] if item.get("run") else True

        if cfg["verify"]["enabled"] and passed and not item.get("verify"):
            self.stage = item["status"] = "verifying"
            await self._verify(project, item, test)
            self.save()

        if cfg["publish"]["enabled"] and not (item.get("publish") or {}).get("status") == "ok":
            self.stage = item["status"] = "publishing"
            self._log(f"«{sc['title']}»: публикация в {publisher.title_of(project)}")
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

    async def _create_test(self, project: dict, item: dict, sc: dict) -> dict | None:
        """Authoring of an item -> the saved test, or None when a person is needed. A session
        interrupted by a restart continues from its checkpoint."""
        cfg = project["pipeline"]
        self.stage = "authoring"
        item["status"] = "authoring"
        a = cfg["authoring"]
        sid = item.get("session_id") or ""
        live = self.sessions.get(sid)
        cp = None if live else agent.load_checkpoint(project["id"], sid)
        if live:
            self._log(f"«{sc['title']}»: генерация продолжается в Studio")
            s = live
        elif cp:
            s = agent.restored(project, cp)
            self._log(f"«{sc['title']}»: продолжение генерации, записано шагов: {len(s.base_steps)}")
        else:
            s = None
        if s:
            outcome = await self._author(project, item, s, started=bool(live))
            return await self._authored(project, item, sc, s, outcome)
        scenario = scenario_text(sc)
        if not self.url:
            raise ValueError("Не указан URL приложения (в запуске или в настройках проекта)")
        # The role the scenario acts as (the application model maps roles to project accounts).
        guest = False
        if cfg["requirements"].get("preflight"):
            # Before the test: the role has an account, the scenario keeps to the restrictions and lifecycles.
            item["status"] = "preflight"
            check = await knowledge.preflight(project, sc)
            item["preflight"] = check["problems"]
            if not check["ok"]:
                item["status"], item["error"] = "needs_attention", knowledge.preflight_text(check)
                self._log(f"«{sc['title']}»: {item['error']}", "warn")
                self.save()
                return None
            item["status"] = "authoring"
            account, guest = check["account"], check["guest"]
        else:
            account = (knowledge.account_for(project["id"], sc.get("role") or "")
                       or knowledge.account_for(project["id"], f"{sc.get('preconditions', '')}\n{sc['instructions']}"))
        s = StudioSession(project, sc["title"], self.url, scenario, headless=a["headless"],
                          credentials={} if guest else projects.account_credentials(project["id"], account),
                          account=account, engine="api" if sc.get("layer") == "api" else "",
                          use_login_state=not guest)
        self._log(f"«{sc['title']}»: генерация теста ({'Auto-Pilot' if a['autopilot'] else 'с подтверждением шагов'})")
        outcome = await self._author(project, item, s)
        return await self._authored(project, item, sc, s, outcome)

    async def _refine(self, project: dict, item: dict, sc: dict, test: dict) -> dict | None:
        """The earlier test is replayed and extended to the new scenario; it keeps its id (a new version)."""
        self.stage = item["status"] = "authoring"
        a = project["pipeline"]["authoring"]
        self._log(f"«{sc['title']}»: доработка теста «{test['name']}»")
        s = StudioSession(project, test["name"], test["url"], test.get("scenario", ""), headless=a["headless"],
                          credentials=storage.credentials(test), base_steps=test["steps"],
                          task=reuse.REFINE_TASK.format(scenario=scenario_text(sc)), engine=agent.test_engine(test))
        s.test_id = test["id"]
        outcome = await self._author(project, item, s)
        steps = s.to_test()["steps"] if outcome == "done" else []
        problem = ("" if outcome == "done" and s.finish_status == "passed" and has_assertion(steps)
                   else "Агент не доработал тест — откройте сессию в Studio")
        if problem:
            item["status"], item["error"] = ("cancelled", "") if outcome == "cancelled" else ("needs_attention", problem)
            item["bug"] = s.finish_status == "failed"
            self._log(f"«{sc['title']}»: {problem}", "warn")
            return None
        scenario = f"{test.get('scenario', '')}\n\nДоработка: {sc['title']}\n{scenario_text(sc)}".strip()
        test = storage.update(test["id"], lambda t: t.update(steps=steps, scenario=scenario))
        if not test:
            item["status"], item["error"] = "needs_attention", "Прежний тест удалён — откройте сессию в Studio"
            return None
        await s.close(discard=True)
        self.sessions.pop(s.id, None)
        item["session_id"], item["test_id"] = None, test["id"]
        item["summary"] = f"Доработан тест «{test['name']}»"
        self._log(f"«{sc['title']}»: {item['summary']}, шагов {len(steps)}")
        self.save()
        return test

    async def _authored(self, project: dict, item: dict, sc: dict, s: StudioSession, outcome: str) -> dict | None:
        item["summary"] = s.summary
        if outcome != "done":
            item["status"] = "needs_attention" if outcome != "cancelled" else "cancelled"
            item["error"] = {"error": "Ошибка агента — откройте сессию в Studio",
                             "stalled": "Агент ждёт человека — откройте сессию в Studio",
                             "timeout": "Истекло время ожидания"}.get(outcome, "")
            self._log(f"«{sc['title']}»: {item['error'] or 'остановлено'}", "warn")
            return None

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
            item["bug"] = s.finish_status == "failed"        # the studio shows it as a possible defect
            self._log(f"«{sc['title']}»: {problem}", "warn")
            return None

        test.update(priority=sc.get("priority", ""), scenario_type=sc.get("type", ""),
                    source=", ".join(self.links) or ("Planner" if self.explore else "")
                    or (sc.get("source_case") or {}).get("name", ""),
                    gherkin_scenario=sc.get("gherkin", ""), status="draft")
        case = sc.get("source_case")
        if case:
            # The automated test is linked to the manual case it came from.
            key = case["system"] if case["system"] in publisher.SYSTEMS else case["system"]
            test.setdefault("external", {})[key] = {"case_id": case["id"], "case_name": case.get("name", "")} | (
                {"work_item_id": case["id"]} if case["system"] == "testit" else {})
        storage.save(test)
        s.save_artifacts(test)
        s.test_id = item["test_id"] = test["id"]
        self._log(f"«{sc['title']}»: тест сохранён, шагов {len(test['steps'])}")
        await s.close(discard=True)
        self.sessions.pop(s.id, None)
        item["session_id"] = None
        self.save()
        return test

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
                          task=mutations.improvement_task(res), engine=agent.test_engine(test))
        s.test_id = test["id"]
        outcome = await self._author(project, item, s)
        if outcome != "done" or s.finish_status != "passed":
            self._log(f"«{name}»: агент не усилил проверки — откройте сессию в Studio", "warn")
            return
        steps = s.to_test()["steps"]
        storage.update(test["id"], lambda t: t.update(steps=steps))
        test["steps"] = steps
        await s.close(discard=True)
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


# A run in these states is working (or waiting for a person) in the instance that owns it.
LIVE = ("running", "awaiting_model", "awaiting_selection", "awaiting_reuse")
MODEL_POLL = 3      # seconds between checks of the application model while a run waits for its confirmation
DONE_ITEMS = ("done", "needs_attention")     # a resumed run does not redo them


def _saved_item(state: dict, sid: str, test: dict) -> bool:
    """The item of a run whose authoring session a person saved as a test is done with that test (it no
    longer needs a person); its scenario of a requirements analysis gets the test."""
    given = state.get("given") or []
    found = False
    for i, item in enumerate(state.get("items") or []):
        # Not an item the run still authors: the run itself saves what its session makes.
        if sid and item.get("session_id") == sid and item.get("status") in ("needs_attention", "error", "cancelled",
                                                                            "queued"):
            item.update(status="done", test_id=test["id"], error="", bug=False,
                        summary=f"Тест сохранён в Studio: «{test['name']}»")
            found = True
            sc = given[i] if len(given) == len(state["items"]) else {}
            if sc.get("analysis_id"):
                analyses.link_test(state["project_id"], sc["analysis_id"], sc["scenario_id"], test["id"])
    return found


def session_saved(pid: str, jid: str, sid: str, test: dict) -> None:
    """A test saved from a session the pipeline started, of a run not in memory here (finished before a
    restart, or on another instance): its item is done in the saved state of the run, so the "нужен
    человек" block empties as people finish the sessions. A run in memory: Job.session_saved."""
    if not re.fullmatch(r"[0-9a-f]{10}", jid or ""):
        return
    path = _jobs_dir(pid) / f"{jid}.json"
    with fs.lock(path):
        state = fs.read_json(path)
        if state and state.get("project_id") == pid and _saved_item(state, sid, test):
            state.setdefault("log", []).append({"at": time.time(), "level": "info",
                                                "text": f"Тест «{test['name']}» сохранён в Studio: сценарий готов"})
            fs.write_json(path, state, indent=1)


def reconcile(jid: str, state: dict, sessions: dict) -> dict:
    """Items that needed a person whose session has a saved test since (saved in Studio while nothing told
    the run, e.g. before the studio knew to): done with that test. A refinement's session points at the
    earlier test from the start, so it does not count."""
    found = False
    for item in state.get("items") or []:
        sid = item.get("session_id") or ""
        if (not sid or item.get("test_id") or item.get("status") not in ("needs_attention", "error", "cancelled")
                or (item.get("match") or {}).get("decision") == "refine"):
            continue
        live = sessions.get(sid)
        tid = live.test_id if live is not None else (agent.load_checkpoint(state["project_id"], sid) or {}).get("test_id")
        test = storage.load(tid) if tid else None
        if test and test.get("project_id") == state["project_id"]:
            found = _saved_item(state, sid, test) or found
    if found:
        if jid in JOBS:
            JOBS[jid].save()
        else:
            with fs.lock(_jobs_dir(state["project_id"]) / f"{jid}.json"):
                saved = fs.read_json(_jobs_dir(state["project_id"]) / f"{jid}.json") or {}
                fs.write_json(_jobs_dir(state["project_id"]) / f"{jid}.json", saved | {"items": state["items"]},
                              indent=1)
    return state


def retryable(item: dict, explicit: bool = False) -> bool:
    """An item whose authoring failed - an error of the agent, the agent stopped, gave up or was stopped:
    "Перезапустить" generates it again. A possible defect (the agent saw the application differ from the
    scenario) is retried only when a person names it."""
    return (not item.get("test_id") and item.get("status") in ("error", "needs_attention", "cancelled")
            and (explicit or not item.get("bug")))


def scenario_text(sc: dict) -> str:
    """What the authoring agent is told of a designed scenario."""
    text = sc["instructions"]
    data = [f"{d['entity']} «{d['name']}»" + (f" ({d['state']})" if d.get("state") else "")
            + (f" — {d['details']}" if d.get("details") else "") for d in sc.get("test_data") or []
            if isinstance(d, dict) and d.get("entity") and d.get("name")]
    if data:
        text = "Тестовые данные: " + "; ".join(data) + f"\n{text}"
    if sc.get("preconditions"):
        text = f"Предусловия: {sc['preconditions']}\n{text}"
    if sc.get("role"):
        text = f"Роль: {sc['role']}\n{text}"
    if sc.get("layer") == "api":
        text = ("Тест API (бэкенд): проверяй через запросы api_request — статус и поля ответа, без действий в "
                f"интерфейсе.\n{text}")
    if sc.get("expected_result"):
        text += f"\nОжидаемый результат: {sc['expected_result']}"
    return text


def _verify_summary(res: dict) -> dict:
    return {k: res.get(k) for k in ("status", "killed", "total", "score", "weak", "error")}


def list_jobs(pid: str, limit: int = 30) -> list[dict]:
    out = {j.id: j.state() for j in JOBS.values() if j.project_id == pid}
    for f, text, _ in fs.documents(_jobs_dir(pid))[:limit]:
        if f.stem not in out:
            try:
                out[f.stem] = j = json.loads(text)
            except ValueError:
                continue
            if j.get("status") in LIVE and not _job_elsewhere(f.stem):
                j["status"] = "error"          # interrupted by a restart, as get_job() says
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
    for f in fs.glob(projects.ROOT, f"*/jobs/{jid}.json"):
        j = fs.read_json(f)
        if j["status"] in LIVE and not _job_elsewhere(jid):
            j["status"], j["error"] = "error", ("Студия была перезапущена во время работы конвейера: нажмите "
                                                "«Продолжить», чтобы доделать запуск с места остановки")
        return j
    return None


def _job_elsewhere(jid: str) -> bool:
    """Another instance of the studio runs this job: its saved state is current."""
    from . import workqueue
    return bool(workqueue.owner("job", jid))
