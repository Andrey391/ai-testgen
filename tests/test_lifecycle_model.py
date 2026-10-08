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
    knowledge.record(pid, {"entity": "Заказ", "name": "Заказ 1", "depends_on": ["Адрес доставки"]}, "Studio: тест")
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

    # The first test that prepares it records it: it is on the stand for the next tests.
    knowledge.record(pid, {"entity": "Покупатель", "name": "Покупатель с адресом доставки",
                           "details": "Москва, ул. Тестовая, 1"}, "Studio: Оплата заказа")
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
