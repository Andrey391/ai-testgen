"""The studio's HTTP API end to end (runs, review, mocks, suites, export) and the MCP server."""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from helpers import arun
from testgen import auth, mcp_server, storage, traffic
from testgen.steps import new_step


@pytest.fixture(scope="module")
def client():
    import server
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def _wait(client, url, key="status", timeout=120):
    for _ in range(timeout * 4):
        r = client.get(url).json()
        if r[key] != "running":
            return r
        time.sleep(0.25)
    raise AssertionError(f"still running: {url}")


def test_run_history_files_and_tags(client, open_studio, stand, project, save_test):
    t = save_test("Форма", [new_step("navigate", "open", f"{stand.url}/form.html"),
                            new_step("assert_text_present", "title", "Нет такого")])
    p = client.get(f"/api/projects/{project['id']}").json()
    p["pipeline"]["run"].update(analyze_failures=False, retry_failed=False)
    client.put(f"/api/projects/{project['id']}", json={"pipeline": p["pipeline"]})

    assert client.patch(f"/api/tests/{t['id']}/meta", json={"tags": ["Smoke", "ui"]}).json()["tags"] == ["smoke", "ui"]
    assert client.get(f"/api/projects/{project['id']}/tags").json() == ["smoke", "ui"]
    rid = client.post(f"/api/tests/{t['id']}/run", json={}).json()["id"]
    run = _wait(client, f"/api/runs/{rid}")
    assert run["status"] == "failed" and "screenshot" not in json.dumps(run["results"])
    assert run["trace"] == "trace.zip"
    r = client.get(f"/api/runs/{rid}/files/trace.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert client.get(f"/api/runs/{rid}/files/..%2Fsecret").status_code == 404
    shot = run["results"][0]["shot"]
    assert client.get(f"/api/runs/{rid}/files/{shot}").headers["content-type"] == "image/jpeg"
    listed = next(x for x in client.get(f"/api/tests?project_id={project['id']}&tag=smoke").json())
    assert listed["recent"][0]["status"] == "failed"
    assert client.get(f"/api/tests/{t['id']}/runs").json()[0]["id"] == rid

    q = client.patch(f"/api/tests/{t['id']}/meta", json={"quarantine": True, "reason": "flaky"}).json()
    assert q["quarantine"]["on"] and q["quarantine"]["reason"] == "flaky"


def test_heal_review_endpoints(client, open_studio, project, save_test):
    old = [{"kind": "testid", "value": "add-btn"}]
    new = [{"kind": "testid", "value": "append-btn"}]
    step = new_step("click", "Добавить", locator=old)
    t = save_test("Ревью", [step], heal_proposals=[
        {"id": "p1", "step_id": step["id"], "description": "Добавить", "action": "click", "old": old, "new": new,
         "reason": "та же кнопка", "screenshot": "", "run_id": "", "at": 0},
        {"id": "p2", "step_id": step["id"], "description": "Добавить", "action": "click", "old": old,
         "new": [{"kind": "css", "value": "#other"}], "reason": "", "screenshot": "", "run_id": "", "at": 0}])
    r = client.post(f"/api/tests/{t['id']}/proposals/p2/reject").json()
    assert r["steps"][0]["heal_rejected"] == [[{"kind": "css", "value": "#other"}]]
    r = client.post(f"/api/tests/{t['id']}/proposals/p1/accept").json()
    assert r["steps"][0]["locator"] == new and r["steps"][0]["healed"] and not r["heal_proposals"]
    assert [x["decision"] for x in r["heal_log"]] == ["reject", "accept"]
    assert client.post(f"/api/tests/{t['id']}/proposals/p1/accept").status_code == 404


def test_mock_from_traffic_and_exports(client, open_studio, stand, project, save_test):
    t = save_test("Список", [new_step("navigate", "open", f"{stand.url}/list.html")])
    traffic.save(project["id"], t["id"], [{
        "method": "GET", "url": f"{stand.url}/api/items", "request_headers": {}, "post_data": "", "status": 200,
        "response_headers": {}, "mime": "application/json", "body": '[{"name": "Мок"}]', "at": 0, "step": 0}],
        stand.url)
    assert client.get(f"/api/tests/{t['id']}/traffic").json()[0]["status"] == 200
    steps = client.post(f"/api/tests/{t['id']}/mock", json={"index": 0}).json()["steps"]
    assert steps[0]["action"] == "mock_route" and json.loads(steps[0]["value"])["body"] == '[{"name": "Мок"}]'
    assert "def test_01_get_api_items" in client.get(f"/api/tests/{t['id']}/export?format=api").text
    assert client.get(f"/api/tests/{t['id']}/export?format=har").json()["log"]["version"] == "1.2"
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{project['id']}/export").content))
    assert "conftest.py" in z.namelist() and any(n.startswith("tests/test_") for n in z.namelist())
    assert "page.route(" in z.read(next(n for n in z.namelist() if n.startswith("tests/"))).decode()


def test_suite_endpoint_and_junit(client, open_studio, stand, project, save_test):
    save_test("A", [new_step("navigate", "open", f"{stand.url}/form.html")], tags=["smoke"])
    save_test("B", [new_step("navigate", "open", f"{stand.url}/list.html")], tags=["smoke"])
    sid = client.post(f"/api/projects/{project['id']}/runs", json={"tags": ["smoke"]}).json()["id"]
    s = _wait(client, f"/api/suites/{sid}")
    assert s["passed"] and s["summary"]["passed"] == 2
    xml = client.get(f"/api/suites/{sid}/junit").text
    assert 'tests="2"' in xml and 'failures="0"' in xml
    assert client.post(f"/api/projects/{project['id']}/runs", json={"tags": ["nope"]}).status_code == 400


def test_dashboard_metrics(client, open_studio, stand, project, save_test):
    good = save_test("A", [new_step("navigate", "open", f"{stand.url}/form.html")],
                     external={"testit": {"work_item_id": "wi-1", "case_id": "wi-1"}},
                     verify={"status": "done", "score": 0.8})
    save_test("B", [new_step("navigate", "open", f"{stand.url}/list.html")], quarantine={"on": True})
    save_test("M", [], role="module")
    sid = client.post(f"/api/projects/{project['id']}/runs", json={}).json()["id"]
    _wait(client, f"/api/suites/{sid}")
    m = client.get(f"/api/projects/{project['id']}/metrics").json()
    assert m["tests"] == 2 and m["automation"]["linked_cases"] == 1          # modules do not count
    assert m["quality"]["mutation_score"] == 0.8 and m["stability"]["quarantined"] == 1
    assert m["saved"]["runs"] == 2 and m["saved"]["hours"] == round(2 * 5 / 60, 1)
    assert m["regression"]["last_minutes"] is not None and m["stability"]["pass_rate"] == 1.0
    assert storage.load(good["id"])


def test_tokens_and_mcp_over_http(client, monkeypatch, project):
    monkeypatch.setattr(auth, "ENABLED", True)
    auth.set_password("ide-user", "password-123")
    token, rec = auth.create_api_token("ide-user", "IDE")
    assert auth.user_for_api_token(token) == "ide-user" and auth.user_for_api_token(token + "x") is None
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    assert client.post("/mcp", json=init, headers=headers).status_code == 401
    auth_h = headers | {"Authorization": f"Bearer {token}"}
    r = client.post("/mcp", json=init, headers=auth_h)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "ai-testgen"
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth_h).json()
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"generate_test", "run_test", "list_failures", "export_test", "get_trace"} <= names
    assert not any("delete" in n for n in names)
    call = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "list_projects", "arguments": {}}}, headers=auth_h).json()
    assert project["name"] in json.dumps(call, ensure_ascii=False)
    auth.delete_api_token("ide-user", rec["id"])
    assert client.post("/mcp", json=init, headers=auth_h).status_code == 401


def test_token_endpoints(client, open_studio):
    created = client.post("/api/auth/tokens", json={"name": "Cursor"}).json()
    assert created["token"].startswith("tg_") and "hash" not in created
    assert any(t["id"] == created["id"] for t in client.get("/api/auth/tokens").json())
    assert client.delete(f"/api/auth/tokens/{created['id']}").json()["ok"]


def test_mcp_tools_in_process(stand, project, save_test):
    t = save_test("Экспорт", [new_step("navigate", "open", f"{stand.url}/form.html")],
                  last_run={"status": "failed", "at": 1, "failed_step": "open", "error": "boom", "run_id": "r"})
    mcp = mcp_server.build(mcp_server.Backend())

    async def go():
        code = await mcp.call_tool("export_test", {"test": "Экспорт", "project": project["name"]})
        failures = await mcp.call_tool("list_failures", {"project": project["id"]})
        return code, failures
    code, failures = arun(go())
    assert "def test_" in json.dumps(code, ensure_ascii=False, default=str)
    assert "boom" in json.dumps(failures, ensure_ascii=False, default=str)
    with pytest.raises(Exception):
        arun(mcp.call_tool("run_test", {"test": "нет такого"}))
    assert storage.load(t["id"])


def test_model_connection_is_a_project_setting(client, open_studio, project, fake_llm):
    pid = project["id"]
    base = client.get(f"/api/projects/{pid}").json()["llm"]
    assert base["model"] == "test-model" and not base["key_set"]

    # The key is stored on the server and never comes back.
    p = client.put(f"/api/projects/{pid}/llm", json={"api_key": "sk-secret-123", "effort": "high"}).json()
    assert p["llm"]["key_set"] and p["llm"]["effort"] == "high" and "sk-secret-123" not in json.dumps(p)

    # The check lists the models the connection offers and remembers them.
    fake_llm.model_list = [{"id": "test-model", "display_name": "Test", "capabilities": {
        "effort": {"supported": True, "low": {"supported": True}, "medium": {"supported": True},
                   "high": {"supported": False}}}}]
    r = client.post(f"/api/projects/{pid}/llm/test").json()
    assert r["models"] == [{"id": "test-model", "name": "Test", "efforts": ["low", "medium"], "missing": []}]
    llm_view = client.get(f"/api/projects/{pid}").json()["llm"]
    assert llm_view["check"]["ok"] and llm_view["models"][0]["id"] == "test-model"

    # Without a model nothing that needs one starts.
    client.put(f"/api/projects/{pid}/llm", json={"model": ""})
    r = client.post("/api/sessions", json={"project_id": pid, "url": "http://127.0.0.1:1", "scenario": "Войти"})
    assert r.status_code == 400 and "Модель не настроена" in r.json()["detail"]
    r = client.post("/api/scenarios", json={"project_id": pid, "requirements": "Вход по логину"})
    assert r.status_code == 400
    client.delete(f"/api/projects/{pid}/llm/key")
    assert not client.get(f"/api/projects/{pid}").json()["llm"]["key_set"]
