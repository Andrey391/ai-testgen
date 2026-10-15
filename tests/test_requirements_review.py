"""The Requirements tab reviews requirements first: the reviewer subagent, on a clean context of its own, finds
the defects of the specification and where it disagrees with the lifecycle of the system. Test cases are
generated in the pipeline - from the reviewed requirements, or after the pipeline's own review; findings that
block test design wait for a person."""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from fastapi.testclient import TestClient

from fakes import Resp, dump, tool
from helpers import arun
from testgen import analyses, auth, knowledge, pipeline, projects, review, validation
from testgen.scenarios import ScenarioBatch, ScenarioPlan

SPEC = "# Заказы\n1. Покупатель оформляет заказ.\n2. Менеджер редактирует отправленный заказ."
SHOP = {"summary": "Интернет-магазин",
        "entities": [{"name": "Заказ", "depends_on": ["Товар"],
                      "lifecycle": "новый → оплачен (покупатель) → отправлен (менеджер); отправленный не меняется"},
                     {"name": "Товар"}],
        "roles": [{"name": "Покупатель", "capabilities": "оформляет и оплачивает свои заказы"},
                  {"name": "Менеджер", "capabilities": "отправляет оплаченные заказы"}]}
CONFLICT = {"kind": "contradiction", "severity": "critical", "location": "п. 2",
            "model_rule": "отправленный не меняется", "problem": "Менеджер редактирует отправленный заказ",
            "suggestion": "запретить правку после отправки"}


def _project(name: str, **pipe) -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    cfg = projects.get(p["id"])["pipeline"]
    for stage, values in pipe.items():
        cfg[stage].update(values)
    projects.update(p["id"], {"pipeline": cfg})
    return projects.get(p["id"])


def _validation(verdict="needs_work") -> Resp:
    return Resp(parsed=validation.Validation(
        summary="Нет критериев приёмки", score=60, verdict=verdict,
        sections=[validation.SectionCheck(section="Общие сведения", status="partial", comment="")],
        findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 1",
                                     problem="нет критерия", suggestion="добавить")],
        questions=["Кто отменяет заказ?"]))


def _report(*conflicts) -> Resp:
    return tool("report", summary="ТЗ расходится с жизненным циклом заказа", conflicts=list(conflicts),
                questions=["Можно ли править отправленный заказ?"])


def _task(kw) -> str:
    task = kw["messages"][-1]["content"]
    return task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)


def _batch(task: str) -> Resp:
    a, b = map(int, re.search(r"scenarios (\d+)–(\d+)", task).groups())
    return Resp(parsed=ScenarioBatch.model_validate({"scenarios": [
        {"title": f"Сценарий {i}", "type": "positive", "priority": "high", "preconditions": "",
         "instructions": f"Шаги {i}", "expected_result": "", "gherkin": ""} for i in range(a, b + 1)]}))


def _plan() -> Resp:
    return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
        {"title": "Сценарий 1", "type": "positive", "layer": "ui", "priority": "high", "covers": "x"}]}))


# ---------- the subagent ----------

def test_the_reviewer_works_on_a_clean_context_with_the_lifecycle(fake_llm):
    p = _project("Ревью", requirements={"learn_model": False})
    knowledge.save(p["id"], SHOP)

    def script(kind, kw):
        if kw.get("output_format") is validation.Validation:
            return _validation()
        return _report(CONFLICT, CONFLICT | {"kind": "new", "severity": "minor", "problem": "новый статус «архив»"})
    fake_llm.script = script
    out = arun(review.review(p, SPEC))

    assert out["verdict"] == "not_ready" and out["errors"] == {}
    assert out["validation"]["score"] == 60
    lc = out["lifecycle"]
    assert [c["kind"] for c in lc["conflicts"]] == ["contradiction", "new"]
    assert lc["model"] == {"entities": 2, "roles": 2, "state": "none"}
    assert review.blocking(out)
    assert "расхождений 1, критичных 1" in review.summary(out)

    [(_, kw)] = [c for c in fake_llm.calls if c[0] == "create"]
    # A clean conversation: one task, the document and the model - nothing else of the studio.
    assert kw["messages"] == [{"role": "user", "content": "Review the document against the lifecycle of the system."}]
    sent = dump(kw)
    assert "Менеджер редактирует отправленный заказ" in sent and "отправленный не меняется" in sent
    assert "independent reviewer of requirements" in sent
    assert [t["name"] for t in kw["tools"]] == ["report"]       # the whole model fits: nothing to look up


def test_a_big_model_is_looked_up_and_one_failed_check_keeps_the_other(monkeypatch, fake_llm):
    p = _project("Большая модель", requirements={"learn_model": False})
    knowledge.save(p["id"], SHOP)
    monkeypatch.setattr(knowledge, "fits", lambda pid, budget=0: False)
    asked = []
    monkeypatch.setattr(knowledge, "lookup", lambda pid, q: asked.append(q) or "Заказ: отправленный не меняется")
    turns = []

    def script(kind, kw):
        if kw.get("output_format") is validation.Validation:
            raise RuntimeError("перегружено")
        turns.append(kw)
        return tool("model_lookup", query="Заказ") if len(turns) == 1 else _report(CONFLICT | {"severity": "major"})
    fake_llm.script = script
    logs = []
    out = arun(review.review(p, SPEC, log=logs.append))

    assert asked == ["Заказ"]
    assert "model_lookup" in [t["name"] for t in turns[0]["tools"]]
    assert "Заказ: отправленный не меняется" in dump(turns[1]["messages"])
    assert out["validation"] is None and "документ" in out["errors"]
    assert out["verdict"] == "needs_work" and not review.blocking(out)
    assert any("Ревьюер смотрит модель приложения: «Заказ»" in x for x in logs)


# ---------- the Requirements tab ----------

def test_the_tab_reviews_and_keeps_the_report(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is validation.Validation:
            return _validation("ready")
        if fmt is None:
            return _report()
        raise RuntimeError("модель приложения недоступна")      # knowledge.extract: a help, not a step
    fake_llm.script = script
    p = _project("Вкладка")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "requirements": SPEC}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "analysis" and kinds[-1] == "done" and "review" in kinds, events
    assert not any(e.get("output_format") in (ScenarioPlan, ScenarioBatch) for _, e in fake_llm.calls)   # no test design
    a = events[-1]["analysis"]
    assert a["status"] == "done" and a["review"]["verdict"] == "ready" and a["scenarios"] == []
    assert a["validation"]["verdict"] == "ready" and not a.get("learned")
    # The findings are not settled yet: the model does not learn from the requirements.
    assert any("когда замечания, блокирующие тест-дизайн, будут" in e["text"] for e in events if e["type"] == "log")
    [listed] = [x for x in client.get(f"/api/projects/{p['id']}/analyses").json() if x["id"] == a["id"]]
    assert listed["review"]["verdict"] == "ready"

    # Reviewed again: the same analysis, a new report.
    fake_llm.script = lambda kind, kw: _validation() if kw.get("output_format") is validation.Validation \
        else _report(CONFLICT) if kw.get("output_format") is None else Resp(stop_reason="refusal")
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "analysis_id": a["id"]}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["type"] == "done" and events[-1]["analysis"]["id"] == a["id"]
    assert analyses.get(a["id"])["review"]["verdict"] == "not_ready"
    assert client.post("/api/requirements/review", json={"project_id": p["id"]}).status_code == 400


# ---------- the pipeline ----------

def test_the_pipeline_designs_test_cases_from_reviewed_requirements(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p = _project("Из ревью", requirements={"learn_model": False}, scenarios={"select": "all"})
    a = analyses.create(p["id"], SPEC, "", "ann")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = client.post(f"/api/projects/{p['id']}/jobs", json={"review_id": a["id"]})
    assert r.status_code == 400 and "не проверены" in r.json()["detail"]

    analyses.finish(p["id"], a["id"])
    analyses.set_review(p["id"], a["id"], {"validation": None, "errors": {}, "verdict": "not_ready",
                                           "lifecycle": {"summary": "", "conflicts": [CONFLICT], "questions": []}})
    # A conflict with the lifecycle not settled: no test design from these requirements, nobody skips it.
    r = client.post(f"/api/projects/{p['id']}/jobs", json={"review_id": a["id"]})
    assert r.status_code == 409 and "расхождения с жизненным циклом: 1" in r.json()["detail"]
    assert analyses.view(analyses.get(a["id"]))["readiness"]["step"] == "lifecycle"
    analyses.decide(p["id"], a["id"], ["l0"], review.DISMISSED, note="правка разрешена", user="ann")
    assert analyses.view(analyses.get(a["id"]))["readiness"]["ready"]
    earlier = analyses.add_scenario(p["id"], a["id"], {"title": "Прежний", "instructions": "Шаги"})
    assert client.post(f"/api/projects/{p['id']}/jobs", json={"review_id": a["id"], "text": "x"}).status_code == 400

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is ScenarioPlan:
            return _plan()
        if fmt is ScenarioBatch:
            return _batch(_task(kw))
        raise AssertionError("the requirements are not reviewed again")
    fake_llm.script = script
    job = pipeline.Job(p, [], "", "", {}, user="ann", review_id=a["id"])
    seen = []

    async def process(project, item, sc):
        seen.append(sc["title"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)
    arun(job.run())

    # Settled in the Requirements tab before: no wait, no second review.
    assert job.status == "done", job.error
    assert seen == ["Сценарий 1"] and job.analysis_id == a["id"] and job.review["verdict"] == "not_ready"
    # The run says how the findings were settled, not the verdict the reviewer gave before that.
    assert any("разобраны во вкладке «Требования»: замечаний 1: исправлено в ТЗ 0, не актуально 1" in x["text"]
               and x["level"] == "info" for x in job.log)
    shown = client.get(f"/api/jobs/{job.id}").json()
    assert shown["review_state"]["settled"] and shown["review_state"]["dismissed"] == 1
    assert [(t["id"], t["status"], t["note"]) for t in shown["review"]["themes"]] == [
        ("l0", "dismissed", "правка разрешена")]
    after = analyses.get(a["id"])
    assert after["status"] == "done"
    assert [s["title"] for s in after["scenarios"]] == ["Сценарий 1", "Прежний"]      # the earlier one stays
    assert earlier["id"] in [s["id"] for s in after["scenarios"]]


def test_blocking_findings_wait_for_a_person(monkeypatch, fake_llm):
    p = _project("Ждёт", requirements={"learn_model": False, "validate": True}, scenarios={"select": "all"})

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is validation.Validation:
            return _validation()
        if fmt is ScenarioPlan:
            return _plan()
        if fmt is ScenarioBatch:
            return _batch(_task(kw))
        return _report(CONFLICT)
    fake_llm.script = script
    job = pipeline.Job(p, [], SPEC, "", {}, user="bob")
    seen = []

    async def process(project, item, sc):
        seen.append(sc["title"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)

    async def go():
        task = asyncio.create_task(job.run())
        while job.status != "awaiting_review":
            assert not task.done(), job.error
            await asyncio.sleep(0.01)
        assert not any(kw.get("output_format") is ScenarioPlan for _, kw in fake_llm.calls)   # nothing designed yet
        assert job.state()["review"]["verdict"] == "not_ready"
        await asyncio.sleep(0.05)
        assert job.status == "awaiting_review"      # it waits for the findings, not for a button
        # The person settles every finding in the Requirements tab: the run goes on by itself.
        a = analyses.get(job.analysis_id)
        analyses.decide(p["id"], a["id"], [it["id"] for it in review.unresolved(a["review"])], review.DISMISSED,
                        note="не про это", user="bob")
        job.model_confirmed()
        await task
    arun(go())
    assert job.status == "done" and seen == ["Сценарий 1"]
    assert analyses.get(job.analysis_id)["review"]["verdict"] == "not_ready"
    assert any("Требования разобраны" in e["text"] for e in job.log)

    # Stopped while it waits: nothing is designed.
    fake_llm.calls.clear()
    job = pipeline.Job(p, [], SPEC, "", {})

    async def stop():
        task = asyncio.create_task(job.run())
        while job.status != "awaiting_review":
            await asyncio.sleep(0.01)
        job.cancel()
        await asyncio.wait_for(task, 5)
    arun(stop())
    assert job.status == "cancelled" and job.items == []
    assert not any(kw.get("output_format") is ScenarioPlan for _, kw in fake_llm.calls)


def test_the_lifecycle_is_checked_again_alone(monkeypatch, fake_llm):
    """The check against the lifecycle failed (the API timed out): checked again, the check of the document and
    the decisions on it stay; until then test design waits."""
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p = _project("Сверка", requirements={"learn_model": False})
    a = analyses.create(p["id"], SPEC, "", "ann")
    analyses.finish(p["id"], a["id"])
    v = _validation().parsed_output.model_dump()
    analyses.set_review(p["id"], a["id"], {"validation": v, "lifecycle": None, "verdict": "needs_work",
                                           "errors": {"жизненный цикл": "Gateway Timeout"}})
    analyses.decide(p["id"], a["id"], ["d0"], review.DISMISSED, note="есть в приложении", user="ann")
    analyses.decide(p["id"], a["id"], ["vq0"], review.DISMISSED, note="никто", user="ann")
    r = analyses.view(analyses.get(a["id"]))["readiness"]
    assert not r["ready"] and r["done"]["findings"] and not r["done"]["lifecycle"] and r["step"] == "lifecycle"
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.post(f"/api/projects/{p['id']}/jobs", json={"review_id": a["id"]}).status_code == 409

    fake_llm.script = lambda kind, kw: _report(CONFLICT)
    body = client.post(f"/api/analyses/{a['id']}/review/lifecycle").json()
    rv = analyses.get(a["id"])["review"]
    assert rv["errors"] == {} and rv["lifecycle"]["conflicts"][0]["problem"] == CONFLICT["problem"]
    assert rv["resolutions"]["d0"]["status"] == review.DISMISSED          # the decisions on the document stay
    assert body["analysis"]["readiness"]["lifecycle_left"] == 2           # the conflict and the question
    assert client.post("/api/analyses/0123456789/review/lifecycle").status_code == 404
