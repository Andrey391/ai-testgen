"""The authoring agent with a scripted LLM, and the invariants of CLAUDE.md:
the password never reaches the LLM or artifacts; destructive MCP tools are never offered."""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
from types import SimpleNamespace

from fakes import dump, last_user_text, latest_page, ref_for, text, tool
from helpers import arun, record
from stand import PASSWORD, USERNAME
from testgen import exporters, llm, mcp_hub, projects, storage, traffic
from testgen.agent import StudioSession
from testgen.mcp_browser import McpBrowser
from testgen.steps import new_step


def _agent(plan):
    """A fake LLM that walks through `plan`: (tool name, {input}, element name for the ref or None)."""
    state = {"i": 0}

    def script(kind, kw):
        page = latest_page(kw)
        if state["i"] >= len(plan):
            return text("Готово.")
        name, inp, element = plan[state["i"]]
        state["i"] += 1
        if element:
            inp = inp | {"ref": ref_for(page, element)}
        return tool(name, **inp)
    return script


async def _drive(s: StudioSession, timeout: float | None = None) -> None:
    """Start the session in Auto-Pilot and wait for it to stop. A session that hangs fails the
    test with its state and the stacks of every task of the loop (where exactly it waits)."""
    timeout = timeout or float(os.environ.get("TESTGEN_TEST_TIMEOUT", "90"))
    await s.start()
    s.set_autopilot(True)
    for _ in range(int(timeout * 10)):
        if s.status in ("done", "error") or (not s.autopilot and s.status == "idle"):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"Сессия не завершилась за {timeout:.0f} с\n" + _session_dump(s))


def _session_dump(s: StudioSession) -> str:
    out = io.StringIO()
    out.write(f"status={s.status} autopilot={s.autopilot} url={s.page_url}\n")
    out.write(f"pending={s.pending and s.pending['step']}\n")
    for i, st in enumerate(s.steps):
        out.write(f"step {i + 1}: [{st['action']}] {st['description']} -> {st['status']} {st.get('error', '')}\n")
    for m in s.chat:
        out.write(f"chat {m['role']}: {m['text'][:300]}\n")
    for task in asyncio.all_tasks():
        out.write(f"\n--- task {task.get_name()}\n")
        task.print_stack(file=out)
    return out.getvalue()


def test_password_never_reaches_claude_or_artifacts(stand, project, fake_llm):
    fake_llm.script = _agent([
        ("fill", {"text": "{{username}}", "press_enter": False, "description": "Ввести логин"}, "Логин"),
        ("fill", {"text": "{{password}}", "press_enter": False, "description": "Ввести пароль"}, "Пароль"),
        ("click", {"description": "Нажать «Войти»"}, "Войти"),
        ("finish", {"status": "passed", "summary": "Вошли"}, None),            # refused: no assertion yet
        ("assert_element_text", {"text": "Всего: 2", "description": "В списке два пункта"}, "Всего: 2"),
        ("finish", {"status": "passed", "summary": "Вошли, список открыт"}, None),
    ])
    s = StudioSession(project, "Вход", f"{stand.url}/login.html", "Войти и увидеть список",
                      credentials={"username": USERNAME, "password": PASSWORD})

    async def go():
        try:
            return await _scenario()
        finally:
            await s.close()

    async def _scenario():
        await _drive(s)
        finished = (s.status, s.finish_status, list(s.chat))
        # An assertion that captures the current value of the password field (Element Picker).
        await s.bs.snapshot()
        ref = next(r for r, e in s.bs.elements.items() if e.get("type") == "password") \
            if any(e.get("type") == "password" for e in s.bs.elements.values()) else None
        if ref is None:          # we are on the list page now: go back to the form
            await s.bs.page.goto(f"{stand.url}/login.html")
            await s.bs.page.fill("#password", PASSWORD)
            await s.bs.snapshot()
            ref = next(r for r, e in s.bs.elements.items() if e.get("type") == "password")
        step = new_step("assert_value", "Пароль введён")
        step["ref"] = ref
        await s._run_step(step)
        return finished, step, s.save()[0]
    (status, finish, chat), picked, test = arun(go())
    assert status == "done" and finish == "passed", chat
    assert picked["value"] == "{{password}}"

    assert [x["action"] for x in test["steps"]] == ["navigate", "fill", "fill", "click", "assert_element_text",
                                                    "assert_value"]
    assert test["steps"][2]["value"] == "{{password}}" and test["steps"][1]["value"] == "{{username}}"
    sent = dump([kw for _, kw in fake_llm.calls])
    everywhere = {
        "requests to the LLM": sent,
        "steps": json.dumps(test, ensure_ascii=False),
        "chat": json.dumps(s.chat, ensure_ascii=False),
        "history": dump(s.messages),
        "export .py": exporters.to_playwright(test),
        "export .feature": exporters.to_gherkin(test),
        "traffic": json.dumps(traffic.load_har(project["id"], test["id"]), ensure_ascii=False),
        "api export": exporters.to_api_tests(test, traffic.load(project["id"], test["id"])),
    }
    for where, blob in everywhere.items():
        assert PASSWORD not in blob, f"password leaked into {where}"
    assert "{{password}}" in everywhere["traffic"]         # the login request was recorded, masked
    assert USERNAME in sent                                 # the login itself may be shown to the LLM

    # Caching: tools + system behind an explicit breakpoint, plus automatic caching of the conversation.
    kw = fake_llm.calls[0][1]
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["context_management"]["edits"][0]["type"] == llm.CLEAR_TOOL_USES
    assert test["authoring_usage"]["requests"] == len(fake_llm.calls)
    assert test["authoring_usage"]["cache_read_input_tokens"] > 0 and test["authoring_usage"]["cost_usd"] > 0


async def _run(s: StudioSession, then=None):
    """Drive the session and do `then()` in the same event loop, then close the browser."""
    try:
        await _drive(s)
        return then() if then else None
    finally:
        await s.close()


def test_prompt_cache_can_be_switched_off(monkeypatch, fake_llm):
    for on in (False, True):
        monkeypatch.setattr(llm, "PROMPT_CACHE", on)
        arun(llm.chat({"model": "test-model"}, system="rules", messages=[{"role": "user", "content": "hi"}],
                      cache_all=True))
    off, on = fake_llm.calls[0][1], fake_llm.calls[1][1]
    assert off["system"] == "rules" and "cache_control" not in off
    assert on["system"][0]["cache_control"] == {"type": "ephemeral"} and on["cache_control"] == {"type": "ephemeral"}


def test_strengthen_session_replays_the_test_then_adds_checks(stand, project, fake_llm):
    async def script(r):
        await r.do("fill", "Что купить", "Молоко")
        await r.do("click", "Добавить")
        await r.do("assert_visible", "Добавить")
    base = arun(record(f"{stand.url}/list.html", script))
    stand.reset()
    fake_llm.script = _agent([
        ("assert_count", {"count": 3, "description": "В списке три пункта"}, "Молоко"),
        ("finish", {"status": "passed", "summary": "Добавлена проверка числа пунктов"}, None),
    ])
    s = StudioSession(project, "Добавление", f"{stand.url}/list.html", "Добавить пункт", base_steps=base,
                      task="Survived mutants: the click does nothing. Add assertion steps.")
    arun(_run(s))
    assert s.status == "done", s.chat
    first = last_user_text({"messages": fake_llm.calls[0][1]["messages"][:1]})
    assert "The recorded test has been replayed" in first and "Survived mutants" in first
    steps = s.to_test()["steps"]
    assert [x["action"] for x in steps] == [b["action"] for b in base] + ["assert_count"]
    assert steps[-1]["value"] == "3"


class _FakeMcp:
    def __init__(self, project_id, conn, *a, **k):
        self.tools = [SimpleNamespace(name=n, description=n, inputSchema={"type": "object", "properties": {}})
                      for n in ("jira_get_issue", "create_test_case", "deleteTestCase", "remove_attachment",
                                "archive_project", "search_issues")]

    async def start(self):
        return self

    async def close(self):
        pass


def test_destructive_mcp_tools_are_never_offered(monkeypatch):
    monkeypatch.setattr(mcp_hub, "McpClient", _FakeMcp)
    conn = {"id": "c1", "preset": "custom", "name": "Tracker"}
    read = arun(mcp_hub.Toolbox("p", [conn], mode="read").start())
    write = arun(mcp_hub.Toolbox("p", [conn], mode="write").start())
    names = lambda box: {t["name"].split("__", 1)[1] for t in box.tools}   # noqa: E731
    assert names(read) == {"jira_get_issue", "search_issues"}
    assert names(write) == {"jira_get_issue", "search_issues", "create_test_case"}
    for box in (read, write):
        assert not names(box) & {"deleteTestCase", "remove_attachment", "archive_project"}


def test_mcp_browser_masks_the_password_in_server_output():
    client = SimpleNamespace(tools=[SimpleNamespace(name="browser_click", inputSchema={"properties": {"target": {}}})])
    b = McpBrowser(client, "")
    b.credentials = {"password": PASSWORD}
    assert b._mask(f"typed {PASSWORD} into the field") == "typed *** into the field"
    assert b.ref_key == "target"
    assert b.expand("{{password}}") == PASSWORD and b.expand("{{unique}}") == b.expand("{{unique}}")


def test_new_secrets_stay_home_login_state_totp_and_before_responses(stand, project, save_test, fake_llm):
    """Stage 2 added secrets: the saved login (cookies), the TOTP secret and its codes, the answers
    of "before" requests. None of them reaches the model, the steps, the export, the HAR or the report."""
    from stand import TOTP_SECRET
    from testgen import pipeline, runner
    from testgen.testdata import totp

    async def login(r):
        await r.do("fill", "Логин", "{{username}}")
        await r.do("fill", "Пароль", "{{password}}")
        await r.do("click", "Войти")
        await r.do("assert_element_text", "Всего: 2")
    save_test("Вход", arun(record(f"{stand.url}/login.html", login, {"username": USERNAME, "password": PASSWORD})),
              role="login")
    projects.set_app_credentials(project["id"], USERNAME, PASSWORD, totp_secret=TOTP_SECRET)
    p = projects.get(project["id"])

    # 1. A session that starts logged in: the cookie of the saved login stays out of everything.
    fake_llm.script = _agent([
        ("assert_text_present", {"text": "Здравствуйте, demo", "description": "Кабинет открыт"}, None),
        ("finish", {"status": "passed", "summary": "ok"}, None)])
    s = StudioSession(p, "Кабинет", f"{stand.url}/account.html", "Открыть кабинет")
    t, _ = arun(_run(s, s.save))
    assert s.status == "done" and s.logged_in, s.chat
    state = pipeline.load_state(p["id"], storage.credentials(t))
    cookie = next(c["value"] for c in state["cookies"] if c["name"] == "auth")
    har = json.dumps(traffic.load_har(p["id"], t["id"]), ensure_ascii=False)
    for where, blob in {"requests to the model": dump([kw for _, kw in fake_llm.calls]), "steps": json.dumps(t),
                        "HAR": har, "export": exporters.to_playwright(t, login=None),
                        "bundle": json.dumps(exporters.bundle(p, [t], lookup=storage.load), ensure_ascii=False)}.items():
        assert cookie not in blob, f"login cookie leaked into {where}"

    # 2. TOTP: the model sees {{totp}}, never the secret or a code.
    fake_llm.calls.clear()
    fake_llm.script = _agent([
        ("fill", {"text": "{{username}}", "press_enter": False, "description": "Логин"}, "Логин"),
        ("fill", {"text": "{{password}}", "press_enter": False, "description": "Пароль"}, "Пароль"),
        ("click", {"description": "Далее"}, "Далее"),
        ("fill", {"text": "{{totp}}", "press_enter": False, "description": "Код"}, "Код из приложения"),
        ("click", {"description": "Подтвердить"}, "Подтвердить"),
        ("assert_text_present", {"text": "Вход подтверждён", "description": "Вход подтверждён"}, None),
        ("finish", {"status": "passed", "summary": "ok"}, None)])
    s = StudioSession(p, "2FA", f"{stand.url}/login-2fa.html", "Войти с кодом", use_login_state=False,
                      credentials=storage.credentials({"id": "x", "project_id": p["id"]}))
    t, _ = arun(_run(s, s.save))
    assert s.status == "done", s.chat
    codes = {totp(TOTP_SECRET, time.time() + d) for d in (-60, -30, 0, 30)}
    everything = dump([kw for _, kw in fake_llm.calls]) + json.dumps(t) + exporters.to_playwright(t) + \
        json.dumps(traffic.load_har(p["id"], t["id"]), ensure_ascii=False)
    assert TOTP_SECRET not in everything and not any(f'"{c}"' in everything for c in codes)

    # 3. The answers of "before" requests are not kept in the run report.
    before = new_step("api_request", "Создать заказ", json.dumps({"method": "POST", "url": "/api/orders",
                                                                  "body": {"title": "секретный-заказ"},
                                                                  "save": {"order_id": "$.id"}}))
    rep = arun(runner.run_test({"id": "b1", "project_id": p["id"], "name": "b", "url": stand.url, "before": [before],
                                "steps": [new_step("navigate", "open", f"{stand.url}/orders.html")]},
                               base_url=stand.url))
    assert rep["passed"] and "секретный-заказ" not in json.dumps(rep, ensure_ascii=False)


def test_saved_test_keeps_studio_metadata_on_resave(stand, project, fake_llm):
    fake_llm.script = _agent([
        ("assert_text_present", {"text": "Регистрация", "description": "Открыта регистрация"}, None),
        ("finish", {"status": "passed", "summary": "ok"}, None)])
    s = StudioSession(project, "Форма", f"{stand.url}/form.html", "Открыть форму")
    t, _ = arun(_run(s, s.save))
    storage.update(t["id"], lambda x: x.update(tags=["smoke"], quarantine={"on": True},
                                               external={"zephyr": {"key": "P-T1"}}))
    t2, _ = s.save()
    assert t2["id"] == t["id"] and t2["tags"] == ["smoke"] and t2["quarantine"]["on"]
    assert t2["external"]["zephyr"]["key"] == "P-T1"
    assert projects.get(project["id"])
