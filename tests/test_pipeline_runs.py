"""pipeline.run_and_record: history, re-run and flaky verdict, heal proposals, quarantine, rotation."""
from __future__ import annotations

from fakes import Resp, last_user_text, ref_for
from helpers import arun, record
from testgen import pipeline, projects, runs, storage
from testgen.runner import FailureAnalysis, HealChoice
from testgen.steps import new_step


def _settings(project, **run):
    p = projects.get(project["id"])
    p["pipeline"]["run"].update(run)
    return projects.update(project["id"], {"pipeline": p["pipeline"]})


def test_heal_review_goes_to_the_test_not_into_it(stand, project, save_test, fake_llm):
    async def script(r):
        await r.do("fill", "Что купить", "Молоко")
        await r.do("click", "Добавить")
        await r.do("assert_element_text", "Всего: 3")
    t = save_test("Добавить пункт", arun(record(f"{stand.url}/list.html", script)))
    before = [s["locator"] for s in t["steps"]]
    stand.reset("v2")

    def script_llm(kind, kw):
        if kw.get("output_format") is HealChoice:
            text = last_user_text(kw).split("Elements:")[1]
            name = "Добавить пункт" if "click" in last_user_text(kw) else "Что купить"
            return Resp(parsed=HealChoice(ref=ref_for(text, name), reason="та же роль"))
        raise AssertionError(kind)
    fake_llm.script = script_llm
    p = _settings(project, analyze_failures=False)
    run = arun(pipeline.run_and_record(p, t))
    assert run["status"] == "passed" and run["healed"] >= 1 and run["proposals"] >= 1

    saved = storage.load(t["id"])
    assert [s["locator"] for s in saved["steps"]] == before            # unchanged until reviewed
    props = saved["heal_proposals"]
    assert props and all(p["run_id"] == run["id"] for p in props)
    assert runs.file(run, props[0]["screenshot"])                      # the outlined element
    assert saved["last_run"]["status"] == "passed" and "/list.html" in saved["last_run"]["paths"]
    assert [x["id"] for x in runs.history(project["id"], t["id"])] == [run["id"]]
    assert run["usage"]["requests"] >= 1


def test_failed_then_passed_is_flaky_and_analysed_with_the_retry(stand, project, save_test, fake_llm):
    t = save_test("Сервис", [new_step("navigate", "Открыть", f"{stand.url}/errors.html"),
                             new_step("assert_text_present", "Сервис доступен", "Сервис доступен")])
    seen = {}

    def script_llm(kind, kw):
        assert kw["output_format"] is FailureAnalysis
        seen["text"] = last_user_text(kw)
        return Resp(parsed=FailureAnalysis(verdict="flaky", summary="Сервис ответил 500 один раз",
                                           suggestion="Ждать ответа сервиса"))
    fake_llm.script = script_llm
    run = arun(pipeline.run_and_record(project, t))       # /api/flaky: 500 first, then 200
    assert run["status"] == "flaky" and run["passed"] and run["flaky"]
    assert [a["passed"] for a in run["attempts"]] == [False, True]
    assert run["analysis"]["verdict"] == "flaky"
    assert "PASSED the second time" in seen["text"] and "500" in seen["text"]      # retry + browser events
    assert run["trace"] == "trace.zip" and runs.file(run, "trace.zip")               # the failed attempt's trace
    assert storage.load(t["id"])["last_run"]["flaky"] is True
    assert runs.history(project["id"], t["id"])[-1]["outcomes"] == [False, True]


def test_auto_quarantine_and_rotation(stand, project, save_test):
    p = _settings(project, analyze_failures=False, self_heal=False, retry_failed=False, auto_quarantine=True,
                  flaky_threshold=30, keep_runs=3, trace="off")
    t = save_test("Два пункта", [new_step("navigate", "Открыть", f"{stand.url}/list.html"),
                                 new_step("assert_text_present", "Всего: 2", "Всего: 2")])
    results = []
    for items in (2, 3, 2, 3):
        stand.default_items = [{"name": str(i)} for i in range(items)]
        results.append(arun(pipeline.run_and_record(p, t))["status"])
    assert results == ["passed", "failed", "passed", "failed"]
    saved = storage.load(t["id"])
    assert saved["quarantine"]["on"] and saved["quarantine"]["by"] == "auto"
    hist = runs.history(project["id"], t["id"], limit=0)
    assert len(hist) == 3                                             # keep_runs
    assert all(runs.get(h["id"]) for h in hist)
    assert runs.flip_rate(hist) == 1
