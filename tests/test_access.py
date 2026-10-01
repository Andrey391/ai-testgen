"""Roles in projects (stage 5.1): who sees and changes what, over the HTTP API and the MCP server."""
from __future__ import annotations

import json
import re
import types
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from helpers import arun
from testgen import auth, fs, mcp_server, pipeline, projects, runs, storage, suite, tasks
from testgen.steps import new_step

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def studio(monkeypatch):
    """Login on; users alice (owner of project A), vic (viewer of A), bob (nothing), admin."""
    import server
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ADMINS", {"root"})
    for u in ("alice", "vic", "bob", "root"):
        auth.set_password(u, "password-123")
    p = projects.create("Проект А " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1", owner="alice")
    projects.set_access(p["id"], "members", {"alice": "owner", "vic": "viewer"})
    projects.set_app_credentials(p["id"], "app-user", "app-secret")

    def client(user: str) -> TestClient:
        return TestClient(server.app, base_url=BASE, cookies={auth.COOKIE: auth.make_token(user)})

    yield types.SimpleNamespace(server=server, project=projects.get(p["id"]), client=client)
    auth.save_sso_settings({})


def _resources(studio) -> dict:
    """One of every kind of resource of project A."""
    p = studio.project
    t = storage.save({"project_id": p["id"], "name": "Тест А", "url": p["base_url"], "scenario": "",
                      "steps": [new_step("navigate", "open", p["base_url"])]})
    task = tasks.create(p["id"], {"title": "Задача А"})
    run = runs.new(t, "manual", user="alice")
    runs.files_dir(run).mkdir(parents=True, exist_ok=True)
    (runs.files_dir(run) / "trace.zip").write_bytes(b"PK")
    run.update(status="failed", passed=False, trace="trace.zip")
    runs.finish(run)
    s = suite.new(p, [t], user="alice")
    s.update(status="done", passed=True)
    suite.LIVE.pop(s["id"], None)
    suite.save(s)
    job_id = "ab" * 5
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{job_id}.json",
                  {"id": job_id, "project_id": p["id"], "status": "done", "created": 0, "items": []})
    sid = "sess-" + p["id"]
    studio.server.SESSIONS[sid] = types.SimpleNamespace(project=p, id=sid)
    return {"pid": p["id"], "test": t["id"], "task": task["id"], "run": run["id"], "suite": s["id"], "job": job_id,
            "session": sid}


def _url(path: str, ids: dict) -> str:
    kinds = {"/api/tasks/": "task", "/api/suites/": "suite", "/api/sessions/": "session", "/api/runs/": "run",
             "/api/jobs/": "job", "/api/tests/": "test"}
    first = next((ids[k] for prefix, k in kinds.items() if path.startswith(prefix)), None)

    def value(m: re.Match) -> str:
        name = m.group(1)
        if name == "pid":
            return ids["pid"]
        if name in ("tid", "rid", "sid", "jid") and first:
            return first
        return "1" if name in ("n", "index") else "x"
    return re.sub(r"\{(\w+)\}", value, path)


def test_no_access_is_404_everywhere(studio):
    ids = _resources(studio)
    try:
        bob = studio.client("bob")
        checked = 0
        for route in studio.server.app.routes:
            if not isinstance(route, APIRoute) or not any(route.path == prefix or route.path.startswith(prefix + "/")
                                                          for prefix in studio.server.RESOURCES):
                continue
            url = _url(route.path, ids)
            for method in route.methods:
                r = bob.request(method, url, json={})
                assert r.status_code == 404, f"{method} {url}: {r.status_code} {r.text[:200]}"
                checked += 1
        assert checked > 70
        # The project as a query or body parameter.
        assert bob.get(f"/api/tests?project_id={ids['pid']}").status_code == 404
        body = {"project_id": ids["pid"], "url": "x", "scenario": "x"}
        assert bob.post("/api/sessions", json=body).status_code == 404
        assert bob.post("/api/scenarios", json={"project_id": ids["pid"], "requirements": "x"}).status_code == 404
        assert bob.post("/api/requirements/fetch", json={"project_id": ids["pid"], "link": "x"}).status_code == 404
        assert ids["pid"] not in {p["id"] for p in bob.get("/api/projects").json()}
        # The same requests of the owner reach the resources.
        alice = studio.client("alice")
        assert alice.get(f"/api/runs/{ids['run']}/files/trace.zip").status_code == 200
        assert alice.get(f"/api/tests/{ids['test']}").status_code == 200
        assert alice.get(f"/api/jobs/{ids['job']}").status_code == 200
    finally:
        studio.server.SESSIONS.pop(ids["session"], None)


def test_roles_viewer_editor_owner(studio):
    ids = _resources(studio)
    try:
        pid, tid = ids["pid"], ids["test"]
        vic, alice, root = studio.client("vic"), studio.client("alice"), studio.client("root")
        view = vic.get(f"/api/projects/{pid}").json()
        assert view["role"] == "viewer" and view["app_username"] == "" and view["app_has_password"]
        assert alice.get(f"/api/projects/{pid}").json()["app_username"] == "app-user"
        assert vic.get(f"/api/tests/{tid}").status_code == 200
        r = vic.put(f"/api/tests/{tid}", json={"name": "Сломано"})
        assert r.status_code == 403 and "редактор" in r.json()["detail"]
        assert vic.get(f"/api/tests/{tid}/credentials").status_code == 403
        assert vic.post(f"/api/projects/{pid}/tasks", json={"title": "x"}).status_code == 403
        assert studio.server.route_role("POST", "/api/tests/{tid}/run") == "viewer"
        assert studio.server.route_role("POST", "/api/projects/{pid}/runs") == "viewer"

        # The owner makes vic an editor; an editor still cannot change the project settings.
        r = alice.put(f"/api/projects/{pid}/access", json={"visibility": "members",
                                                            "members": {"alice": "owner", "vic": "editor"}})
        assert r.status_code == 200 and {"user": "vic", "role": "editor"} in r.json()["members"]
        assert vic.put(f"/api/tests/{tid}", json={"name": "Новое имя"}).json()["name"] == "Новое имя"
        assert vic.put(f"/api/projects/{pid}", json={"name": "x"}).status_code == 403
        assert vic.put(f"/api/projects/{pid}/access", json={"members": {"vic": "owner"}}).status_code == 403
        # At least one owner stays; roles are checked.
        assert alice.put(f"/api/projects/{pid}/access", json={"members": {"vic": "editor"}}).status_code == 400
        assert alice.put(f"/api/projects/{pid}/access", json={"members": {"alice": "god"}}).status_code == 400

        # Studio admins own every project; "open" projects let every user edit.
        assert root.get(f"/api/projects/{pid}").json()["role"] == "owner"
        bob = studio.client("bob")
        assert bob.get(f"/api/projects/{pid}").status_code == 404
        alice.put(f"/api/projects/{pid}/access", json={"visibility": "open", "members": {"alice": "owner"}})
        assert bob.get(f"/api/projects/{pid}").json()["role"] == "editor"

        # A new project belongs to its author only.
        mine = bob.post("/api/projects", json={"name": "Проект Боба " + pid}).json()
        assert mine["role"] == "owner" and mine["visibility"] == "members"
        assert alice.get(f"/api/projects/{mine['id']}").status_code == 404
    finally:
        studio.server.SESSIONS.pop(ids["session"], None)


def test_directory_groups_and_admin_groups(studio):
    pid = studio.project["id"]
    bob, root = studio.client("bob"), studio.client("root")
    assert bob.get(f"/api/projects/{pid}").status_code == 404
    users = auth._users()
    users["bob"]["groups"] = ["qa-team"]
    auth._write_users(users)
    assert bob.put("/api/sso", json={"group_roles": []}).status_code == 403
    r = root.put("/api/sso", json={"group_roles": [{"group": "qa-team", "project": pid, "role": "viewer"}],
                                   "admin_groups": ["studio-admins"]})
    assert r.status_code == 200
    assert bob.get(f"/api/projects/{pid}").json()["role"] == "viewer"
    rule = {"group": "qa-team", "project": pid, "role": "viewer"}
    assert rule in bob.get(f"/api/projects/{pid}/access").json()["groups"]
    assert not bob.get("/api/auth/me").json()["is_admin"]
    users = auth._users()
    users["bob"]["groups"] = ["qa-team", "studio-admins"]
    auth._write_users(users)
    assert bob.get("/api/auth/me").json()["is_admin"]
    assert bob.get(f"/api/projects/{pid}").json()["role"] == "owner"


def test_mcp_tools_respect_roles(studio):
    ids = _resources(studio)
    studio.server.SESSIONS.pop(ids["session"], None)
    mcp = mcp_server.build(mcp_server.Backend())

    def call(user: str, tool: str, args: dict):
        async def go():
            token = mcp_server.CURRENT_USER.set(user)
            try:
                return await mcp.call_tool(tool, args)
            finally:
                mcp_server.CURRENT_USER.reset(token)
        return json.dumps(arun(go()), ensure_ascii=False, default=str)

    assert studio.project["name"] in call("vic", "list_projects", {})
    assert studio.project["name"] not in call("bob", "list_projects", {})
    for tool, args in (("list_tests", {"project": ids["pid"]}), ("export_test", {"test": ids["test"]}),
                       ("get_run", {"run_id": ids["run"]}), ("get_trace", {"run_id": ids["run"]}),
                       ("get_suite", {"suite_id": ids["suite"]}), ("run_test", {"test": "Тест А"})):
        with pytest.raises(Exception, match="не найден"):
            call("bob", tool, args)
    assert "def test_" in call("vic", "export_test", {"test": ids["test"]})
    with pytest.raises(Exception, match="редактор"):
        call("vic", "generate_test", {"project": ids["pid"], "scenario": "x"})
