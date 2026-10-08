"""New scenarios are compared with the earlier ones: a person decides to reuse, refine or create a new
test; scenarios whose authoring failed are generated again in one go."""
from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient

from fakes import Resp
from helpers import arun
from testgen import analyses, auth, fs, pipeline, projects, reuse, scenarios, storage
from testgen.scenarios import PlannedScenario, Scenario, ScenarioBatch, ScenarioPlan


def _project() -> dict:
    p = projects.create("Повтор " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    p["pipeline"]["run"]["enabled"] = False
    p = projects.update(p["id"], {"pipeline": p["pipeline"]})
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    return projects.get(p["id"])


def _test(p: dict, name: str) -> dict:
    return storage.save({"project_id": p["id"], "name": name, "url": p["base_url"], "scenario": f"{name}: шаги",
                         "steps": []})


def test_earlier_tests_and_scenarios_are_candidates():
    p = _project()
    login = _test(p, "Вход")
    a = analyses.create(p["id"], "Корзина")
    analyses.set_plan(p["id"], a["id"], "Корзина", [], [{"title": "Добавить в корзину", "type": "positive",
                                                       "priority": "high"}])
    analyses.finish(p["id"], a["id"], {"feature": "Корзина", "assumptions": [], "scenarios": [
        {"title": "Добавить в корзину", "type": "positive", "priority": "high", "instructions": "Нажать «В корзину»"}]})
    cands = reuse.candidates(p["id"])
    assert [(c["ref"], c["kind"], c["title"]) for c in cands] == [("T1", "test", "Вход"),
                                                                  ("S2", "scenario", "Добавить в корзину")]
    assert reuse.resolve(cands, "t1", "same") == {"kind": "test", "id": login["id"], "analysis_id": "",
                                                        "title": "Вход", "relation": "same", "decision": ""}
    assert reuse.resolve(cands, "T9", "same") is None and reuse.resolve(cands, "T1", "") is None
    assert "T1 — Вход — Вход: шаги" in reuse.listing(cands)


def test_generated_scenarios_carry_the_match_and_reuse_links_the_earlier_test(monkeypatch, fake_llm):
    p = _project()
    login = _test(p, "Вход по логину")

    def script(kind, kw):
        task = kw["messages"][-1]["content"]
        task = task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)
        if "scenarios 1–" not in task:
            assert "T1 — Вход по логину" in str(kw["system"]) + str(kw["messages"])
            return Resp(parsed=ScenarioPlan(feature="Вход", assumptions=[], scenarios=[
                PlannedScenario(title="Вход", type="positive", priority="high", covers="вход", existing="T1",
                                relation="same"),
                PlannedScenario(title="Выход", type="positive", priority="high", covers="выход")]))
        return Resp(parsed=ScenarioBatch(scenarios=[
            Scenario(title=t, type="positive", priority="high", preconditions="", instructions="Шаги",
                     expected_result="", gherkin="") for t in ("Вход", "Выход")]))
    fake_llm.script = script
    res = arun(scenarios.generate("Вход и выход", project=p))
    assert res.scenarios[0].match["id"] == login["id"] and res.scenarios[1].match is None

    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    a = analyses.create(p["id"], "Вход")
    analyses.set_plan(p["id"], a["id"], "Вход", [], [s.model_dump() for s in res.scenarios])
    analyses.finish(p["id"], a["id"], res.model_dump())
    sc = analyses.get(a["id"])["scenarios"][0]
    assert sc["match"]["decision"] == "" and sc["match"]["title"] == "Вход по логину"
    r = client.put(f"/api/analyses/{a['id']}/scenarios/{sc['id']}", json={"decision": "copy"})
    assert r.status_code == 400
    r = client.put(f"/api/analyses/{a['id']}/scenarios/{sc['id']}", json={"decision": "reuse"})
    assert r.json()["match"]["decision"] == "reuse" and r.json()["test_ids"] == [login["id"]]
    other = analyses.get(a["id"])["scenarios"][1]
    assert client.put(f"/api/analyses/{a['id']}/scenarios/{other['id']}", json={"decision": "new"}).status_code == 409


def test_pipeline_waits_for_the_decision_and_reuses(monkeypatch):
    p = _project()
    login = _test(p, "Вход")
    match = {"kind": "test", "id": login["id"], "title": "Вход", "relation": "same", "decision": ""}
    dup = {"kind": "scenario", "id": "s1", "analysis_id": "a1", "title": "Выход", "relation": "same", "decision": ""}
    given = [{"title": t, "type": "positive", "priority": "high", "preconditions": "", "instructions": "Шаги",
              "expected_result": "", "gherkin": "", "match": m} for t, m in (("Вход", match), ("Выход", dup),
                                                                            ("Профиль", None))]
    job = pipeline.Job(p, [], "", "", {}, scenarios=given, feature="Аккаунт")
    monkeypatch.setitem(pipeline.JOBS, job.id, job)
    monkeypatch.setattr(pipeline, "MODEL_POLL", 0.05)
    created = []

    async def create(project, item, sc):
        created.append(sc["title"])
        return storage.save({"project_id": p["id"], "name": sc["title"], "url": p["base_url"], "steps": []})
    monkeypatch.setattr(job, "_create_test", create)

    async def scenario():
        task = asyncio.create_task(job.run())
        for _ in range(100):
            if job.status == "awaiting_reuse":
                break
            await asyncio.sleep(0.02)
        assert job.status == "awaiting_reuse" and not created
        job.decide({0: "reuse", 1: "reuse"})
        await asyncio.wait_for(task, 5)
    arun(scenario())
    assert job.status == "done", job.log
    assert created == ["Профиль"]
    assert job.items[0]["test_id"] == login["id"] and "Переиспользован" in job.items[0]["summary"]
    assert job.items[1]["status"] == "done" and not job.items[1]["test_id"]


def test_failed_items_are_generated_again(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    p = _project()
    jid = uuid.uuid4().hex[:10]
    items = [pipeline.Job._item({"title": t}) for t in ("Ошибка", "Застрял", "Дефект", "Готово")]
    items[0].update(status="error", error="Сбой агента", session_id="ab" * 5)
    items[1].update(status="needs_attention", error="Агент ждёт человека")
    items[2].update(status="needs_attention", bug=True)
    items[3].update(status="done", test_id="t1")
    state = {"id": jid, "project_id": p["id"], "links": [], "text": "", "url": "", "user": "alice",
             "explore": False, "cases": None, "status": "done", "stage": "", "created": 1, "finished": 2, "log": [],
             "requirements": [], "feature": "", "assumptions": [], "scenarios": [{"title": i["title"]} for i in items],
             "items": items, "error": "", "usage": {}}
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{jid}.json", state)
    assert [pipeline.retryable(i) for i in items] == [True, True, False, False]
    started = []
    monkeypatch.setattr(server, "submit", lambda coro: started.append(coro) or coro.close())
    r = client.post(f"/api/jobs/{jid}/retry", json={})
    assert r.json() == {"id": jid, "retried": 2}
    job = pipeline.JOBS.pop(jid)
    assert [i["status"] for i in job.items] == ["queued", "queued", "needs_attention", "done"]
    assert job.items[0]["error"] == "" and job.items[0]["session_id"] is None
    assert any("Перезапуск генерации" in line["text"] for line in job.log) and len(started) == 1
    # A possible defect only when a person names it; nothing to retry -> 400.
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{jid}.json", state | {"items": items[2:]})
    assert client.post(f"/api/jobs/{jid}/retry", json={"indices": [1]}).status_code == 400
    assert client.post(f"/api/jobs/{jid}/retry", json={"indices": [0]}).json()["retried"] == 1
    pipeline.JOBS.pop(jid)


def test_items_needing_a_person_are_done_when_their_test_is_saved_in_studio():
    """«Нужен человек»: the person finishes the session in Studio and saves the test - the item is done."""
    import types
    p = _project()
    jid, jid2 = uuid.uuid4().hex[:10], uuid.uuid4().hex[:10]
    a = analyses.create(p["id"], "Регистрация")
    analyses.set_plan(p["id"], a["id"], "Регистрация", [], [{"title": "Пустые поля", "type": "negative",
                                                             "priority": "high"}])
    scid = analyses.get(a["id"])["scenarios"][0]["id"]
    items = [pipeline.Job._item({"title": t}) | {"status": "needs_attention", "error": "Ошибка агента",
                                                  "session_id": sid}
             for t, sid in (("Пустые поля", "aa" * 5), ("Неверный email", "bb" * 5))]
    state = {"id": jid, "project_id": p["id"], "status": "done", "created": 1, "log": [], "items": items,
             "given": [{"title": "Пустые поля", "analysis_id": a["id"], "scenario_id": scid}, {"title": "Неверный email"}]}
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{jid}.json", state)
    saved = _test(p, "Пустые поля")
    pipeline.session_saved(p["id"], jid, "aa" * 5, saved)          # saved after the run finished
    j = pipeline.get_job(jid)
    assert j["items"][0]["status"] == "done" and j["items"][0]["test_id"] == saved["id"]
    assert j["items"][1]["status"] == "needs_attention"
    assert analyses.get(a["id"])["scenarios"][0]["test_ids"] == [saved["id"]]
    assert "сохранён в Studio" in j["log"][-1]["text"]

    # Saved before the studio told the run: the run is set right when it is read.
    fs.write_json(pipeline._jobs_dir(p["id"]) / f"{jid2}.json", state | {"id": jid2, "items": [dict(i) for i in items]})
    other = _test(p, "Неверный email")
    sessions = {"bb" * 5: types.SimpleNamespace(test_id=other["id"])}
    j = pipeline.reconcile(jid2, pipeline.get_job(jid2), sessions)
    assert [i["status"] for i in j["items"]] == ["needs_attention", "done"]
    assert pipeline.get_job(jid2)["items"][1]["test_id"] == other["id"]
