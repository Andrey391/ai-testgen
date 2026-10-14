"""«Ждёт вас» (inbox.py): what waits for a person's decision, over the HTTP API; the notification of a waiting run."""
from __future__ import annotations

import datetime
import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import agent, analyses, auth, db, fs, inbox, knowledge, notify, pipeline, projects, storage, tasks
from testgen.repo import tests as tests_repo
from testgen.steps import new_step


@pytest.fixture(scope="module")
def client():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def _project(confirm: bool = False) -> dict:
    p = projects.create("Входящие " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    p["pipeline"]["requirements"]["confirm_model"] = confirm
    return projects.update(p["id"], {"pipeline": p["pipeline"]})


def _kinds(body: dict) -> dict:
    out: dict = {}
    for e in body["items"]:
        out.setdefault(e["kind"], []).append(e)
    return out


def test_an_empty_project_waits_for_nothing(client, open_studio):
    pid = _project()["id"]
    assert client.get(f"/api/projects/{pid}/inbox").json() == {"total": 0, "items": []}
    assert {"project_id": pid, "name": projects.get(pid)["name"], "total": 0} in client.get("/api/inbox").json()


def test_every_kind_of_decision_is_seen(client, open_studio, monkeypatch):
    p = _project(confirm=True)
    pid = p["id"]
    # A run in memory waits for the lifecycle; a finished one has an item that needs a person.
    waiting = {"id": "a1" * 5, "project_id": pid, "status": "awaiting_model", "created": 1, "waiting_since": 5,
               "feature": "Корзина", "items": []}
    monkeypatch.setitem(pipeline.JOBS, waiting["id"], types.SimpleNamespace(
        id=waiting["id"], project_id=pid, state=lambda: waiting))
    fs.write_json(pipeline._jobs_dir(pid) / f"{'b2' * 5}.json",
                  {"id": "b2" * 5, "project_id": pid, "status": "done", "created": 2, "finished": 3, "items": [
                      {"title": "Оплата", "status": "needs_attention", "test_id": None},
                      {"title": "Возврат", "status": "needs_attention", "test_id": None, "skipped": "ann"},
                      {"title": "Доставка", "status": "done", "test_id": "t1"}]})
    # Requirements with a blocking finding and a question; a minor one does not count.
    a = analyses.create(pid, "Покупатель оплачивает заказ")
    analyses.finish(pid, a["id"])
    analyses.set_review(pid, a["id"], {"at": 10, "verdict": "not_ready", "validation": {
        "findings": [{"severity": "major", "location": "1", "problem": "Нет статусов", "suggestion": "Добавить"},
                     {"severity": "minor", "location": "2", "problem": "Опечатка", "suggestion": ""}],
        "questions": ["Кто отменяет заказ?"]}, "lifecycle": {}})
    # The model: an update waits, the lifecycle is not confirmed.
    knowledge.save(pid, {"entities": [{"name": "Заказ", "lifecycle": "новый → оплачен"}],
                         "roles": [{"name": "Покупатель", "capabilities": "оплачивает"}]})
    knowledge.record(pid, {"entity": "Заказ", "name": "Заказ", "depends_on": ["Адрес"]}, "Studio: тест")
    # A test with self-healing proposals, an interrupted session, the user's overdue task and a task of another.
    t = storage.save({"project_id": pid, "name": "Вход", "url": p["base_url"], "scenario": "",
                      "steps": [new_step("navigate", "open", p["base_url"])],
                      "heal_proposals": [{"id": "x1", "step_id": "s1"}, {"id": "x2", "step_id": "s2"}]})
    storage.save({"project_id": pid, "name": "Без предложений", "url": p["base_url"], "scenario": "", "steps": [],
                  "heal_proposals": []})
    fs.write_json(agent._checkpoints(pid) / "sess01.json", {"id": "sess01", "name": "Регистрация", "steps": [],
                                                           "updated": 7})
    fs.write_json(agent._checkpoints(pid) / "sess02.json", {"id": "sess02", "name": "Из конвейера", "steps": [],
                                                           "origin": {"job": "b2" * 5}})
    today = datetime.date.today().isoformat()
    mine = tasks.create(pid, {"title": "Покрыть оплату", "assignee": auth.ANONYMOUS, "due": "2000-01-01"})
    tasks.create(pid, {"title": "Сегодня", "assignee": auth.ANONYMOUS, "due": today})
    tasks.create(pid, {"title": "Чужая", "assignee": "ivan", "due": "2000-01-01"})
    tasks.create(pid, {"title": "Без срока", "assignee": auth.ANONYMOUS})

    body = client.get(f"/api/projects/{pid}/inbox").json()
    k = _kinds(body)
    assert list(k) == list(inbox.KINDS)              # by kind, in this order
    [job] = k["job"]
    assert job["route"] == f"/p/{pid}/pipe/{waiting['id']}" and job["since"] == 5 and "жизненного цикла" in job["title"]
    [att] = k["attention"]
    assert att["count"] == 1 and att["route"] == f"/p/{pid}/pipe/{'b2' * 5}"
    [rv] = k["review"]
    assert rv["count"] == 2 and rv["route"] == f"/p/{pid}/req/{a['id']}"
    routes = {e["route"].rsplit("/", 1)[1]: e for e in k["model"]}
    assert routes["pending"]["count"] == 1 and "не подтверждён" in routes["overview"]["title"]
    [heal] = k["heal"]
    assert heal["count"] == 2 and heal["route"] == f"/p/{pid}/tests/{t['id']}"
    [sess] = k["session"]
    assert sess["route"] == f"/p/{pid}/studio/sess01"
    assert [e["route"].rsplit("/", 1)[1] for e in k["task"]] == [mine["id"], k["task"][1]["route"].rsplit("/", 1)[1]]
    assert "просрочена" in k["task"][0]["title"] and "сегодня" in k["task"][1]["title"]
    assert body["total"] == len(body["items"])
    assert {"project_id": pid, "name": p["name"], "total": body["total"]} in client.get("/api/inbox").json()

    # Decided: the entries go away.
    analyses.decide(pid, a["id"], ["d0", "vq0"], "dismissed", note="не про это")
    knowledge.resolve(pid, None, "reject")
    knowledge.confirm(pid, "ann")
    k = _kinds(client.get(f"/api/projects/{pid}/inbox").json())
    assert "review" not in k and "model" not in k


def test_the_inbox_answers_fast_on_a_thousand_tests(open_studio):
    pid = _project()["id"]
    rows = [tests_repo.Sql.row({"project_id": pid, "id": f"t{i:04d}", "name": f"Тест {i}", "steps": [],
                                "heal_proposals": [{"id": "p", "step_id": "s"}] if i % 100 == 0 else []})
            for i in range(1000)]
    with db.engine().begin() as c:
        c.execute(db.tests.insert(), rows)
    inbox.project_inbox(pid)            # warm: the first query of a process prepares the connection
    start = time.perf_counter()
    entries = inbox.project_inbox(pid)
    assert time.perf_counter() - start < 0.2
    assert sum(1 for e in entries if e["kind"] == "heal") == 10


def test_a_waiting_run_notifies_once(monkeypatch):
    p = _project()
    sent = []
    monkeypatch.setattr(notify, "event", lambda project, kind, **kw: sent.append((kind, kw["job"]["status"])))
    job = pipeline.Job(p, [], "текст", "", {})
    job._waiting("awaiting_selection")
    since = job.waiting_since
    job._waiting("awaiting_selection")
    assert job.waiting_since == since and sent == [("job_waiting", "awaiting_selection")]
    job.status = "running"
    job._waiting("awaiting_selection")       # the same kind of wait again: no repeat
    job._waiting("awaiting_model")
    assert sent == [("job_waiting", "awaiting_selection"), ("job_waiting", "awaiting_model")]
    assert job.state()["notified"] == ["awaiting_selection", "awaiting_model"]


def test_the_message_of_a_waiting_run(monkeypatch):
    monkeypatch.setenv("TESTGEN_PUBLIC_URL", "https://studio.example/")
    p = {"id": "p1", "name": "Магазин"}
    subject, text = notify.message(p, "job_waiting", job={"id": "j1", "status": "awaiting_model", "items": [{}, {}]})
    assert subject == "Магазин: конвейер ждёт решения"
    assert "подтверждения жизненного цикла" in text and "Сценариев: 2" in text
    assert 'href="https://studio.example/#/p/p1/pipe/j1"' in text
    off = p | {"notify": {"events": {"job_waiting": False}}}
    assert notify.message(off, "job_waiting", job={"id": "j1", "status": "awaiting_model"}) is None


def test_a_run_waits_in_the_inbox_however_many_newer_runs_there_are(client, open_studio):
    pid = _project()["id"]
    old = "c3" * 5
    fs.write_json(pipeline._jobs_dir(pid) / f"{old}.json",
                  {"id": old, "project_id": pid, "status": "awaiting_selection", "created": 1, "waiting_since": 2,
                   "items": []})
    for i in range(inbox.JOBS + 5):           # newer runs, saved after it
        fs.write_json(pipeline._jobs_dir(pid) / f"n{i:09d}.json",
                      {"id": f"n{i:09d}", "project_id": pid, "status": "done", "created": 10 + i, "items": []})
    owner = pipeline._job_elsewhere
    try:
        pipeline._job_elsewhere = lambda jid: jid == old     # another instance works on it: it really waits
        [job] = _kinds(client.get(f"/api/projects/{pid}/inbox").json())["job"]
    finally:
        pipeline._job_elsewhere = owner
    assert job["route"] == f"/p/{pid}/pipe/{old}" and job["since"] == 2


def test_the_inbox_reads_again_only_what_changed(open_studio, monkeypatch):
    p = _project(confirm=True)
    pid = p["id"]
    knowledge.save(pid, {"entities": [{"name": f"Сущность {i}", "lifecycle": "новая → готова", "group": f"Группа {i % 20}"}
                                      for i in range(600)],
                         "roles": [{"name": "Гость", "capabilities": "смотрит"}]})
    for i in range(30):
        a = analyses.create(pid, f"Требования {i}")
        analyses.finish(pid, a["id"])
    first = inbox.project_inbox(pid)
    counted = []
    monkeypatch.setattr(knowledge, "duplicates", lambda doc: counted.append(1) or [])
    start = time.perf_counter()
    assert inbox.project_inbox(pid) == first
    assert time.perf_counter() - start < 0.2 and not counted     # the model did not change: not counted again
    knowledge.confirm(pid, "ann")
    counted.clear()
    assert "model" not in _kinds({"items": inbox.project_inbox(pid)}) and counted == [1]   # changed: counted again


def test_the_waits_of_runs_are_kept_for_the_dashboard(monkeypatch):
    from testgen import metrics
    p = _project()
    monkeypatch.setattr(notify, "event", lambda *a, **kw: None)
    job = pipeline.Job(p, [], "текст", "", {})
    job._waiting("awaiting_model")
    job.waiting_since -= 600                     # 10 minutes for the model
    job._waiting("awaiting_selection")
    job.waiting_since -= 1800                    # then 30 minutes for the choice
    job.status = "running"
    assert [(w["status"], round((w["until"] - w["since"]) / 60)) for w in job.waits] ==         [("awaiting_model", 10), ("awaiting_selection", 30)]
    assert job.waiting_since is None and job.state()["waits"] == job.waits
    job.save()
    w = metrics.project_metrics(p["id"])["waits"]
    assert w == {"count": 2, "median_minutes": 20.0, "model_count": 1, "model_median_minutes": 10.0, "now": 0}
    resumed = pipeline.Job.resumed(projects.get(p["id"]), pipeline.get_job(job.id), {})
    assert resumed.waits == job.waits                # a restart keeps them
