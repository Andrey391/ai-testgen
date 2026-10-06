"""Work that outlives a restart of the studio: Studio sessions are checkpointed after every step and
continued (or saved as a test) later; an interrupted pipeline run goes on from where it stopped."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from fakes import dump, latest_page, ref_for, text, tool
from helpers import arun
from stand import PASSWORD, USERNAME
from testgen import agent, auth, fs, pipeline, projects, storage
from testgen.agent import StudioSession
from testgen.steps import new_step


def _plan(steps):
    state = {"i": 0}

    def script(kind, kw):
        if state["i"] >= len(steps):
            return text("Жду указаний.")
        name, inp, element = steps[state["i"]]
        state["i"] += 1
        if element:
            inp = inp | {"ref": ref_for(latest_page(kw), element)}
        return tool(name, **inp)
    return script


async def _drive(s: StudioSession, timeout: float = 90) -> None:
    """Start in Auto-Pilot and wait until the agent finishes or waits for a person."""
    await s.start()
    s.set_autopilot(True)
    for _ in range(int(timeout * 10)):
        if s.status in ("done", "error") or (s.status == "idle" and s._auto_task.done()):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"Сессия не остановилась: {s.status} {s.chat}")


LOGIN = [("fill", {"text": "{{username}}", "press_enter": False, "description": "Ввести логин"}, "Логин"),
         ("fill", {"text": "{{password}}", "press_enter": False, "description": "Ввести пароль"}, "Пароль")]
FINISH = [("click", {"description": "Нажать «Войти»"}, "Войти"),
          ("assert_element_text", {"text": "Всего: 2", "description": "Открыт список"}, "Всего: 2"),
          ("finish", {"status": "passed", "summary": "Вошли", "evidence": "проверка результата"}, None)]


def _interrupted_session(stand, project, fake_llm, creds=None) -> str:
    """A session that recorded the login form and then "crashed" with the studio (no close(discard))."""
    fake_llm.script = _plan(LOGIN)
    s = StudioSession(project, "Вход", f"{stand.url}/login.html", "Войти и увидеть список",
                      credentials=creds or projects.app_credentials(project["id"]))

    async def go():
        try:
            await _drive(s)
        finally:
            await s.bs.close()          # the browser dies with the process; the checkpoint stays
    arun(go())
    return s.id


def test_session_checkpoint_is_restored_and_continued(stand, project, fake_llm):
    own = {"username": USERNAME, "password": PASSWORD + ""}
    projects.set_app_credentials(project["id"], "someone-else", "other-password")
    sid = _interrupted_session(stand, project, fake_llm, creds=own)

    cp = agent.load_checkpoint(project["id"], sid)
    assert [s["action"] for s in cp["steps"]] == ["navigate", "fill", "fill"]
    assert cp["own_credentials"] and PASSWORD not in json.dumps(cp, ensure_ascii=False)
    assert [c["id"] for c in agent.list_checkpoints(project["id"])] == [sid]

    # The studio restarted: the same session continues - steps replayed, then the agent goes on.
    fake_llm.script = _plan(FINISH)
    first_call = len(fake_llm.calls)
    s = agent.restored(project, cp)
    assert s.id == sid and s.credentials == own      # its own login came back from the vault

    async def go():
        try:
            await _drive(s)
            return s.save()[0]
        finally:
            await s.close(discard=True)
    t = arun(go())
    assert s.status == "done", s.chat
    assert "was interrupted" in dump(fake_llm.calls[first_call][1]["messages"])   # the agent knows it continues
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill", "fill", "click", "assert_element_text"]
    assert t["steps"][2]["value"] == "{{password}}"
    assert agent.load_checkpoint(project["id"], sid) is None       # closed by a person: gone
    assert storage.own_credentials(t) == own


def test_restored_session_keeps_its_steps_when_the_replay_fails(stand, project, fake_llm):
    sid = _interrupted_session(stand, project, fake_llm)
    cp = agent.load_checkpoint(project["id"], sid)
    # The application changed meanwhile: a recorded check no longer holds.
    cp["steps"].insert(1, new_step("assert_text_present", "Заголовок", "Такого текста нет") | {"status": "passed"})
    s = agent.restored(project, cp)

    async def go():
        try:
            await s.start()
            return s.status
        finally:
            await s.close()
    assert arun(go()) == "idle" and len(s.steps) == 4, s.chat
    assert "Не удалось воспроизвести" in s.chat[-1]["text"]


@pytest.fixture
def client(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    # Without `with`: the MCP app's lifespan runs once per process (test_server_mcp.py).
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_interrupted_sessions_over_the_api(client, stand, project, fake_llm):
    pid = project["id"]
    sid = _interrupted_session(stand, project, fake_llm)
    listed = client.get(f"/api/projects/{pid}/sessions").json()
    assert [(c["id"], c["steps"]) for c in listed] == [(sid, 3)]

    # Saved as a test right away (no browser), twice -> the same test.
    t = client.post(f"/api/projects/{pid}/sessions/{sid}/save", json={}).json()
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill", "fill"] and t["status"] == "draft"
    assert "проверки" in t["warnings"][-1]
    again = client.post(f"/api/projects/{pid}/sessions/{sid}/save", json={}).json()
    assert again["id"] == t["id"] and len(storage.all_tests(pid)) == 1

    # Continued in Studio: live again, so it leaves the list of interrupted ones.
    fake_llm.script = _plan(FINISH)
    assert client.post(f"/api/projects/{pid}/sessions/{sid}/restore", json={}).json() == {"id": sid}
    for _ in range(400):
        st = client.get(f"/api/sessions/{sid}").json()
        if st["status"] in ("awaiting_approval", "idle", "error"):
            break
        time.sleep(0.25)
    assert st["status"] == "awaiting_approval" and len(st["steps"]) == 3, st["chat"]
    assert client.get(f"/api/projects/{pid}/sessions").json() == []
    assert client.delete(f"/api/projects/{pid}/sessions/{sid}").status_code == 409
    client.delete(f"/api/sessions/{sid}")
    assert agent.load_checkpoint(pid, sid) is None
    assert client.post(f"/api/projects/{pid}/sessions/{sid}/restore", json={}).status_code == 404


def test_pipeline_run_resumes_where_it_stopped(stand, project, fake_llm):
    p = projects.get(project["id"])
    p["pipeline"]["authoring"]["autopilot"] = True
    for stage in ("run", "verify", "publish"):
        p["pipeline"][stage]["enabled"] = False
    projects.save(p)
    p = projects.get(p["id"])
    sid = _interrupted_session(stand, p, fake_llm)
    done = storage.save({"project_id": p["id"], "name": "Готовый", "url": stand.url, "scenario": "", "steps": []})
    scenario = {"title": "Вход", "type": "", "priority": "high", "preconditions": "", "expected_result": "",
                "instructions": "Войти и увидеть список", "gherkin": ""}
    item = pipeline.Job._item(scenario) | {"status": "authoring", "session_id": sid}
    finished = pipeline.Job._item(scenario | {"title": "Готовый"}) | {"status": "done", "test_id": done["id"]}
    jid = "cd" * 5
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{jid}.json", {
        "id": jid, "project_id": p["id"], "links": [], "text": "Требования", "url": f"{stand.url}/login.html",
        "user": "alice", "explore": False, "cases": None, "status": "running", "stage": "authoring", "created": 1,
        "finished": None, "log": [], "requirements": [{"source": "текст", "title": "Требования", "chars": 10}],
        "feature": "Вход", "assumptions": [], "scenarios": [scenario, scenario | {"title": "Готовый"}],
        "items": [item, finished], "error": "", "usage": {}})
    j = pipeline.get_job(jid)
    assert j["status"] == "error" and "Продолжить" in j["error"]       # the restart is seen

    fake_llm.script = _plan(FINISH)
    job = pipeline.Job.resumed(p, j, {}, user="bob")
    arun(job.run())
    assert job.status == "done", (job.error, job.log)
    first, second = job.items
    assert first["status"] == "done" and first["session_id"] is None, first
    t = storage.load(first["test_id"])
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill", "fill", "click", "assert_element_text"]
    assert second["status"] == "done" and second["test_id"] == done["id"]
    assert agent.load_checkpoint(p["id"], sid) is None
    saved = pipeline.get_job(jid)
    assert saved["status"] == "done" and saved["user"] == "alice" and saved["text"] == "Требования"
    assert any("продолжен" in line["text"] for line in saved["log"])
