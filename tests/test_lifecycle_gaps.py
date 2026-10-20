"""What changes after a test was made reaches it: outdated tests (requirements, scenario, model), the sources of the
requirements checked again, suites that do not run tests sharing stand data at once, the roles of a scenario,
accepted updates of the model, what of the model a scenario names, what Planner did not reach."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import analyses, auth, explorer, fs, inbox, knowledge, projects, sources, storage, suite
from testgen.steps import new_step


@pytest.fixture(scope="module")
def client():
    import server
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def open_studio(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)


def _project() -> dict:
    return projects.create("Изменения " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")


def _test(pid: str, name: str, scenario: str = "", **more) -> dict:
    return storage.save({"project_id": pid, "name": name, "url": "http://127.0.0.1:1", "scenario": scenario,
                         "steps": [new_step("navigate", "open", "http://127.0.0.1:1")]} | more)


MODEL = {"entities": [{"name": "Заказ", "lifecycle": "новый → оплачен"}],
         "roles": [{"name": "Покупатель", "capabilities": "оплачивает заказ"},
                   {"name": "Менеджер", "capabilities": "отгружает заказ"}]}


# ---------- suites: tests sharing stand data run one after another ----------

def test_a_suite_chains_the_tests_that_share_stand_data():
    pid = _project()["id"]
    tests = [{"id": "b", "data_refs": [{"id": "r1", "use": "uses"}]},
             {"id": "a", "data_refs": [{"id": "r1", "use": "creates"}]},
             {"id": "c", "data_refs": [{"id": "r1", "use": "uses"}, {"id": "r2", "use": "uses"}]},
             {"id": "d", "data_refs": [{"id": "r2", "use": "uses"}]},
             {"id": "e"}]
    items = [{"test_id": t["id"], "status": "queued"} for t in tests]
    suite.plan(pid, items, tests)
    after = {it["test_id"]: [items[j]["test_id"] for j in it.get("after", [])] for it in items}
    # the test that creates r1 goes first, then the others in the suite's order
    assert after == {"a": [], "b": ["a"], "c": ["b"], "d": ["c"], "e": []}
    assert {it["test_id"]: it.get("shares") for it in items} == {"a": 1, "b": 1, "c": 2, "d": 1, "e": None}


def test_recipes_are_shared_by_nobody():
    pid = _project()["id"]
    knowledge.save(pid, {"entities": [{"name": "Заказ"}], "data": [
        {"entity": "Заказ", "name": "Заказ {{unique}}", "status": "recipe"}]})
    rid = knowledge.get(pid)["data"][0]["id"]
    tests = [{"id": "a", "data_refs": [{"id": rid, "use": "creates"}]}, {"id": "b", "data_refs": [{"id": rid}]}]
    items = [{"test_id": "a", "status": "queued"}, {"test_id": "b", "status": "queued"}]
    suite.plan(pid, items, tests)
    assert not any(it.get("after") for it in items)


def test_a_spread_suite_queues_an_item_when_the_ones_before_it_finish():
    s = {"items": [{"status": "running", "run_id": "r0"}, {"status": "queued", "after": [0]},
                   {"status": "queued"}, {"status": "queued", "run_id": "r3"}]}
    assert suite.ready(s) == [2]
    s["items"][0]["status"] = "failed"          # whatever its result
    assert suite.ready(s) == [1, 2]


# ---------- outdated tests ----------

def test_an_outdated_test_is_found_waits_in_the_inbox_and_is_cleared(client, open_studio):
    pid = _project()["id"]
    t = _test(pid, "Оплата", author="ann")
    other = _test(pid, "Вход")
    assert storage.mark_outdated([t["id"], t["id"]], "Сценарий изменён", "scenario", "a:s") == [t["id"]]
    storage.mark_outdated([t["id"]], "Сценарий изменён снова", "scenario", "a:s")     # the same reason - replaced
    assert [o["why"] for o in storage.load(t["id"])["outdated"]] == ["Сценарий изменён снова"]
    listed = client.get("/api/tests", params={"project_id": pid, "health": "outdated"}).json()
    assert [x["id"] for x in listed] == [t["id"]]
    entries = [e for e in client.get(f"/api/projects/{pid}/inbox").json()["items"] if e["kind"] == "outdated"]
    assert entries and entries[0]["owner"] == "ann" and entries[0]["route"].endswith("health=outdated")
    assert client.post(f"/api/tests/{t['id']}/current").status_code == 200
    assert not storage.load(t["id"]).get("outdated") and storage.load(t["id"])["outdated_cleared"]
    assert not [e for e in inbox.project_inbox(pid) if e["kind"] == "outdated"]
    assert "outdated" not in storage.load(other["id"])


def test_steps_a_person_edits_bring_the_test_up_to_date():
    pid = _project()["id"]
    t = _test(pid, "Оплата")
    storage.mark_outdated([t["id"]], "Новая версия ТЗ", "requirements", "a1")
    step = storage.load(t["id"])["steps"][0]
    storage.edit_steps(t["id"], [{"id": step["id"], "action": "navigate", "value": "http://127.0.0.1:1/new",
                                  "description": "open"}])
    assert not storage.load(t["id"]).get("outdated")


def test_an_edited_scenario_marks_its_tests(client, open_studio):
    pid = _project()["id"]
    a = analyses.create(pid, "Покупатель оплачивает заказ")
    analyses.finish(pid, a["id"])
    sc = analyses.add_scenario(pid, a["id"], {"title": "Оплата заказа", "instructions": "Оплатить"})
    t = _test(pid, "Оплата заказа")
    analyses.link_test(pid, a["id"], sc["id"], t["id"])
    url = f"/api/analyses/{a['id']}/scenarios/{sc['id']}"
    assert client.put(url, json={"priority": sc.get("priority") or "medium"}).status_code == 200
    assert not storage.load(t["id"]).get("outdated")       # nothing it says changed
    assert client.put(url, json={"instructions": "Оплатить картой"}).status_code == 200
    assert "Оплата заказа" in storage.load(t["id"])["outdated"][0]["why"]
    view = analyses.view(analyses.get(a["id"]))
    assert view["scenarios"][0]["tests"][0]["outdated"] is True


def test_new_requirements_mark_the_tests_of_their_scenarios():
    import server
    pid = _project()["id"]
    a = analyses.create(pid, "Покупатель оплачивает заказ")
    analyses.finish(pid, a["id"])
    sc = analyses.add_scenario(pid, a["id"], {"title": "Оплата"})
    t = _test(pid, "Оплата")
    analyses.link_test(pid, a["id"], sc["id"], t["id"])
    assert server._outdate_by_requirements(analyses.get(a["id"]), "Загружена новая версия ТЗ", "ann") == 1
    assert storage.load(t["id"])["outdated"][0]["kind"] == "requirements"


def test_a_part_confirmed_with_changes_marks_the_tests_about_it(client, open_studio):
    pid = _project()["id"]
    knowledge.save(pid, MODEL)
    knowledge.confirm(pid, "lead")
    about = _test(pid, "Оплата", "Покупатель оплачивает заказ")
    other = _test(pid, "Профиль", "Открыть страницу профиля")
    doc = knowledge.get(pid)
    doc["entities"][0]["lifecycle"] = "новый → оплачен → отгружен"
    knowledge.save(pid, doc)
    assert client.post(f"/api/projects/{pid}/knowledge/confirm", json={}).status_code == 200
    why = storage.load(about["id"])["outdated"][0]["why"]
    assert "Заказ" in why and "жизненный цикл" in why
    assert "outdated" not in storage.load(other["id"])


# ---------- the parts of the model a scenario waits for ----------

def test_roles_a_scenario_does_not_name_do_not_hold_it():
    pid = _project()["id"]
    knowledge.save(pid, MODEL)
    knowledge.confirm(pid, "lead")
    doc = knowledge.get(pid)
    next(r for r in doc["roles"] if r["name"] == "Менеджер")["capabilities"] = "отгружает и отменяет заказ"
    knowledge.save(pid, doc)
    buyer = {"title": "Оплата заказа", "role": "Покупатель", "instructions": "Оплатить заказ"}
    manager = {"title": "Отмена заказа", "role": "Менеджер", "instructions": "Отменить заказ"}
    assert knowledge.confirmed_for(pid, buyer)["state"] == "confirmed"
    assert knowledge.confirmed_for(pid, manager)["state"] == "changed"
    assert knowledge.confirmation(knowledge.get(pid))["state"] == "changed"     # the model as a whole


def test_an_accepted_update_of_a_confirmed_part_needs_no_second_confirmation():
    pid = _project()["id"]
    knowledge.save(pid, MODEL)
    knowledge.confirm(pid, "lead")
    knowledge.record(pid, {"entity": "Заказ", "name": "Заказ", "depends_on": ["Адрес"]}, "Studio: тест")
    assert knowledge.get(pid)["pending"]
    before = knowledge.get(pid)
    knowledge.resolve(pid, None, "accept", "lead")
    after = knowledge.get(pid)
    assert knowledge.confirmation(after)["state"] == "confirmed"
    assert [u["unit"] for u in knowledge.diff_parts(before, after)] == [""]


def test_a_rejected_update_changes_nothing():
    pid = _project()["id"]
    knowledge.save(pid, MODEL)
    knowledge.confirm(pid, "lead")
    knowledge.record(pid, {"entity": "Заказ", "name": "Заказ", "depends_on": ["Адрес"]}, "Studio: тест")
    before = knowledge.get(pid)
    knowledge.resolve(pid, None, "reject", "lead")
    assert knowledge.diff_parts(before, knowledge.get(pid)) == []


def test_what_of_the_model_a_scenario_names(client, open_studio):
    pid = _project()["id"]
    assert client.post(f"/api/projects/{pid}/knowledge/touches", json={"text": "Оплатить"}).json() == {"empty": True}
    knowledge.save(pid, MODEL)
    r = client.post(f"/api/projects/{pid}/knowledge/touches", json={"text": "Покупатель оплачивает заказы"}).json()
    assert r["entities"] == ["Заказ"] and r["roles"] == ["Покупатель"] and r["whole"] is True
    r = client.post(f"/api/projects/{pid}/knowledge/touches", json={"text": "Оплатить счёт"}).json()
    assert r["entities"] == [] and r["roles"] == []


# ---------- the sources of the requirements ----------

def test_the_sources_of_requirements_are_checked_again(client, open_studio, monkeypatch):
    pid = _project()["id"]
    a = analyses.create(pid, "# PROJ-1 Оплата\n\nПокупатель оплачивает заказ")
    analyses.finish(pid, a["id"])
    assert client.post(f"/api/analyses/{a['id']}/sources/check").status_code == 400       # nothing to check
    analyses.set_sources(pid, a["id"], [{"link": "PROJ-1", "kind": "jira", "title": "PROJ-1 Оплата",
                                         "digest": analyses.digest("Покупатель оплачивает заказ")}])
    now = {"text": "Покупатель оплачивает заказ"}

    async def fetch(link, project):
        return {"kind": "jira", "source": link, "title": "PROJ-1 Оплата", "text": now["text"], "truncated": False}
    monkeypatch.setattr(sources, "fetch", fetch)
    r = client.post(f"/api/analyses/{a['id']}/sources/check").json()
    assert r["changed"] == 0 and r["text"] == ""
    now["text"] = "Покупатель оплачивает заказ картой"
    r = client.post(f"/api/analyses/{a['id']}/sources/check").json()
    assert r["changed"] == 1 and r["text"] == "# PROJ-1 Оплата\n\nПокупатель оплачивает заказ картой"
    assert r["sources"][0]["digest"] == analyses.digest(now["text"])
    assert analyses.get(a["id"])["sources"][0]["changed"] is True


# ---------- what Planner did not reach ----------

def test_a_draft_of_a_part_of_the_application_says_so():
    pid = _project()["id"]
    assert knowledge.explored(pid) is None
    m = {"id": "m1", "project_id": pid, "status": "done", "pages": [{"url": "u"}] * 20, "finished": 5,
         "limits": {"max_pages": 20, "max_depth": 2, "left": 7, "deeper": 0, "logged_in": True}}
    explorer.save(m)
    x = knowledge.view(pid)["explored"]
    assert x["partial"] and x["left"] == 7 and x["pages"] == 20
    explorer.save(m | {"limits": m["limits"] | {"left": 0}})
    assert knowledge.explored(pid)["partial"] is False
    fs.unlink(explorer._dir(pid) / "latest.json")
