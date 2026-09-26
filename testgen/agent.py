"""The test-authoring agent (CoTester-style "Auto-Pilot").

Claude sees the live page (screenshot + list of interactive elements), and
proposes ONE browser action at a time as a tool call. Each executed action is
recorded as a test step. In guarded mode every proposed step waits for the
human to press Continue / Reject; in autopilot mode steps run back to back.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import anthropic

from . import llm, mcp_hub, skills
from .browser import BrowserSession
from .mcp_browser import McpBrowser
from .steps import new_step

MAX_HELPER_CALLS = 12   # connection tool calls in a row before the agent must act

SYSTEM_PROMPT = """You are a QA engineer that authors end-to-end UI test cases by driving a real web browser.

The user gives you a web application and a test scenario in plain language. You carry out the scenario one browser action at a time using the tools. Every tool call you make is recorded as a step of the test case, and the recorded test will later be replayed automatically and exported as Playwright code and Gherkin. So:

- Take the most direct path a real user would take. Avoid exploratory clicks that do not belong in the final test.
- Call exactly one tool per turn, then look at the new page state before deciding the next step.
- Write each `description` as a clear test step in imperative form, e.g. "Click the 'Add to Cart' button" or "Enter 'kindle' into the search field". Write descriptions in the same language as the scenario.
- Verify outcomes, not just actions: after each meaningful state change (search results shown, item added to cart, form submitted) add an assertion step (assert_visible, assert_text_present or assert_url_contains). The test must end with at least one assertion of the scenario's expected result.
- Target elements only by `ref` values from the LATEST page snapshot; refs change after every action.
- Dismiss cookie banners, newsletter pop-ups and similar overlays when they block the flow.
- Never complete irreversible real-world actions: do not submit real payments, place real orders, send messages or delete data. Go up to that point, assert you reached it, then finish.
- Login credentials, when provided, are given as placeholders. To type them, pass the literal text {{username}} or {{password}} to `fill`; the real values are substituted when the step runs. Never guess credentials and never put a password in a step description.
- A step that fails is not kept in the recorded test. If a step fails, fix the cause (e.g. close an overlay, pick another element) and do it again.
- If you are blocked (CAPTCHA, login you have no credentials for, the site is down), call finish with status "blocked" and explain. If the application does not behave as the scenario expects (a product bug), call finish with status "failed" and describe the bug.
- When the scenario is fully covered and its expected result is asserted, call finish with status "passed" and a short summary. Keep any text outside tool calls to one short sentence.
- Tools whose names contain "__" come from the project's connected systems (Jira, Confluence, test management...). They are read-only helpers for context, e.g. to read the requirements of an issue; they are not test steps. Use them only when the scenario needs that information."""


def _tool(name: str, description: str, props: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


_REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. 'e12'."}
_DESC = {"type": "string", "description": "The test step, written for a human reader."}

TOOLS = [
    _tool("navigate", "Open a URL in the current tab.", {
        "url": {"type": "string"}, "description": _DESC}),
    _tool("click", "Click an element.", {"ref": _REF, "description": _DESC}),
    _tool("fill", "Clear an input and type text into it. Optionally press Enter afterwards.", {
        "ref": _REF, "text": {"type": "string"},
        "press_enter": {"type": "boolean"}, "description": _DESC}),
    _tool("select_option", "Choose an option of a <select> element by its visible label.", {
        "ref": _REF, "option": {"type": "string"}, "description": _DESC}),
    _tool("hover", "Hover the mouse over an element (e.g. to open a menu).", {
        "ref": _REF, "description": _DESC}),
    _tool("press_key", "Press a keyboard key, e.g. 'Enter', 'Escape', 'Tab'.", {
        "key": {"type": "string"}, "description": _DESC}),
    _tool("scroll", "Scroll the page to reveal more content.", {
        "direction": {"type": "string", "enum": ["up", "down"]}, "description": _DESC}),
    _tool("wait", "Wait for the page to update (1-10 seconds).", {
        "seconds": {"type": "integer"}, "description": _DESC}),
    _tool("assert_visible", "Assertion: the element is visible.", {
        "ref": _REF, "description": _DESC}),
    _tool("assert_text_present", "Assertion: this text is visible on the page.", {
        "text": {"type": "string"}, "description": _DESC}),
    _tool("assert_url_contains", "Assertion: the current URL contains this substring.", {
        "text": {"type": "string"}, "description": _DESC}),
    _tool("finish", "End test authoring.", {
        "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
        "summary": {"type": "string"}}),
]


def tool_to_step(name: str, inp: dict) -> dict:
    """Map a tool call to an (unresolved) test step."""
    value = {
        "navigate": inp.get("url", ""),
        "fill": inp.get("text", ""),
        "select_option": inp.get("option", ""),
        "press_key": inp.get("key", ""),
        "scroll": inp.get("direction", "down"),
        "wait": str(inp.get("seconds", 1)),
        "assert_text_present": inp.get("text", ""),
        "assert_url_contains": inp.get("text", ""),
    }.get(name, "")
    step = new_step(name, inp.get("description", name), value,
                    press_enter=bool(inp.get("press_enter")))
    step["ref"] = inp.get("ref", "")
    return step


def recorded_steps(steps: list[dict]) -> list[dict]:
    """Steps that go into the saved test: failed attempts are dropped, since the
    agent retries them another way and replay stops at the first failing step."""
    return [{k: v for k, v in st.items() if k != "ref"} for st in steps if st.get("status") != "failed"]


def has_assertion(steps: list[dict]) -> bool:
    return any(st["action"].startswith("assert") and st.get("status") != "failed" for st in steps)


class StudioSession:
    """One authoring session: a browser, a conversation with Claude, the steps.

    `project` is the project dict: its pipeline "authoring" stage sets the browser
    engine (built-in Playwright or Playwright MCP), skills, extra MCP tools,
    model and the autopilot step limit.
    """

    def __init__(self, project: dict, name: str, url: str, scenario: str, headless: bool = True,
                 credentials: dict | None = None, engine: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.project_id, self.project_name = project["id"], project["name"]
        self.cfg = project["pipeline"]["authoring"]
        self.connections = project.get("connections", [])
        self.engine = engine or self.cfg["engine"]
        self.name, self.url, self.scenario = name, url, scenario
        self.headless = headless
        self.system = SYSTEM_PROMPT + skills.prompt(self.project_id, self.cfg["skills"])
        # {"username", "password"} for the app under test; the password never goes to Claude.
        self.credentials = {k: v for k, v in (credentials or {}).items() if v}
        self.autopilot = False
        self.status = "starting"   # starting|thinking|awaiting_approval|executing|idle|done|error
        self.steps: list[dict] = []
        self.chat: list[dict] = []          # what the UI shows
        self.messages: list[dict] = []      # Claude API history
        self.pending: dict | None = None    # proposed tool call awaiting approval
        self.unanswered: list[dict] = []    # tool_results not yet sent back
        self.notes: list[str] = []          # manual actions to tell Claude about
        self.summary = ""
        self.finish_status = ""             # passed|failed|blocked from the agent's finish
        self.screenshot = ""
        self.page_url = ""
        self.bs: BrowserSession | McpBrowser | None = None
        self.toolbox: mcp_hub.Toolbox | None = None
        self.test_id: str | None = None
        self.lock = asyncio.Lock()
        self._auto_task: asyncio.Task | None = None

    # ---------- public API (called from the server) ----------

    def state(self) -> dict:
        return {
            "id": self.id, "project_id": self.project_id, "project": self.project_name,
            "name": self.name, "url": self.url, "engine": self.engine, "test_id": self.test_id,
            "scenario": self.scenario, "status": self.status, "autopilot": self.autopilot,
            "steps": self.steps, "chat": self.chat, "summary": self.summary,
            "finish_status": self.finish_status,
            "pending": self.pending and {k: self.pending[k] for k in ("name", "input", "step")},
            "page_url": self.page_url, "has_credentials": bool(self.credentials),
        }

    def to_test(self) -> dict:
        """The recorded test, ready for storage.save (keeps its id across re-saves)."""
        return {"id": self.test_id or uuid.uuid4().hex[:10], "project_id": self.project_id,
                "name": self.name, "url": self.url, "scenario": self.scenario, "summary": self.summary,
                "engine": self.engine, "steps": recorded_steps(self.steps)}

    async def start(self) -> None:
        async with self.lock:
            if self.engine == "playwright-mcp":
                conn = mcp_hub.find({"connections": self.connections}, "playwright",
                                    self.cfg.get("browser_connection", ""))
                if not conn:
                    raise mcp_hub.McpError("Движок Playwright MCP выбран, но в проекте нет включённого "
                                           "подключения Playwright MCP")
                self.bs = await McpBrowser.launch(self.project_id, conn, headless=self.headless)
            else:
                self.bs = await BrowserSession.launch(headless=self.headless)
            self.bs.credentials = self.credentials
            conns = [c for c in self.connections if c["id"] in self.cfg.get("tool_connections", [])
                     and c.get("enabled", True) and c["preset"] != "playwright"]
            if conns:
                self.toolbox = await mcp_hub.Toolbox(self.project_id, conns, mode="read").start()
                for err in self.toolbox.errors:
                    self._say("system", f"Инструменты подключения недоступны: {err}")
            step = new_step("navigate", f"Open {self.url}", self.url, source="system")
            await self._run_step(step)
            self._say("user", self.scenario)
            content = [{"type": "text", "text": (
                f"Application under test: {self.url}\nTest scenario: {self.scenario}\n\n"
                f"{self._credentials_note()}"
                "The browser has already opened the application. Current page state:")}]
            content += await self._page_state()
            await self._think(content)

    async def approve(self) -> None:
        async with self.lock:
            await self._execute_pending()
            await self._think()

    async def resume(self) -> None:
        """Hand control back to the AI (after an error or manual steps)."""
        async with self.lock:
            await self._think([{"type": "text", "text": "Continue the scenario. Current page state:"}]
                              + await self._page_state())

    async def reject(self, feedback: str) -> None:
        async with self.lock:
            if not self.pending:
                return
            p = self.pending
            self.pending = None
            self._say("user", f"Rejected: {feedback}" if feedback else "Rejected")
            self.unanswered.append({
                "type": "tool_result", "tool_use_id": p["id"], "is_error": True,
                "content": "The user rejected this step without running it. " +
                           (f"Their feedback: {feedback}" if feedback else "Propose a different step."),
            })
            await self._think()

    async def send_chat(self, text: str) -> None:
        if self.pending:
            await self.reject(text)
            return
        async with self.lock:
            self._say("user", text)
            await self._think([{"type": "text", "text": text + "\n\nCurrent page state:"}]
                              + await self._page_state())

    def set_autopilot(self, on: bool) -> None:
        self.autopilot = on
        if on and (self._auto_task is None or self._auto_task.done()):
            self._auto_task = asyncio.create_task(self._autopilot_loop())

    async def manual_step(self, step: dict) -> dict:
        """A non-element step added by hand (navigate, wait, assert text/url...)."""
        async with self.lock:
            await self._run_manual(step)
            return step

    async def pick(self, x: float, y: float, action: str, value: str, description: str,
                   source: str) -> dict:
        """Element picker / recorder: act on the element at viewport point (x, y)."""
        if not isinstance(self.bs, BrowserSession):
            raise ValueError("Element Picker и запись доступны только со встроенным движком Playwright")
        async with self.lock:
            info = await self.bs.pick_at(x, y)
            if not info:
                raise ValueError("No element at this point")
            ref = info["ref"]
            name = info.get("name") or info.get("placeholder") or info.get("tag", "element")
            if action == "assert_text_present":
                value = value or name
            if not description:
                description = {
                    "click": f"Click '{name}'",
                    "fill": f"Enter '{value}' into '{name}'",
                    "hover": f"Hover over '{name}'",
                    "select_option": f"Select '{value}' in '{name}'",
                    "assert_visible": f"Verify '{name}' is visible",
                    "assert_text_present": f"Verify text '{value}' is shown",
                }.get(action, action)
            step = new_step(action, description, value, source=source)
            step["ref"] = ref if action != "assert_text_present" else ""
            await self._run_manual(step)
            return step

    async def _run_manual(self, step: dict) -> None:
        if self.pending:
            # The page is about to change and element refs get renumbered, so the
            # AI's proposed step is no longer valid.
            self.unanswered.append({
                "type": "tool_result", "tool_use_id": self.pending["id"], "is_error": True,
                "content": "Not executed: the user took over and performed steps manually.",
            })
            self.pending = None
        await self._run_step(step)
        if step["status"] == "passed":
            self.notes.append(f"The user manually performed a step: {step['description']}")
        self.status = "idle"

    async def refresh_screenshot(self) -> None:
        if self.bs and not self.lock.locked():
            async with self.lock:
                self.screenshot = await self.bs.screenshot_b64()
                self.page_url = self.bs.url

    async def close(self) -> None:
        self.autopilot = False
        if self.status != "done":
            self.status = "closed"
        if self.toolbox:
            await self.toolbox.close()
            self.toolbox = None
        if self.bs:
            await self.bs.close()
            self.bs = None

    # ---------- internals ----------

    def _credentials_note(self) -> str:
        if not self.credentials:
            return "No login credentials were provided.\n\n"
        parts = []
        if "username" in self.credentials:
            parts.append(f"username (type {{{{username}}}}, its value is '{self.credentials['username']}')")
        if "password" in self.credentials:
            parts.append("password (type {{password}})")
        return ("Login credentials for this application are available: " + ", ".join(parts) +
                ". Log in with them when the scenario or the site requires it.\n\n")

    def _mask(self, step: dict) -> None:
        """Recorded steps never contain real credentials, only placeholders."""
        password, username = self.credentials.get("password"), self.credentials.get("username")
        if password:
            step["value"] = (step.get("value") or "").replace(password, "{{password}}")
            step["description"] = step["description"].replace(password, "***")
        if username and step.get("value") == username:
            step["value"] = "{{username}}"

    def _say(self, role: str, text: str) -> None:
        if text:
            self.chat.append({"role": role, "text": text})

    async def _page_state(self) -> list[dict]:
        text = await self.bs.describe()
        self.screenshot = await self.bs.screenshot_b64()
        self.page_url = self.bs.url
        if not self.screenshot:
            return [{"type": "text", "text": text}]
        return [
            {"type": "text", "text": text},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": self.screenshot}},
        ]

    async def _run_step(self, step: dict) -> None:
        """Execute a step on the live page and record it."""
        self.status = "executing"
        self._mask(step)
        try:
            await self.bs.execute(step)
            step["status"] = "passed"
        except Exception as e:
            step["status"] = "failed"
            step["error"] = str(e).splitlines()[0][:300]
        step.pop("ref", None)
        self.steps.append(step)
        self.screenshot = await self.bs.screenshot_b64()
        self.page_url = self.bs.url

    async def _execute_pending(self) -> None:
        p = self.pending
        if not p:
            return
        self.pending = None
        step = p["step"]
        await self._run_step(step)
        result = ("Step executed successfully." if step["status"] == "passed"
                  else f"Step FAILED: {step['error']}")
        self.unanswered.append({
            "type": "tool_result", "tool_use_id": p["id"],
            "is_error": step["status"] != "passed",
            "content": [{"type": "text", "text": result + " New page state:"}] + await self._page_state(),
        })

    async def _think(self, extra: list[dict] | None = None) -> None:
        """Send queued tool results / notes / `extra` as a user turn, get the next step.

        Calls to connection tools (Jira, Confluence...) run right away and the agent
        is asked again; a browser action becomes the pending step.
        """
        for _ in range(MAX_HELPER_CALLS):
            content = (self.unanswered + [{"type": "text", "text": n} for n in self.notes]
                       + (extra or []))
            self.unanswered, self.notes, extra = [], [], None
            if content:
                self.messages.append({"role": "user", "content": content})
            if not self.messages or self.messages[-1]["role"] != "user":
                self.status = "idle"
                return
            self.status = "thinking"
            try:
                resp = await llm.client().beta.messages.create(
                    **llm.common_params(self.cfg),
                    max_tokens=16000,
                    system=self.system,
                    tools=TOOLS + (self.toolbox.tools if self.toolbox else []),
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                    messages=self.messages,
                    cache_control={"type": "ephemeral"},
                    betas=[llm.FALLBACK_BETA, llm.CONTEXT_BETA],
                    # Old screenshots/snapshots are useless once the page moved on.
                    context_management={"edits": [{
                        "type": "clear_tool_uses_20250919",
                        "trigger": {"type": "input_tokens", "value": 40000},
                        "keep": {"type": "tool_uses", "value": 4},
                        "clear_at_least": {"type": "input_tokens", "value": 8000},
                    }]},
                )
            except anthropic.APIError as e:
                self.status = "error"
                self.autopilot = False
                self._say("system", llm.api_error_text(e))
                # Put the unsent user turn back in the queue so Retry can resend it.
                self.unanswered = self.messages.pop()["content"]
                return

            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                self.status = "error"
                self._say("system", "The model declined this request.")
                return

            for block in resp.content:
                if block.type == "text":
                    self._say("agent", block.text)
            tool = next((b for b in resp.content if b.type == "tool_use"), None)
            if tool is None:
                self.status = "idle"   # the agent is waiting for the user
                return
            inp = tool.input if isinstance(tool.input, dict) else json.loads(tool.input)
            if self.toolbox and self.toolbox.owns(tool.name):
                self._say("system", f"🔧 {tool.name.replace('__', ' → ', 1)}")
                text, is_error = await self.toolbox.call(tool.name, inp)
                self.unanswered.append({"type": "tool_result", "tool_use_id": tool.id,
                                        "is_error": is_error, "content": text})
                continue
            if tool.name == "finish":
                if inp.get("status") == "passed" and not has_assertion(self.steps):
                    # A test without a passing assertion is green whatever the app does.
                    self.unanswered.append({
                        "type": "tool_result", "tool_use_id": tool.id, "is_error": True,
                        "content": "Not finished: the test has no passing assertion yet. Add an assertion "
                                   "of the scenario's expected result, then call finish again.",
                    })
                    continue
                self.finish_status = inp.get("status", "")
                self.summary = f"[{inp.get('status')}] {inp.get('summary', '')}"
                self._say("agent", self.summary)
                self.unanswered.append({"type": "tool_result", "tool_use_id": tool.id,
                                        "content": "Authoring finished."})
                self.status = "done"
                self.autopilot = False
                return
            step = tool_to_step(tool.name, inp)
            self.pending = {"id": tool.id, "name": tool.name, "input": inp, "step": step}
            self.status = "awaiting_approval"
            return
        self.status = "idle"
        self._say("system", "Агент слишком много ходов подряд не предлагал действий в браузере "
                            "(инструменты подключений или завершение без проверки).")

    async def _autopilot_loop(self) -> None:
        n = 0
        while self.autopilot and n < self.cfg["max_steps"]:
            if self.status == "awaiting_approval" and self.pending:
                await self.approve()
                n += 1
            elif self.status in ("done", "error", "idle"):
                break
            else:
                await asyncio.sleep(0.3)
        self.autopilot = False
