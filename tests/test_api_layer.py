"""API tests without a browser: the API catalog from the recorded traffic, the project's API address and
authorization, an API scenario written by the agent and replayed by the runner, mutations of its checks."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from fakes import dump, text, tool
from helpers import arun
from stand import PASSWORD, USERNAME
from testgen import agent, mutations, projects, runner, storage, traffic
from testgen.agent import StudioSession
from testgen.steps import new_step

CREDS = {"username": USERNAME, "password": PASSWORD}
LOGIN = {"method": "POST", "path": "/api/login", "body": '{"username": "{{username}}", "password": "{{password}}"}',
         "token": "$.token", "header": "Authorization"}


def _new_project() -> dict:
    return projects.create(f"API {uuid.uuid4().hex[:6]}", base_url="https://app.test")


def test_endpoints_group_paths_and_keep_only_names():
    entries = [
        {"method": "GET", "url": "https://app.test/api/orders/42?expand=items", "status": 200, "post_data": "",
         "body": '{"id": 42, "status": "new"}'},
        {"method": "GET", "url": "https://app.test/api/orders/43", "status": 404, "post_data": "",
         "body": '{"error": "not found"}'},
        {"method": "POST", "url": "https://app.test/api/orders", "status": 201, "post_data": '{"product": 1, "qty": 2}',
         "body": '[{"id": 7}]'},
        {"method": "GET", "url": "https://cdn.other.com/data.json", "status": 200, "post_data": "", "body": "{}"},
    ]
    eps = traffic.endpoints(entries, "https://app.test")

    assert [(e["method"], e["path"]) for e in eps] == [("POST", "/api/orders"), ("GET", "/api/orders/{id}")]
    post, get = eps
    assert post["request"] == ["product", "qty"] and post["response"] == ["[].id"] and post["statuses"] == [201]
    assert get["statuses"] == [200, 404] and get["query"] == ["expand"] and get["response"] == ["id", "status", "error"]
    assert "42" not in json.dumps(eps) and "not found" not in json.dumps(eps)        # names only, no values

    merged = traffic.merge(eps, [{"method": "GET", "path": "/api/orders/{id}", "count": 1, "statuses": [500],
                                  "query": [], "request": [], "response": ["total"]}])
    listing = traffic.catalog_text(merged)
    assert "GET /api/orders/{id} ?expand -> 200, 404, 500; response fields: id, status, error, total" in listing
    assert "POST /api/orders -> 201; request fields: product, qty; response fields: [].id" in listing


def test_api_settings_are_validated_and_the_token_stays_in_the_vault():
    pid = _new_project()["id"]
    p = projects.update_api(pid, {"base_url": "https://api.app.test/", "auth": "bearer"}, token="tok-123")

    assert p["api"]["base_url"] == "https://api.app.test" and projects.api_base(p) == "https://api.app.test"
    assert projects.api_token(pid) == "tok-123" and "tok-123" not in json.dumps(projects.get(pid))
    assert projects.api_base(projects.update_api(pid, {"base_url": ""})) == "https://app.test"   # the app's URL
    with pytest.raises(ValueError):
        projects.update_api(pid, {"base_url": "ftp://api.app.test"})
    assert projects.update_api(pid, {"auth": "anything"})["api"]["auth"] == "cookies"
    assert projects.api_token(pid) == "tok-123"                      # a save without a token keeps it
    projects.update_api(pid, {}, clear_token=True)
    assert projects.api_token(pid) == ""


def test_an_api_session_has_no_browser_tools():
    p = projects.get(_new_project()["id"])
    s = StudioSession(p, "Заказы", "https://app.test", "Создать заказ по API", engine="api")

    assert {t["name"] for t in s.tools} == {"api_request", "finish", "remember", "test_data"}
    assert s.system.startswith(agent.API_SYSTEM_PROMPT[:60]) and "DELETE is not allowed" in s.system
    assert agent.test_engine({"layer": "api"}) == "api" and agent.test_engine({"layer": "ui"}) == ""
    # a saved API test is edited, fixed and strengthened without a browser too
    assert StudioSession(p, "Заказы", "https://app.test", "", base_steps=[], engine="api").engine == "api"


def _script(plan):
    state = {"i": 0}

    def script(kind, kw):
        if state["i"] >= len(plan):
            return text("Готово.")
        name, inp = plan[state["i"]]
        state["i"] += 1
        return tool(name, **inp)
    return script


async def _drive(s: StudioSession) -> None:
    await s.start()
    s.set_autopilot(True)
    for _ in range(600):
        if s.status in ("done", "error") or (not s.autopilot and s.status == "idle"):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"Сессия не завершилась: {s.status}, {s.chat[-3:]}")


def _request(path: str, description: str, **extra) -> dict:
    return {"method": "GET", "path": path, "body": "", "expect_status": 0, "expect_json": "{}", "save": "{}",
            "description": description} | extra


def test_api_test_is_written_and_run_without_a_browser(stand, project, fake_llm):
    """A login request gives the token; the agent sees the responses, never the token or the password;
    the runner replays the test without a browser; mutations of the response are caught."""
    pid = project["id"]
    p = projects.update_api(pid, {"auth": "login", "login": LOGIN})
    fake_llm.script = _script([
        ("api_request", _request("/api/me", "Запросить профиль")),
        ("api_request", _request("/api/me", "Профиль принадлежит пользователю", expect_status=200,
                                 expect_json='{"$.user": "{{username}}"}')),
        ("finish", {"status": "passed", "summary": "Профиль доступен по токену", "evidence": "шаг 2"}),
    ])
    s = StudioSession(p, "Профиль", stand.url, "Профиль текущего пользователя по API", credentials=CREDS,
                      engine="api")

    async def go():
        try:
            await _drive(s)
            return s.save()[0], s.state()
        finally:
            await s.close()
    test, state = arun(go())

    assert s.finish_status == "passed", s.chat
    assert test["layer"] == "api" and [st["action"] for st in test["steps"]] == ["api_request", "api_request"]
    sent = dump(fake_llm.calls)
    assert "Response of the API request (HTTP 200)" in sent and r'{\"user\": \"demo\"}' in sent
    assert "Known endpoints" in sent and "API under test" in sent
    secrets = [PASSWORD, *stand.tokens]
    assert not any(x in sent or x in json.dumps(test) or x in json.dumps(state) for x in secrets)
    assert [x["status"] for x in state["api_log"]] == [200, 200]
    assert all(path.startswith("/api/") for _, path in stand.requests)          # no page was opened

    rep = arun(runner.run_test(storage.load(test["id"]), credentials=CREDS,
                               cfg={"self_heal": False, "analyze_failures": False, "trace": "always"}))
    assert rep["passed"] and rep["browser"] == "api" and not rep["trace"], rep["results"]
    assert not any(r.get("screenshot") for r in rep["results"])

    res = arun(mutations.verify(projects.get(pid), storage.load(test["id"])))
    assert res["status"] == "done" and res["total"] >= 2 and res["killed"] == res["total"], res["mutants"]
    assert {m["kind"] for m in res["mutants"]} == {"api_field", "api_status"}


def test_api_test_without_authorization_gets_401(stand, project):
    pid = project["id"]
    projects.update_api(pid, {"auth": "none"})
    step = new_step("api_request", "Профиль", json.dumps({"method": "GET", "url": "/api/me", "expect_status": 200}))
    test = storage.save({"project_id": pid, "name": "Профиль", "url": stand.url, "scenario": "", "layer": "api",
                         "steps": [step]})

    rep = arun(runner.run_test(test, credentials=CREDS))
    assert not rep["passed"] and "ответил 401" in rep["results"][0]["error"]


def test_people_add_edit_and_remove_endpoints(monkeypatch):
    pid = _new_project()["id"]
    seen = [{"method": "GET", "path": "/api/orders", "count": 3, "statuses": [200], "query": [], "request": [],
             "response": ["id"]},
            {"method": "GET", "path": "/api/internal", "count": 1, "statuses": [200], "query": [], "request": [],
             "response": []}]
    monkeypatch.setattr(traffic, "recorded", lambda p: [dict(x) for x in seen])

    traffic.save_endpoint(pid, {"method": "post", "path": "/api/orders", "request": "product, qty", "statuses": "201",
                                "note": "Создаёт заказ"})
    traffic.save_endpoint(pid, {"method": "GET", "path": "/api/orders", "response": ["id", "status"], "statuses": [200]},
                          old="GET /api/orders")
    assert traffic.delete_endpoint(pid, "GET /api/internal")
    cat = {f"{e['method']} {e['path']}": e for e in traffic.catalog(pid)}

    assert set(cat) == {"GET /api/orders", "POST /api/orders"}               # removed: not back from the traffic
    assert cat["GET /api/orders"]["source"] == "edited" and cat["GET /api/orders"]["count"] == 3
    assert cat["GET /api/orders"]["response"] == ["id", "status"]
    assert cat["POST /api/orders"]["source"] == "manual" and cat["POST /api/orders"]["request"] == ["product", "qty"]
    assert "POST /api/orders -> 201; request fields: product, qty — Создаёт заказ" in traffic.catalog_text(list(cat.values()))

    traffic.save_endpoint(pid, {"method": "GET", "path": "/api/v2/orders"}, old="GET /api/orders")      # renamed
    keys = {f"{e['method']} {e['path']}" for e in traffic.catalog(pid)}
    assert keys == {"GET /api/v2/orders", "POST /api/orders"}
    for bad in ({"method": "POST", "path": "/api/orders"}, {"method": "FETCH", "path": "/x"},
                {"method": "GET", "path": "api/x"}, {"method": "GET", "path": "/x", "statuses": "abc"}):
        with pytest.raises(ValueError):
            traffic.save_endpoint(pid, bad)
    assert traffic.delete_endpoint(pid, "POST /api/orders") and not traffic.delete_endpoint(pid, "POST /api/orders")