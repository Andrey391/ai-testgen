"""The mode «Без подтверждений» (decisions.autonomous): tests are built without waiting for people - reviewed
requirements go to test design with their findings open, the model needs no confirmation, the cases take every
proposal of the reviewer and do not wait, every scenario is chosen, a similar test is reused as the model saw it."""
from __future__ import annotations

import asyncio
import uuid

from helpers import arun
from testgen import analyses, audit, cases, inbox, knowledge, pipeline, projects, validation

SPEC = "# Заказы\n1. Покупатель оформляет заказ."
SHOP = {"entities": [{"name": "Заказ", "group": "Продажи", "lifecycle": "новый → оплачен"}],
        "roles": [{"name": "Покупатель", "capabilities": "покупает"}]}


def _project(autonomous: bool) -> dict:
    p = projects.create("Без подтверждений " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    projects.update(p["id"], {"pipeline": {"decisions": {"autonomous": autonomous},
                                           "requirements": {"confirm_model": True, "learn_model": False}}})
    return projects.get(p["id"])


def _reviewed(p: dict) -> dict:
    """Requirements the reviewer found a blocking defect in, nobody settled it; the model is not confirmed."""
    knowledge.save(p["id"], SHOP)
    a = analyses.create(p["id"], SPEC, "", "ann")
    found = validation.Validation(
        summary="", score=60, verdict="needs_work", sections=[], questions=[],
        findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 1",
                                     problem="Нет критерия оформления", suggestion="добавить критерий")])
    analyses.set_review(p["id"], a["id"], {"validation": found.model_dump(),
                                           "lifecycle": {"summary": "", "conflicts": [], "questions": []}})
    return analyses.get(a["id"])


def test_reviewed_requirements_go_to_test_design_with_open_findings():
    held = analyses.readiness(_reviewed(p := _project(False)), p)
    assert not held["ready"] and held["waived"] == []
    assert any("замечания" in b for b in held["blocked"]) and any("жизненный цикл" in b for b in held["blocked"])
    # the same requirements in the mode: ready, what was skipped is told
    a = _reviewed(q := _project(True))
    r = analyses.readiness(a, q)
    assert r["ready"] and r["settled"] and r["step"] == "scenarios" and r["blocked"] == []
    assert any("замечания" in b for b in r["waived"]) and any("жизненный цикл" in b for b in r["waived"])
    analyses.require_ready(a, q)
    # not reviewed at all is not waived: the reviewer's check is what the mode keeps
    bare = analyses.create(q["id"], "# Другое\n1. Что-то", "", "ann")
    assert not analyses.readiness(bare, q)["ready"]
    # nothing waits for a person: the findings and the lifecycle are not in «Ждёт вас»
    kinds = {e["kind"] for e in inbox.project_inbox(q["id"], "ann")}
    assert "review" not in kinds and not any("Жизненный цикл" in e["title"] for e in inbox.project_inbox(q["id"], "ann"))
    assert any("Жизненный цикл" in e["title"] for e in inbox.project_inbox(p["id"], "ann"))


def _case(title: str, *findings) -> dict:
    return {"title": title, "instructions": "1. Открыть каталог", "expected_result": "Каталог открыт",
            "case_review": {"at": 1, "role": "", "test_data": [], "findings": list(findings)}}


def _finding(n: int, **kw) -> dict:
    return {"id": f"c0f{n}", "kind": kw.get("kind", "account"), "severity": kw.get("severity", "blocker"), "step": 0,
            "quote": "", "problem": "Не сказано, под каким пользователем входить", "question": "",
            "suggestion": kw.get("suggestion", ""), "confidence": 0, "decision": None}


def test_every_proposal_of_the_case_reviewer_is_taken():
    scs = [_case("Вход", _finding(0, suggestion="Войти покупателем"), _finding(1, kind="irreversible",
                                                                              suggestion="Не оплачивать"),
                 _finding(2))]
    got = cases.auto_accept(scs, 90, everything=True)
    assert [f["id"] for _, f in got] == ["c0f0", "c0f1"] and got[0][1]["decision"]["auto"] == "autonomous"
    assert "Войти покупателем" in " ".join(scs[0]["clarifications"])


def test_nothing_holds_an_item_of_the_run(monkeypatch):
    p = _project(True)
    knowledge.save(p["id"], SHOP)
    monkeypatch.setattr(pipeline, "MODEL_POLL", 0.05)
    scs = [_case("Вход", _finding(0)), _case("Заказ")]
    job = pipeline.Job(p, [], "", "", {}, cases={"connection": "x", "themes": []})
    job.scenarios = scs
    job.items = [job._item(s) | {"scenario_index": i} for i, s in enumerate(scs)]
    job.items[1]["match"] = {"kind": "test", "id": "t1", "analysis_id": "", "title": "Старый заказ",
                             "relation": "refine", "decision": ""}
    done = []

    async def process(project, item, sc):
        done.append(sc["title"])
        item["status"] = "done"
    job._process = process
    arun(asyncio.wait_for(job._process_all(p, list(zip(job.items, scs))), 5))
    # an unsettled blocker of a case, a model nobody confirmed, a similar test: none of them waits for a person
    assert done == ["Вход", "Заказ"] and not job.waits
    assert job.items[1]["match"]["decision"] == "refine"
    assert any(r["action"] == "decision.auto" for r in audit.read(project_id=p["id"]))


def test_every_scenario_is_chosen():
    p = _project(True)
    job = pipeline.Job(p, [], "", "", {})
    job.scenarios = [{"title": "A", "priority": "high"}, {"title": "B", "priority": "low"}]
    arun(asyncio.wait_for(job._select("manual"), 5))
    assert [it["title"] for it in job.items] == ["A", "B"] and job.status != "awaiting_selection"
