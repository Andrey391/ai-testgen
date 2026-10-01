"""Project tasks: CRUD over the HTTP API, filters, links to tests, the MCP tool."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import arun
from testgen import auth, mcp_server, tasks
from testgen.steps import new_step


@pytest.fixture(scope="module")
def client():
    # Without `with`: the MCP app's lifespan runs once per process (test_server_mcp.py) and /mcp is not needed here.
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def test_task_crud_and_filters(client, open_studio, project, save_test):
    pid = project["id"]
    t = save_test("Корзина", [new_step("navigate", "open", project["base_url"])])
    url = f"/api/projects/{pid}/tasks"
    assert client.post(url, json={"title": "  "}).status_code == 400
    assert client.post(url, json={"title": "x", "status": "bogus"}).status_code == 400
    assert client.post(url, json={"title": "x", "due": "31.12.2026"}).status_code == 400

    a = client.post(url, json={"title": "Покрыть корзину", "priority": "high", "assignee": "ivan",
                               "due": "2000-01-01", "test_ids": [t["id"], "nope"]}).json()
    assert a["status"] == "todo" and a["created_by"] == auth.ANONYMOUS and a["test_ids"] == [t["id"]]
    b = client.post(url, json={"title": "Разобрать падение", "priority": "low"}).json()

    listed = client.get(url).json()
    assert [x["id"] for x in listed["tasks"]] == [a["id"], b["id"]]   # high priority first
    assert listed["tasks"][0]["tests"] == [{"id": t["id"], "name": "Корзина"}] and listed["tasks"][0]["overdue"]
    assert listed["counts"] == {"todo": 2, "in_progress": 0, "review": 0, "done": 0, "open": 2}
    assert [x["id"] for x in client.get(f"{url}?assignee=ivan").json()["tasks"]] == [a["id"]]

    done = client.patch(f"/api/tasks/{a['id']}", json={"status": "done"}).json()
    assert done["status"] == "done" and done["done_at"] and done["title"] == "Покрыть корзину"
    assert not client.get(url).json()["tasks"][1]["overdue"]
    assert [x["id"] for x in client.get(f"{url}?status=open").json()["tasks"]] == [b["id"]]
    assert [x["id"] for x in client.get(f"{url}?status=done").json()["tasks"]] == [a["id"]]
    assert client.patch(f"/api/tasks/{a['id']}", json={"status": "todo"}).json()["done_at"] is None

    # Deleting a test unlinks it from tasks.
    client.delete(f"/api/tests/{t['id']}")
    assert client.get(f"/api/tasks/{a['id']}").json()["test_ids"] == []

    assert client.delete(f"/api/tasks/{b['id']}").json()["ok"]
    assert client.get(f"/api/tasks/{b['id']}").status_code == 404
    assert client.get("/api/tasks/..%2Fproject").status_code == 404
    assert next(p for p in client.get("/api/projects").json() if p["id"] == pid)["open_tasks"] == 1


def test_test_from_task_is_linked_on_save(project, save_test):
    task = tasks.create(project["id"], {"title": "Оформление заказа"})
    t = save_test("Заказ", [])
    linked = tasks.link_test(task["id"], t["id"])
    assert linked["test_ids"] == [t["id"]] and linked["status"] == "in_progress"
    tasks.update(task["id"], {"status": "review"})
    again = tasks.link_test(task["id"], t["id"])
    assert again["test_ids"] == [t["id"]] and again["status"] == "review"


def test_mcp_list_tasks(project):
    tasks.create(project["id"], {"title": "Мне", "assignee": auth.ANONYMOUS})
    tasks.create(project["id"], {"title": "Другому", "assignee": "petr"})
    tasks.create(project["id"], {"title": "Готово", "status": "done"})
    mcp = mcp_server.build()

    async def call(**kw):
        _, out = await mcp.call_tool("list_tasks", {"project": project["name"], **kw})
        return [x["title"] for x in out["result"]]
    assert sorted(arun(call())) == ["Другому", "Мне"]   # done is not open
    assert arun(call(assignee="me")) == ["Мне"]
    assert sorted(arun(call(status="all"))) == ["Готово", "Другому", "Мне"]
