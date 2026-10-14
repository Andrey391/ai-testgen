"""One work on an analysis at a time (analyses.WORK): what would break the work goes on it - another work, a decision
on the findings, deleting it - waits for it to end (409 with what goes and who started it). The work goes on when
the page goes away; its progress is in the analysis for any page, «Остановить» stops it on purpose. An analyst's
long answers are matched in batches, a corrected specification is reviewed again knowing the decisions made."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

from fastapi.testclient import TestClient

from fakes import Resp, tool
from testgen import analyses, auth, knowledge, projects, review, validation

SPEC = "# Заказы\n1. Покупатель оформляет заказ.\n2. Менеджер редактирует отправленный заказ."
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


def _validation(questions=("Кто отменяет заказ?",)) -> validation.Validation:
    return validation.Validation(
        summary="Нет критериев приёмки", score=60, verdict="needs_work", sections=[],
        findings=[validation.Finding(criterion="verifiable", severity="major", location="п. 1",
                                     problem="Нет критерия оформления", suggestion="добавить критерий")],
        questions=list(questions))


def _reviewed(name: str, **pipe) -> tuple[dict, str]:
    p = _project(name, **pipe)
    a = analyses.create(p["id"], SPEC, "", "ann", work="review")
    analyses.set_review(p["id"], a["id"], {"validation": _validation().model_dump(),
                                           "lifecycle": {"summary": "", "conflicts": [CONFLICT], "questions": []}})
    analyses.finish(p["id"], a["id"])
    return p, a["id"]


def _client(monkeypatch) -> TestClient:
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_a_work_on_an_analysis_blocks_what_would_break_it(monkeypatch, fake_llm):
    p, aid = _reviewed("Блокировка")
    client = _client(monkeypatch)
    analyses.start_work(p["id"], aid, "answers", "ann")
    url = f"/api/analyses/{aid}"
    calls = [client.put(f"{url}/review/findings", json={"ids": ["d0"], "status": "dismissed"}),
             client.post(f"{url}/review/answers", json={"text": "1. да"}),
             client.post(f"{url}/review/group"),
             client.post(f"{url}/review/lifecycle"),
             client.post(f"/api/projects/{p['id']}/knowledge/extract", json={"analysis_id": aid}),
             client.post("/api/requirements/review", json={"project_id": p["id"], "analysis_id": aid}),
             client.delete(url)]
    assert [r.status_code for r in calls] == [409] * len(calls)
    assert all("Сейчас идёт: разбор ответов аналитика по замечаниям (запустил ann)" in r.json()["detail"] for r in calls)
    # the report and the decisions are untouched, nothing was asked of the model
    assert not analyses.get(aid)["review"].get("resolutions") and fake_llm.calls == []
    # The fix (findings answered) waits as well; scenarios may still be edited meanwhile.
    analyses.end_work(p["id"], aid)
    analyses.decide(p["id"], aid, ["d0"], review.TODO, "критерий: статус «новый»", user="ann")
    analyses.start_work(p["id"], aid, "lifecycle", "bob")
    r = client.post(f"{url}/review/fix")
    assert r.status_code == 409 and "Сейчас идёт: сверка с жизненным циклом системы (запустил bob)" in r.json()["detail"]
    analyses.end_work(p["id"], aid, result="Сверка завершена")
    a = analyses.get(aid)
    assert a["status"] == "done" and a["work_result"] == "Сверка завершена" and "before_work" not in a


def test_the_model_is_not_confirmed_while_it_learns(monkeypatch, fake_llm):
    p, aid = _reviewed("Обучение")
    client = _client(monkeypatch)
    analyses.start_work(p["id"], aid, "learn", "ann")
    r = client.post(f"/api/projects/{p['id']}/knowledge/confirm", json={})
    assert r.status_code == 409 and "учится на требованиях" in r.json()["detail"]
    analyses.end_work(p["id"], aid)
    assert analyses.learning(p["id"]) is None


def test_a_work_cut_off_leaves_the_analysis_as_it_was(monkeypatch):
    p, aid = _reviewed("Перезапуск")
    analyses.start_work(p["id"], aid, "answers", "ann")
    analyses._change(p["id"], aid, lambda a: a.update(beat=0))      # the studio restarted: no beats
    a = analyses.get(aid)
    assert a["status"] == "done" and a["work"] == "review" and "прервано перезапуском" in a["work_error"].lower()
    analyses.start_work(p["id"], aid, "group", "ann")         # nothing goes now: a new work starts
    analyses.end_work(p["id"], aid)
    assert analyses.get(aid)["status"] == "done" and analyses.get(aid)["work_error"] == ""


def test_the_progress_of_a_work_is_in_the_analysis_and_it_is_stopped_on_purpose(monkeypatch, fake_llm):
    p, aid = _reviewed("Остановка")
    client = _client(monkeypatch)
    started = threading.Event()

    async def slow(project, report, text, log=None):
        log("Агент сопоставляет ответы аналитика")
        started.set()
        await asyncio.sleep(30)
        return []
    monkeypatch.setattr(review, "distribute", slow)
    out = {}

    def answer():       # the page that started it waits for its answer
        out["r"] = _client(monkeypatch).post(f"/api/analyses/{aid}/review/answers", json={"text": "1. да"})
    t = threading.Thread(target=answer)
    t.start()
    assert started.wait(10)
    a = client.get(f"/api/analyses/{aid}").json()
    # Any page sees what goes and how far it is: after another tab, F5, on another computer.
    assert a["status"] == "running" and a["work"] == "answers"
    assert a["progress"]["stage"] == "Агент сопоставляет ответы аналитика"
    assert [x["text"] for x in a["progress"]["log"]] == ["Агент сопоставляет ответы аналитика"]
    began = time.time()
    r = client.post(f"/api/analyses/{aid}/stop")
    assert r.status_code == 200 and r.json()["stopped"]
    t.join(10)
    assert out["r"].status_code == 499 and time.time() - began < 10
    a = analyses.get(aid)
    assert a["status"] == "done" and a["work"] == "review" and a["work_error"].startswith("Остановлено")
    assert not client.post(f"/api/analyses/{aid}/stop").json()["stopped"]        # nothing goes now


def test_the_review_keeps_its_progress_and_a_new_version_is_reviewed_with_the_decisions(monkeypatch, fake_llm):
    p, aid = _reviewed("Новая версия")
    client = _client(monkeypatch)
    analyses.decide(p["id"], aid, ["vq0"], review.DISMISSED, note="заказ не отменяют", user="ann")
    analyses.decide(p["id"], aid, ["l0"], review.TODO, "Менеджер не редактирует отправленный заказ", user="ann")

    def script(kind, kw):
        if kw.get("output_format") is validation.Validation:
            return Resp(parsed=_validation(questions=()))
        return tool("report", summary="Совпадает", conflicts=[], questions=[])
    fake_llm.script = script
    new = SPEC.replace("Менеджер редактирует отправленный заказ", "Менеджер не редактирует отправленный заказ")
    with client.stream("POST", "/api/requirements/review",
                       json={"project_id": p["id"], "analysis_id": aid, "requirements": new}) as r:
        events = [json.loads(x) for x in r.iter_lines() if x.strip()]
    assert events[-1]["type"] == "done", events[-1]
    assert any(e["type"] == "log" and "Новая версия ТЗ" in e["text"] for e in events)
    a = analyses.get(aid)
    assert a["requirements"] == new and a["revisions"][-1]["kind"] == "version"
    assert a["revisions"][-1]["requirements"] == SPEC
    # The reviewer knows what was decided and answered on the earlier findings.
    sent = [str(kw["messages"]) for _, kw in fake_llm.calls]
    assert all("do not report it again: Кто отменяет заказ? (why: заказ не отменяют)" in x for x in sent)
    assert all("the document should say so now" in x and "Менеджер не редактирует отправленный заказ" in x
               for x in sent)
    # The log of the review stays with the analysis: a page opened later shows how it went.
    assert any("Ревьюер требований" in x["text"] for x in a["progress"]["log"])


def test_themes_have_numbers_to_answer_by():
    report = {"validation": _validation().model_dump(),
              "lifecycle": {"summary": "", "conflicts": [CONFLICT, CONFLICT], "questions": ["Есть ли архив?"]}}
    assert [(t["id"], t["no"]) for t in review.themes(report)] == [
        ("d0", "1"), ("l0", "Ж1"), ("l1", "Ж2"), ("vq0", "2"), ("lq0", "Ж3")]
    report["groups"] = [{"id": "t0", "title": "Правка заказа", "ids": ["d0", "l0"]}]
    assert [(t["id"], t["no"]) for t in review.themes(report)] == [
        ("t0", "1"), ("l1", "Ж1"), ("vq0", "2"), ("lq0", "Ж2")]


def test_long_answers_of_an_analyst_are_matched_in_batches(monkeypatch, fake_llm):
    p, aid = _reviewed("Длинный ответ")
    client = _client(monkeypatch)
    monkeypatch.setattr(review, "DISTRIBUTE_BATCH", 1)
    long = "1. " + "Критерий оформления: заказ получает статус «новый». " * 2000      # ~100 000 characters
    asked = []

    def script(kind, kw):
        found = [i for i in ("d0", "l0", "vq0") if f"[{i}]" in str(kw)]
        asked.append(found)
        return Resp(parsed=review.Answers(answers=[review.Answer(id=found[0], answer="Как в тексте: " + "x" * 30000,
                                                                 not_relevant=False, note="")]))
    fake_llm.script = script
    r = client.post(f"/api/analyses/{aid}/review/answers", json={"text": long})
    assert r.status_code == 200, r.text
    assert r.json()["answered"] == 3 and len(asked) == 3
    # the text goes whole to every batch, the answers come back whole (up to review.ANSWER_MAX)
    assert all(long[-200:] in str(kw["messages"]) for _, kw in fake_llm.calls)
    res = analyses.get(aid)["review"]["resolutions"]
    assert len(res["d0"]["answer"]) == review.ANSWER_MAX
    a = analyses.get(aid)
    assert a["status"] == "done" and "ждут исправления в ТЗ 3" in a["work_result"]
    r = client.post(f"/api/analyses/{aid}/review/answers", json={"text": "x" * (review.ANSWERS_MAX + 1)})
    assert r.status_code == 400 and "Новая версия ТЗ" in r.json()["detail"]


def test_the_last_finding_dismissed_teaches_the_model_in_the_background(monkeypatch, fake_llm):
    p, aid = _reviewed("Фоновое обучение", requirements={"learn_model": True})
    client = _client(monkeypatch)
    gate = threading.Event()

    async def extract(project, text, user=""):
        while not gate.is_set():
            await asyncio.sleep(0.02)
        return knowledge.get(project["id"])
    monkeypatch.setattr(knowledge, "extract", extract)
    url = f"/api/analyses/{aid}/review/findings"
    assert client.put(url, json={"ids": ["d0", "l0"], "status": "dismissed"}).json()["model"].startswith("Модель")
    r = client.put(url, json={"ids": ["vq0"], "status": "dismissed", "note": "не про это"})
    assert r.status_code == 200 and "учится на требованиях" in r.json()["model"]
    a = analyses.get(aid)
    assert a["status"] == "running" and a["work"] == "learn"       # the requirements wait for the model
    assert client.put(url, json={"ids": ["vq0"], "status": "open"}).status_code == 409
    gate.set()
    for _ in range(200):
        if analyses.get(aid)["status"] != "running":
            break
        time.sleep(0.02)
    a = analyses.get(aid)
    assert a["status"] == "done" and a["learned"] and a["work_result"].startswith("Модель приложения")
