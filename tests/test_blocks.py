"""Reused steps (blocks.py): actions shared by tests run as modules, never as copies, and never contain
assertions; the agent reuses the actions of a stable test in one turn, and Auto-Pilot is told it may batch."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from fakes import latest_page, ref_for, tool
from helpers import arun
from stand import PASSWORD, USERNAME
from testgen import agent, auth, blocks, projects, storage
from testgen.agent import StudioSession
from testgen.steps import new_step

BASE = "http://127.0.0.1:8765"


def _project(base_url: str = "http://127.0.0.1:1") -> dict:
    p = projects.create(f"Блоки {uuid.uuid4().hex[:6]}", base_url=base_url)
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    return projects.get(p["id"])


def _step(action: str, description: str, value: str = "", testid: str = "") -> dict:
    return new_step(action, description, value, [{"kind": "testid", "value": testid}] if testid else None) | {
        "status": "passed"}


def _login(base: str, password: str = "{{password}}") -> list[dict]:
    return [_step("navigate", "Открыть вход", f"{base}/login.html"),
            _step("fill", "Ввести логин", "{{username}}", "username"),
            _step("fill", "Ввести пароль", password, "password"),
            _step("click", "Нажать 'Войти'", "", "login-btn")]


def _test(p: dict, name: str, steps: list[dict], **extra) -> dict:
    return storage.save({"project_id": p["id"], "name": name, "url": p["base_url"], "scenario": name,
                         "steps": steps, "status": "ready", "last_run": {"status": "passed"}, **extra})


def test_repeated_actions_become_a_module_without_assertions_and_tests_call_it():
    p = _project()
    base = p["base_url"]
    a = _test(p, "Список после входа", _login(base, "{{password}}") + [_step("assert_text_present", "Виден список", "Список")])
    b = _test(p, "Неверный пароль", _login(base, "wrong") + [_step("assert_text_present", "Ошибка", "Неверный")])
    # A test in quarantine or with a failed last run is no source of reusable steps.
    _test(p, "Карантин", _login(base), quarantine={"on": True})
    _test(p, "Упал", _login(base), last_run={"status": "failed"})

    found = blocks.candidates(p["id"])
    assert len(found) == 1
    c = found[0]
    assert [s["action"] for s in c["steps"]] == ["navigate", "fill", "fill", "click"]   # no assertion in it
    assert c["params"] == 1 and {t["id"] for t in c["tests"]} == {a["id"], b["id"]}
    assert c["section"] == "/login.html"

    res = blocks.accept(p["id"], c["key"], "Войти")
    m = res["module"]
    assert res["replaced"] == 2 and m["role"] == "module" and m["start_path"] == ""
    assert not any(s["action"].startswith("assert") for s in m["steps"])
    assert m["steps"][2]["value"] == "{{params.password}}"
    for t, own in ((a, "{{password}}"), (b, "wrong")):
        steps = storage.load(t["id"])["steps"]
        assert [s["action"] for s in steps] == ["use_module", "assert_text_present"]   # a call, not a copy
        assert json.loads(steps[0]["value"]) == {"module": m["id"], "params": {"password": own}}
    assert blocks.candidates(p["id"]) == []                       # the module covers the chain now


def test_a_dismissed_chain_is_not_offered_again():
    p = _project()
    for name in ("Один", "Два"):
        _test(p, name, _login(p["base_url"]) + [_step("assert_url_contains", "Список", "list")])
    key = blocks.candidates(p["id"])[0]["key"]
    blocks.dismiss(p["id"], key)
    assert blocks.candidates(p["id"]) == []


def test_offer_lists_runs_of_actions_of_close_tests_and_chain_refuses_assertions():
    p = _project()
    src = _test(p, "Войти и открыть список", _login(p["base_url"]) + [_step("assert_text_present", "Список", "Список")])
    _test(p, "Совсем другое", [_step("navigate", "Открыть", p["base_url"]), _step("click", "Клик", "", "x")])
    found = blocks.offer(p["id"], "Войти и добавить пункт")
    assert [c["id"] for c in found["chains"]] == [f"{src['id']}:1-4"]
    assert "reuse_steps" in blocks.note(found) and "Нажать 'Войти'" in blocks.note(found)
    t, a, b = blocks.chain(p["id"], f"{src['id']}:1-4", count=2)
    assert (a, b) == (0, 2)
    with pytest.raises(ValueError):
        blocks.chain(p["id"], f"{src['id']}:1-5")                 # step 5 is an assertion
    other = _project()
    with pytest.raises(ValueError):
        blocks.chain(other["id"], f"{src['id']}:1-4")             # another project's test


def test_blocks_api(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url=BASE)
    p = _project()
    for name in ("Один", "Два"):
        _test(p, name, _login(p["base_url"]) + [_step("assert_url_contains", "Список", "list")])
    d = client.get(f"/api/projects/{p['id']}/blocks").json()
    assert len(d["candidates"]) == 1 and d["modules"] == []
    key = d["candidates"][0]["key"]
    r = client.post(f"/api/projects/{p['id']}/blocks/{key}/accept", json={"name": "Вход"})
    assert r.status_code == 200 and r.json()["replaced"] == 2
    d = client.get(f"/api/projects/{p['id']}/blocks").json()
    assert d["candidates"] == [] and d["modules"][0]["name"] == "Вход" and len(d["modules"][0]["used_by"]) == 2
    assert client.post(f"/api/projects/{p['id']}/blocks/{key}/accept", json={}).status_code == 404


# ---------- the agent ----------

def test_agent_reuses_the_actions_of_a_stable_test_as_a_module(stand, project, fake_llm):
    from test_agent_invariants import _run
    src = _test(project, "Войти в список", _login(stand.url) + [_step("assert_text_present", "Список", "Список")])
    turns = []

    def script(kind, kw):
        turns.append(kw)
        if len(turns) == 1:
            return tool("reuse_steps", run=f"{src['id']}:1-4", count=0, description="Войти в приложение")
        if len(turns) == 2:
            return tool("assert_url_contains", text="list", description="Открыт список")
        return tool("finish", status="passed", summary="ok", evidence="шаг 3")

    fake_llm.script = script
    s = StudioSession(project, "Войти и проверить список", f"{stand.url}/index.html", "Войти и открыть список",
                      credentials={"username": USERNAME, "password": PASSWORD})
    s.starts_in_autopilot = True
    t = arun(_run(s, s.to_test))
    assert s.status == "done", s.chat
    first = json.dumps(turns[0], ensure_ascii=False)
    assert f"{src['id']}:1-4" in first and "reuse_steps" in first          # the agent sees the run
    assert agent.BATCH_NOTE.strip() in json.dumps(turns[0]["system"], ensure_ascii=False)
    assert [x["action"] for x in t["steps"]] == ["navigate", "use_module", "assert_url_contains"]
    mid = json.loads(t["steps"][1]["value"])["module"]
    module = storage.load(mid)
    assert module["role"] == "module" and [x["action"] for x in module["steps"]] == ["navigate", "fill", "fill", "click"]
    assert [x["action"] for x in storage.load(src["id"])["steps"]] == ["use_module", "assert_text_present"]
    assert "Reused" in json.dumps(turns[1]["messages"][-1], ensure_ascii=False)
    assert PASSWORD not in json.dumps(t, ensure_ascii=False)


def test_test_data_goes_along_with_actions_and_a_reload_of_the_same_page_is_refused(stand, project, fake_llm):
    from test_agent_invariants import _run
    from fakes import Resp
    from types import SimpleNamespace
    turns = []

    def calls(*items):
        return Resp([SimpleNamespace(type="tool_use", id=f"toolu_t{len(turns)}_{i}", name=n, input=inp)
                     for i, (n, inp) in enumerate(items)], stop_reason="tool_use")

    def script(kind, kw):
        turns.append(kw)
        page = latest_page(kw)
        if len(turns) == 1:
            return tool("navigate", url=f"{stand.url}/login.html", description="Открыть вход снова")
        if len(turns) == 2:
            return calls(("test_data", {"entity": "Пользователь", "name": "demo", "details": "", "state": "",
                                        "lifecycle": "", "create": "", "role": "", "depends_on": "", "login": "",
                                        "password": "", "fulfills": "", "created": False}),
                         ("fill", {"ref": ref_for(page, "Логин"), "text": "{{username}}", "press_enter": False,
                                   "description": "Ввести логин"}))
        return tool("finish", status="blocked", summary="хватит", evidence="")

    fake_llm.script = script
    s = StudioSession(project, "Вход", f"{stand.url}/login.html", "Войти",
                      credentials={"username": USERNAME, "password": PASSWORD})
    s.starts_in_autopilot = True
    t = arun(_run(s, s.to_test))
    assert "already on this page" in json.dumps(turns[1]["messages"][-1], ensure_ascii=False)
    third = json.dumps(turns[2]["messages"][-1], ensure_ascii=False)
    assert third.count('"tool_result"') == 2 and "Not executed" not in third    # the record and the action
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill"]
    assert all(x.get("page") for x in t["steps"][1:])                  # where each step starts
