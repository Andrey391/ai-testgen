"""A person works through the reviewer's findings: answers how it should be (the editor fixes the requirements
by the answer) or marks a finding not relevant. The application model learns from the requirements only when
every finding is settled."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

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


def _events(r) -> list[dict]:
    """The NDJSON events of a streamed answer of the studio."""
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


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
    body = _events(r)[-1]
    assert body["type"] == "done"
    assert body["fixed"] == ["l0"] and "d0" in body["failed"] and body["unresolved"] == 1 and learned == []
    a = analyses.get(aid)
    assert "Менеджер не редактирует отправленный заказ" in a["requirements"]
    assert a["revisions"][0]["requirements"] == SPEC and a["revisions"][0]["fixed"] == ["l0"]
    assert a["review"]["resolutions"]["d0"]["error"].startswith("не найден фрагмент")

    fake_llm.script = lambda kind, kw: Resp(parsed=review.Revision(summary="Критерий", edits=[
        review.Edit(item="d0", find="1. Покупатель оформляет заказ.", replace="1. Покупатель оформляет заказ: статус «новый».")]))
    body = _events(client.post(f"{url}/fix"))[-1]
    # The last finding settled: the fix does not wait for the model - the page asks it to learn, once.
    assert body["unresolved"] == 0 and learned == [] and not body["analysis"].get("learned")
    assert client.post(f"/api/projects/{p['id']}/knowledge/extract", json={"analysis_id": aid}).status_code == 200
    assert learned == [analyses.get(aid)["requirements"]] and analyses.get(aid)["learned"]
    assert "статус «новый»" in learned[0] and "не редактирует" in learned[0]
    assert "error" not in analyses.get(aid)["review"]["resolutions"]["d0"]

    # Reviewed again: the reviewer is told what the authors decided.
    fake_llm.calls.clear()
    fake_llm.script = _review_script
    with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "analysis_id": aid}) as r:
        list(r.iter_lines())
    sent = [kw["messages"][0]["content"] for _, kw in fake_llm.calls]
    assert sent and all("do not report it again: Кто отменяет заказ? (why: не про заказы)" in x for x in sent)
    assert len(learned) == 1


def test_the_pipeline_teaches_the_model_only_settled_requirements(monkeypatch, fake_llm):
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
        await asyncio.sleep(0.05)
        # Waiting for the findings: the model does not learn the requirements, nothing is designed.
        assert learned == [] and not any(kw.get("output_format") is ScenarioPlan for _, kw in fake_llm.calls)
        assert any("не разобраны замечания, блокирующие тест-дизайн" in e["text"] for e in job.log)
        a = analyses.get(job.analysis_id)
        analyses.decide(p["id"], a["id"], [it["id"] for it in review.unresolved(a["review"])], review.DISMISSED,
                        note="не про это", user="bob")
        job.model_confirmed()
        await task
    arun(go())
    # Settled: the model learns from them once, then the scenarios are designed.
    assert job.status == "done", job.error
    assert learned == [SPEC] and analyses.get(job.analysis_id).get("learned")


def _many() -> dict:
    """A report with what blocks test design and what is the form of the document."""
    v = _findings().model_dump()
    v["findings"] += [
        {"criterion": "traceable", "severity": "critical", "location": "весь документ", "problem": "Нет номеров",
         "suggestion": "пронумеровать"},
        {"criterion": "complete", "severity": "minor", "location": "п. 1", "problem": "Нет примера", "suggestion": ""},
        {"criterion": "complete", "severity": "major", "location": "п. 2", "problem": "Не сказано, кто правит заказ",
         "suggestion": "назвать роль"}]
    return {"validation": v, "lifecycle": {"summary": "", "conflicts": [CONFLICT], "questions": ["Кто правит заказ?"]}}


def test_only_what_blocks_test_design_holds_the_model_back():
    report = _many()
    its = {x["id"]: x["blocking"] for x in review.items(report)}
    assert its == {"d0": True, "d1": False, "d2": False, "d3": True, "l0": True, "vq0": True, "lq0": True}
    assert review.verdict(report) == "not_ready"         # the critical conflict with the lifecycle
    review.decide(report, ["d0", "d3", "l0", "vq0", "lq0"], "dismissed")
    assert review.resolved(report) and len(review.unresolved(report, everything=True)) == 2
    report["lifecycle"]["conflicts"] = []
    report["validation"]["findings"] = report["validation"]["findings"][1:3]     # numbering and a minor one
    report["validation"]["questions"], report["lifecycle"]["questions"] = [], []
    report["validation"]["verdict"] = "not_ready"
    assert review.verdict(report) == "needs_work"        # the form of the document does not make it not ready


def test_a_theme_is_answered_once_and_fixed_together(fake_llm):
    p = _project("Темы")
    report = _many()
    report["groups"] = [{"id": "t0", "title": "Кто правит заказ", "ids": ["d3", "l0", "lq0"]}]
    ts = review.themes(report)
    assert [t["id"] for t in ts] == ["d0", "d1", "d2", "t0", "vq0"]
    t0 = ts[3]
    assert t0["ids"] == ["d3", "l0", "lq0"] and t0["blocking"] and t0["question"] and t0["severity"] == "critical"
    review.decide(report, t0["ids"], "todo", "Правит только менеджер до отправки")
    assert review.themes(report)[3]["status"] == "todo"
    asked = []

    def script(kind, kw):
        asked.append(kw["messages"][0]["content"])
        return Resp(parsed=review.Revision(summary="Роль названа", edits=[
            review.Edit(item="t0", find="2. Менеджер редактирует отправленный заказ.",
                        replace="2. Менеджер редактирует заказ до отправки.")]))
    fake_llm.script = script
    out = arun(review.revise(p, SPEC, report))
    assert out["fixed"] == ["d3", "l0", "lq0"] and out["failed"] == {}
    assert "[t0] Кто правит заказ - settle them together" in asked[0] and asked[0].count("the answer of the authors") == 1
    review.fixed(report, out["fixed"])
    assert review.themes(report)[3]["status"] == "fixed"


def test_the_answers_of_an_analyst_are_matched_to_the_findings(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p = _project("Ответы")
    a = analyses.create(p["id"], SPEC, "", "ann")
    report = _many()
    report["groups"] = [{"id": "t0", "title": "Кто правит заказ", "ids": ["d3", "l0", "lq0"]}]
    analyses.set_review(p["id"], a["id"], report)
    analyses.finish(p["id"], a["id"])
    seen = []

    def script(kind, kw):
        seen.append(kw)
        return Resp(parsed=review.Answers(answers=[
            review.Answer(id="t0", answer="Только менеджер, до отправки", not_relevant=False, note=""),
            review.Answer(id="vq0", answer="", not_relevant=True, note="заказ не отменяют"),
            review.Answer(id="zz", answer="чужое", not_relevant=False, note="")]))
    fake_llm.script = script
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = client.post(f"/api/analyses/{a['id']}/review/answers", json={"text": "1. Только менеджер\n2. не актуально"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answered"] == 1 and body["dismissed"] == 1
    assert "[t0] Кто правит заказ" in str(seen[0])
    st = {t["id"]: (t["status"], t["answer"], t["note"]) for t in body["analysis"]["review"]["themes"]}
    assert st["t0"] == ("todo", "Только менеджер, до отправки", "") and st["vq0"] == ("dismissed", "", "заказ не отменяют")
    res = analyses.get(a["id"])["review"]["resolutions"]
    assert {i: res[i]["status"] for i in res} == {"d3": "todo", "l0": "todo", "lq0": "todo", "vq0": "dismissed"}


def test_the_same_requirements_checked_again_update_their_analysis(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    fake_llm.script = _review_script
    p = _project("Повтор")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    for text in (SPEC, "  " + SPEC.replace("\n", "\n\n") + "\n"):
        with client.stream("POST", "/api/requirements/review", json={"project_id": p["id"], "requirements": text}) as r:
            lines = list(r.iter_lines())
    assert len(analyses.list_analyses(p["id"])) == 1
    assert any("уже проверялись" in x for x in lines)


def test_a_standard_not_meant_for_the_document_does_not_make_defects(fake_llm):
    p = _project("Стандарт")
    fake_llm.script = lambda kind, kw: Resp(parsed=validation.Validation(
        summary="ТЗ модуля", document_kind="техническое ТЗ модуля", standard_fits=False, suggested_standard="iso29148",
        score=40, verdict="not_ready", sections=[],
        findings=[validation.Finding(criterion="section", severity="critical", location="—", problem="Нет DoD",
                                     suggestion=""),
                  validation.Finding(criterion="verifiable", severity="major", location="п. 1",
                                     problem="Нет лимита", suggestion="")], questions=[]))
    v = arun(validation.validate(p, SPEC, "agile"))
    assert [f["problem"] for f in v["findings"]] == ["Нет лимита"] and v["suggested_standard"] == "iso29148"


def _answered(name: str) -> tuple[dict, str]:
    """An analysis of SPEC with its three findings answered: d0, l0 (a conflict with the lifecycle), vq0."""
    p = _project(name)
    a = analyses.create(p["id"], SPEC, "", "ann", work="review")
    report = {"validation": _findings().model_dump(), "lifecycle": {"summary": "", "conflicts": [CONFLICT],
                                                                              "questions": []}}
    review.decide(report, ["d0"], "todo", "")
    review.decide(report, ["l0"], "todo", "Менеджер не редактирует отправленный заказ")
    review.decide(report, ["vq0"], "todo", "Отменяет менеджер")
    analyses.set_review(p["id"], a["id"], report)
    analyses.finish(p["id"], a["id"])
    return p, a["id"]


FIXES = {"d0": review.Edit(item="d0", find="1. Покупатель оформляет заказ.",
                           replace="1. Покупатель оформляет заказ: статус «новый»."),
         "l0": review.Edit(item="l0", find="Менеджер редактирует отправленный заказ",
                           replace="Менеджер не редактирует отправленный заказ"),
         "vq0": review.Edit(item="vq0", find="отправленный заказ.", replace="отправленный заказ.\n3. Отменяет менеджер.")}


def test_a_model_that_only_thinks_under_structured_output_gets_the_schema_in_the_prompt(monkeypatch, fake_llm):
    """GLM behind a gateway: with output_config.format it thinks until max_tokens and never answers (the fix of the
    requirements hung for its whole batch time); the same request with the schema in the prompt answers."""
    from testgen import llm
    from testgen.providers import anthropic as provider
    monkeypatch.setattr(provider, "NO_STRUCTURED", set())
    p = _project("Только думает")
    answer = review.Revision(summary="Готово", edits=[FIXES["d0"]])

    def script(kind, kw):
        if kind == "parse":
            return Resp([SimpleNamespace(type="thinking", thinking="Думаю… " * 50, signature="")],
                        stop_reason="max_tokens")
        return Resp([SimpleNamespace(type="text", text=answer.model_dump_json())])
    fake_llm.script = script

    def ask():
        return arun(llm.parse(p["pipeline"]["requirements"], system="s", messages=[{"role": "user", "content": "x"}],
                              schema=review.Revision, project_id=p["id"], stream=True, timeout=60))
    assert ask().parsed == answer
    assert [kind for kind, _ in fake_llm.calls] == ["parse", "create"]
    assert "Answer with ONE JSON object only" in str(fake_llm.calls[1][1]["system"])
    # the connection keeps the schema in the prompt: no more requests spent on thinking
    fake_llm.calls.clear()
    assert ask().parsed == answer and [kind for kind, _ in fake_llm.calls] == ["create"]


def test_the_editor_keeps_each_batch_and_a_failure_loses_only_the_batch_in_work(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(review, "FIX_BATCH", 1)
    p, aid = _answered("Пакеты")

    def script(kind, kw):
        task = kw["messages"][0]["content"]
        if "[vq0]" in task:
            raise RuntimeError("шлюз модели не отвечает")
        item = "d0" if "[d0]" in task else "l0"
        return Resp(parsed=review.Revision(summary=f"Пакет {item}", edits=[FIXES[item]]))
    fake_llm.script = script
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    events = _events(client.post(f"/api/analyses/{aid}/review/fix"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "analysis" and events[0]["analysis"]["status"] == "running" and events[0]["analysis"]["work"] == "fix"
    assert kinds.count("batch") == 2 and kinds[-1] == "error" and "не отвечает" in events[-1]["text"]
    assert any(e["type"] == "log" and e["text"].startswith("Пакет 3 из 3") for e in events)
    # one request per batch, streamed and with a wait of its own
    assert [kind for kind, _ in fake_llm.calls] == ["parse"] * 3
    assert all("format" in kw["output_config"] and kw["timeout"].read == review.FIX_WAIT for _, kw in fake_llm.calls)

    a = analyses.get(aid)
    assert "статус «новый»" in a["requirements"] and "не редактирует" in a["requirements"]
    assert len(a["revisions"]) == 1 and a["revisions"][0]["fixed"] == ["d0", "l0"]      # one fix - one revision
    assert a["revisions"][0]["requirements"] == SPEC and a["revisions"][0]["summary"] == "Пакет d0 Пакет l0"
    res = a["review"]["resolutions"]
    assert res["d0"]["status"] == res["l0"]["status"] == "fixed" and res["vq0"]["status"] == "todo"
    assert a["status"] == "done" and a["work"] == "review" and not a["error"]
    assert "готовые правки сохранены" in a["work_error"]

    # Fixed again: only what is left is asked; the fix before does not stay as an error.
    fake_llm.calls.clear()
    fake_llm.script = lambda kind, kw: Resp(parsed=review.Revision(summary="Отмена", edits=[FIXES["vq0"]]))
    done = _events(client.post(f"/api/analyses/{aid}/review/fix"))[-1]
    assert done["type"] == "done" and done["fixed"] == ["vq0"] and done["unresolved"] == 0
    assert len(fake_llm.calls) == 1 and "[d0]" not in fake_llm.calls[0][1]["messages"][0]["content"]
    a = analyses.get(aid)
    assert len(a["revisions"]) == 2 and a["work_error"] == "" and "3. Отменяет менеджер." in a["requirements"]


def test_a_fix_is_one_at_a_time_and_the_findings_wait_for_it(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    p, aid = _answered("Одно исправление")
    analyses.start_fix(p["id"], aid)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    url = f"/api/analyses/{aid}/review"
    assert client.post(f"{url}/fix").status_code == 409
    assert client.put(f"{url}/findings", json={"ids": ["d0"], "status": "open"}).status_code == 409
    # The studio restarted during the fix: the analysis is what it was, the fix is said to be cut off.
    analyses._change(p["id"], aid, lambda a: a.update(beat=0))
    a = analyses.get(aid)
    assert a["status"] == "done" and "прервано перезапуском" in a["work_error"]
    analyses.end_fix(p["id"], aid)
    assert analyses.get(aid)["status"] == "done" and "before_work" not in analyses.get(aid)


def test_an_answer_cut_off_keeps_the_complete_edits(fake_llm):
    p = _project("Обрыв")
    report = {"validation": _findings().model_dump(), "lifecycle": {"summary": "", "conflicts": [CONFLICT],
                                                                              "questions": []}}
    review.decide(report, ["d0", "l0"], "todo", "")
    review.decide(report, ["vq0"], "todo", "Отменяет менеджер")
    cut = ('{"summary": "x", "edits": [' + FIXES["d0"].model_dump_json() + ", " + FIXES["l0"].model_dump_json()
           + ', {"item": "vq0", "find": "отправл')
    answers = iter([Resp(content=[SimpleNamespace(type="text", text=cut)], stop_reason="max_tokens"),
                    Resp(parsed=review.Revision(summary="Отмена", edits=[FIXES["l0"], FIXES["vq0"]]))])
    asked = []

    def script(kind, kw):
        asked.append(kw["messages"][0]["content"])
        return next(answers)
    fake_llm.script = script
    out = arun(review.revise(p, SPEC, report))
    # The edit of l0 came whole, but l0 was the finding the answer was cut on (it may need more edits): it is
    # asked again with vq0; the edit of d0 is kept.
    assert out["fixed"] == ["d0", "l0", "vq0"] and out["failed"] == {}
    assert "[d0]" not in asked[1] and "[l0]" in asked[1] and "[vq0]" in asked[1]
    assert out["requirements"].count("не редактирует") == 1 and "статус «новый»" in out["requirements"]
