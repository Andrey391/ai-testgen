"""The lifecycle of a test: steps the agent changed without a person send a regression test back to review, the
inbox shows the tests waiting for review, a module in use is not deleted."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import arun
from testgen import auth, inbox, pipeline, projects, storage
from testgen.steps import new_step


@pytest.fixture(scope="module")
def client():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def _project() -> dict:
    p = projects.create("Ревью " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    return projects.get(p["id"])


def _test(p: dict, name: str, status: str = "ready", steps: list | None = None) -> dict:
    return storage.save({"project_id": p["id"], "name": name, "url": p["base_url"], "scenario": name,
                         "status": status, "steps": steps or [new_step("navigate", "Открыть", p["base_url"])]})


class FakeSession:
    """The agent's session: finishes at once with an assertion added to the replayed steps."""

    def __init__(self, project, name, url, scenario, *, base_steps=None, **kw):
        self.id = uuid.uuid4().hex[:8]
        self.steps = list(base_steps or []) + [new_step("assert_visible", "Видна корзина")]
        self.finish_status = "passed"
        self.summary = ""

    def to_test(self) -> dict:
        return {"steps": self.steps}

    async def close(self, discard: bool = False) -> None:
        pass


@pytest.fixture
def agent_session(monkeypatch):
    monkeypatch.setattr(pipeline, "StudioSession", FakeSession)

    async def author(self, project, item, s, started=False):
        return "done"
    monkeypatch.setattr(pipeline.Job, "_author", author)


def test_refined_regression_test_goes_back_to_review(fake_llm, agent_session):
    p = _project()
    t = _test(p, "Корзина")
    job = pipeline.Job(p, [], "", "", {})
    item = {"title": "Корзина", "status": "pending"}
    out = arun(job._refine(p, item, {"title": "Скидка в корзине", "instructions": "Шаги"}, t))
    saved = storage.load(t["id"])
    assert out and len(saved["steps"]) == 2
    assert saved["status"] == "review" and "Скидка в корзине" in saved["review_reason"]
    assert storage.select(p["id"]) == []          # out of the regression suite until a person checks it


def test_strengthened_assertions_send_a_regression_test_to_review(fake_llm, agent_session, monkeypatch):
    p = _project()
    p["pipeline"]["verify"]["improve"] = True
    t = _test(p, "Оплата")
    draft = _test(p, "Черновик", status="draft")
    runs = []

    async def verify(project, test, log=None):
        runs.append(test["id"])
        weak = len(runs) % 2 == 1
        return {"status": "done", "killed": 1, "total": 2, "weak": weak,
                "mutants": [{"description": "Кнопка не нажимается", "result": "survived"}]}
    monkeypatch.setattr(pipeline.mutations, "verify", verify)
    job = pipeline.Job(p, [], "", "", {})
    for test in (t, draft):
        arun(job._verify(p, {"title": test["name"]}, storage.load(test["id"])))
    saved = storage.load(t["id"])
    assert len(saved["steps"]) == 2 and saved["status"] == "review"
    assert "усилены" in saved["review_reason"]
    # A draft is its author's work: it stays a draft.
    assert storage.load(draft["id"])["status"] == "draft" and "review_reason" not in storage.load(draft["id"])


def test_a_person_settles_the_review_and_the_reason_goes(client, open_studio, fake_llm, agent_session):
    p = _project()
    t = _test(p, "Профиль")
    arun(pipeline.Job(p, [], "", "", {})._refine(p, {"title": "x"}, {"title": "Аватар", "instructions": ""}, t))
    r = client.patch(f"/api/tests/{t['id']}/meta", json={"status": "ready"})
    assert r.status_code == 200 and r.json()["status"] == "ready" and "review_reason" not in r.json()


def test_the_inbox_shows_tests_waiting_for_review_not_drafts(client, open_studio):
    p = _project()
    pid = p["id"]
    _test(p, "Готов")
    _test(p, "Черновик", status="draft")
    assert not [e for e in inbox.project_inbox(pid) if e["kind"] == "test_review"]
    one = _test(p, "На ревью", status="review")
    [e] = [e for e in client.get(f"/api/projects/{pid}/inbox").json()["items"] if e["kind"] == "test_review"]
    assert e["count"] == 1 and "«На ревью»" in e["title"] and e["route"] == f"/p/{pid}/tests?status=review"
    _test(p, "Ещё на ревью", status="review")
    [e] = [e for e in inbox.project_inbox(pid) if e["kind"] == "test_review"]
    assert e["count"] == 2 and e["title"] == "Тесты ждут ревью"
    storage.update(one["id"], lambda x: x.update(status="ready"))
    [e] = [e for e in inbox.project_inbox(pid) if e["kind"] == "test_review"]
    assert e["count"] == 1


def test_a_module_in_use_is_not_deleted(client, open_studio):
    p = _project()
    module = storage.save({"project_id": p["id"], "name": "Вход", "url": p["base_url"], "role": "module",
                           "steps": [new_step("navigate", "Открыть", p["base_url"])]})
    call = new_step("use_module", "Войти", json.dumps({"module": module["id"]}))
    caller = _test(p, "Заказ", steps=[call])
    r = client.delete(f"/api/tests/{module['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["used_by"] == [{"id": caller["id"], "name": "Заказ"}]
    assert storage.load(module["id"])
    # The modules list counts the same callers.
    [m] = client.get(f"/api/projects/{p['id']}/blocks").json()["modules"]
    assert m["used_by"] == ["Заказ"]
    # The caller gone, the module goes.
    assert client.delete(f"/api/tests/{caller['id']}").json() == {"ok": True}
    assert client.delete(f"/api/tests/{module['id']}").json() == {"ok": True}
    assert storage.load(module["id"]) is None
