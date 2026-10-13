"""An analysis of requirements cut off by a restart of the studio: it reads as interrupted at once (no beats of
its work), and the work goes on from what was done - the finished check of a review, the plan of a design with
the scenarios detailed by then."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from fastapi.testclient import TestClient

from fakes import Resp, tool
from helpers import arun
from testgen import analyses, auth, fs, pipeline, projects, review, scenarios, validation
from testgen.scenarios import ScenarioBatch, ScenarioPlan

SPEC = "# Заказы\n1. Покупатель оформляет заказ.\n2. Менеджер отправляет оплаченный заказ."
N = 5


def _project(name: str, **pipe) -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    cfg = projects.get(p["id"])["pipeline"]
    for stage, values in pipe.items():
        cfg[stage].update(values)
    projects.update(p["id"], {"pipeline": cfg})
    return projects.get(p["id"])


def _validation() -> Resp:
    return Resp(parsed=validation.Validation(summary="Без замечаний", score=90, verdict="ready", sections=[],
                                             findings=[], questions=[]))


def _report() -> Resp:
    return tool("report", summary="Расхождений нет", conflicts=[], questions=[])


def _task(kw) -> str:
    task = kw["messages"][-1]["content"]
    return task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)


def _plan() -> Resp:
    return Resp(parsed=ScenarioPlan.model_validate({"feature": "Заказы", "assumptions": [], "scenarios": [
        {"title": f"Сценарий {i}", "type": "positive", "layer": "ui", "priority": "high", "covers": "x"}
        for i in range(1, N + 1)]}))


def _batch(task: str) -> Resp:
    a, b = map(int, re.search(r"scenarios (\d+)–(\d+)", task).groups())
    return Resp(parsed=ScenarioBatch.model_validate({"scenarios": [
        {"title": f"Сценарий {i}", "type": "positive", "priority": "high", "preconditions": "",
         "instructions": f"Шаги {i}", "expected_result": "", "gherkin": ""} for i in range(a, b + 1)]}))


def _age(pid: str, aid: str, **fields) -> None:
    a = fs.read_json(analyses._path(pid, aid))
    fs.write_json(analyses._path(pid, aid), a | fields)


def test_a_cut_off_analysis_reads_as_interrupted_at_once():
    p = _project("Прервано")
    a = analyses.create(p["id"], SPEC, work="review")
    assert analyses.get(a["id"])["status"] == "running"
    try:
        analyses.restart(p["id"], a["id"])
        raise AssertionError("an analysis in work is not started twice")
    except analyses.Conflict:
        pass

    # No beat for a minute: the studio that worked on it is gone - not an hour later.
    _age(p["id"], a["id"], beat=time.time() - analyses.BEAT_STALE - 1)
    cut = analyses.get(a["id"])
    assert cut["status"] == "error" and cut["error"] == analyses.INTERRUPTED["review"]
    assert analyses.restart(p["id"], a["id"])["status"] == "running"

    # Written by a version without beats: the hour still decides.
    old = analyses.create(p["id"], SPEC)
    doc = fs.read_json(analyses._path(p["id"], old["id"]))
    doc.pop("beat")
    fs.write_json(analyses._path(p["id"], old["id"]), doc | {"started": time.time() - 600})
    assert analyses.get(old["id"])["status"] == "running"


def test_work_on_an_analysis_beats(monkeypatch):
    p = _project("Пульс")
    a = analyses.create(p["id"], SPEC)
    _age(p["id"], a["id"], beat=time.time() - 30)
    monkeypatch.setattr(analyses, "BEAT", 0.02)

    async def go():
        async with analyses.working(p["id"], a["id"]):
            await asyncio.sleep(0.2)
    arun(go())
    assert time.time() - fs.read_json(analyses._path(p["id"], a["id"]))["beat"] < 5


def test_a_review_goes_on_with_the_check_it_finished(fake_llm):
    p = _project("Ревью по частям", requirements={"learn_model": False})

    def script(kind, kw):
        if kw.get("output_format") is validation.Validation:
            return _validation()
        raise RuntimeError("сбой сети")
    fake_llm.script = script
    kept = {}
    out = arun(review.review(p, SPEC, keep=kept.__setitem__))
    assert list(kept) == ["validation"] and "жизненный цикл" in out["errors"]

    # Again: the finished check is not asked again, only the other one.
    fake_llm.calls.clear()

    def again(kind, kw):
        assert kw.get("output_format") is not validation.Validation, "the finished check is asked again"
        return _report()
    fake_llm.script = again
    logs = []
    out = arun(review.review(p, SPEC, log=logs.append, done=kept, keep=kept.__setitem__))
    assert out["errors"] == {} and out["validation"]["score"] == 90 and out["lifecycle"]["conflicts"] == []
    assert any("уже готово и не повторяется: проверка документа по стандарту" in x for x in logs)


def test_the_tab_reviews_again_from_the_finished_check(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p = _project("Вкладка по частям")
    a = analyses.create(p["id"], SPEC, work="review")
    fake_llm.script = lambda kind, kw: _validation()
    v = arun(validation.validate(p, SPEC))
    analyses.keep_review_part(p["id"], a["id"], review.parts_key(p, SPEC), "validation", v)
    _age(p["id"], a["id"], beat=time.time() - analyses.BEAT_STALE - 1)        # the studio restarted
    assert analyses.get(a["id"])["status"] == "error"

    def script(kind, kw):
        fmt = kw.get("output_format")
        assert fmt is not validation.Validation, "the finished check is asked again"
        return _report() if fmt is None else Resp(stop_reason="refusal")
    fake_llm.script = script
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "analysis_id": a["id"]}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["type"] == "done", events
    assert any("уже готово" in e["text"] for e in events if e["type"] == "log")
    after = analyses.get(a["id"])
    assert after["review"]["validation"]["score"] == 90 and "review_parts" not in after

    # Another text (or standard, notes, settings): nothing is reused.
    assert analyses.review_parts(after | {"review_parts": {"key": "x", "validation": v, "at": time.time()}},
                                 review.parts_key(p, SPEC)) == {}


def test_a_pipeline_run_goes_on_designing_from_its_plan(monkeypatch, fake_llm):
    p = _project("Конвейер по частям", requirements={"learn_model": False, "validate": False},
                 scenarios={"select": "all"})
    monkeypatch.setattr(scenarios, "PARALLEL", 1)       # batches one after another: the first is ready, the next fails

    def script(kind, kw):
        fmt = kw.get("output_format")
        if fmt is ScenarioPlan:
            return _plan()
        task = _task(kw)
        if "scenarios 4–" in task:
            raise RuntimeError("студия перезапускается")
        return _batch(task)
    fake_llm.script = script
    job = pipeline.Job(p, [], SPEC, "", {}, user="ann")
    arun(job.run())
    assert job.status == "error" and not job.scenarios
    aid = job.analysis_id
    cut = analyses.get(aid)
    assert [s["title"] for s in cut["scenarios"]] == ["Сценарий 1", "Сценарий 2", "Сценарий 3"]
    assert cut["plan"]["job"] == job.id and len(cut["plan"]["scenarios"]) == N
    planned = [s["id"] for s in cut["plan"]["scenarios"]]

    # Resumed: the requirements are not loaded again, the plan is not asked again, only 4–5 are detailed.
    fake_llm.calls.clear()

    def rest(kind, kw):
        assert kw.get("output_format") is ScenarioBatch, "only the rest of the plan is detailed"
        return _batch(_task(kw))
    fake_llm.script = rest
    again = pipeline.Job.resumed(projects.get(p["id"]), json.loads(json.dumps(job.state())), {}, "ann")
    seen = []

    async def process(project, item, sc):
        seen.append(sc["title"])
        item["status"] = "done"
    monkeypatch.setattr(again, "_process", process)
    arun(again.run())

    assert again.status == "done", again.error
    assert [re.search(r"scenarios (\d+)–(\d+)", _task(kw)).groups() for _, kw in fake_llm.calls] == [("4", "5")]
    assert again.analysis_id == aid and seen == [f"Сценарий {i}" for i in range(1, N + 1)]
    after = analyses.get(aid)
    assert after["status"] == "done" and "plan" not in after
    assert [s["id"] for s in after["scenarios"]] == planned
    assert [s["instructions"] for s in after["scenarios"]] == [f"Шаги {i}" for i in range(1, N + 1)]
    assert [x["id"] for x in analyses.list_analyses(p["id"])] == [aid]       # no second analysis
    assert any("загружены в прошлый раз" in e["text"] for e in again.log)
