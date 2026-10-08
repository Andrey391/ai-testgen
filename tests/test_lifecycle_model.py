"""The test data of a project: the lifecycle of the system and the roles with their capabilities are
confirmed by a person before tests are generated; scenarios name their role and test data, which is
recorded in the application model and reused."""
from __future__ import annotations

import asyncio
import re
import uuid

from fastapi.testclient import TestClient

from fakes import Resp
from helpers import arun
from testgen import analyses, auth, knowledge, pipeline, projects, scenarios, skills
from testgen.scenarios import DataNeed, PlannedScenario, Scenario, ScenarioBatch, ScenarioPlan

SHOP = {"summary": "Интернет-магазин",
        "entities": [{"name": "Заказ", "depends_on": ["Товар"], "lifecycle": "новый → оплачен (покупатель) → отправлен (менеджер)"},
                     {"name": "Товар", "depends_on": ["Категория"]}],
        "roles": [{"name": "Покупатель", "capabilities": "оформляет и оплачивает свои заказы",
                   "restrictions": "не видит чужие заказы"},
                  {"name": "Менеджер", "capabilities": "отправляет оплаченные заказы"}]}


def _project(confirm: bool = True) -> dict:
    p = projects.create("Модель " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    p["pipeline"]["requirements"]["confirm_model"] = confirm
    return projects.update(p["id"], {"pipeline": p["pipeline"]})


def test_lifecycle_is_confirmed_and_a_change_asks_again():
    pid = _project()["id"]
    assert knowledge.view(pid)["confirmation"]["state"] == "none"
    try:
        knowledge.confirm(pid, "ann")
        raise AssertionError("an empty model cannot be confirmed")
    except ValueError as e:
        assert "нет сущностей" in str(e) and "нет ролей" in str(e)
    knowledge.save(pid, SHOP | {"roles": [{"name": "Покупатель"}]})
    try:
        knowledge.confirm(pid, "ann")
        raise AssertionError("roles without capabilities")
    except ValueError as e:
        assert "возможности" in str(e)

    knowledge.save(pid, SHOP)
    doc = knowledge.confirm(pid, "ann")
    assert doc["confirmation"]["state"] == "confirmed" and doc["confirmation"]["by"] == "ann"
    assert knowledge.is_confirmed(pid)

    # Stand data, memory and the order of records do not touch the lifecycle; a person cannot forge it.
    knowledge.record(pid, {"entity": "Товар", "name": "Тестовый товар А", "state": "в наличии"}, "Studio: тест")
    knowledge.remember(pid, "Оплаченный заказ отменить нельзя")
    knowledge.save(pid, knowledge.get(pid) | {"roles": list(reversed(knowledge.get(pid)["roles"]))})
    assert knowledge.is_confirmed(pid)
    knowledge.save(pid, knowledge.get(pid) | {"confirmed": {"at": 1, "by": "x", "sig": "forged"}})
    assert knowledge.get(pid)["confirmed"]["by"] == "ann"

    # A new transition, a new capability or a new dependency found by a test: confirm again.
    doc = knowledge.get(pid)
    doc["roles"][0]["capabilities"] += ", отменяет неоплаченный заказ"
    assert knowledge.save(pid, doc)["confirmation"]["state"] == "changed"
    knowledge.confirm(pid, "bob")
    doc = knowledge.record(pid, {"entity": "Заказ", "name": "Заказ 1", "depends_on": ["Адрес доставки"]}, "Studio: тест")
    assert knowledge.is_confirmed(pid)          # the known entity is not changed until a person accepts it
    [p] = [p for p in doc["pending"] if p["kind"] == "entities"]
    assert p["changes"] == {"depends_on": ["Товар", "Адрес доставки"]} and p["source"] == "Studio: тест"
    knowledge.resolve(pid, [p["id"]], "accept", "bob")
    assert not knowledge.is_confirmed(pid)


def test_scenario_test_data_is_recorded_and_reused():
    pid = _project()["id"]
    knowledge.save(pid, SHOP | {"data": [{"entity": "Товар", "name": "Тестовый товар А", "state": "в наличии"}]})
    need = [{"title": "Оплата заказа", "role": "Покупатель",
             "test_data": [{"entity": "Товар", "name": "Тестовый товар А", "state": "в наличии"},
                           {"entity": "Покупатель", "name": "покупатель с адресом доставки", "role": "Покупатель"}]},
            {"title": "Отправка заказа", "role": "Кладовщик",
             "test_data": [{"entity": "Покупатель", "name": "Покупатель с адресом доставки"}]}]
    doc = knowledge.need(pid, need)
    data = {d["name"].lower(): d for d in doc["data"]}
    assert data["тестовый товар а"]["status"] == "" and data["тестовый товар а"]["needed_by"] == ["Оплата заказа"]
    buyer = data["покупатель с адресом доставки"]
    assert buyer["status"] == "needed" and buyer["needed_by"] == ["Оплата заказа", "Отправка заказа"]
    assert buyer["role"] == "Покупатель" and len(doc["data"]) == 2
    assert "Кладовщик" in [r["name"] for r in doc["roles"]]          # a role the scenarios act as joins the model

    text = knowledge.prompt(pid)
    assert "may: оформляет и оплачивает свои заказы" in text and "may not: не видит чужие заказы" in text
    assert re.search(r"need that nobody has seen.*\n- \[Покупатель\] покупатель с адресом доставки", text)

    # The first test that prepares it records it: once a person accepts it, it is on the stand for the next tests.
    doc = knowledge.record(pid, {"entity": "Покупатель", "name": "Покупатель с адресом доставки",
                                 "details": "Москва, ул. Тестовая, 1"}, "Studio: Оплата заказа")
    [p] = doc["pending"]
    assert p["changes"] == {"details": "Москва, ул. Тестовая, 1", "status": "", "source": "Studio: Оплата заказа"}
    assert "update awaits a person's confirmation" in knowledge.prompt(pid)
    knowledge.resolve(pid, None, "accept")
    buyer = next(d for d in knowledge.get(pid)["data"] if d["entity"] == "Покупатель")
    assert buyer["status"] == "" and buyer["source"] == "Studio: Оплата заказа" and buyer["needed_by"]
    assert "need that nobody has seen" not in knowledge.prompt(pid)


def _script(kind, kw):
    task = kw["messages"][-1]["content"]
    task = task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)
    if "scenarios 1–" not in task:
        return Resp(parsed=ScenarioPlan(feature="Заказы", assumptions=[], scenarios=[
            PlannedScenario(title="Оплата", type="positive", priority="high", role="Покупатель", covers="оплата"),
            PlannedScenario(title="Чужой заказ", type="roles", priority="high", role="Покупатель", covers="права")]))
    return Resp(parsed=ScenarioBatch(scenarios=[
        Scenario(title=t, type="positive", priority="high", preconditions="", instructions="Шаги",
                 expected_result="", gherkin="", test_data=[DataNeed(entity="Заказ", name="новый заказ покупателя",
                                                                     details="", state="новый", role="Покупатель")])
        for t in ("Оплата", "Чужой заказ")]))


def test_scenarios_carry_roles_and_record_their_test_data(fake_llm):
    fake_llm.script = _script
    p = _project()
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    knowledge.save(p["id"], SHOP)
    res = arun(scenarios.generate("Оплата заказа", project=projects.get(p["id"])))
    assert [s.role for s in res.scenarios] == ["Покупатель", "Покупатель"]
    system = fake_llm.calls[0][1]["system"]
    system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
    assert "`role` is the role the test acts as" in system and "Роли, возможности и тестовые данные" in system
    order = next(d for d in knowledge.get(p["id"])["data"] if d["entity"] == "Заказ")
    assert order["status"] == "needed" and order["needed_by"] == ["Оплата", "Чужой заказ"]

    sc = res.scenarios[0].model_dump()
    text = pipeline.scenario_text(sc)
    assert text.startswith("Роль: Покупатель\n") and "Тестовые данные: Заказ «новый заказ покупателя» (новый)" in text


def test_pipeline_waits_for_the_lifecycle_before_authoring(monkeypatch):
    p = _project()
    knowledge.save(p["id"], SHOP)
    monkeypatch.setattr(pipeline, "MODEL_POLL", 0.05)
    given = [{"title": "Оплата", "type": "positive", "priority": "high", "role": "Покупатель", "preconditions": "",
              "instructions": "Шаги", "expected_result": "", "gherkin": ""}]
    job = pipeline.Job(p, [], "", "", {}, scenarios=given, feature="Заказы")
    monkeypatch.setitem(pipeline.JOBS, job.id, job)
    seen = []

    async def process(project, item, sc):
        seen.append(sc["title"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)

    async def scenario():
        task = asyncio.create_task(job.run())
        for _ in range(100):
            if job.status == "awaiting_model":
                break
            await asyncio.sleep(0.02)
        assert job.status == "awaiting_model" and not seen
        assert pipeline.get_job(job.id)["status"] == "awaiting_model"
        knowledge.confirm(p["id"], "ann")       # confirmed elsewhere: the run sees it by itself
        await asyncio.wait_for(task, 5)
    arun(scenario())
    assert job.status == "done" and seen == ["Оплата"]
    assert any("Жизненный цикл системы подтверждён" in line["text"] for line in job.log)


def test_confirmation_over_the_api_and_studio_from_a_scenario(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    p = _project()
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    r = client.post(f"/api/projects/{p['id']}/knowledge/confirm")
    assert r.status_code == 400 and "нет сущностей" in r.json()["detail"]

    a = analyses.create(p["id"], "Оплата")
    body = {"project_id": p["id"], "name": "Оплата", "url": p["base_url"], "scenario": "Шаги",
            "analysis_id": a["id"], "scenario_id": "x"}
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 409 and "жизненный цикл" in r.json()["detail"]

    client.put(f"/api/projects/{p['id']}/knowledge", json=SHOP)
    doc = client.post(f"/api/projects/{p['id']}/knowledge/confirm").json()
    assert doc["confirmation"]["state"] == "confirmed"
    assert client.get(f"/api/projects/{p['id']}/knowledge").json()["confirmation"]["state"] == "confirmed"


def test_test_data_skills_are_attached_by_default():
    pid = _project()["id"]
    cfg = projects.get(pid)["pipeline"]
    assert cfg["requirements"]["model_skills"] == ["application-model"]
    assert "test-data-roles" in cfg["scenarios"]["skills"]
    assert skills.attach(pid, "application-model", ["requirements.model_skills"]) == ["requirements.model_skills"]
    assert "Жизненный цикл системы" in skills.prompt(pid, ["application-model"])


def test_a_model_saved_by_an_earlier_version_is_read():
    from testgen import fs
    pid = _project()["id"]
    fs.write_json(knowledge._path(pid), {"summary": "Магазин", "entities": [{"id": "e1", "name": "Заказ"}],
                                         "roles": [{"id": "r1", "name": "Покупатель", "description": ""}],
                                         "data": [{"id": "d1", "entity": "Товар", "name": "А", "source": ""}],
                                         "memory": [], "updated": 5, "updated_by": "ann"})
    doc = knowledge.view(pid)
    assert doc["roles"][0]["capabilities"] == "" and doc["data"][0]["status"] == "" and doc["updated"] == 5
    assert doc["confirmation"]["state"] == "none" and "Покупатель" in knowledge.prompt(pid)


def test_a_record_found_again_waits_for_a_person():
    pid = _project()["id"]
    knowledge.save(pid, SHOP | {"data": [{"entity": "Товар", "name": "Тестовый товар А", "state": "в наличии"}]})
    stored = knowledge.get(pid)

    # An analysis finds what is already there (the same and similar names): nothing is added twice or changed silently.
    doc = knowledge.merge(pid, {"entities": [{"name": "заказы", "lifecycle": "новый → отменён"}, {"name": "Корзина"}],
                                "data": [{"entity": "товар", "name": "Тестовый товар «А»", "state": "нет в наличии",
                                          "source": "Planner"}]})
    assert [e["name"] for e in doc["entities"]] == ["Заказ", "Товар", "Корзина"]
    assert doc["entities"][0] == stored["entities"][0] and doc["data"] == stored["data"]
    item, order = sorted(doc["pending"], key=lambda p: p["kind"])
    assert order["match"] == "similar" and order["changes"] == {"lifecycle": "новый → отменён"}
    assert order["before"] == {"lifecycle": "новый → оплачен (покупатель) → отправлен (менеджер)"}
    assert item["match"] == "same" and item["changes"] == {"state": "нет в наличии"}     # people wrote it: stays theirs

    # Found again before a person decided: one proposal per record. Saving the model keeps the proposals.
    doc = knowledge.merge(pid, {"data": [{"entity": "Товар", "name": "Тестовый товар А", "details": "цена 1000"}]})
    assert len(doc["pending"]) == 2
    item = next(p for p in doc["pending"] if p["kind"] == "data")
    assert item["changes"] == {"state": "нет в наличии", "details": "цена 1000"}
    assert len(knowledge.save(pid, knowledge.get(pid) | {"pending": []})["pending"]) == 2

    # Each one: reject, add separately (a similar name meant another object), accept.
    knowledge.resolve(pid, [order["id"]], "separate")
    assert [e["name"] for e in knowledge.get(pid)["entities"]] == ["Заказ", "Товар", "Корзина", "заказы"]
    doc = knowledge.resolve(pid, None, "accept")
    assert doc["pending"] == [] and doc["data"][0]["state"] == "нет в наличии" and doc["data"][0]["details"] == "цена 1000"
    try:
        knowledge.resolve(pid, None, "drop")
        raise AssertionError("an unknown action")
    except ValueError:
        pass

    # A person adds a record that is already there: it becomes an update of the stored one, not a duplicate.
    doc = knowledge.get(pid)
    doc["data"].append({"entity": "Товар", "name": "тестовый товар а", "state": "на складе"})
    doc["roles"].append({"name": "Менеджер", "restrictions": "не оплачивает заказы"})
    doc = knowledge.save(pid, doc, "ann")
    assert len(doc["data"]) == 1 and len(doc["roles"]) == 2
    assert {p["kind"]: p["source"] for p in doc["pending"]} == {"data": "вручную: ann", "roles": "вручную: ann"}
    assert knowledge.resolve(pid, None, "reject")["data"][0]["state"] == "нет в наличии"


def test_duplicates_are_found_and_merged():
    from testgen import fs
    pid = _project()["id"]
    knowledge.save(pid, SHOP | {"data": [{"entity": "Товар", "name": "Товар А"}]})
    doc = knowledge.get(pid)
    doc["entities"].append({"id": "ord2", "name": "Заказы", "rules": "оплаченный не отменяется", "depends_on": ["Покупатель"]})
    doc["entities"].append({"id": "ord3", "name": "Заказ 2"})              # another number: another thing
    doc["roles"].append({"id": "man2", "name": "Менеджер магазина", "restrictions": "не оплачивает"})
    doc["data"] += [{"id": "d2", "entity": "Заказы", "name": "Заказ покупателя", "role": "Менеджер магазина"},
                    {"id": "d3", "entity": "товар", "name": "товар «А»", "state": "в наличии", "source": "Planner"}]
    doc["entities"][1]["depends_on"] = ["Заказы"]
    fs.write_json(knowledge._path(pid), doc)    # duplicates made before the check on adding: the check finds them
    doc = knowledge.view(pid)
    assert doc["duplicates"] == 3
    groups = {g["kind"]: g for g in knowledge.duplicates(doc)}
    assert [x["name"] for x in groups["entities"]["items"]] == ["Заказ", "Заказы"]
    assert groups["data"]["keep"] != "d3"                                     # the record people wrote is kept
    assert groups["entities"]["merged"]["rules"] == "оплаченный не отменяется"
    assert groups["entities"]["merged"]["depends_on"] == ["Товар", "Покупатель"]

    # "Not duplicates": the group is not offered again.
    knowledge.dismiss_duplicates(pid, [groups["roles"]["id"]])
    assert {g["kind"] for g in knowledge.duplicates(knowledge.get(pid))} == {"entities", "data"}

    doc = knowledge.merge_duplicates(pid, None, "ann")
    assert doc["merged"] == 2 and doc["duplicates"] == 0
    assert [e["name"] for e in doc["entities"]] == ["Заказ", "Товар", "Заказ 2"]
    order = doc["entities"][0]
    assert order["rules"] == "оплаченный не отменяется" and order["depends_on"] == ["Товар", "Покупатель"]
    assert doc["entities"][1]["depends_on"] == ["Заказ"]                      # references follow the kept record
    assert [(d["entity"], d["name"]) for d in doc["data"]] == [("Товар", "Товар А"), ("Заказ", "Заказ покупателя")]
    assert doc["data"][0]["state"] == "в наличии"

    # Merging a role renames the role of the data; a group is chosen with the record to keep.
    doc = knowledge.merge_duplicates(pid, [{"kind": "roles", "ids": ["man2", doc["roles"][1]["id"]], "keep": "man2"}])
    assert [r["name"] for r in doc["roles"]] == ["Покупатель", "Менеджер магазина"]
    assert doc["roles"][1]["capabilities"] == "отправляет оплаченные заказы"


def test_duplicates_and_updates_over_the_api(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    pid = _project()["id"]
    url = f"/api/projects/{pid}/knowledge"
    doc = client.put(url, json=SHOP).json()
    doc["entities"][1]["name"] = "Заказы"
    client.put(url, json=doc)
    groups = client.get(url + "/duplicates").json()
    assert [[x["name"] for x in g["items"]] for g in groups] == [["Заказ", "Заказы"]]
    doc = client.post(url + "/duplicates", json={"groups": [{"kind": "entities", "ids": [x["id"] for x in groups[0]["items"]],
                                                             "keep": groups[0]["items"][1]["id"]}]}).json()
    assert doc["merged"] == 1 and [e["name"] for e in doc["entities"]] == ["Заказы"]

    knowledge.merge(pid, {"roles": [{"name": "покупатель", "description": "клиент магазина"}]})
    doc = client.get(url).json()
    assert len(doc["pending"]) == 1
    assert client.post(url + "/pending", json={"action": "nope", "all": True}).status_code == 400
    doc = client.post(url + "/pending", json={"action": "accept", "ids": [doc["pending"][0]["id"]]}).json()
    assert doc["pending"] == [] and doc["roles"][0]["description"] == "клиент магазина"
    assert client.post(url + "/duplicates", json={"dismiss": ["x"]}).json()["distinct"] == ["x"]
