"""Several Studio sessions of one person (usability plan, stage 3): whose session it is, «Открытые сессии», the
limit, where a session came from, what a test is linked to, the status a task's tests suggest."""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import agent, analyses, auth, fs, pipeline, projects, storage, tasks
from testgen.steps import new_step

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def studio(monkeypatch):
    """Login on; alice owns projects A and B, bob owns project C."""
    import server
    monkeypatch.setattr(auth, "ENABLED", True)
    for u in ("alice", "bob"):
        auth.set_password(u, "password-123")
    made = {}
    for key, owner in (("a", "alice"), ("b", "alice"), ("c", "bob")):
        p = projects.create(f"Сессии {key} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1", owner=owner)
        made[key] = projects.get(p["id"])
    opened: list[str] = []

    def session(p: dict, user: str, name: str = "Тест", **attrs) -> agent.StudioSession:
        s = agent.StudioSession(p, name, p["base_url"], "Сценарий")
        s.user = user
        for k, v in attrs.items():
            setattr(s, k, v)
        server.SESSIONS[s.id] = s
        opened.append(s.id)
        return s

    def client(user: str) -> TestClient:
        return TestClient(server.app, base_url=BASE, cookies={auth.COOKIE: auth.make_token(user)})

    yield types.SimpleNamespace(server=server, client=client, session=session, **made)
    for sid in opened:
        server.SESSIONS.pop(sid, None)


def test_the_user_is_kept_in_the_checkpoint(studio):
    s = studio.session(studio.a, "alice", task_id="", analysis_id="an1", scenario_id="sc1")
    s.checkpoint()
    cp = agent.load_checkpoint(studio.a["id"], s.id)
    assert (cp["user"], cp["analysis_id"], cp["scenario_id"]) == ("alice", "an1", "sc1")
    assert next(x for x in agent.list_checkpoints(studio.a["id"]) if x["id"] == s.id)["user"] == "alice"
    back = agent.restored(studio.a, cp)
    assert (back.user, back.analysis_id, back.scenario_id) == ("alice", "an1", "sc1")


def test_my_sessions_are_mine_and_in_my_projects(studio):
    a1 = studio.session(studio.a, "alice", "Корзина")
    b1 = studio.session(studio.b, "alice", "Оплата")
    studio.session(studio.c, "bob", "Чужой")
    piped = studio.session(studio.a, "alice", "Из конвейера")
    piped.origin = {"job": "ab" * 5}
    gone = studio.session(studio.a, "alice", "Закрыта")
    gone.status = "closed"
    body = studio.client("alice").get("/api/sessions?mine=1").json()
    assert body["limit"] == studio.server.SESSIONS_PER_USER
    assert [(x["id"], x["project"]) for x in body["items"]] == [(a1.id, studio.a["name"]), (b1.id, studio.b["name"])]
    assert [x["name"] for x in studio.client("bob").get("/api/sessions?mine=1").json()["items"]] == ["Чужой"]
    # A project taken away: its sessions are not listed (nor named).
    projects.set_access(studio.b["id"], "members", {"bob": "owner"})
    assert [x["id"] for x in studio.client("alice").get("/api/sessions?mine=1").json()["items"]] == [a1.id]


def test_the_limit_of_open_sessions(studio, monkeypatch):
    monkeypatch.setattr(studio.server, "SESSIONS_PER_USER", 1)
    studio.session(studio.a, "alice")
    t = storage.save({"project_id": studio.b["id"], "name": "Вход", "url": studio.b["base_url"], "scenario": "",
                      "steps": [new_step("navigate", "open", studio.b["base_url"])]})
    r = studio.client("alice").post(f"/api/tests/{t['id']}/edit", json={})
    assert r.status_code == 409 and r.headers["X-Session-Limit"] == "1" and "1 из 1" in r.json()["detail"]
    r = studio.client("alice").post("/api/sessions", json={"project_id": studio.a["id"], "url": "x", "scenario": ""})
    assert r.status_code == 409
    # Another person's sessions do not count.
    studio.session(studio.c, "bob")
    assert len(studio.client("bob").get("/api/sessions?mine=1").json()["items"]) == 1


def test_dirty_until_saved(studio):
    s = studio.session(studio.a, "alice")
    assert not s.dirty
    s.steps = [new_step("navigate", "open", studio.a["base_url"]) | {"status": "passed"}]
    assert s.dirty
    s.save()
    assert not s.dirty
    s.steps[0]["description"] = "Другое"
    assert s.dirty
    # A session over a saved test starts clean: its steps are the test's.
    t = storage.load(s.test_id)
    edit = agent.StudioSession(studio.a, t["name"], t["url"], "", base_steps=t["steps"], task=agent.EDIT_TASK)
    edit.steps = [dict(x, status="passed") for x in t["steps"]]
    assert not edit.dirty


def test_where_the_session_came_from(studio):
    pid = studio.a["id"]
    task = tasks.create(pid, {"title": "Покрыть корзину"})
    a = analyses.create(pid, "Корзина")
    sc = analyses.add_scenario(pid, a["id"], {"title": "Добавить товар", "instructions": "…"})
    alice = studio.client("alice")
    s = studio.session(studio.a, "alice", task_id=task["id"])
    assert alice.get(f"/api/sessions/{s.id}").json()["source"] == {
        "kind": "task", "id": task["id"], "title": "Покрыть корзину", "route": f"/p/{pid}/tasks/{task['id']}"}
    s = studio.session(studio.a, "alice", analysis_id=a["id"], scenario_id=sc["id"])
    src = alice.get(f"/api/sessions/{s.id}").json()["source"]
    assert (src["kind"], src["title"], src["route"]) == ("scenario", "Добавить товар", f"/p/{pid}/req/{a['id']}")
    assert alice.get(f"/api/sessions/{studio.session(studio.a, 'alice').id}").json()["source"] is None
    # Saving links the test to the scenario (kept in the session, not in the memory of the instance).
    s.steps = [new_step("navigate", "open", studio.a["base_url"]) | {"status": "passed"}]
    tid = alice.post(f"/api/sessions/{s.id}/save", json={}).json()["id"]
    assert tid in analyses.get(a["id"])["scenarios"][0]["test_ids"]


def test_what_a_test_is_linked_to(studio):
    pid = studio.a["id"]
    t = storage.save({"project_id": pid, "name": "Корзина", "url": studio.a["base_url"], "scenario": "",
                      "steps": [new_step("navigate", "open", studio.a["base_url"])]})
    other = storage.save({"project_id": pid, "name": "Другой", "url": studio.a["base_url"], "scenario": "", "steps": []})
    task = tasks.create(pid, {"title": "Покрыть корзину", "test_ids": [t["id"]]})
    a = analyses.create(pid, "# Корзина")
    sc = analyses.add_scenario(pid, a["id"], {"title": "Добавить товар", "instructions": "…"})
    analyses.link_test(pid, a["id"], sc["id"], t["id"])
    jid = "cd" * 5
    fs.write_json(pipeline._jobs_dir(pid) / f"{jid}.json", {"id": jid, "project_id": pid, "status": "done",
                                                            "created": 7, "items": [{"test_id": t["id"]}]})
    links = studio.client("alice").get(f"/api/tests/{t['id']}/links").json()
    assert links["tasks"] == [{"id": task["id"], "title": "Покрыть корзину", "status": "todo"}]
    assert [(x["analysis_id"], x["scenario_id"], x["title"]) for x in links["scenarios"]] == \
        [(a["id"], sc["id"], "Добавить товар")]
    assert links["jobs"] == [{"id": jid, "created": 7, "status": "done"}]
    assert studio.client("alice").get(f"/api/tests/{other['id']}/links").json() == \
        {"tasks": [], "scenarios": [], "jobs": []}
    assert studio.client("bob").get(f"/api/tests/{t['id']}/links").status_code == 404


def test_the_status_the_tests_of_a_task_suggest(studio):
    row = lambda status, last: {"status": status, "last_status": last}     # noqa: E731
    assert tasks.suggest("in_progress", []) == ""
    assert tasks.suggest("in_progress", [row("draft", "passed"), row("ready", "passed")]) == "review"
    assert tasks.suggest("in_progress", [row("draft", "passed"), row("ready", "failed")]) == ""
    assert tasks.suggest("in_progress", [row("ready", "passed"), row("ready", "passed")]) == "done"
    # In the suite, but failing or never run: neither «Готово» nor «На проверке».
    assert tasks.suggest("in_progress", [row("ready", "failed"), row("ready", "passed")]) == ""
    assert tasks.suggest("in_progress", [row("ready", "passed"), row("ready", None)]) == ""
    assert tasks.suggest("review", [row("draft", "passed")]) == ""        # not further than the task
    assert tasks.suggest("done", [row("ready", "passed")]) == ""
    pid = studio.a["id"]
    t = storage.save({"project_id": pid, "name": "Корзина", "url": studio.a["base_url"], "scenario": "", "steps": [],
                      "status": "draft", "last_run": {"status": "passed"}})
    task = tasks.create(pid, {"title": "Покрыть корзину", "test_ids": [t["id"]], "status": "in_progress"})
    (listed,) = [x for x in studio.client("alice").get(f"/api/projects/{pid}/tasks").json()["tasks"]
                 if x["id"] == task["id"]]
    assert listed["suggest"] == "review" and listed["status"] == "in_progress"
    assert listed["tests"] == [{"id": t["id"], "name": "Корзина", "status": "draft", "last_status": "passed"}]
