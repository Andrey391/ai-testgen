"""Mutation testing: a test that checks the result kills the mutants, a weak one does not."""
from __future__ import annotations

from helpers import arun, record
from testgen import mutations, storage


def _add_item(stand, final):
    async def script(r):
        await r.do("fill", "Что купить", "Молоко")
        await r.do("click", "Добавить")
        await final(r)
    return arun(record(f"{stand.url}/list.html", script))


def test_strong_assertions_kill_every_mutant(stand, project, save_test):
    async def final(r):
        await r.do("assert_count", "Хлеб", "3")
        await r.do("assert_element_text", "Всего: 3")
    t = save_test("Добавление в список", _add_item(stand, final))
    stand.reset()
    res = arun(mutations.verify(project, t))
    assert res["status"] == "done", res
    kinds = [m["kind"] for m in res["mutants"]]
    assert kinds[0] == "noop_action" and "api_500" in kinds and "assertion" in kinds
    assert res["killed"] == res["total"] >= 3 and not res["weak"], res["mutants"]
    assert storage.load(t["id"])["verify"]["score"] == 1.0


def test_weak_assertion_survives_and_the_agent_gets_a_task(stand, project, save_test):
    async def final(r):
        await r.do("assert_visible", "Добавить")          # "the button is there" proves nothing
    t = save_test("Слабый тест", _add_item(stand, final))
    stand.reset()
    res = arun(mutations.verify(project, t))
    assert res["status"] == "done" and res["weak"]
    survived = [m for m in res["mutants"] if m["result"] == "survived"]
    assert {m["kind"] for m in survived} >= {"noop_action", "api_500"}
    task = mutations.improvement_task(res)
    assert "ничего не делает" in task and "Add assertion steps" in task


def test_failing_test_is_not_mutated(stand, project, save_test):
    async def final(r):
        await r.do("assert_element_text", "Всего: 3")
    steps = _add_item(stand, final)
    steps[-1]["value"] = "Всего: 99"
    t = save_test("Падающий", steps)
    stand.reset()
    res = arun(mutations.verify(project, t))
    assert res["status"] == "baseline_failed" and not res["mutants"]
