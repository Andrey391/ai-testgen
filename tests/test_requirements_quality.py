"""What keeps requirements and scenarios traceable and the fixed text honest: a scenario keeps the requirements it
checks from the plan (coverage of the numbered requirements), a defect of the application an answer reports goes
to a list of its own instead of the text, and requirements the agent fixed are reviewed again before test design."""
from __future__ import annotations

import json
import re
import time
import uuid

from fastapi.testclient import TestClient

from fakes import Resp
from helpers import arun
from testgen import analyses, auth, projects, review, validation
from testgen.scenarios import PlannedScenario, Scenario, ScenarioBatch, ScenarioPlan

SPEC = ("# Аккаунт\nM1-REQ-01. Регистрация с согласием.\nM1-REQ-02. Вход по email.\n"
        "M1-REQ-03. Удаление аккаунта.\nИсточник: 152-ФЗ, UTF-8.")


def _project(name: str, **requirements) -> dict:
    p = projects.create(f"{name} {uuid.uuid4().hex[:6]}", base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    cfg = projects.get(p["id"])["pipeline"]
    cfg["requirements"].update(requirements)
    projects.update(p["id"], {"pipeline": cfg})
    return projects.get(p["id"])


def test_numbered_requirements_and_their_coverage():
    assert re.findall(analyses.REQ_ID, SPEC) == ["M1-REQ-01", "M1-REQ-02", "M1-REQ-03"]
    assert analyses.req_ids("M1-REQ-01, M1-REQ-02;M1-REQ-01") == ["M1-REQ-01", "M1-REQ-02"]
    a = {"requirements": SPEC, "scenarios": [{"req_ids": ["M1-REQ-01"]}, {"req_ids": ["M1-REQ-01", "M1-REQ-03"]},
                                             {"req_ids": ["M1-REQ-02"], "pending": True}]}
    assert analyses.coverage(a) == {"ids": ["M1-REQ-01", "M1-REQ-02", "M1-REQ-03"],
                                    "covered": {"M1-REQ-01": 2, "M1-REQ-03": 1}, "missing": ["M1-REQ-02"]}
    assert analyses.coverage({"requirements": "Просто текст без номеров", "scenarios": []})["ids"] == []


def test_a_detailed_scenario_keeps_what_the_plan_said_it_checks(monkeypatch, fake_llm):
    """The plan names the requirements of every scenario; the details of a batch do not repeat them, and the
    analysis keeps them - a person sees the coverage and edits the ids."""
    import server
    monkeypatch.setattr(auth, "ENABLED", False)

    def script(kind, kw):
        task = kw["messages"][-1]["content"]
        task = task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)
        if not re.search(r"scenarios (\d+)–(\d+)", task):
            assert "req_ids" in task
            return Resp(parsed=ScenarioPlan(feature="Аккаунт", assumptions=[], scenarios=[
                PlannedScenario(title="Регистрация", type="positive", priority="high", covers="согласие",
                                req_ids=["M1-REQ-01"]),
                PlannedScenario(title="Удаление", type="state_transition", priority="high", covers="анонимизация",
                                req_ids=["M1-REQ-03"])]))
        return Resp(parsed=ScenarioBatch(scenarios=[
            Scenario(title=t, type="positive", priority="high", preconditions="", instructions=f"Шаги: {t}",
                     expected_result="", gherkin="") for t in ("Регистрация", "Удаление")]))
    fake_llm.script = script
    p = _project("Покрытие")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": SPEC,
                                                       "stream": True}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    final = events[-1]["analysis"]
    assert [(s["covers"], s["req_ids"]) for s in final["scenarios"]] == [("согласие", ["M1-REQ-01"]),
                                                                         ("анонимизация", ["M1-REQ-03"])]
    assert final["coverage"]["missing"] == ["M1-REQ-02"]

    # A person adds the missing requirement to a scenario: the field comes as the text of the form.
    aid, scid = final["id"], final["scenarios"][0]["id"]
    res = client.put(f"/api/analyses/{aid}/scenarios/{scid}", json={"req_ids": "M1-REQ-01, M1-REQ-02"})
    assert res.status_code == 200, res.text
    assert analyses.coverage(analyses.get(aid))["missing"] == []


def _todo_report() -> dict:
    clean = validation.Validation(summary="", score=50, verdict="needs_work", sections=[], questions=[], findings=[
        validation.Finding(criterion="complete", severity="major", location="M1-REQ-03",
                           problem="Не сказано, что видит гость у удалённого", suggestion="описать")])
    report = {"validation": clean.model_dump(), "lifecycle": {"summary": "", "conflicts": [], "questions": []}}
    review.decide(report, ["d0"], "todo",
                  "«Удалённый игрок» для всех. Исправить: сейчас гость видит «Скрытый игрок».")
    return report


def test_a_defect_of_the_application_is_not_written_into_the_requirements(fake_llm):
    p = _project("Дефекты")
    edit = review.Edit(item="d0", find="M1-REQ-03. Удаление аккаунта.",
                       replace="M1-REQ-03. Удаление аккаунта: для всех — «Удалённый игрок».")
    defect = review.Defect(item="d0", problem="гость видит удалённого как «Скрытый игрок»",
                           expected="«Удалённый игрок» для всех")
    fake_llm.script = lambda kind, kw: Resp(parsed=review.Revision(summary="Метка", edits=[edit], defects=[defect]))
    report = _todo_report()
    out = arun(review.revise(p, SPEC, report))
    system = fake_llm.calls[0][1]["system"]
    system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
    assert "defect of the application" in system
    assert out["fixed"] == ["d0"] and "Скрытый" not in out["requirements"]
    assert out["defects"] == [{"items": ["d0"], "problem": defect.problem, "expected": defect.expected}]

    a = analyses.create(p["id"], SPEC, "", "ann")
    analyses.set_review(p["id"], a["id"], report)
    analyses.revise(p["id"], a["id"], out, "ann", run="r1")
    analyses.revise(p["id"], a["id"], out | {"fixed": []}, "ann", run="r1")     # the same defect is listed once
    [d] = analyses.get(a["id"])["defects"]
    assert d["problem"] == defect.problem and d["items"] == ["d0"] and d["by"] == "ann"


def test_requirements_fixed_by_the_agent_are_reviewed_again():
    p = _project("Повторная проверка", learn_model=False, confirm_model=False)
    a = analyses.create(p["id"], SPEC, "", "ann")
    clean = validation.Validation(summary="", score=100, verdict="ready", sections=[], findings=[], questions=[])
    analyses.set_review(p["id"], a["id"], {"validation": clean.model_dump(),
                                           "lifecycle": {"summary": "", "conflicts": [], "questions": []}})
    assert analyses.readiness(analyses.get(a["id"]))["ready"]

    # The agent fixed the text after the review: test design waits for the fixed text to be reviewed.
    analyses._change(p["id"], a["id"], lambda x: x.setdefault("revisions", []).append(
        {"at": time.time() + 1, "requirements": SPEC, "summary": "", "edits": [], "fixed": ["d0"]}))
    r = analyses.readiness(analyses.get(a["id"]))
    assert not r["ready"] and r["recheck"] and not r["settled"] and r["step"] == "review"
    assert "ТЗ исправлено агентом после проверки — проверьте его заново" in r["blocked"]

    # The project may do without it.
    cfg = projects.get(p["id"])["pipeline"]
    cfg["requirements"]["recheck_after_fix"] = False
    projects.update(p["id"], {"pipeline": cfg})
    assert analyses.readiness(analyses.get(a["id"]))["ready"]

    # A new version of the document is reviewed anyway: it does not ask for one more review.
    cfg["requirements"]["recheck_after_fix"] = True
    projects.update(p["id"], {"pipeline": cfg})
    analyses._change(p["id"], a["id"], lambda x: x["revisions"][-1].update(kind="version"))
    assert analyses.readiness(analyses.get(a["id"]))["ready"]
