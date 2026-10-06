"""Studio: a person sees what the agent does and when the test counts as finished, and can stop it."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from fakes import text, tool
from testgen import projects
from testgen.agent import StudioSession, expected_result
from testgen.steps import new_step


def _session() -> StudioSession:
    p = projects.create("Studio " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")
    projects.update_llm(p["id"], {"model": "test-model", "effort": "medium"})
    return StudioSession(projects.get(p["id"]), "T", "http://127.0.0.1:1",
                         "Войти в систему\nОжидаемый результат: открыт личный кабинет")


def test_expected_result_is_taken_from_the_scenario():
    assert expected_result("Шаги\nОжидаемый результат: корзина пуста") == "корзина пуста"
    assert expected_result("Steps\nExpected result - cart is empty") == "cart is empty"
    assert expected_result("Просто сценарий") == ""


def test_stop_cancels_the_request_to_the_model(fake_llm):
    s = _session()
    started = asyncio.Event()

    async def slow(**kw):
        started.set()
        await asyncio.sleep(30)
    fake_llm.beta.messages.create = slow

    async def scenario():
        task = asyncio.create_task(s._think([{"type": "text", "text": "Начни"}]))
        await asyncio.wait_for(started.wait(), 5)
        s.autopilot = True
        s.stop()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert s.status == "idle" and not s.autopilot
    assert s.unanswered == [{"type": "text", "text": "Начни"}]     # "Continue with AI" sends it again
    assert s.messages == [] and "остановлена" in s.chat[-1]["text"]
    assert s.state()["expected"] == "открыт личный кабинет"


def test_finish_explains_itself_and_is_refused_without_an_assertion(fake_llm):
    s = _session()
    answers = iter([tool("finish", status="passed", summary="Вошли", evidence=""), text("Добавлю проверку")])
    fake_llm.script = lambda kind, kw: next(answers)
    s.steps = [new_step("click", "Войти", "") | {"status": "passed"},
               new_step("assert_text_present", "Кабинет открыт", "Кабинет") | {"status": "failed"}]

    asyncio.run(s._think([{"type": "text", "text": "Начни"}]))
    assert s.status == "idle" and s.finish is None
    assert any("нет проверки" in m["text"] for m in s.chat if m["role"] == "system")

    s.steps[1]["status"] = "passed"
    fake_llm.script = lambda kind, kw: tool("finish", status="passed", summary="Вошли",
                                            evidence="Шаг 2 проверяет заголовок кабинета")
    asyncio.run(s._think([{"type": "text", "text": "Добавил проверку"}]))
    assert s.status == "done"
    assert s.finish == {"status": "passed", "summary": "Вошли", "evidence": "Шаг 2 проверяет заголовок кабинета",
                        "assertions": [2]}
    assert "Подтверждение" in s.chat[-1]["text"]
