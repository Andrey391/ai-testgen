"""The chains of model requests behind "Требования": the specification is validated, the application model
learns from it, the scenarios are planned and detailed in batches - in the Requirements tab (/api/scenarios)
and in the pipeline (Job._design -> selection). Every request goes to the scripted stand-in of the API."""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from fakes import Resp, dump
from helpers import arun
from testgen import auth, pipeline, projects, scenarios, validation
from testgen.scenarios import ScenarioBatch, ScenarioPlan

SPEC = "# Заказы\n1. Покупатель оформляет заказ.\n2. Менеджер подтверждает оплаченный заказ."


def _task(kw) -> str:
    task = kw["messages"][-1]["content"]
    return task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)


def _report(verdict="needs_work") -> Resp:
    return Resp(parsed=validation.Validation(
        summary="Нет критериев приёмки", score=55, verdict=verdict,
        sections=[validation.SectionCheck(section="Общие сведения", status="partial", comment="")],
        findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 2",
                                     problem="нет срока", suggestion="указать срок")],
        questions=["Кто отменяет заказ?"]))


def _planned(i: int, **kw) -> dict:
    return {"title": f"Сценарий {i}", "type": "positive", "layer": "ui", "priority": "high", "covers": "x"} | kw


def _full(i: int, **kw) -> dict:
    return {"title": f"Сценарий {i}", "type": "positive", "priority": "high", "preconditions": "",
            "instructions": f"Шаги {i}", "expected_result": "", "gherkin": ""} | kw


def _batch(task: str, fn=_full) -> Resp:
    a, b = map(int, re.search(r"scenarios (\d+)–(\d+)", task).groups())
    return Resp(parsed=ScenarioBatch.model_validate({"scenarios": [fn(i) for i in range(a, b + 1)]}))


def _project(name: str, **pipe) -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    if pipe:
        cfg = projects.get(p["id"])["pipeline"]
        for stage, values in pipe.items():
            cfg[stage].update(values)
        projects.update(p["id"], {"pipeline": cfg})
    return projects.get(p["id"])


# ---------- the Requirements tab: validation -> plan -> batches, one stream ----------

def test_the_tab_validates_the_spec_then_plans_and_details(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    order = []

    def script(kind, kw):
        fmt = kw.get("output_format")
        order.append(fmt.__name__ if fmt else kind)
        if fmt is validation.Validation:
            return _report()
        if fmt is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
                _planned(1, role="Покупатель"), _planned(2, type="negative", priority="low", role="Менеджер")]}))
        if fmt is ScenarioBatch:
            return _batch(_task(kw))
        raise RuntimeError("модель приложения недоступна")     # knowledge.extract: a help, not a step
    fake_llm.script = script
    p = _project("Цепочка")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": SPEC,
                                                       "stream": True, "validate_spec": True}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]

    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done", events
    assert kinds.index("validation") < kinds.index("plan") < kinds.index("batch")
    # The specification is checked before the first scenario is planned.
    assert order.index("Validation") < order.index("ScenarioPlan") < order.index("ScenarioBatch")
    report = next(e for e in events if e["type"] == "validation")["validation"]
    assert report["standard"] == "gost34" and report["questions"] == ["Кто отменяет заказ?"]
    logs = [e["text"] for e in events if e["type"] == "log"]
    assert any("Проверка ТЗ: 55/100, замечаний 1" in t for t in logs)
    assert any("Модель приложения не обновлена" in t for t in logs)       # the chain goes on without it

    final = events[-1]["analysis"]
    assert final["validation"]["score"] == 55
    assert [(s["title"], s["role"], s["instructions"]) for s in final["scenarios"]] == [
        ("Сценарий 1", "Покупатель", "Шаги 1"), ("Сценарий 2", "Менеджер", "Шаги 2")]

    # Every request has the requirements: the validation as the document, the scenarios as their context.
    sent = {name: dump(kw) for (_, kw), name in zip(fake_llm.calls, order)}
    assert "The document:" in sent["Validation"] and "Покупатель оформляет заказ" in sent["Validation"]
    assert "Required sections" in sent["Validation"]
    for name in ("ScenarioPlan", "ScenarioBatch"):
        assert "Requirements:" in sent[name] and "Покупатель оформляет заказ" in sent[name]


def test_a_failed_validation_does_not_stop_the_scenarios(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [],
                                                            "scenarios": [_planned(1)]}))
        if fmt is ScenarioBatch:
            return _batch(_task(kw))
        raise RuntimeError("перегружено")
    fake_llm.script = script
    p = _project("Без проверки", requirements={"validate": True, "learn_model": False})
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": SPEC,
                                                       "stream": True}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["type"] == "done" and len(events[-1]["analysis"]["scenarios"]) == 1
    assert "validation" not in [e["type"] for e in events]
    assert any("Проверка ТЗ не удалась" in e["text"] for e in events if e["type"] == "log")

    # Switched off for one generation: no validation request at all.
    fake_llm.calls.clear()
    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": SPEC,
                                                       "stream": True, "validate_spec": False}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["type"] == "done"
    assert all(kw.get("output_format") is not validation.Validation for _, kw in fake_llm.calls)


# ---------- scenarios.generate: the plan decides, the batches fill in ----------

def test_the_plan_is_authoritative_and_short_batches_are_completed(fake_llm):
    asked = []

    def script(kind, kw):
        task = _task(kw)
        if kw["output_format"] is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": ["a"], "scenarios": [
                _planned(i, priority="medium", role="Менеджер" if i == 2 else "") for i in range(1, 7)]}))
        a, b = map(int, re.search(r"scenarios (\d+)–(\d+)", task).groups())
        asked.append((a, b))
        if (a, b) == (1, 3):        # one of three, with what the plan already decided changed
            return Resp(parsed=ScenarioBatch.model_validate({"scenarios": [
                _full(1, title="Другое", type="security", priority="low", role="Гость")]}))
        if (a, b) == (4, 6):        # nothing: the batch is split in halves
            return Resp(parsed=ScenarioBatch(scenarios=[]))
        return _batch(task, lambda i: _full(i, role="Покупатель"))
    fake_llm.script = script
    p = _project("План", requirements={"learn_model": False})
    res = arun(scenarios.generate(SPEC, project=p))

    assert [s.title for s in res.scenarios] == [f"Сценарий {i}" for i in range(1, 7)]
    assert [s.instructions for s in res.scenarios] == [f"Шаги {i}" for i in range(1, 7)]
    first = res.scenarios[0]
    assert (first.type, first.priority) == ("positive", "medium")      # the plan's, not the batch's
    assert first.role == "Гость"                                        # the batch's when the plan has none
    assert res.scenarios[1].role == "Менеджер"                          # the plan's when it has one
    assert (2, 3) in asked                                              # the rest of the short batch
    assert (4, 4) in asked and (5, 6) in asked                          # the empty batch in halves
    assert res.assumptions == ["a"]


def test_a_plan_that_repeats_itself_stops(fake_llm):
    """A model that keeps answering `more` with the titles it already gave does not loop to the guard."""
    def script(kind, kw):
        if kw["output_format"] is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "more": True,
                                                            "scenarios": [_planned(1), _planned(2, title="сценарий 1 ")]}))
        return _batch(_task(kw))
    fake_llm.script = script
    p = _project("Повтор", requirements={"learn_model": False})
    res = arun(scenarios.generate(SPEC, project=p))
    assert [s.title for s in res.scenarios] == ["Сценарий 1"]
    plans = [kw for _, kw in fake_llm.calls if kw["output_format"] is ScenarioPlan]
    assert len(plans) == 2
    assert "Already planned (titles):\n1. Сценарий 1" in _task(plans[1])


def test_an_empty_plan_and_a_refusal(fake_llm):
    p = _project("Пусто", requirements={"learn_model": False})
    fake_llm.script = lambda kind, kw: Resp(parsed=ScenarioPlan(feature="Заказы", assumptions=["нечего"], scenarios=[]))
    res = arun(scenarios.generate(SPEC, project=p))
    assert res.scenarios == [] and res.assumptions == ["нечего"] and len(fake_llm.calls) == 1

    fake_llm.script = lambda kind, kw: Resp(stop_reason="refusal")
    with pytest.raises(RuntimeError, match="не смогла составить"):
        arun(scenarios.generate(SPEC, project=p))


def test_the_chosen_design_reaches_every_request(fake_llm):
    """The kinds of checks, layers and techniques chosen for a generation go to the plan and the batches."""
    def script(kind, kw):
        if kw["output_format"] is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "API", "assumptions": [], "scenarios": [
                _planned(1, type="security", layer="api")]}))
        return _batch(_task(kw), lambda i: _full(i, type="security", layer="api"))
    fake_llm.script = script
    p = _project("Дизайн", requirements={"learn_model": False})
    cfg = scenarios.settings(p, types=["security"], layers=["api"], techniques=["boundary"])
    res = arun(scenarios.generate(SPEC, project=p, cfg=cfg))
    assert [(s.type, s.layer) for s in res.scenarios] == [("security", "api")]
    for _, kw in fake_llm.calls:
        sent = dump(kw)
        assert "- security:" in sent and "Layers: api." in sent and "- positive:" not in sent


# ---------- the pipeline: requirements -> validation -> scenarios -> selection ----------

def test_the_pipeline_waits_for_a_choice_and_keeps_the_scenarios_not_chosen(monkeypatch, fake_llm):
    from testgen import analyses, storage
    order = []

    def script(kind, kw):
        fmt = kw.get("output_format")
        order.append(fmt.__name__ if fmt else kind)
        if fmt is validation.Validation:
            return _report("ready")
        if fmt is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
                _planned(1, role="Покупатель"), _planned(2, priority="low"), _planned(3, role="Менеджер")]}))
        if fmt is ScenarioBatch:
            data = [{"entity": "Заказ", "name": "Оплаченный заказ", "details": "", "state": "оплачен",
                     "role": "Покупатель"}]
            return _batch(_task(kw), lambda i: _full(i, test_data=data if i == 3 else []))
        raise RuntimeError("модель приложения недоступна")
    fake_llm.script = script
    p = _project("Конвейер", requirements={"validate": True}, scenarios={"select": "manual"})
    job = pipeline.Job(p, [], SPEC, "", {}, user="alice")
    seen = []

    async def process(project, item, sc):
        seen.append((item["title"], sc["instructions"]))
        t = storage.save({"project_id": p["id"], "name": item["title"], "url": "http://x", "scenario": "", "steps": []})
        item["status"], item["test_id"] = "done", t["id"]
    monkeypatch.setattr(job, "_process", process)

    async def go():
        task = asyncio.create_task(job.run())
        while job.status != "awaiting_selection":
            assert not task.done(), job.error
            await asyncio.sleep(0.01)
        # A person chooses; all but the low-priority ones are checked to begin with.
        assert job.preselect == [0, 2] and job.items == []
        job.select([2])
        await task
    arun(go())

    assert job.status == "done", job.error
    assert order[0] == "Validation" and "ScenarioPlan" in order
    assert seen == [("Сценарий 3", "Шаги 3")]
    # The designed scenarios are an analysis of requirements: the ones not chosen stay for a later run.
    a = analyses.get(job.analysis_id)
    assert a["status"] == "done" and a["user"] == "alice" and a["validation"]["verdict"] == "ready"
    common = {s["title"]: s for s in analyses.all_scenarios(p["id"])}
    assert set(common) == {"Сценарий 1", "Сценарий 2", "Сценарий 3"}
    assert [common[t]["tests"] for t in ("Сценарий 1", "Сценарий 2", "Сценарий 3")] == [0, 0, 1]
    assert job.items[0]["analysis_id"] == a["id"] and job.items[0]["scenario_id"] == common["Сценарий 3"]["id"]
    # The test data of a scenario joins the application model and the scenario keeps the id of its record.
    assert job.scenarios[2]["test_data"][0]["record_id"]
    assert a["scenarios"][2]["test_data"][0]["record_id"] == job.scenarios[2]["test_data"][0]["record_id"]
    text = " ".join(e["text"] for e in job.log)
    assert "Проверка ТЗ: 55/100" in text and "Тестовые данные сценариев записаны" in text
    assert "Модель приложения не обновлена" in text and "не выбранные останутся" in text


def test_automatic_selection_keeps_the_rest_in_the_list(monkeypatch, fake_llm):
    """"Только high-приоритет" chooses without a person; the other scenarios stay for a later run."""
    from testgen import analyses

    def script(kind, kw):
        if kw["output_format"] is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
                _planned(1), _planned(2, priority="low"), _planned(3, priority="medium")]}))
        return _batch(_task(kw))
    fake_llm.script = script
    p = _project("Авто", requirements={"learn_model": False}, scenarios={"select": "high"})
    job = pipeline.Job(p, [], SPEC, "", {})
    seen = []

    async def process(project, item, sc):
        seen.append(sc["title"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)
    arun(job.run())
    assert job.status == "done" and seen == ["Сценарий 1"]
    assert {s["title"] for s in analyses.all_scenarios(p["id"])} == {"Сценарий 1", "Сценарий 2", "Сценарий 3"}
    assert any("остальные остаются" in e["text"] for e in job.log)


def test_stopping_interrupts_the_design_at_once_and_keeps_what_is_ready(monkeypatch, fake_llm):
    from testgen import analyses
    p = _project("Стоп", requirements={"learn_model": False})
    started = []

    async def generate(requirements, url="", project=None, log=None, progress=None, cfg=None, record=True):
        progress({"type": "plan", "feature": "Заказы", "assumptions": [], "scenarios": [_planned(1), _planned(2)]})
        progress({"type": "batch", "start": 0, "scenarios": [_full(1)]})
        started.append(True)
        await asyncio.Event().wait()        # a long request to the model
    monkeypatch.setattr(scenarios, "generate", generate)
    job = pipeline.Job(p, [], SPEC, "", {})

    async def go():
        task = asyncio.create_task(job.run())
        while not started:
            await asyncio.sleep(0.01)
        job.cancel()
        await asyncio.wait_for(task, 5)
    arun(go())
    assert job.status == "cancelled" and job.items == []
    a = analyses.get(job.analysis_id)
    assert a["status"] == "cancelled" and [s["title"] for s in a["scenarios"]] == ["Сценарий 1"]
    assert [s["title"] for s in analyses.all_scenarios(p["id"])] == ["Сценарий 1"]


def test_stopping_while_a_person_chooses(fake_llm):
    from testgen import analyses

    def script(kind, kw):
        if kw["output_format"] is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [],
                                                            "scenarios": [_planned(1)]}))
        return _batch(_task(kw))
    fake_llm.script = script
    p = _project("Выбор", requirements={"learn_model": False}, scenarios={"select": "manual"})
    job = pipeline.Job(p, [], SPEC, "", {})

    async def go():
        task = asyncio.create_task(job.run())
        while job.status != "awaiting_selection":
            await asyncio.sleep(0.01)
        job.cancel()
        await asyncio.wait_for(task, 5)
    arun(go())
    assert job.status == "cancelled" and job.items == []
    assert [s["title"] for s in analyses.get(job.analysis_id)["scenarios"]] == ["Сценарий 1"]


def test_one_scenario_of_a_run_is_cancelled(monkeypatch):
    p = _project("Один", requirements={"learn_model": False})
    given = [_full(i, title=t) for i, t in enumerate("ABC")]
    job = pipeline.Job(p, [], "", "", {}, scenarios=given)

    async def process(project, item, sc):
        if item["title"] == "A":
            assert job.cancel_item(1, "bob") and job.cancel_item(0, "bob")     # the next one, and this one
            assert not job.cancel_item(0) and not job.cancel_item(7)
        if job._stopped(item):
            return
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)
    arun(job.run())
    assert job.status == "done"
    assert [(i["status"], i.get("skipped") or "") for i in job.items] == [
        ("cancelled", "bob"), ("cancelled", "bob"), ("done", "")]
    # Going on with the run does not bring them back; "Перезапустить сбойные" neither, unless named.
    state = job.state()
    again = pipeline.Job.resumed(p, state, {})
    assert [i["status"] for i in again.items] == ["cancelled", "cancelled", "done"]
    assert not pipeline.retryable(state["items"][0]) and pipeline.retryable(state["items"][0], explicit=True)
    retried = pipeline.Job.retried(p, state, {}, [0])
    assert retried.items[0]["status"] == "queued" and not retried.items[0]["skipped"]


def test_cancelling_an_item_over_the_api(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p = _project("API отмены", requirements={"learn_model": False})
    job = pipeline.Job(p, [], "", "", {}, scenarios=[_full(1)])
    job.items = [job._item(s) for s in job.given]
    pipeline.JOBS[job.id] = job
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        assert client.post(f"/api/jobs/{job.id}/items/0/cancel").status_code == 200
        assert job.items[0]["status"] == "cancelled"
        assert client.post(f"/api/jobs/{job.id}/items/0/cancel").status_code == 409
        assert client.post("/api/jobs/0123456789/items/0/cancel").status_code == 404
    finally:
        pipeline.JOBS.pop(job.id, None)


def test_the_pipeline_without_test_design_takes_the_whole_text(monkeypatch, fake_llm):
    p = _project("Без дизайна", requirements={"learn_model": False}, scenarios={"enabled": False})
    job = pipeline.Job(p, [], SPEC, "", {})
    seen = []

    async def process(project, item, sc):
        seen.append(sc["instructions"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)
    arun(job.run())
    assert job.status == "done" and seen == [SPEC] and fake_llm.calls == []


def test_the_pipeline_fails_clearly_without_requirements(fake_llm):
    p = _project("Пусто")
    job = pipeline.Job(p, [], "  ", "", {})
    arun(job.run())
    assert job.status == "error" and "Нет требований" in job.error and fake_llm.calls == []
