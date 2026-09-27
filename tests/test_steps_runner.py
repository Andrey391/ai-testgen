"""steps.perform for every action, replay with runner.run_test, events, trace, self-healing."""
from __future__ import annotations

import json
import zipfile

import pytest

from fakes import Resp, last_user_text, ref_for
from helpers import arun, record
from stand import PASSWORD, USERNAME
from testgen import runner
from testgen.runner import HealChoice
from testgen.steps import ALL_ACTIONS, new_step

CREDS = {"username": USERNAME, "password": PASSWORD}


def _form_script():
    async def script(r):
        await r.do("fill", "Email", "{{faker.email}}")
        await r.do("fill", "Имя", "{{faker.first_name}}")
        await r.do("select_option", "Страна", "Казахстан")
        await r.do("assert_enabled", "Зарегистрироваться", "false")
        await r.do("click", "Согласен с правилами")
        await r.do("assert_checked", "Согласен с правилами")          # value taken from the page: "true"
        await r.do("assert_enabled", "Зарегистрироваться")            # "true"
        await r.do("assert_value", "Email", "{{faker.email}}")
        await r.do("hover", "Зарегистрироваться")
        await r.do("click", "Зарегистрироваться")
        await r.do("assert_text_present", value="Спасибо")
        await r.do("assert_url_contains", value="form.html")
        await r.do("press_key", value="Tab")
        await r.do("scroll", value="down")
        await r.do("wait", value="0.2")
        await r.do("assert_no_console_errors")
    return script


def test_every_action_records_and_replays(stand):
    steps = arun(record(f"{stand.url}/form.html", _form_script()))
    by = {s["description"]: s for s in steps}
    assert by["assert_checked Согласен с правилами"]["value"] == "true"
    assert by["assert_enabled Зарегистрироваться"]["value"] == "true"
    assert by["assert_value Email"]["value"] == "{{faker.email}}"    # the placeholder stays in the step
    assert all(s["locator"] for s in steps if s["action"] in ("fill", "click", "assert_value"))

    report = arun(runner.run_test({"id": "t1", "project_id": "", "name": "form", "steps": steps}))
    assert report["passed"], [r for r in report["results"] if r["status"] == "failed"]
    assert len(report["results"]) == len(steps)


def test_count_text_and_mock_route(stand):
    async def script(r):
        await r.do("assert_count", "Хлеб")               # the group of list items: 2
        await r.do("assert_element_text", "Всего: 2")
        await r.do("fill", "Что купить", "Молоко")
        await r.do("click", "Добавить")
        await r.do("assert_count", "Молоко", "3")
    steps = arun(record(f"{stand.url}/list.html", script))
    assert steps[1]["value"] == "2" and steps[1]["locator"][0]["kind"] in ("testid", "css")
    stand.reset()
    assert arun(runner.run_test({"id": "t2", "project_id": "", "name": "list", "steps": steps}))["passed"]

    # A mocked API answer replaces the real one: five items instead of two.
    items = [{"name": f"n{i}"} for i in range(5)]
    mock = new_step("mock_route", "mock", json.dumps({"url": "**/api/items*", "method": "GET", "status": 200,
                                                      "body": json.dumps(items)}))
    count = dict(steps[1], value="5")
    rep = arun(runner.run_test({"id": "t3", "project_id": "", "name": "mock", "steps": [mock, steps[0], count]}))
    assert rep["passed"], rep["results"]


def test_all_actions_are_known():
    assert {"assert_value", "assert_checked", "assert_enabled", "assert_count", "assert_element_text",
            "assert_no_console_errors", "assert_accessible", "assert_screenshot", "mock_route"} <= ALL_ACTIONS


def test_events_and_console_check(stand):
    steps = [new_step("navigate", "open", f"{stand.url}/errors.html"),
             new_step("assert_no_console_errors", "no errors")]
    rep = arun(runner.run_test({"id": "t4", "project_id": "", "name": "errors", "steps": steps}))
    assert not rep["passed"]
    assert "Ошибка загрузки виджета" in rep["results"][1]["error"]
    kinds = {e["type"] for e in rep["events"]}
    assert {"console", "http"} <= kinds                   # console.error and the 404 banner / 500 api
    assert any("banner.png" in e["text"] for e in rep["events"])


def _login_steps(stand):
    async def script(r):
        await r.do("fill", "Логин", "{{username}}")
        await r.do("fill", "Пароль", "{{password}}")
        await r.do("click", "Войти")
        await r.do("assert_element_text", "Всего: 2")
    return arun(record(f"{stand.url}/login.html", script, CREDS))


def test_trace_is_recorded_and_password_masked(stand, tmp_path):
    steps = _login_steps(stand)
    assert all(PASSWORD not in json.dumps(s) for s in steps)
    rep = arun(runner.run_test({"id": "t5", "project_id": "", "name": "login", "steps": steps}, credentials=CREDS,
                               cfg={"trace": "always"}, run_dir=tmp_path))
    assert rep["passed"] and rep["trace"] == "trace.zip"
    with zipfile.ZipFile(tmp_path / "trace.zip") as z:
        blob = b"".join(z.read(n) for n in z.namelist())
    assert PASSWORD.encode() not in blob and b"***" in blob
    assert all((tmp_path / r["shot"]).exists() for r in rep["results"])


def test_trace_only_for_failures_by_default(stand, tmp_path):
    steps = [new_step("navigate", "open", f"{stand.url}/form.html")]
    rep = arun(runner.run_test({"id": "t6", "project_id": "", "name": "ok", "steps": steps},
                               cfg={"trace": "failed"}, run_dir=tmp_path))
    assert rep["passed"] and rep["trace"] == "" and not (tmp_path / "trace.zip").exists()


def _list_add_test(stand):
    async def script(r):
        await r.do("fill", "Что купить", "Молоко")
        await r.do("click", "Добавить")
        await r.do("assert_element_text", "Всего: 3")
    return {"id": "heal1", "project_id": "", "name": "add", "steps": arun(record(f"{stand.url}/list.html", script))}


def _healer(fake_llm, name="Добавить пункт"):
    def script(kind, kw):
        assert kind == "parse" and kw["output_format"] is HealChoice
        return Resp(parsed=HealChoice(ref=ref_for(last_user_text(kw).split("Elements:")[1], name),
                                      reason="Та же кнопка добавления, новый текст"))
    fake_llm.script = script


def test_heal_review_mode_proposes_without_changing_the_test(stand, fake_llm, tmp_path):
    test = _list_add_test(stand)
    old = json.dumps(test["steps"])
    stand.reset("v2")
    _healer(fake_llm)
    rep = arun(runner.run_test(test, cfg={"self_heal": True, "heal_mode": "review"}, run_dir=tmp_path))
    assert rep["passed"], rep["results"]
    assert json.dumps(test["steps"]) == old                       # nothing rewritten without review
    assert len(rep["proposals"]) >= 1
    p = next(p for p in rep["proposals"] if p["action"] == "click")
    assert p["reason"] and p["new"] != p["old"] and (tmp_path / p["screenshot"]).exists()
    # The prompt of the healer is cached.
    assert fake_llm.calls[0][1]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_heal_auto_mode_rewrites_and_rejected_locator_is_not_reused(stand, fake_llm):
    test = _list_add_test(stand)
    stand.reset("v2")
    _healer(fake_llm)
    rep = arun(runner.run_test(test, cfg={"self_heal": True, "heal_mode": "auto"}))
    assert rep["passed"]
    click = next(s for s in test["steps"] if s["action"] == "click")
    assert click["healed"] and any(c.get("value") == "append-btn" for c in click["locator"])

    # A person rejected that choice: the same element is not accepted again.
    stand.reset()
    test2 = _list_add_test(stand)
    click2 = next(s for s in test2["steps"] if s["action"] == "click")
    click2["heal_rejected"] = [click["locator"]]
    stand.reset("v2")
    rep = arun(runner.run_test(test2, cfg={"self_heal": True, "heal_mode": "review"}))
    assert not rep["passed"]
    assert "отклонён" in next(r for r in rep["results"] if r["status"] == "failed")["error"]


def test_heal_off_fails_the_step(stand):
    test = _list_add_test(stand)
    stand.reset("v2")
    rep = arun(runner.run_test(test, cfg={"self_heal": False}))
    assert not rep["passed"] and "self-healing is off" in rep["results"][-1]["error"]
