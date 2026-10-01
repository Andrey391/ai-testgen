"""The authoring agent with a scripted LLM, and the invariants of CLAUDE.md:
the password never reaches the LLM or artifacts; destructive MCP tools are never offered."""
from __future__ import annotations

import asyncio
import json
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


async def _drive(s: StudioSession, timeout: float = 90) -> None:
    await s.start()
    s.set_autopilot(True)
    for _ in range(int(timeout * 10)):
        if s.status in ("done", "error") or (not s.autopilot and s.status == "idle"):
            break
        await asyncio.sleep(0.1)


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


def test_prompt_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(llm, "PROMPT_CACHE", False)
    assert llm.system("rules") == "rules" and llm.auto_cache() == {}
    monkeypatch.setattr(llm, "PROMPT_CACHE", True)
    assert llm.system("rules")[0]["cache_control"] == {"type": "ephemeral"}


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
