"""A person works through the reviewer's findings: answers how it should be (the editor fixes the requirements
by the answer) or marks a finding not relevant. The application model learns from the requirements only when
every finding is settled."""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from fakes import Resp, tool
from helpers import arun
from testgen import analyses, auth, knowledge, pipeline, projects, review, validation
from testgen.scenarios import ScenarioBatch, ScenarioPlan

SPEC = "# Заказы\n1. Покупатель оформляет заказ.\n2. Менеджер редактирует отправленный заказ."
CONFLICT = {"kind": "contradiction", "severity": "critical", "location": "п. 2",
            "model_rule": "отправленный не меняется", "problem": "Менеджер редактирует отправленный заказ",
            "suggestion": "запретить правку после отправки"}
NEW = CONFLICT | {"kind": "new", "severity": "minor", "problem": "новый статус «архив»"}


def _project(name: str, **pipe) -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    cfg = projects.get(p["id"])["pipeline"]
    for stage, values in pipe.items():
        cfg[stage].update(values)
    projects.update(p["id"], {"pipeline": cfg})
    return projects.get(p["id"])


def _findings() -> validation.Validation:
    return validation.Validation(
        summary="Нет критериев приёмки", score=60, verdict="needs_work", sections=[],
        findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 1",
                                     problem="Нет критерия оформления", suggestion="добавить критерий")],
        questions=["Кто отменяет заказ?"])


def _validation() -> Resp:
    return Resp(parsed=_findings())


def _review_script(kind, kw):
    if kw.get("output_format") is validation.Validation:
        return _validation()
    return tool("report", summary="Расходится", conflicts=[CONFLICT, NEW], questions=[])


def test_findings_have_ids_and_decisions():
    report = {"validation": _findings().model_dump(),
              "lifecycle": {"summary": "", "conflicts": [NEW, CONFLICT], "questions": ["Есть ли архив?"]}}
    assert [(x["id"], x["kind"]) for x in review.items(report)] == [
        ("d0", "defect"), ("l1", "lifecycle"), ("vq0", "question"), ("lq0", "question")]     # "new" is not a defect
    assert len(review.unresolved(report)) == 4

    review.decide(report, ["d0", "l1"], "todo", "", user="ann")
    review.decide(report, ["lq0"], "dismissed", note="архива не будет")
    try:
        review.decide(report, ["vq0"], "todo", "")
        raise AssertionError("a question needs an answer")
    except ValueError:
        pass
    try:
        review.decide(report, ["x9"], "dismissed")
        raise AssertionError("an unknown finding")
    except KeyError:
        pass
    assert [x["status"] for x in review.items(report)] == ["todo", "todo", "open", "dismissed"]
    review.decide(report, ["vq0"], "todo", "Менеджер")
    review.fixed(report, ["d0", "l1", "vq0"])
    assert review.resolved(report)
    notes = review.notes(report)
    assert "do not report it again: Есть ли архив? (why: архива не будет)" in notes
    assert "Кто отменяет заказ? -> Менеджер" in notes
    review.decide(report, ["lq0"], "open")
    assert not review.resolved(report)


def test_the_editor_fixes_the_requirements_by_the_answers(fake_llm):
    p = _project("Редактор")
    report = {"validation": _findings().model_dump(), "lifecycle": {"summary": "", "conflicts": [CONFLICT],
                                                                              "questions": []}}
    review.decide(report, ["d0"], "todo", "")
    review.decide(report, ["l0"], "todo", "Менеджер не редактирует отправленный заказ")
    review.decide(report, ["vq0"], "todo", "Отменяет менеджер")
    asked = []

    def script(kind, kw):
        asked.append(kw)
        if len(asked) == 1:
            return Resp(parsed=review.Revision(summary="Исправлено", edits=[
                review.Edit(item="l0", find="2. Менеджер редактирует\n отправленный заказ.",     # other line breaks
                            replace="2. Менеджер не редактирует отправленный заказ."),
                review.Edit(item="d0", find="1. Покупатель оформляет заказ.",
                            replace="1. Покупатель оформляет заказ; заказ получает статус «новый»."),
                review.Edit(item="vq0", find="нет такого текста", replace="x")]))
        return Resp(parsed=review.Revision(summary="Добавлено", edits=[
            review.Edit(item="vq0", find="отправленный заказ.", replace="отправленный заказ.\n3. Заказ отменяет менеджер.")]))
    fake_llm.script = script
    out = arun(review.revise(p, SPEC, report))

    assert out["fixed"] == ["d0", "l0", "vq0"] and out["failed"] == {}
    assert out["requirements"] == ("# Заказы\n1. Покупатель оформляет заказ; заказ получает статус «новый».\n"
                                   "2. Менеджер не редактирует отправленный заказ.\n3. Заказ отменяет менеджер.")
    task = asked[0]["messages"][0]["content"]
    assert "the answer of the authors: Менеджер не редактирует" in task and "none - apply the suggestion" in task
    assert "[vq0]" in asked[1]["messages"][0]["content"] and "[d0]" not in asked[1]["messages"][0]["content"]


def test_the_model_learns_only_when_every_finding_is_settled(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    learned = []

    async def extract(project, text, user=""):
        learned.append(text)
        return knowledge.get(project["id"])
    monkeypatch.setattr(knowledge, "extract", extract)
    fake_llm.script = _review_script
    p = _project("Замечания", requirements={"learn_model": True})
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "requirements": SPEC}) as r:
        list(r.iter_lines())
    [aid] = [a["id"] for a in analyses.list_analyses(p["id"])]
    assert learned == []        # findings to settle: the lifecycle waits
    url = f"/api/analyses/{aid}/review"
    r = client.post(f"/api/projects/{p['id']}/knowledge/extract", json={"analysis_id": aid})
    assert r.status_code == 409 and "осталось 3" in r.json()["detail"]
    assert client.post(f"{url}/fix").status_code == 400                 # nothing answered yet

    r = client.put(f"{url}/findings", json={"ids": ["vq0"], "status": "todo", "answer": ""})
    assert r.status_code == 400                                         # a question needs an answer
    assert client.put(f"{url}/findings", json={"ids": ["zz"], "status": "dismissed"}).status_code == 404
    r = client.put(f"{url}/findings", json={"ids": ["vq0"], "status": "dismissed", "note": "не про заказы"})
    assert r.status_code == 200 and r.json()["unresolved"] == 2 and learned == []
    r = client.put(f"{url}/findings", json={"ids": ["d0", "l0"], "status": "todo"})
    assert r.json()["unresolved"] == 2

    fake_llm.script = lambda kind, kw: Resp(parsed=review.Revision(summary="Правка после отправки запрещена", edits=[
        review.Edit(item="l0", find="Менеджер редактирует отправленный заказ",
                    replace="Менеджер не редактирует отправленный заказ"),
        review.Edit(item="d0", find="ничего такого", replace="")]))
    r = client.post(f"{url}/fix")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fixed"] == ["l0"] and "d0" in body["failed"] and body["unresolved"] == 1 and learned == []
    a = analyses.get(aid)
    assert "Менеджер не редактирует отправленный заказ" in a["requirements"]
    assert a["revisions"][0]["requirements"] == SPEC and a["revisions"][0]["fixed"] == ["l0"]
    assert a["review"]["resolutions"]["d0"]["error"].startswith("не найден фрагмент")

    fake_llm.script = lambda kind, kw: Resp(parsed=review.Revision(summary="Критерий", edits=[
        review.Edit(item="d0", find="1. Покупатель оформляет заказ.", replace="1. Покупатель оформляет заказ: статус «новый».")]))
    body = client.post(f"{url}/fix").json()
    # The last finding settled: the model learns from the corrected requirements, once.
    assert body["unresolved"] == 0 and learned == [analyses.get(aid)["requirements"]]
    assert "статус «новый»" in learned[0] and "не редактирует" in learned[0]
    assert body["analysis"]["learned"] and body["model"].startswith("Модель приложения")
    assert "error" not in analyses.get(aid)["review"]["resolutions"]["d0"]

    # Reviewed again: the reviewer is told what the authors decided.
    fake_llm.calls.clear()
    fake_llm.script = _review_script
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "analysis_id": aid}) as r:
        list(r.iter_lines())
    sent = [kw["messages"][0]["content"] for _, kw in fake_llm.calls]
    assert sent and all("do not report it again: Кто отменяет заказ? (why: не про заказы)" in x for x in sent)
    assert len(learned) == 1


def test_the_pipeline_does_not_teach_the_model_unsettled_requirements(monkeypatch, fake_llm):
    p = _project("Конвейер", requirements={"learn_model": True, "validate": True}, scenarios={"select": "all"})
    learned = []

    async def extract(project, text, user=""):
        learned.append(text)
        return knowledge.get(project["id"])
    monkeypatch.setattr(knowledge, "extract", extract)

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is ScenarioPlan:
            return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
                {"title": "Сценарий 1", "type": "positive", "layer": "ui", "priority": "high", "covers": "x"}]}))
        if fmt is ScenarioBatch:
            return Resp(parsed=ScenarioBatch.model_validate({"scenarios": [
                {"title": "Сценарий 1", "type": "positive", "priority": "high", "preconditions": "",
                 "instructions": "Шаги", "expected_result": "", "gherkin": ""}]}))
        return _review_script(kind, kw)
    fake_llm.script = script
    job = pipeline.Job(p, [], SPEC, "", {}, user="bob")

    async def process(project, item, sc):
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)

    async def go():
        task = asyncio.create_task(job.run())
        while job.status != "awaiting_review":
            assert not task.done(), job.error
            await asyncio.sleep(0.01)
        job.accept_review("bob")
        await task
    arun(go())
    assert job.status == "done", job.error
    assert learned == [] and not analyses.get(job.analysis_id).get("learned")
    assert any("замечаний ревьюера не разобрано 3" in e["text"] for e in job.log)
