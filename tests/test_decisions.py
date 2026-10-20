"""Less waiting for people (the project's «Решения команды»): an item of a pipeline run waits only for its own
decisions, the findings one answer settles are asked once (themes), the team's answers are remembered and settle
the same question next time, the reviewer's confident proposals settle by themselves (and are undone), the review
policy sends a strong test to the suite, a changed part of the model shows what changed, the inbox routes each
decision to its owner with a due time, and the dashboard counts the decisions."""
from __future__ import annotations

import asyncio
import time
import uuid

from fakes import Resp
from helpers import arun
from testgen import answers, audit, cases, inbox, knowledge, metrics, pipeline, projects, storage


def _project(requirements: dict | None = None, **decisions) -> dict:
    p = projects.create("Решения " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium", "prices": {"test-model": [5, 25]}})
    if decisions or requirements:
        projects.update(p["id"], {"pipeline": {"decisions": decisions, "requirements": requirements or {}}})
    return projects.get(p["id"])


def _finding(i: int, n: int, **kw) -> dict:
    return {"id": f"c{i}f{n}", "kind": kw.get("kind", "account"), "severity": kw.get("severity", "major"), "step": 0,
            "quote": "", "problem": kw.get("problem", "Не сказано, под каким пользователем входить"),
            "question": kw.get("question", "Под каким пользователем входить?"), "suggestion": kw.get("suggestion", ""),
            "confidence": kw.get("confidence", 0), "decision": None}


def _case(title: str, *findings) -> dict:
    return {"title": title, "instructions": "1. Открыть каталог", "expected_result": "Каталог открыт",
            "case_review": {"at": 1, "role": "", "test_data": [], "findings": list(findings)}}


def _job(p: dict, scenarios: list[dict], themes: list[dict] | None = None) -> pipeline.Job:
    job = pipeline.Job(p, [], "", "", {}, cases={"connection": "x", "themes": themes or []})
    job.scenarios = scenarios
    job.items = [job._item(s) | {"scenario_index": i} for i, s in enumerate(scenarios)]
    return job


def test_a_case_without_findings_goes_to_work_while_another_waits(monkeypatch):
    p = _project()
    monkeypatch.setattr(pipeline, "MODEL_POLL", 0.05)
    scs = [_case("Вход", _finding(0, 0)), _case("Каталог"), _case("Корзина", _finding(2, 0))]
    theme = {"id": "ct0", "title": "Учётная запись", "question": "Под каким пользователем входить?",
             "suggestion": "", "ids": ["c0f0", "c2f0"]}
    job = _job(p, scs, [theme])
    done = []

    async def process(project, item, sc):
        done.append(sc["title"])
        item["status"] = "done"
    job._process = process

    async def go():
        task = asyncio.create_task(job._process_all(p, list(zip(job.items, scs))))
        for _ in range(100):
            if job.status == "awaiting_cases":
                break
            await asyncio.sleep(0.02)
        # the case with nothing to settle is done already; the other two wait, the run says so
        assert done == ["Каталог"] and job.status == "awaiting_cases"
        assert [it["status"] for it in job.items] == ["waiting", "done", "waiting"]
        assert job.items[0]["wait"] == "cases"
        # one answer to the theme settles both cases
        assert job.settle_cases([{"id": "ct0", "action": "answer", "text": "Под покупателем buyer"}], "ann") == 2
        await asyncio.wait_for(task, 5)
    arun(go())
    assert done == ["Каталог", "Вход", "Корзина"]
    assert all("Под покупателем buyer" in c for sc in (scs[0], scs[2]) for c in sc["clarifications"])
    # each item's own wait is kept (the dashboard's median by kind), not the run's
    assert sorted(w["item"] for w in job.waits) == ["Вход", "Корзина"]
    assert {w["status"] for w in job.waits} == {"awaiting_cases"}
    # the answer is the team's now: the same question settles by it next time
    assert answers.known(p["id"], "Под каким пользователем нужно входить?")["answer"] == "Под покупателем buyer"
    assert any("buyer" in m["text"] for m in knowledge.get(p["id"])["memory"])


def test_the_inbox_shows_items_waiting_while_the_run_works(monkeypatch):
    p = _project(owner_cases="olga", sla_hours=2)
    job = _job(p, [_case("Вход", _finding(0, 0)), _case("Каталог")])
    job.user = "ivan"
    job.items[0].update(status="waiting", wait="cases", waiting_since=time.time() - 600)
    job.items[1]["status"] = "authoring"
    monkeypatch.setitem(pipeline.JOBS, job.id, job)         # the run works in this instance
    entries = inbox.project_inbox(p["id"], "olga")
    e = next(e for e in entries if e["kind"] == "job")
    assert "остальные в работе" in e["title"] and e["count"] == 1 and e["owner"] == "olga"
    assert abs(e["due"] - (e["since"] + 2 * 3600)) < 1


def test_the_reviewers_confident_proposals_settle_by_themselves_and_are_undone():
    scs = [_case("Вход", _finding(0, 0, kind="data", severity="minor", suggestion="Товар «Молоко»"),
                 _finding(0, 1, kind="ambiguous", severity="major", suggestion="Нажать «Купить»", confidence=95),
                 _finding(0, 2, kind="ambiguous", severity="major", suggestion="Открыть каталог", confidence=60),
                 _finding(0, 3, kind="account", severity="major", suggestion="Войти покупателем", confidence=99),
                 _finding(0, 4, kind="other", severity="blocker", suggestion="Что-то", confidence=100))]
    got = cases.auto_accept(scs, 90)
    assert [f["id"] for _, f in got] == ["c0f0", "c0f1"]      # never an account or a blocker, not unsure ones
    assert got[1][1]["decision"] | {"at": 0} == {"action": "accept", "text": "Нажать «Купить»", "user": cases.AUTO,
                                                 "auto": "confidence", "confidence": 95, "at": 0}
    assert "Нажать «Купить»" in " ".join(scs[0]["clarifications"])
    # undone: the finding waits for a person again
    assert len(cases.settle(scs, [{"id": "c0f1", "action": "reopen"}], "ann")) == 1
    assert scs[0]["case_review"]["findings"][1]["decision"] is None
    assert [f["id"] for f in cases.unsettled(scs)] == ["c0f1", "c0f2", "c0f3", "c0f4"]


def test_the_review_of_cases_settles_what_the_team_answered_and_groups_the_rest(fake_llm):
    p = _project(auto_accept=True, auto_confidence=90)
    answers.record(p["id"], "Под каким пользователем входить?", "Под покупателем buyer", user="ann")

    def script(kind, kw):
        system = kw.get("system") or ""
        system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
        if "Group the findings" in system:
            return Resp(parsed=cases.XThemes(themes=[cases.XTheme(title="Товар", question="Какой товар взять?",
                                                                  items=["c0f1", "c1f1"])]))
        assert "Под покупателем buyer" in system          # the reviewer knows what the team answered
        return Resp(parsed=cases.XCases(cases=[cases.XCase(index=i, findings=[
            cases.XFinding(kind="account", severity="major", problem="Нет пользователя",
                           question="Под каким пользователем входить?"),
            cases.XFinding(kind="data", severity="major", problem="Не сказано, какой товар",
                           question="Какой товар взять?")]) for i in (0, 1)]))
    fake_llm.script = script
    scs = [{"title": t, "instructions": "1. Купить", "expected_result": ""} for t in ("Покупка", "Возврат")]
    job = pipeline.Job(p, [], "", "", {}, cases={"connection": "x"})
    job.scenarios = scs
    job.items = [job._item(s) | {"scenario_index": i} for i, s in enumerate(scs)]
    arun(job._review_cases(p))
    first = [sc["case_review"]["findings"][0]["decision"] for sc in scs]
    assert all(d["auto"] == "memory" and d["text"] == "Под покупателем buyer" for d in first)
    assert job.cases["themes"] == [{"id": "ct0", "title": "Товар", "question": "Какой товар взять?", "suggestion": "",
                                    "ids": ["c0f1", "c1f1"]}]
    assert any(r["action"] == "decision.auto" for r in audit.read(project_id=p["id"]))
    assert any("по прежнему ответу команды" in line["text"] for line in job.log)


def test_a_strong_test_joins_the_suite_by_the_review_policy():
    p = _project(review_policy="risk", min_score=80)
    job = pipeline.Job(p, [], "", "", {})
    strong = storage.save({"project_id": p["id"], "name": "Сильный", "url": "", "steps": [], "status": "draft"})
    weak = storage.save({"project_id": p["id"], "name": "Слабый", "url": "", "steps": [], "status": "draft"})
    good = {"created": True, "run": {"status": "passed", "passed": True, "healed": 0},
            "verify": {"status": "done", "killed": 5, "total": 5, "score": 1.0}}
    job._by_policy(p, dict(good), strong)
    job._by_policy(p, dict(good, verify={"status": "done", "killed": 2, "total": 5, "score": 0.4}), weak)
    assert storage.status(storage.load(strong["id"])) == "ready"
    t = storage.load(weak["id"])
    assert storage.status(t) == "review" and "слабые проверки" in t["review_reason"]
    assert any(r["action"] == "test.auto_ready" for r in audit.read(project_id=p["id"]))
    # the policy "all": every test is its author's to review
    other = _project()
    t3 = storage.save({"project_id": other["id"], "name": "Тест", "url": "", "steps": [], "status": "draft"})
    pipeline.Job(other, [], "", "", {})._by_policy(other, dict(good), t3)
    assert storage.status(storage.load(t3["id"])) == "draft"


SHOP = {"entities": [{"name": "Заказ", "group": "Продажи", "lifecycle": "новый → оплачен", "depends_on": ["Товар"]},
                     {"name": "Товар", "group": "Продажи", "lifecycle": "в наличии → нет"}],
        "roles": [{"name": "Покупатель", "capabilities": "покупает"}]}


def test_a_changed_part_of_the_model_shows_what_changed():
    p = _project()
    knowledge.save(p["id"], SHOP)
    knowledge.confirm(p["id"], "lead")
    doc = knowledge.get(p["id"])
    for e in doc["entities"]:
        if e["name"] == "Заказ":
            e["lifecycle"] = "новый → оплачен → доставлен"
    doc["entities"] = [e for e in doc["entities"] if e["name"] != "Товар"] + [
        {"name": "Корзина", "group": "Продажи", "lifecycle": "пустая → полная"}]
    knowledge.save(p["id"], doc)
    unit = next(u for u in knowledge.confirmation(knowledge.get(p["id"]))["units"] if u["unit"] == "Продажи")
    assert unit["state"] == "changed"
    ch = unit["changes"]
    assert ch["added"] == ["Корзина"] and ch["removed"] == ["Товар"]
    assert ch["changed"] == [{"name": "Заказ", "fields": [{"field": "lifecycle", "before": "новый → оплачен",
                                                             "after": "новый → оплачен → доставлен"}]}]
    roles = next(u for u in knowledge.confirmation(knowledge.get(p["id"]))["units"] if u["unit"] == knowledge.ROLES_UNIT)
    assert roles["state"] == "confirmed" and "changes" not in roles


def test_the_inbox_routes_decisions_to_their_owners():
    p = _project({"confirm_model": True}, owner_model="lead", owner_tests="qa")
    knowledge.save(p["id"], SHOP)
    storage.save({"project_id": p["id"], "name": "Мой", "url": "", "steps": [], "status": "review", "author": "ann"})
    storage.save({"project_id": p["id"], "name": "Ничей", "url": "", "steps": [], "status": "review"})
    entries = inbox.project_inbox(p["id"], "ann")
    model = next(e for e in entries if e["kind"] == "model" and "Жизненный цикл" in e["title"])
    assert model["owner"] == "lead"
    reviews = {e["owner"]: e for e in entries if e["kind"] == "test_review"}
    assert set(reviews) == {"ann", "qa"} and reviews["ann"]["count"] == 1


def test_the_dashboard_counts_decisions_and_waits_by_kind():
    p = _project()
    storage.save({"project_id": p["id"], "name": "Новый", "url": "", "steps": []})
    audit.record("decision.cases", user="ann", project_id=p["id"], status=200)
    audit.record("decision.cases", user="ann", project_id=p["id"], status=200)
    audit.record("decision.cases", user="ann", project_id=p["id"], status=409)        # refused: not a decision
    audit.record("decision.auto", user=cases.AUTO, project_id=p["id"])
    job = pipeline.Job(p, [], "", "", {})
    now = time.time()
    job.waits = [{"status": "awaiting_cases", "since": now - 600, "until": now, "item": "A"},
                 {"status": "awaiting_cases", "since": now - 1200, "until": now, "item": "B"},
                 {"status": "awaiting_model", "since": now - 60, "until": now}]
    job.status = "done"
    job.save()
    m = metrics.project_metrics(p["id"])
    d = m["decisions"]
    assert d["people"] == 2 and d["automatic"] == 1 and d["new_tests"] == 1 and d["per_test"] == 2.0
    assert d["by_kind"] == [{"title": "замечания к кейсам", "count": 2}]
    kinds = {k["status"]: k for k in m["waits"]["by_kind"]}
    assert kinds["awaiting_cases"]["count"] == 2 and kinds["awaiting_cases"]["median_minutes"] == 15.0
    assert kinds["awaiting_model"]["median_minutes"] == 1.0
