"""Scheduled suite runs: the next time of a schedule, the turns tick() takes, the HTTP API."""
from __future__ import annotations

import datetime as dt
import time
import uuid
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from testgen import auth, projects, schedules, storage, suite
from testgen.steps import new_step

MSK = ZoneInfo("Europe/Moscow")


def at(*args) -> float:
    return dt.datetime(*args, tzinfo=MSK).timestamp()


@pytest.fixture
def proj():
    return projects.create(f"Расписание {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")


@pytest.fixture
def ready_test(proj):
    return storage.save({"project_id": proj["id"], "name": "Вход", "url": proj["base_url"], "status": "ready",
                         "tags": ["smoke"], "steps": [new_step("navigate", "open", proj["base_url"])]})


def test_next_time_daily_and_interval():
    s = schedules.clean({"days": [0, 1, 2, 3, 4], "times": ["14:00", "02:00"], "tz": "Europe/Moscow"})
    assert s["name"] == "пн–пт в 02:00, 14:00"
    # Friday 2026-10-30 10:00 -> Friday 14:00; Friday 15:00 -> Monday 02:00
    assert schedules.next_time(s, at(2026, 10, 30, 10, 0)) == at(2026, 10, 30, 14, 0)
    assert schedules.next_time(s, at(2026, 10, 30, 15, 0)) == at(2026, 11, 2, 2, 0)
    assert schedules.next_time(s, at(2026, 10, 30, 14, 0)) == at(2026, 11, 2, 2, 0)     # strictly later
    h = schedules.clean({"mode": "interval", "every_hours": 4})
    assert schedules.next_time(h, 1000.0) == 1000.0 + 4 * 3600 and h["name"] == "каждые 4 ч"


@pytest.mark.parametrize("body, text", [
    ({"days": [], "times": ["02:00"]}, "день недели"),
    ({"days": [1], "times": ["25:00"]}, "ЧЧ:ММ"),
    ({"mode": "interval", "every_hours": 0}, "Интервал"),
    ({"days": [1], "times": ["02:00"], "tz": "Mars/Base"}, "часовой пояс"),
])
def test_invalid_schedules(body, text):
    with pytest.raises(schedules.Invalid, match=text):
        schedules.clean(body)


def test_tick_starts_once_skips_running_and_missed(proj, ready_test):
    started = []
    s = schedules.save(proj["id"], {"name": "Ночь", "days": list(range(7)), "times": ["02:00"],
                                    "tags": ["smoke"]}, "ivan")
    due = s["next"]
    assert schedules.tick(lambda *a: started.append(a), now=due - 1) == []

    done = schedules.tick(lambda *a: started.append(a), now=due + 5)
    assert [d["status"] for d in done] == ["started"] and len(started) == 1
    run = suite.get(done[0]["suite_id"])
    assert run["trigger"] == "schedule" and run["user"] == "ivan" and run["schedule"]["name"] == "Ночь"
    assert [i["test_id"] for i in run["items"]] == [ready_test["id"]]
    after = schedules.get(proj["id"], s["id"])
    assert after["next"] == due + 86400 and after["last"]["suite_id"] == run["id"]
    assert schedules.tick(lambda *a: started.append(a), now=due + 6) == []      # taken once

    # the previous suite still runs: the turn is skipped
    done = schedules.tick(lambda *a: started.append(a), now=due + 86400 + 5)
    assert done[0]["status"] == "skipped" and len(started) == 1

    # the studio was down for the turn: recorded as missed, not caught up
    suite.LIVE.pop(run["id"], None)
    run.update(status="passed")
    suite.save(run)
    done = schedules.tick(lambda *a: started.append(a), now=due + 2 * 86400 + schedules.GRACE + 60)
    assert done[0]["status"] == "missed" and len(started) == 1


def test_tick_without_tests(proj):
    s = schedules.save(proj["id"], {"mode": "interval", "every_hours": 1, "tags": ["нет-такого"]}, "ivan")
    done = schedules.tick(lambda *a: None, now=s["next"] + 1)
    assert done[0]["status"] == "empty" and "нет-такого" in done[0]["note"]


def test_disabled_schedule_has_no_next(proj):
    s = schedules.save(proj["id"], {"days": [0], "times": ["02:00"], "enabled": False}, "ivan")
    assert s["next"] is None
    schedules.tick(lambda *a: None, now=time.time() + 8 * 86400)
    assert schedules.get(proj["id"], s["id"])["last"] is None


def test_api(monkeypatch, proj, ready_test):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    started = []
    monkeypatch.setattr(server.worker_mod, "start_suite", lambda p, s, tests, submit: started.append(s["id"]))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    url = f"/api/projects/{proj['id']}/schedules"
    assert client.post(url, json={"days": [], "times": ["02:00"]}).status_code == 400
    assert client.post(url, json={"days": [1], "times": ["02:00"], "device": "0x0"}).status_code == 400
    s = client.post(url, json={"name": "Ночь", "days": [0, 1, 2, 3, 4], "times": ["02:00"], "tz": "Europe/Moscow"}).json()
    assert s["when"] == "пн–пт в 02:00" and s["next"] and s["user"] == auth.ANONYMOUS
    assert [x["id"] for x in client.get(url).json()] == [s["id"]]

    off = client.put(f"{url}/{s['id']}", json={**s, "enabled": False}).json()
    assert off["enabled"] is False and off["next"] is None and off["name"] == "Ночь"

    r = client.post(f"{url}/{s['id']}/run").json()
    assert r["last"]["status"] == "started" and started == [r["last"]["suite_id"]]
    assert client.post(f"{url}/{s['id']}/run").status_code == 409        # the previous one still runs
    listed = client.get(f"/api/projects/{proj['id']}/suites").json()
    assert listed[0]["schedule"]["name"] == "Ночь"

    assert client.delete(f"{url}/{s['id']}").status_code == 200
    assert client.delete(f"{url}/{s['id']}").status_code == 404
    assert client.put(f"{url}/nope", json={"days": [1], "times": ["02:00"]}).status_code == 404
