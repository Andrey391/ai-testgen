"""Scenario generation streams its intermediate results: the plan, every detailed batch, log lines."""
from __future__ import annotations

import json
import re
import uuid

from fastapi.testclient import TestClient

from fakes import Resp
from testgen import auth, projects, scenarios
from testgen.scenarios import PlannedScenario, Scenario, ScenarioBatch, ScenarioPlan

N = 8   # several batches of scenarios.BATCH


def _script(kind, kw):
    task = kw["messages"][-1]["content"]
    task = task if isinstance(task, str) else " ".join(b.get("text", "") for b in task)
    m = re.search(r"scenarios (\d+)–(\d+)", task)
    if not m:
        return Resp(parsed=ScenarioPlan(feature="Вход", assumptions=["Есть тестовый пользователь"], scenarios=[
            PlannedScenario(title=f"Сценарий {i + 1}", type="positive", priority="high", covers="вход")
            for i in range(N)]))
    a, b = int(m.group(1)), int(m.group(2))
    return Resp(parsed=ScenarioBatch(scenarios=[
        Scenario(title=f"Сценарий {i}", type="positive", priority="high", preconditions="", instructions=f"Шаги {i}",
                 expected_result="Вход выполнен", gherkin="") for i in range(a, b + 1)]))


def test_generation_streams_plan_batches_and_log(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    fake_llm.script = _script
    p = projects.create("Сценарии " + uuid.uuid4().hex[:6])
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": "Вход по логину",
                                                       "stream": True}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]

    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["analysis", "log"] and kinds[-1] == "done"
    plan = next(e for e in events if e["type"] == "plan")
    assert plan["feature"] == "Вход" and len(plan["scenarios"]) == N
    assert kinds.index("plan") < kinds.index("batch")          # titles come before the details
    batches = [e for e in events if e["type"] == "batch"]
    assert sorted(e["start"] for e in batches) == list(range(0, N, scenarios.BATCH))
    assert sum(len(e["scenarios"]) for e in batches) == N
    assert any("Готовы сценарии" in e["text"] for e in events if e["type"] == "log")
    final = events[-1]["analysis"]
    assert [s["instructions"] for s in final["scenarios"]] == [f"Шаги {i + 1}" for i in range(N)]
    # The scenarios keep the ids they were planned with: the page edits them while the rest is detailed.
    assert [s["id"] for s in final["scenarios"]] == [s["id"] for s in plan["scenarios"]]
    assert batches[0]["scenarios"][0]["id"] == plan["scenarios"][batches[0]["start"]]["id"]

    # The generation is kept in the history of the project's analyses.
    history = client.get(f"/api/projects/{p['id']}/analyses").json()
    assert history[0]["id"] == final["id"] and history[0]["scenarios"] == N and history[0]["status"] == "done"
    assert history[0]["title"] == "Вход по логину"

    # Without `stream` the answer is the same JSON as before (plus the analysis).
    r = client.post("/api/scenarios", json={"project_id": p["id"], "requirements": "Вход по логину"})
    assert r.status_code == 200 and len(r.json()["scenarios"]) == N and r.json()["analysis_id"]


def test_stream_reports_a_model_error(monkeypatch, fake_llm):
    import server
    monkeypatch.setattr(auth, "ENABLED", False)

    def broken(kind, kw):
        raise RuntimeError("модель недоступна")
    fake_llm.script = broken
    p = projects.create("Ошибка " + uuid.uuid4().hex[:6])
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with client.stream("POST", "/api/scenarios", json={"project_id": p["id"], "requirements": "x",
                                                       "stream": True}) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["type"] == "error" and events[-1]["text"]


def test_ready_scenarios_go_straight_to_authoring(monkeypatch):
    """"Generate all" in the Requirements tab: the edited scenarios, each one, no requirements stage."""
    from helpers import arun
    from testgen import pipeline

    p = projects.create("Готовые " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    given = [{"title": "Вход", "type": "positive", "priority": "high", "preconditions": "", "instructions": f"Шаги {i}",
              "expected_result": "", "gherkin": ""} for i in range(3)]       # equal titles stay separate
    job = pipeline.Job(p, [], "", "", {}, scenarios=given, feature="Вход")
    seen = []

    async def process(project, item, sc):
        seen.append(sc["instructions"])
        item["status"] = "done"
    monkeypatch.setattr(job, "_process", process)
    arun(job.run())
    assert job.status == "done" and seen == ["Шаги 0", "Шаги 1", "Шаги 2"]
    assert job.feature == "Вход" and [i["status"] for i in job.items] == ["done"] * 3

    import server
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    r = client.post(f"/api/projects/{p['id']}/jobs", json={"scenarios": [given[0] | {"instructions": " "}]})
    assert r.status_code == 400


def test_analysis_scenarios_are_edited_and_keep_their_tests(monkeypatch):
    import server
    from testgen import analyses, pipeline, storage
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    p = projects.create("Анализ " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    a = analyses.create(p["id"], "# Корзина\nДобавление товаров")
    plan = analyses.set_plan(p["id"], a["id"], "Корзина", [], [{"title": "Добавить", "type": "positive",
                                                                "priority": "high", "covers": "x"}] * 2)
    # A scenario still being detailed cannot be edited.
    assert client.put(f"/api/analyses/{a['id']}/scenarios/{plan[0]['id']}", json={"title": "x"}).status_code == 409
    analyses.set_batch(p["id"], a["id"], 0, [{"title": "Добавить", "type": "positive", "priority": "high",
                                             "instructions": "Шаги"}])
    analyses.finish(p["id"], a["id"], {"feature": "Корзина", "assumptions": [], "scenarios": [
        {"title": "Добавить", "type": "positive", "priority": "high", "instructions": "Шаги"},
        {"title": "Второй", "type": "edge", "priority": "low", "instructions": "Ещё шаги"}]})
    sc = client.get(f"/api/analyses/{a['id']}").json()["scenarios"]
    assert [s["title"] for s in sc] == ["Добавить", "Второй"] and sc[0]["id"] == plan[0]["id"]

    r = client.put(f"/api/analyses/{a['id']}/scenarios/{sc[0]['id']}", json={"title": "Добавить товар",
                                                                             "priority": "medium"})
    assert r.status_code == 200 and r.json()["title"] == "Добавить товар"
    assert client.put(f"/api/analyses/{a['id']}/scenarios/{sc[0]['id']}", json={"type": "bogus"}).status_code == 400
    new = client.post(f"/api/analyses/{a['id']}/scenarios", json={"title": "Свой", "instructions": "Сам"}).json()
    assert client.delete(f"/api/analyses/{a['id']}/scenarios/{sc[1]['id']}").status_code == 200
    assert [s["title"] for s in client.get(f"/api/analyses/{a['id']}").json()["scenarios"]] == ["Добавить товар", "Свой"]

    # A test made from a scenario shows under it; a deleted test goes away.
    t = storage.save({"project_id": p["id"], "name": "Тест корзины", "url": "http://x", "scenario": "", "steps": []})
    analyses.link_test(p["id"], a["id"], sc[0]["id"], t["id"])
    view = client.get(f"/api/analyses/{a['id']}").json()
    assert view["scenarios"][0]["tests"][0]["name"] == "Тест корзины"
    assert client.get(f"/api/projects/{p['id']}/analyses").json()[0]["tests"] == 1
    storage.delete(t["id"])
    assert client.get(f"/api/analyses/{a['id']}").json()["scenarios"][0]["tests"] == []

    # "Generate all" from an analysis: its scenarios go to a pipeline run that links the tests back.
    started = {}
    monkeypatch.setattr(server, "submit", lambda coro: (coro.close(), started.setdefault("yes", True)))
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    r = client.post(f"/api/projects/{p['id']}/jobs", json={"analysis_id": a["id"], "scenario_ids": [new["id"]]})
    assert r.status_code == 200, r.text
    job = pipeline.JOBS.pop(r.json()["id"])
    assert [s["title"] for s in job.given] == ["Свой"] and job.given[0]["scenario_id"] == new["id"]
    assert job.feature == "Корзина"


def _cut(objs: list[dict], key: str = "scenarios") -> Resp:
    """An answer cut off by the output limit: the complete objects and the start of the next one."""
    text = json.dumps({"feature": "Вход", "assumptions": [], key: objs}, ensure_ascii=False)
    text = text[:-2] + ', {"title": "Оборва'
    return Resp(content=[{"type": "text", "text": text}], stop_reason="max_tokens")


def test_a_long_plan_comes_in_short_parts_and_cut_answers_are_kept(monkeypatch, fake_llm):
    """The plan and the details come in short requests; an answer cut off by max_tokens keeps its
    complete scenarios and the rest is asked from where it stopped - not the same request again."""
    from helpers import arun
    total, asked, details = 40, [], []

    def planned(i):
        return {"title": f"Сценарий {i + 1}", "type": "positive", "layer": "ui", "priority": "high", "covers": "x"}

    def script(kind, kw):
        task = kw["messages"][-1]["content"]
        m = re.search(r"scenarios (\d+)–(\d+)", task)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            details.append((a, b))
            assert "Сценарий" in task and task.count("\n") == b - a + 1      # only this batch, not the plan
            full = [{"title": "", "type": "positive", "priority": "high", "preconditions": "",
                     "instructions": f"Шаги {i}", "expected_result": "", "gherkin": ""} for i in range(a, b + 1)]
            return _cut(full[:1]) if len(full) > 2 else Resp(parsed=ScenarioBatch.model_validate({"scenarios": full}))
        page = int(re.search(r"at most (\d+)", task).group(1))
        done = len(re.findall(r"^\d+\. ", task, re.M))
        asked.append((page, done))
        part = [planned(i) for i in range(done, min(done + page, total))]
        if page > 5:
            return _cut(part[:4])
        return Resp(parsed=ScenarioPlan.model_validate({"feature": "Вход", "assumptions": [], "scenarios": part,
                                                        "more": done + len(part) < total}))
    fake_llm.script = script
    p = projects.create("Части " + uuid.uuid4().hex[:6])
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    logs = []
    result = arun(scenarios.generate("Вход", project=projects.get(p["id"]), log=logs.append))
    # The first answer was cut after 4 scenarios: they are kept and the plan goes on from the 5th.
    assert asked[:3] == [(15, 0), (7, 4), (3, 8)]
    assert [s.title for s in result.scenarios] == [f"Сценарий {i + 1}" for i in range(total)]
    assert [s.instructions for s in result.scenarios] == [f"Шаги {i + 1}" for i in range(total)]
    # A cut batch of 3 keeps the complete one and asks for the other 2 from the 2nd one on.
    assert (1, 3) in details and (2, 3) in details
    assert any("оборвался" in line for line in logs)


def test_salvage_keeps_complete_scenarios_of_a_cut_answer():
    text = '{"feature":"Вход","assumptions":["a"],"scenarios":[{"title":"A","type":"positive","priority":"high",' \
           '"covers":"x"} , {"title":"B","type":"negative","priority":"low","covers":"y"},{"title":"\u043e'
    got = scenarios._salvage(text, PlannedScenario)
    assert [s.title for s in got] == ["A", "B"]
    assert scenarios._field(text, "feature", "") == "Вход" and scenarios._field(text, "assumptions", []) == ["a"]
    assert scenarios._salvage('{"feature":"Вх', PlannedScenario) == []


def test_a_cut_off_structured_answer_reports_max_tokens():
    """The SDK raises on a structured answer cut off by max_tokens; the provider reports the stop reason."""
    import anthropic
    import httpx2
    from helpers import arun
    from testgen.providers.anthropic import AnthropicProvider
    from testgen.providers.base import Request

    def handler(request):
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": '{"feature":"table-tennis","assumptions":["\u043e\u0446'}],
            "stop_reason": "max_tokens", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 16000}})
    client = anthropic.AsyncAnthropic(api_key="x",
                                      http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    reply = arun(AnthropicProvider(client, {"structured"}).parse(
        Request(model="test-model", system="s", messages=[{"role": "user", "content": "x"}]), ScenarioPlan))
    assert reply.stop == "max_tokens" and reply.parsed is None and reply.usage["output_tokens"] == 16000
    assert reply.text.startswith('{"feature"')
