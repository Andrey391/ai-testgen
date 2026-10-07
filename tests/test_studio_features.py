"""Retrying runs, editing saved steps, faster authoring (a batch of actions per turn), API tests,
read-only built-in skills with local copies, the application model, the choice of what to design
and the validation of a specification."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fakes import Resp, dump, latest_page, ref_for, text, tool
from helpers import arun
from stand import PASSWORD, USERNAME
from testgen import agent, auth, knowledge, projects, runs, scenarios, skills, storage, suite, validation
from testgen.agent import StudioSession
from testgen.steps import new_step

BASE = "http://127.0.0.1:8765"


def _project(name: str = "Проект") -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    return projects.get(p["id"])


def _client(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    return server, TestClient(server.app, base_url=BASE)


# ---------- retrying a run from the history, the failed tests of a suite ----------

def test_retry_a_failed_run_and_the_failed_tests_of_a_suite(monkeypatch):
    server, client = _client(monkeypatch)
    p = _project("Повтор")
    t = storage.save({"project_id": p["id"], "name": "Тест", "url": p["base_url"], "scenario": "",
                      "steps": [new_step("navigate", "open", p["base_url"])], "status": "ready"})
    ok = storage.save({"project_id": p["id"], "name": "Зелёный", "url": p["base_url"], "scenario": "",
                       "steps": [new_step("navigate", "open", p["base_url"])], "status": "ready"})
    started = []
    monkeypatch.setattr(server.worker_mod, "start_run", lambda proj, test, run, submit, **kw: started.append((test["id"], kw)))
    monkeypatch.setattr(server.worker_mod, "start_suite", lambda proj, s, tests, submit, **kw:
                        started.append(("suite", [x["id"] for x in tests])))

    run = runs.new(t, "suite", live=False)
    run.update(status="failed", passed=False, browser="firefox", device="iPhone 13")
    runs.finish(run)
    r = client.post(f"/api/runs/{run['id']}/retry")
    assert r.status_code == 200 and r.json()["id"] != run["id"]
    assert started[-1][0] == t["id"] and started[-1][1]["browser"] == "firefox"
    assert started[-1][1]["device"] == "iPhone 13" and started[-1][1]["trigger"] == "manual"

    s = suite.new(p, [t, ok])
    for item in s["items"]:
        item["status"] = "failed" if item["test_id"] == t["id"] else "passed"
    s.update(status="done", passed=False)
    suite.LIVE.pop(s["id"], None)
    suite.save(s)
    r = client.post(f"/api/suites/{s['id']}/retry")
    assert r.status_code == 200 and started[-1] == ("suite", [t["id"]])     # only the failed one

    s.update(items=[i | {"status": "passed"} for i in s["items"]], passed=True)
    suite.save(s)
    assert client.post(f"/api/suites/{s['id']}/retry").status_code == 400


# ---------- editing the steps of a saved test ----------

def test_edit_steps_keeps_what_is_not_shown_masks_secrets_and_makes_a_version():
    p = _project("Шаги")
    projects.set_app_credentials(p["id"], "demo", "top-secret-1")
    a = new_step("fill", "Логин", "{{username}}", locator=[{"kind": "testid", "value": "username", "frame": ["#f"]},
                                                         {"kind": "css", "value": "#username"}])
    b = new_step("click", "Войти", locator=[{"kind": "role", "role": "button", "name": "Войти"}])
    c = new_step("assert_text_present", "Вошли", "Здравствуйте")
    t = storage.save({"project_id": p["id"], "name": "Вход", "url": p["base_url"], "scenario": "",
                      "steps": [a, b, c], "verify": {"status": "done"}, "heal_proposals": [{"step_id": c["id"]}]})

    edited = storage.edit_steps(t["id"], [
        {"id": b["id"], "action": "click", "description": "Нажать «Войти»", "value": "",
         "locator_text": 'role=button[name="Войти"]\ntestid=login-btn\n#login'},
        {"id": a["id"], "action": "fill", "description": "Ввести логин", "value": "{{username}}"},
        {"id": "", "action": "fill", "description": "Пароль", "value": "top-secret-1", "locator_text": "label=Пароль"},
    ])
    steps = edited["steps"]
    assert [s["description"] for s in steps] == ["Нажать «Войти»", "Ввести логин", "Пароль"]
    assert steps[0]["locator"] == [{"kind": "role", "role": "button", "name": "Войти"},
                                   {"kind": "testid", "value": "login-btn"}, {"kind": "css", "value": "#login"}]
    assert steps[1]["locator"] == a["locator"]                       # untouched: frames and alternatives stay
    assert steps[2]["value"] == "{{password}}" and steps[2]["source"] == "manual"
    assert "verify" not in edited and edited["heal_proposals"] == []  # removed step, other steps
    assert storage.versions(edited)[0]["steps"] == 3                  # the previous state is a version

    with pytest.raises(ValueError, match="локатор"):
        storage.edit_steps(t["id"], [{"action": "click", "description": "Куда?", "value": ""}])
    with pytest.raises(ValueError, match="неизвестное действие"):
        storage.edit_steps(t["id"], [{"action": "rm -rf", "description": "", "value": ""}])


def test_parse_locator_lines():
    assert storage.parse_locator('role=link[name="Он сказал "да""]\n\ntext=Войти\n.btn > a') == [
        {"kind": "role", "role": "link", "name": 'Он сказал "да"'}, {"kind": "text", "value": "Войти"},
        {"kind": "css", "value": ".btn > a"}]


# ---------- built-in skills are read-only: a local copy, used by a checkbox ----------

def test_builtin_skill_clone_switch_and_delete():
    p = _project("Скиллы")
    pid, name = p["id"], "test-design"
    builtin = skills.get(pid, name)
    assert builtin["builtin"] and not builtin["local"] and not builtin["has_local"]

    copy_ = skills.clone(pid, name)
    assert copy_["local"] and copy_["use_local"] and copy_["text"] == builtin["text"]
    skills.save(pid, name, copy_["text"].replace("# Тест-дизайн сценариев", "# Наш тест-дизайн"))
    assert "Наш тест-дизайн" in skills.prompt(pid, [name])

    skills.use_local(pid, name, False)                     # the checkbox: back to the built-in one
    assert "Наш тест-дизайн" not in skills.prompt(pid, [name])
    assert skills.get(pid, name, "local")["text"].count("Наш тест-дизайн") == 1    # the copy is kept
    listed = next(s for s in skills.list_skills(pid) if s["name"] == name)
    assert listed["has_local"] and not listed["use_local"]

    skills.use_local(pid, name, True)
    assert "Наш тест-дизайн" in skills.prompt(pid, [name])
    assert skills.delete(pid, name) and not skills.get(pid, name)["has_local"]
    assert skills.get(pid, name)["text"] == builtin["text"]
    assert skills.get(pid, "requirements-validation")["stage"] == "requirements"


# ---------- the application model ----------

def test_application_model_prompt_memory_and_accounts():
    p = _project("Теннис")
    pid = p["id"]
    projects.save_account(pid, {"name": "Администратор клуба", "username": "club-admin", "password": "pw-123456"})
    acc = next(a["id"] for a in projects.accounts_view(pid))
    doc = knowledge.save(pid, {
        "summary": "Турниры теннисных клубов",
        "entities": [{"name": "Клуб", "lifecycle": "активен → закрыт"},
                     {"name": "Турнир", "depends_on": "Клуб (турниры разрешены), Корт", "create": "администратор клуба",
                      "lifecycle": "черновик → набор → идёт → завершён", "rules": "завершённый нельзя изменить"},
                     {"description": "без имени — не сохраняется"}],
        "roles": [{"name": "Администратор клуба", "description": "создаёт турниры", "account": acc}],
        "data": [{"entity": "Клуб", "name": "Теннис Про", "details": "турниры разрешены", "account": acc}],
        "memory": [{"text": "Корт №1 закрыт на ремонт"}], "junk": 1})
    assert [e["name"] for e in doc["entities"]] == ["Клуб", "Турнир"] and "junk" not in doc
    assert doc["entities"][1]["depends_on"] == ["Клуб (турниры разрешены)", "Корт"]

    knowledge.remember(pid, "Турнир «Осень» создан и открыт для записи", source="Studio: тест")
    knowledge.remember(pid, "Турнир «Осень» создан и открыт для записи")           # no duplicates
    text_ = knowledge.prompt(pid)
    assert "requires first: Клуб (турниры разрешены), Корт" in text_
    assert "project account «Администратор клуба»" in text_ and "Теннис Про" in text_
    assert text_.count("Турнир «Осень»") == 1 and "pw-123456" not in text_
    assert knowledge.account_for(pid, "Предусловия: войти как администратор клуба") == acc
    assert knowledge.account_for(pid, "Гость открывает расписание") == ""

    # What the requirements say joins the model; what people wrote stays.
    knowledge.merge(pid, {"summary": "другое", "entities": [
        {"name": "турнир", "description": "Соревнование", "depends_on": ["Судья"], "lifecycle": "иначе",
         "create": "", "rules": ""},
        {"name": "Матч", "description": "Игра двух игроков", "depends_on": ["Турнир"], "lifecycle": "", "create": "",
         "rules": ""}], "roles": [{"name": "Игрок", "description": "записывается на турнир"}]})
    doc = knowledge.get(pid)
    tour = next(e for e in doc["entities"] if e["name"] == "Турнир")
    assert tour["lifecycle"] == "черновик → набор → идёт → завершён" and tour["description"] == "Соревнование"
    assert tour["depends_on"] == ["Клуб (турниры разрешены)", "Корт", "Судья"]
    assert doc["summary"] == "Турниры теннисных клубов" and {e["name"] for e in doc["entities"]} >= {"Матч"}
    assert {r["name"] for r in doc["roles"]} == {"Администратор клуба", "Игрок"}


def test_application_model_api_and_extraction(monkeypatch, fake_llm):
    server, client = _client(monkeypatch)
    p = _project("Модель")

    def script(kind, kw):
        return Resp(parsed=knowledge.XModel(summary="Клубы и турниры", entities=[knowledge.XEntity(
            name="Турнир", description="", depends_on=["Клуб"], lifecycle="", create="", rules="")], roles=[]))
    fake_llm.script = script
    r = client.post(f"/api/projects/{p['id']}/knowledge/extract", json={"requirements": "Клуб проводит турниры"})
    assert r.status_code == 200 and r.json()["entities"][0]["depends_on"] == ["Клуб"]
    assert "Клуб проводит турниры" in dump(fake_llm.calls[-1][1])
    r = client.put(f"/api/projects/{p['id']}/knowledge", json=r.json() | {"memory": [{"text": "факт"}]})
    assert r.status_code == 200 and client.get(f"/api/projects/{p['id']}/knowledge").json()["memory"][0]["text"] == "факт"


# ---------- what to design: kinds of checks, layers, techniques ----------

def test_scenario_settings_and_context():
    p = _project("Сценарии")
    cfg = scenarios.settings(p, types=["security", "bogus"], layers=["api"], techniques=["pairwise"])
    assert cfg["types"] == ["security"] and cfg["layers"] == ["api"] and cfg["techniques"] == ["pairwise"]
    assert scenarios.settings(p)["layers"] == ["ui"]                  # the project's defaults
    text_ = scenarios._context("Требования", "https://app", cfg, "Application model: …")
    assert "- security:" in text_ and "Layers: api." in text_ and "попарное тестирование" in text_
    assert "positive" not in text_ and "Application model" in text_
    assert scenarios.Scenario(title="x", type="validation", priority="low", preconditions="", instructions="",
                              expected_result="", gherkin="").layer == "ui"


def test_pipeline_text_of_an_api_scenario():
    from testgen.pipeline import scenario_text
    t = scenario_text({"instructions": "POST /api/clubs", "preconditions": "Есть администратор", "layer": "api",
                       "expected_result": "201"})
    assert t.startswith("Тест API") and "api_request" in t and "Предусловия: Есть администратор" in t


# ---------- validation of a specification ----------

def test_validation_against_the_documentation_standard(monkeypatch, fake_llm):
    server, client = _client(monkeypatch)
    p = _project("ТЗ")
    pipeline = p["pipeline"]
    pipeline["requirements"].update(standard="gost19", checklist="У каждого требования есть номер")
    projects.update(p["id"], {"pipeline": pipeline})

    def script(kind, kw):
        return Resp(parsed=validation.Validation(
            summary="Нет требований к надёжности", score=140, verdict="needs_work",
            sections=[validation.SectionCheck(section="Введение", status="present", comment="")],
            findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 3",
                                         problem="«быстро»", suggestion="не дольше 2 с")],
            questions=["Сколько игроков в турнире?"]))
    fake_llm.script = script
    r = client.post("/api/requirements/validate", json={"project_id": p["id"], "requirements": "1. Быстро"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["score"] == 100 and v["standard"] == "gost19" and "19.201" in v["standard_title"]
    sent = dump(fake_llm.calls[-1][1])
    assert "Требования к надёжности" in sent and "У каждого требования есть номер" in sent and "1. Быстро" in sent
    assert client.post("/api/requirements/validate", json={"project_id": p["id"]}).status_code == 400


# ---------- authoring: a batch of actions in one turn, API steps, editing in the Studio ----------

def _tools(*calls) -> Resp:
    return Resp([SimpleNamespace(type="tool_use", id=f"toolu_b{i}_{uuid.uuid4().hex[:6]}", name=n, input=inp)
                 for i, (n, inp) in enumerate(calls)], stop_reason="tool_use")


def test_autopilot_batches_form_actions_and_records_api_checks(stand, project, fake_llm):
    from test_agent_invariants import _run
    turns = []

    def script(kind, kw):
        turns.append(kw)
        page = latest_page(kw)
        if len(turns) == 1:
            # One turn fills the whole form and submits it; the assertion after the click is skipped.
            return _tools(("fill", {"ref": ref_for(page, "Логин"), "text": "{{username}}", "press_enter": False,
                                    "description": "Ввести логин"}),
                          ("fill", {"ref": ref_for(page, "Пароль"), "text": "{{password}}", "press_enter": False,
                                    "description": "Ввести пароль"}),
                          ("click", {"ref": ref_for(page, "Войти"), "description": "Войти"}),
                          ("assert_text_present", {"text": "что угодно", "description": "лишнее"}))
        if len(turns) == 2:
            return tool("api_request", method="POST", path="/api/items", body='{"name": "Хлеб {{unique}}"}',
                        expect_status=200, expect_json="{}", save="{}", description="Добавить пункт через API")
        if len(turns) == 3:
            return tool("finish", status="passed", summary="ok", evidence="шаг 4: статус 200")
        return text("Готово.")

    fake_llm.script = script
    s = StudioSession(project, "Пакет", f"{stand.url}/login.html", "Войти и добавить пункт",
                      credentials={"username": USERNAME, "password": PASSWORD})
    s.starts_in_autopilot = True                 # as the server and the pipeline do for Auto-Pilot
    t = arun(_run(s, s.to_test))
    assert s.status == "done", s.chat
    assert len(turns) == 3                                       # 4 steps + finish in three requests
    assert turns[0]["tool_choice"]["disable_parallel_tool_use"] is False      # Auto-Pilot may batch
    second = json.dumps(turns[1]["messages"][-1], ensure_ascii=False)
    assert second.count('"tool_result"') == 4 and "Not executed: the page may have changed" in second
    assert "Response of the API request (HTTP 200)" in json.dumps(turns[2]["messages"][-1], ensure_ascii=False)
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill", "fill", "click", "api_request"]
    spec = json.loads(t["steps"][-1]["value"])
    assert spec == {"method": "POST", "url": "/api/items", "body": {"name": "Хлеб {{unique}}"}, "expect_status": 200}
    assert agent.has_assertion(t["steps"])                      # an API check proves the result
    assert s.timing["turns"] == 3 and s.timing["model"] >= 0 and s.timing["browser"] > 0
    assert PASSWORD not in json.dumps(t, ensure_ascii=False)


def test_a_long_authoring_conversation_starts_over_on_a_clean_context(stand, project, fake_llm, monkeypatch):
    """Past FRESH_TURNS (or FRESH_INPUT_TOKENS) the next request is short: the scenario, the steps
    recorded so far and the result of the last action - not the whole conversation."""
    from test_agent_invariants import _run
    monkeypatch.setattr(agent, "FRESH_TURNS", 2)
    turns = []

    def script(kind, kw):
        turns.append(kw)
        page = latest_page(kw)
        if len(turns) == 1:
            return tool("fill", ref=ref_for(page, "Логин"), text="{{username}}", press_enter=False,
                        description="Ввести логин")
        if len(turns) == 2:
            return tool("fill", ref=ref_for(page, "Пароль"), text="{{password}}", press_enter=False,
                        description="Ввести пароль")
        if len(turns) == 3:
            return tool("click", ref=ref_for(page, "Войти"), description="Войти")
        return tool("finish", status="blocked", summary="хватит", evidence="")

    fake_llm.script = script
    s = StudioSession(project, "Длинный", f"{stand.url}/login.html", "Войти",
                      credentials={"username": USERNAME, "password": PASSWORD})
    s.starts_in_autopilot = True
    arun(_run(s, s.to_test))
    assert s.status == "done", s.chat
    assert len(turns[1]["messages"]) == 3                       # the conversation grows...
    fresh = turns[2]["messages"]
    assert len(fresh) == 1 and fresh[0]["role"] == "user"       # ...then starts over with one request
    body = json.dumps(fresh, ensure_ascii=False)
    assert agent.FRESH_TASK in body and "Ввести логин" in body and "Ввести пароль" in body
    assert "Result of your last call" in body and '"tool_result"' not in body
    assert len(turns[3]["messages"]) == 3                       # and grows again from there
    assert PASSWORD not in body
    assert any("чистого контекста" in m["text"] for m in s.chat)


def test_edit_in_studio_replays_and_waits_for_the_person(stand, project, save_test, fake_llm):
    t = save_test("Список", [new_step("navigate", "Открыть", f"{stand.url}/list.html"),
                             new_step("assert_text_present", "Список открыт", "Хлеб")])
    s = StudioSession(project, t["name"], t["url"], "", base_steps=t["steps"], task=agent.EDIT_TASK)
    s.test_id = t["id"]

    async def go():
        try:
            await s.start()
            return s.status, [x["status"] for x in s.steps]
        finally:
            await s.close()
    status, statuses = arun(go())
    assert status == "idle" and statuses == ["passed", "passed"] and not fake_llm.calls   # no model until asked
    assert "Тест воспроизведён" in s.chat[-1]["text"]


def test_api_steps_are_exported_with_their_checks():
    from testgen import exporters
    step = new_step("api_request", "Создать клуб", json.dumps({
        "method": "POST", "url": "/api/clubs", "body": {"name": "Клуб {{unique}}"}, "expect_status": 201,
        "expect": {"$.status": "active", "$.courts": "4"}, "save": {"club_id": "$.id"}}, ensure_ascii=False))
    t = {"id": "t1", "project_id": "p", "name": "API клуба", "url": "https://app.test/", "scenario": "", "steps": [step]}
    code = exporters.to_playwright(t)
    compile(code, "test_api.py", "exec")
    assert "response = _api(page, 'POST', app_url + '/api/clubs'" in code and "expect=201" in code
    assert "_json_path(response.json(), '$.status')" in code and "json.dumps(field" in code
    assert "data['club_id'] = _json_path(response.json(), '$.id')" in code and "import json" in code
