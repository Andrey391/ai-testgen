"""Filters of the «Тесты» list (GET /api/tests): text, status, health, layer, task - in SQL, flaky by the run history."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import auth, projects, runs, storage, tasks, traffic


@pytest.fixture(scope="module")
def client():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def _save(pid: str, name: str, **extra) -> dict:
    return storage.save({"project_id": pid, "name": name, "url": "http://127.0.0.1:1/" + extra.pop("path", ""),
                         "steps": [], **extra})


def _runs(test: dict, results: list[bool]) -> None:
    for i, ok in enumerate(results):
        r = runs.new(test, live=False)
        r.update(status="passed" if ok else "failed", passed=ok, started=100 + i)
        runs.finish(r)


def test_filters_of_the_test_list(client, open_studio):
    pid = projects.create("Фильтры " + uuid.uuid4().hex[:6])["id"]
    cart = _save(pid, "Корзина: оплата_100%", path="cart", status="draft",
                 last_run={"status": "failed", "at": 1}, tags=["smoke"])
    login = _save(pid, "Вход", path="login", quarantine={"on": True, "reason": "x"},
                  last_run={"status": "passed", "at": 1, "flaky": True})
    api = _save(pid, "Заказы API", layer="api", status="review", verify={"status": "done", "weak": True},
                last_run={"status": "error", "at": 1})
    heal = _save(pid, "Профиль", path="profile", heal_proposals=[{"id": "p1"}])
    flips = _save(pid, "Поиск", path="search")
    _runs(flips, [True, False, True, False])
    traffic.save(pid, cart["id"], [{"method": "GET", "url": "http://127.0.0.1:1/api/cart", "request_headers": {}, "post_data": None, "status": 200, "response_headers": {}, "body": "{}", "mime": "application/json"}])

    def ids(**params) -> set[str]:
        r = client.get("/api/tests", params={"project_id": pid, **params})
        assert r.status_code == 200, r.text
        return {t["id"] for t in r.json()}

    assert ids() == {cart["id"], login["id"], api["id"], heal["id"], flips["id"]}
    assert ids(q="корзина") == {cart["id"]}                 # the name, any case
    assert ids(q="/login") == {login["id"]}                 # the address
    assert ids(q="_100%") == {cart["id"]} and ids(q="%") == {cart["id"]}     # LIKE characters are text
    assert ids(status="draft") == {cart["id"]} and ids(status="review") == {api["id"]}
    assert ids(status="ready") == {login["id"], heal["id"], flips["id"]}
    assert ids(health="failing") == {cart["id"], api["id"]}
    assert ids(health="quarantined") == {login["id"]}
    assert ids(health="weak") == {api["id"]}
    assert ids(health="heal") == {heal["id"]}
    assert ids(health="flaky") == {login["id"], flips["id"]}     # passed on a re-run; flips every run
    assert ids(layer="api") == {api["id"]} and api["id"] not in ids(layer="ui")
    assert ids(tag="smoke", status="draft") == {cart["id"]} and ids(tag="smoke", health="weak") == set()

    task = tasks.create(pid, {"title": "Покрыть вход", "test_ids": [login["id"], heal["id"]]}, "ivan")
    assert ids(task=task["id"]) == {login["id"], heal["id"]}
    assert ids(task=task["id"], health="heal") == {heal["id"]}
    other = projects.create("Чужой " + uuid.uuid4().hex[:6])["id"]
    foreign = tasks.create(other, {"title": "Не наша"}, "ivan")
    assert ids(task=foreign["id"]) == set() and ids(task="nope") == set()

    for bad in ({"status": "bogus"}, {"health": "sick"}, {"layer": "db"}):
        assert client.get("/api/tests", params={"project_id": pid, **bad}).status_code == 400
    listed = client.get("/api/tests", params={"project_id": pid, "q": "Поиск"}).json()[0]
    assert listed["flip_rate"] == 1.0 and len(listed["recent"]) == 4 and not listed["has_traffic"]
    assert client.get("/api/tests", params={"project_id": pid, "q": "Корзина"}).json()[0]["has_traffic"]


def test_a_thousand_tests_are_listed_quickly(client, open_studio):
    pid = projects.create("Тысяча " + uuid.uuid4().hex[:6])["id"]
    from testgen import db
    from testgen.repo import tests as repo
    now = time.time()
    with db.engine().begin() as c:
        for i in range(1000):
            repo.Sql.put(c, {"project_id": pid, "id": f"t{i:04}", "name": f"Тест {i}", "url": "http://x/",
                             "updated": now - i, "steps": [{"action": "click"}] * 10,
                             "last_run": {"status": "failed" if i % 10 == 0 else "passed"}})
    client.get("/api/tests", params={"project_id": pid, "health": "failing"})      # warm up
    started = time.perf_counter()
    r = client.get("/api/tests", params={"project_id": pid, "health": "failing"})
    took = time.perf_counter() - started
    assert len(r.json()) == 100
    assert took < 0.2, f"{took:.3f} s"
