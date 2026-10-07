"""The test-authoring agent (guarded mode and Auto-Pilot).

The LLM sees the live page (screenshot + list of interactive elements), and
proposes ONE browser action at a time as a tool call. Each executed action is
recorded as a test step. In guarded mode every proposed step waits for the
human to press Continue / Reject; in autopilot mode steps run back to back.

A session can also start from a saved test (`base_steps`): the steps are
replayed first and the agent gets a `task` about them - used to strengthen
weak assertions found by mutation testing (mutations.py).

With the built-in engine the XHR/fetch traffic of the session is recorded
(traffic.py) and saved with the test, for API tests and mocks.

A session lives in the memory of its studio instance; its progress (steps, chat, scenario)
is checkpointed to the database after every step (`checkpoint`). After a restart of the studio
an interrupted session is continued (`restored`: its steps are replayed and the agent goes on
from there) or saved as a test as it is (`save_checkpoint`).

The project's model drives it (llm.py). For a weaker model the "authoring" stage offers:
- a compact system prompt plus example turns (the "authoring-examples" skill);
- text mode: the element list is the main input and a screenshot is sent only when
  the agent calls `look` ("on_request"), or never for a model without vision.
Whatever the model, an unknown tool, broken JSON arguments or a ref that is not on the
page are sent back as an error and asked again, at most MAX_REPAIRS times per step.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from urllib.parse import urlparse

from . import fs, knowledge, llm, mailbox, mcp_hub, projects, skills, storage, testdata, traffic, vault
from .browser import BrowserSession, describe_element
from .mcp_browser import McpBrowser
from .providers.base import check_call
from .steps import AUXILIARY_ASSERTIONS, new_step

MAX_HELPER_CALLS = 12   # connection tool calls in a row before the agent must act
MAX_REPAIRS = 2         # invalid answers in a row before the agent stops and waits for a person
KEEP_STATES = 4         # page states (screenshots, snapshots) kept in the conversation
# Short requests: a conversation that grew past this (input tokens of the last request, or turns)
# starts over on a clean context - the scenario, the steps recorded so far and the current page.
FRESH_INPUT_TOKENS = 40000
FRESH_TURNS = 30
# Auto-Pilot: several actions of one answer run back to back without asking the model in between,
# while the page stays the same - the fields of a form are filled in one turn instead of one turn each.
# After an action that may change the page (a click, a navigation) the rest of the batch is skipped.
BATCH_SAFE = {"fill", "select_option", "hover", "upload_file", "assert_visible", "assert_text_present",
              "assert_element_text", "assert_value", "assert_checked", "assert_enabled", "assert_count",
              "assert_url_contains"}
MAX_BATCH = 8

SYSTEM_PROMPT = """You are a QA engineer that authors end-to-end UI test cases by driving a real web browser.

The user gives you a web application and a test scenario in plain language. You carry out the scenario one browser action at a time using the tools. Every tool call you make is recorded as a step of the test case, and the recorded test will later be replayed automatically and exported as Playwright code and Gherkin. So:

- Take the most direct path a real user would take. Avoid exploratory clicks that do not belong in the final test.
- Call one tool per turn, then look at the new page state before deciding the next step. When several tool calls in one turn are allowed (Auto-Pilot), you may batch actions on the SAME page state, typically filling the fields of one form, with the action that submits or navigates LAST: they run in order, and whatever follows an action that changed the page is skipped and must be proposed again.
- Write each `description` as a clear test step in imperative form, e.g. "Click the 'Add to Cart' button" or "Enter 'kindle' into the search field". Write descriptions in the same language as the scenario.
- Verify outcomes, not just actions: after each meaningful state change (search results shown, item added to cart, form submitted) add an assertion step. Assert the actual result: assert_element_text or assert_text_present for messages and data, assert_value for form fields, assert_count for lists, tables and carts, assert_checked / assert_enabled for state, assert_url_contains for navigation. assert_visible only proves that an element is there, so it is rarely enough on its own. The test must end with at least one assertion of the scenario's expected result.
- Target elements only by `ref` values from the LATEST page snapshot; refs change after every action.
- Dismiss cookie banners, newsletter pop-ups and similar overlays when they block the flow.
- Never complete irreversible real-world actions: do not submit real payments, place real orders, send messages or delete data. Go up to that point, assert you reached it, then finish.
- Login credentials, when provided, are given as placeholders. To type them, pass the literal text {{username}} or {{password}} to `fill`; the real values are substituted when the step runs. Never guess credentials and never put a password in a step description.
- Data that must be new on every run (a registration e-mail or login, a name of something you create) must not be a literal, or the second run fails with "already exists". Type these placeholders instead: {{faker.email}}, {{faker.name}}, {{faker.first_name}}, {{faker.last_name}}, {{faker.phone}}, {{faker.company}}, {{faker.city}}, {{faker.address}}, {{faker.user_name}}, {{unique}} (a unique number, e.g. "Order {{unique}}"), {{today}}. Each gets a fresh value on every run and the same value within one run, so you can type {{faker.email}} at registration and again at login, and assert on it.
- A step that fails is not kept in the recorded test. If a step fails, fix the cause (e.g. close an overlay, pick another element) and do it again.
- If you are blocked (CAPTCHA, login you have no credentials for, the site is down), call finish with status "blocked" and explain. If the application does not behave as the scenario expects (a product bug), call finish with status "failed" and describe the bug.
- When the scenario is fully covered and its expected result is asserted, call finish with status "passed" and a short summary. In `evidence` name the assertion steps (by their numbers) that prove the expected result and what each one checks.
- A person watches you work. Before every tool call write exactly one short sentence in the language of the scenario: which part of the scenario you are on and why this step (e.g. "Логин введён, теперь пароль"). No other text outside tool calls.
- Tools whose names contain "__" come from the project's connected systems (Jira, Confluence, test management...). They are read-only helpers for context, e.g. to read the requirements of an issue; they are not test steps. Use them only when the scenario needs that information."""

# For weaker models: fewer words, numbered rules; examples come from a skill.
SYSTEM_PROMPT_COMPACT = """You are a QA engineer. You write an end-to-end UI test by driving a real web browser with the tools: exactly ONE tool call per turn. Every call becomes a step of the test.

Rules:
1. Target elements only by `ref` (like e12) from the LATEST page state.
2. Take the shortest path a real user would take. `description` is a short imperative step in the language of the scenario, e.g. "Click the 'Login' button".
3. After each important change, check the result: assert_element_text or assert_text_present for messages and data, assert_value for fields, assert_count for lists, assert_url_contains for navigation.
4. End with an assertion of the expected result, then call finish with status "passed".
5. Login: type {{username}} and {{password}} literally. Data that must be new on every run: {{faker.email}}, {{faker.name}}, {{unique}}.
6. Never pay, place orders, send messages or delete data: stop before it, assert, finish.
7. Blocked (CAPTCHA, no access): finish with status "blocked". The application behaves wrongly: finish with status "failed".
8. If a step fails, fix the cause (close a pop-up, pick another element) and try again.
9. Tools with "__" in the name are read-only helpers of connected systems, not test steps.
10. Before every tool call write one short sentence in the language of the scenario: what you do and why. In finish, `evidence` lists the assertion steps (numbers) that prove the result."""

LOOK_NOTE = ("\n- You get the page as a list of elements. Call `look` when you need to SEE the page (layout, "
             "images, a result that is only visible). `look` is not a test step.")
EXAMPLES_SKILL = "authoring-examples"


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
    _tool("assert_element_text", "Assertion: this element contains the text (a message, a total, a name).", {
        "ref": _REF, "text": {"type": "string"}, "description": _DESC}),
    _tool("assert_value", "Assertion: the input, textarea or select has exactly this value.", {
        "ref": _REF, "value": {"type": "string"}, "description": _DESC}),
    _tool("assert_checked", "Assertion: the checkbox, radio button or switch is checked (true) or not (false).", {
        "ref": _REF, "checked": {"type": "boolean"}, "description": _DESC}),
    _tool("assert_enabled", "Assertion: the element is enabled (true) or disabled (false).", {
        "ref": _REF, "enabled": {"type": "boolean"}, "description": _DESC}),
    _tool("assert_count", "Assertion: the list, table or group this element belongs to (similar sibling elements: "
                          "rows, cards, cart items) has exactly `count` elements. Pass the ref of any one of them.", {
        "ref": _REF, "count": {"type": "integer"}, "description": _DESC}),
    _tool("assert_no_console_errors", "Assertion: no JavaScript errors appeared in the browser console since the "
                                      "previous such check (or the start of the test).", {"description": _DESC}),
    _tool("assert_accessible", "Assertion: the page has no WCAG 2.1 A/AA accessibility violations of the project's "
                               "severity threshold (axe-core). Use when the scenario is about accessibility.", {
        "description": _DESC}),
    _tool("assert_screenshot", "Visual check: the element, or the whole visible page if ref is empty, looks like its "
                               "baseline image, which is captured on the first run. Use only when the scenario asks "
                               "for a visual check.", {"ref": {"type": "string", "description": "Element ref or ''."},
                                                       "description": _DESC}),
    _tool("double_click", "Double-click an element.", {"ref": _REF, "description": _DESC}),
    _tool("drag_to", "Drag an element and drop it onto another element.", {
        "ref": _REF, "target_ref": {"type": "string", "description": "Ref of the element to drop onto."},
        "description": _DESC}),
    _tool("switch_tab", "Switch to another browser tab: 'last' (the newest), 'first', a number (1 = first) or "
                        "text of its URL.", {"tab": {"type": "string"}, "description": _DESC}),
    _tool("handle_dialog", "Prepare the answer to the NEXT browser dialog (alert / confirm / prompt) BEFORE the "
                           "step that opens it: accept or dismiss, text for a prompt, text the dialog must contain.", {
        "action": {"type": "string", "enum": ["accept", "dismiss"]},
        "prompt_text": {"type": "string", "description": "Text to type into a prompt dialog, or ''."},
        "expect": {"type": "string", "description": "Text the dialog message must contain, or ''."},
        "description": _DESC}),
    _tool("assert_download", "Assertion: the previous step downloaded a file whose name matches the pattern "
                             "(e.g. 'report*.xlsx') and whose size is at least min_bytes.", {
        "name": {"type": "string"}, "min_bytes": {"type": "integer"}, "description": _DESC}),
    _tool("finish", "End test authoring: passed - the scenario is done and its expected result asserted; "
                    "failed - the application does not behave as the scenario expects; blocked - you cannot go on.", {
        "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
        "summary": {"type": "string"},
        "evidence": {"type": "string", "description": "Which assertion steps (numbers) prove the expected result "
                                                      "and what each checks; for failed/blocked - what you saw."}}),
]
UPLOAD_TOOL = _tool("upload_file", "Choose a file in a file input: one of the project's test files listed in the "
                                   "task.", {"ref": _REF, "file": {"type": "string"}, "description": _DESC})
EMAIL_TOOL = _tool("read_email", "Wait for an e-mail in the project's test mailbox and save the code it contains "
                                 "as {{vars.<save>}} for the next steps (2FA, confirmation codes).", {
    "to": {"type": "string", "description": "Recipient address (may be a placeholder like {{faker.email}}), or ''."},
    "subject": {"type": "string", "description": "Text of the subject, or ''."},
    "pattern": {"type": "string", "description": "Regular expression of the code; its first group is saved. "
                                                 "'' = a number of 4-8 digits."},
    "save": {"type": "string", "description": "Variable name, e.g. 'code'."},
    "description": _DESC})
MODULE_TOOL = _tool("use_module", "Run a module of the project (a saved block of steps, e.g. log in or add an item "
                                  "to the cart) as ONE step, with its parameters. Prefer modules to repeating their "
                                  "steps.", {
    "module": {"type": "string", "description": "Module id from the task."},
    "params": {"type": "string", "description": "Parameters as a JSON object, e.g. {\"item\": \"Молоко\"}; '{}' "
                                                "if none."},
    "description": _DESC})
# Need the built-in engine (the page itself, not an MCP server's view of it).
BUILTIN_ONLY = {"assert_accessible", "assert_screenshot", "handle_dialog", "assert_download", "upload_file",
                "read_email", "use_module"}
LOOK_TOOL = _tool("look", "Get a screenshot of the current page (not a test step).", {})
FIND_TOOL = _tool("find_elements", "Find elements of the whole page (all frames, off-screen too) whose name, label, "
                                   "placeholder or value contain the text; returns their refs. Not a test step.", {
    "text": {"type": "string"}})
API_TOOL = _tool(
    "api_request",
    "Send a request to the API of the application under test (same origin as the app; the browser's cookies, so the "
    "login of the page) and record it as a step of the test: for API (backend) checks and for preparing data the "
    "scenario depends on. DELETE is not allowed. The step fails when the status or a checked field differs. The "
    "response body is shown to you after the step: check its fields in the next call or with `expect_json`.", {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH"]},
        "path": {"type": "string", "description": "Path of the API, e.g. /api/products?limit=10 (or a full URL of the app)."},
        "body": {"type": "string", "description": "JSON body, or an empty string."},
        "expect_status": {"type": "integer", "description": "Expected HTTP status; 0 = any 2xx."},
        "expect_json": {"type": "string", "description": "JSON object of checks {\"$.path\": \"expected value\"}, "
                                                         "e.g. {\"$.status\": \"active\"}; \"{}\" for none."},
        "save": {"type": "string", "description": "JSON object {\"variable\": \"$.path\"}: values of the response "
                                                  "used later as {{vars.variable}}; \"{}\" for none."},
        "description": _DESC})
REMEMBER_TOOL = _tool(
    "remember",
    "Save a fact about the application to the project's memory, for later tests (not a test step): a rule or a "
    "behaviour you discovered (\"a paid order cannot be cancelled by the customer\"). Objects of the test data go to "
    "`test_data`. Only facts that hold beyond this run; never passwords.", {"fact": {"type": "string"}})
DATA_TOOL = _tool(
    "test_data",
    "Record test data in the project's application model, so later tests reuse it instead of creating it again (not "
    "a test step): an object the scenario relies on that exists on the stand (the product «Test product A» in stock), "
    "or one this test creates and keeps, with what it requires and its lifecycle as you saw them. Call it when you "
    "find or create such an object; for an object created with {{unique}} name the kind (\"a new order of the "
    "customer\") and how it is created. Never passwords.", {
        "entity": {"type": "string", "description": "Entity of the domain: Product, Category, Order, Customer…"},
        "name": {"type": "string", "description": "The object as the application shows it: «Test product A»."},
        "details": {"type": "string", "description": "What matters for tests: price, settings, links to other objects."},
        "state": {"type": "string", "description": "Its state in the lifecycle: in stock, paid, blocked…; or \"\"."},
        "depends_on": {"type": "string", "description": "Entities that must exist before it, comma-separated; or \"\"."},
        "lifecycle": {"type": "string", "description": "States and transitions seen: \"new → paid → shipped\"; or \"\"."},
        "create": {"type": "string", "description": "How and by which role it is created; or \"\"."}})
# Tools that help the agent but are not recorded as steps.
HELPERS = {"look", "find_elements", "remember", "test_data"}


def _json(d: dict) -> str:
    return json.dumps({k: v for k, v in d.items() if v not in ("", None)}, ensure_ascii=False)


def _params(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def tool_to_step(name: str, inp: dict) -> dict:
    """Map a tool call to an (unresolved) test step."""
    value = {
        "navigate": inp.get("url", ""),
        "fill": inp.get("text", ""),
        "select_option": inp.get("option", ""),
        "press_key": inp.get("key", ""),
        "scroll": inp.get("direction", "down"),
        "wait": str(inp.get("seconds", 1)),
        "upload_file": inp.get("file", ""),
        "switch_tab": inp.get("tab", "last"),
        "assert_text_present": inp.get("text", ""),
        "assert_url_contains": inp.get("text", ""),
        "assert_element_text": inp.get("text", ""),
        "assert_value": inp.get("value", ""),
        "assert_checked": str(bool(inp.get("checked", True))).lower(),
        "assert_enabled": str(bool(inp.get("enabled", True))).lower(),
        "assert_count": str(inp.get("count", "")),
    }.get(name, "")
    if name == "handle_dialog":
        value = _json({"action": inp.get("action", "accept"), "prompt_text": inp.get("prompt_text", ""),
                       "expect": inp.get("expect", "")})
    elif name == "assert_download":
        value = _json({"name": inp.get("name") or "*", "min_bytes": int(inp.get("min_bytes") or 1)})
    elif name == "read_email":
        value = _json({"to": inp.get("to", ""), "subject": inp.get("subject", ""), "pattern": inp.get("pattern", ""),
                       "save": inp.get("save") or "code"})
    elif name == "use_module":
        value = json.dumps({"module": inp.get("module", ""), "params": _params(inp.get("params"))}, ensure_ascii=False)
    elif name == "api_request":
        body = (inp.get("body") or "").strip()
        try:
            body = json.loads(body) if body else ""
        except ValueError:
            pass
        value = json.dumps({k: v for k, v in {
            "method": (inp.get("method") or "GET").upper(), "url": inp.get("path") or "/", "body": body,
            "expect_status": int(inp.get("expect_status") or 0) or None,
            "expect": _params(inp.get("expect_json")), "save": _params(inp.get("save"))}.items() if v not in ("", None, {})},
            ensure_ascii=False)
    step = new_step(name, inp.get("description", name), value,
                    press_enter=bool(inp.get("press_enter")))
    step["ref"] = inp.get("ref", "")
    if inp.get("target_ref"):
        step["target_ref"] = inp["target_ref"]
    return step


def expected_result(scenario: str) -> str:
    """The expected result stated in a scenario ("Ожидаемый результат: ..." / "Expected result: ..."), or ""."""
    m = re.search(r"(?:ожидаемый результат|expected result)\s*[:\-—]\s*(.+)", scenario or "", re.I | re.S)
    return m.group(1).strip()[:500] if m else ""


def _origin(url: str) -> str:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


def project_files(pid: str) -> list[str]:
    """Files of the project for upload_file steps: data/projects/<id>/files/."""
    return sorted(f.name for f in fs.glob(projects.path(pid) / "files", "*") if fs.is_file(f))


RESUME_TASK = ("The authoring session was interrupted (the studio restarted) and is being continued. Do not repeat "
               "the steps above: continue the test scenario from the current page.")
FRESH_TASK = ("To keep the requests short, the conversation was started over: the steps above are already done in "
              "the browser and recorded. Do not repeat them: continue the test scenario from the current page.")
EDIT_TASK = ("A person opened this saved test to edit it: the steps above are already recorded in the test and have "
             "been replayed. Do only what the person asks in the chat (add, change or re-record steps from the current "
             "page); do not repeat the steps above and do not go on with the scenario on your own.")


# ---------- checkpoints of sessions (survive a restart of the studio) ----------

def _checkpoints(pid: str):
    return projects.path(pid) / "sessions"


def load_checkpoint(pid: str, sid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{10}", sid or ""):
        return None
    cp = fs.read_json(_checkpoints(pid) / f"{sid}.json")
    return cp if cp and cp.get("project_id") == pid else None


def list_checkpoints(pid: str) -> list[dict]:
    """Checkpoints of the project's sessions, newest first (live ones too: the caller filters)."""
    out = []
    for _, text, updated in fs.documents(_checkpoints(pid)):
        try:
            cp = json.loads(text)
        except ValueError:
            continue
        out.append({k: cp.get(k) for k in ("id", "name", "url", "scenario", "status", "test_id", "task_id", "origin",
                                           "summary", "created")}
                   | {"updated": cp.get("updated") or updated, "steps": len(recorded_steps(cp.get("steps") or []))})
    return out


def drop_checkpoint(pid: str, sid: str) -> None:
    fs.unlink(_checkpoints(pid) / f"{sid}.json")
    vault.delete(projects.secrets_kind(pid), f"session-{sid}")


def restored(project: dict, cp: dict) -> "StudioSession":
    """A session that continues an interrupted one (same id): the recorded steps are replayed,
    then the agent goes on with the scenario."""
    creds = vault.load(projects.secrets_kind(project["id"]), f"session-{cp['id']}") if cp.get("own_credentials") \
        else None
    account = cp.get("account") or ""
    if account and not projects.account_exists(project["id"], account):
        account = ""
    s = StudioSession(project, cp["name"], cp["url"], cp.get("scenario", ""), headless=cp.get("headless", True),
                      credentials=creds or projects.account_credentials(project["id"], account),
                      base_steps=recorded_steps(cp.get("steps") or []), task=RESUME_TASK, account=account,
                      session_id=cp["id"])
    s.restored, s.origin = True, cp.get("origin")
    s.test_id, s.task_id = cp.get("test_id"), cp.get("task_id") or ""
    s.chat = list(cp.get("chat") or []) + [{"role": "system", "text": "Сессия восстановлена после перезапуска студии: "
                                            "записанные шаги воспроизводятся в новом браузере, затем агент продолжит."}]
    s.summary, s.finish_status = cp.get("summary", ""), cp.get("finish_status", "")
    return s


def save_checkpoint(project: dict, cp: dict, status: str = "") -> tuple[dict, list[str]]:
    """Save an interrupted session's steps as a test without a browser -> (test, warnings)."""
    s = restored(project, cp)
    s.steps = copy.deepcopy(cp.get("steps") or [])
    s.chat = list(cp.get("chat") or [])
    s.status = cp.get("status") or "idle"
    return s.save(status)


def recorded_steps(steps: list[dict]) -> list[dict]:
    """Steps that go into the saved test: failed attempts are dropped, since the
    agent retries them another way and replay stops at the first failing step."""
    return [{k: v for k, v in st.items() if k != "ref"} for st in steps if st.get("status") != "failed"]


def _api_check(step: dict) -> bool:
    """An api_request that checks its response (an expected status or fields): the assertion of an API test."""
    if step["action"] != "api_request":
        return False
    try:
        spec = json.loads(step.get("value") or "{}")
    except ValueError:
        return False
    return isinstance(spec, dict) and bool(spec.get("expect") or spec.get("expect_status"))


def has_assertion(steps: list[dict]) -> bool:
    """A passing check of the result (console, accessibility and visual checks do not count)."""
    return any(((st["action"].startswith("assert") and st["action"] not in AUXILIARY_ASSERTIONS) or _api_check(st))
               and st.get("status") != "failed" for st in steps)


class StudioSession:
    """One authoring session: a browser, a conversation with the LLM, the steps.

    `project` is the project dict: its pipeline "authoring" stage sets the browser
    engine (built-in Playwright or Playwright MCP), skills, extra MCP tools,
    model and the autopilot step limit.
    """

    def __init__(self, project: dict, name: str, url: str, scenario: str, headless: bool = True,
                 credentials: dict | None = None, engine: str = "", base_steps: list[dict] | None = None,
                 task: str = "", use_login_state: bool = True, account: str = "", session_id: str = ""):
        self.id = session_id or uuid.uuid4().hex[:10]
        self.created = time.time()
        self.restored = False               # continues an interrupted session (restored)
        self.origin: dict | None = None     # {"job": id} for a session of a pipeline run
        self.task_id = ""                   # project task the test is made for (server.py)
        self._creds_kept = False
        self.project_id, self.project_name = project["id"], project["name"]
        self.cfg = project["pipeline"]["authoring"]
        self.run_cfg = project["pipeline"]["run"]
        self.connections = project.get("connections", [])
        # A saved test is replayed with its locators, which only the built-in engine resolves.
        self.engine = "builtin" if base_steps else (engine or self.cfg["engine"])
        self.name, self.url, self.scenario = name, url, scenario
        self.base_steps, self.task = copy.deepcopy(base_steps or []), task
        self.headless = headless
        # A weaker model gets a compact prompt with examples and screenshots on request (the stage settings).
        compact = self.cfg.get("prompt") == "compact"
        self.screenshots = self.cfg.get("screenshots") or "always"
        try:
            self.model = llm.model(self.project_id, self.cfg).name
        except llm.NotConfigured:
            self.model = ""
        chosen = list(self.cfg["skills"]) + ([EXAMPLES_SKILL] if compact and EXAMPLES_SKILL not in self.cfg["skills"]
                                             else [])
        self.system = ((SYSTEM_PROMPT_COMPACT if compact else SYSTEM_PROMPT)
                       + (LOOK_NOTE if self.screenshots == "on_request" else "")
                       + projects.language_rule(project)
                       + skills.prompt(self.project_id, chosen))
        builtin = self.engine == "builtin"
        # What the project offers the agent: test files, modules (tests used as steps), a test mailbox.
        self.files = project_files(self.project_id)
        self.modules = [t for t in storage.all_tests(self.project_id) if t.get("role") == "module"] if builtin else []
        self.mailbox = bool((project.get("mailbox") or {}).get("kind"))
        self.tools = [t for t in TOOLS if builtin or t["name"] not in BUILTIN_ONLY] + [REMEMBER_TOOL, DATA_TOOL]
        if builtin:
            self.tools += [FIND_TOOL, API_TOOL] + ([UPLOAD_TOOL] if self.files else []) + \
                ([EMAIL_TOOL] if self.mailbox else []) + ([MODULE_TOOL] if self.modules else [])
        if self.screenshots == "on_request":
            self.tools.append(LOOK_TOOL)
        self.repairs = 0                    # invalid answers in a row
        self.device = self.cfg.get("device") or ""
        # With a login test in the project the session starts logged in, like the tests will run
        # (use_login_state=False: record from the logged-out state, e.g. a new login test).
        self.use_login_state = use_login_state
        self.logged_in = False
        self.project = project
        # {"username", "password", "totp_secret", "params"} for the app under test: the password and
        # secret login parameters never go to the LLM. `account`: the project account they come from.
        self.credentials = {k: v for k, v in (credentials or {}).items() if v}
        self.account = account
        self.autopilot = False
        self.status = "starting"   # starting|thinking|awaiting_approval|executing|idle|done|error
        self.steps: list[dict] = []
        self.chat: list[dict] = []          # what the UI shows
        self.messages: list[dict] = []      # LLM conversation history
        self.turns, self.last_input = 0, 0  # its length: past FRESH_* it starts over (_start_over)
        self.pending: dict | None = None    # proposed tool call awaiting approval
        self.unanswered: list[dict] = []    # tool_results not yet sent back
        self.notes: list[str] = []          # manual actions to tell the LLM about
        self.summary = ""
        self.finish_status = ""             # passed|failed|blocked from the agent's finish
        self.finish: dict | None = None     # the agent's finish: status, summary, evidence, assertions
        self.screenshot = ""
        self.page_url = ""
        self.bs: BrowserSession | McpBrowser | None = None
        self.toolbox: mcp_hub.Toolbox | None = None
        self.test_id: str | None = None
        budget = project["pipeline"].get("budget") or {}
        self.usage = llm.Usage(budget.get("session") or 0, budget.get("currency") or "USD", "сессии")
        self.usage.on_warn = lambda text: self._say("system", text)
        self.edits = 0                      # steps a person rejected or changed
        # Where the time of generation goes: waiting for the model, the browser (steps, page states), turns.
        self.timing = {"model": 0.0, "browser": 0.0, "turns": 0}
        self.started = None
        self.lock = asyncio.Lock()
        self._auto_task: asyncio.Task | None = None
        self._llm_task: asyncio.Task | None = None    # the task waiting for the model: Stop cancels it
        # Auto-Pilot from the start (set before start()): the first turn may already batch actions.
        self.starts_in_autopilot = False
        self.max_steps = self.cfg["max_steps"]

    # ---------- public API (called from the server) ----------

    def state(self) -> dict:
        return {
            "id": self.id, "project_id": self.project_id, "project": self.project_name,
            "name": self.name, "url": self.url, "engine": self.engine, "test_id": self.test_id,
            "scenario": self.scenario, "status": self.status, "autopilot": self.autopilot,
            "steps": self.steps, "chat": self.chat, "summary": self.summary,
            "finish_status": self.finish_status, "finish": self.finish, "max_steps": self.max_steps,
            "expected": expected_result(self.scenario),
            "pending": self.pending and {k: self.pending[k] for k in ("name", "input", "step")},
            "page_url": self.page_url, "has_credentials": bool(self.credentials),
            "usage": self.usage.as_dict(), "traffic": len(getattr(self.bs, "traffic", None) or []),
            "model": {"name": self.model, "screenshots": self.screenshots},
            "timing": {k: round(v, 1) for k, v in self.timing.items()},
        }

    def to_test(self) -> dict:
        """The recorded test, ready for storage.save (keeps its id across re-saves)."""
        return {"id": self.test_id or uuid.uuid4().hex[:10], "project_id": self.project_id,
                "name": self.name, "url": self.url, "scenario": self.scenario, "summary": self.summary,
                "engine": self.engine, "steps": recorded_steps(self.steps),
                "authoring_usage": self.usage.as_dict(),
                "authoring_stats": {"model": self.model, "edits": self.edits,
                                    "timing": {k: round(v, 1) for k, v in self.timing.items()},
                                    "seconds": round(time.monotonic() - self.started, 1) if self.started else None}}

    def save(self, status: str = "") -> tuple[dict, list[str]]:
        """Save the recorded test -> (test, warnings). A re-save keeps the test's id and what
        the studio attached to it (tags, quarantine, external keys, last run, status, comments...).
        A new test is a draft: it joins the regression suite after a person's review (`status`
        "ready" when the person saving it says so)."""
        t = self.to_test()
        old = storage.load(t["id"])
        t["status"] = "draft"
        if old:
            for key in ("external", "last_run", "priority", "scenario_type", "source", "gherkin_scenario", "tags",
                        "quarantine", "role", "before", "after", "status", "comments", "review", "account"):
                if key in old:
                    t[key] = old[key]
        if status in storage.STATUSES:
            t["status"] = status
            ids = {s["id"] for s in t["steps"]}
            t["heal_proposals"] = [p for p in old.get("heal_proposals") or [] if p["step_id"] in ids]
            if old.get("verify") and [(s["id"], s.get("value")) for s in old["steps"]] == \
                    [(s["id"], s.get("value")) for s in t["steps"]]:
                t["verify"] = old["verify"]
        if self.account:
            t["account"] = self.account
        storage.save(t)
        self.test_id = t["id"]
        self.checkpoint()
        # A login typed for this session (not a project account) stays with the test.
        if self.credentials and not self.account and self.credentials != projects.app_credentials(self.project_id):
            storage.set_own_credentials(t, self.credentials)
        self.save_artifacts(t)
        warnings = []
        dropped = len(self.steps) - len(t["steps"])
        if dropped:
            warnings.append(f"Упавшие шаги не сохранены в тест: {dropped}")
        if not has_assertion(t["steps"]):
            warnings.append("В тесте нет ни одной проверки результата: прогон будет успешным, что бы ни показало "
                            "приложение")
        return t, warnings

    def save_artifacts(self, test: dict) -> None:
        """What the session recorded besides the steps: the XHR/fetch traffic."""
        entries = getattr(self.bs, "traffic", None)
        if entries is None:
            return
        index, n = {}, 0
        for i, st in enumerate(self.steps):
            if st.get("status") != "failed":
                index[i] = n
                n += 1
        traffic.save(self.project_id, test["id"], [e | {"step": index.get(e.get("step", -1), -1)} for e in entries],
                     self.url)

    async def start(self) -> None:
        self.started = time.monotonic()
        async with self.lock:
            if self.engine == "playwright-mcp":
                conn = mcp_hub.find({"connections": self.connections}, "playwright",
                                    self.cfg.get("browser_connection", ""))
                if not conn:
                    raise mcp_hub.McpError("Движок Playwright MCP выбран, но в проекте нет включённого "
                                           "подключения Playwright MCP")
                self.bs = await McpBrowser.launch(self.project_id, conn, headless=self.headless)
            else:
                state = await self._login_state()
                self.bs = await BrowserSession.launch(headless=self.headless, record_traffic=True, device=self.device,
                                                      locale=self.run_cfg.get("locale", ""),
                                                      timezone=self.run_cfg.get("timezone", ""), storage_state=state)
                self.bs.options = {"a11y_impact": self.run_cfg["a11y_impact"],
                                   "visual_threshold": self.run_cfg["visual_threshold"],
                                   "project_id": self.project_id,
                                   "base_url": self.project.get("base_url") or _origin(self.url), "own_vars": []}
            self.bs.credentials = self.credentials
            conns = [c for c in self.connections if c["id"] in self.cfg.get("tool_connections", [])
                     and c.get("enabled", True) and c["preset"] != "playwright"]
            if conns:
                self.toolbox = await mcp_hub.Toolbox(self.project_id, conns, mode="read").start()
                for err in self.toolbox.errors:
                    self._say("system", f"Инструменты подключения недоступны: {err}")
            if self.base_steps:
                try:
                    await self._replay()
                except ValueError as e:
                    if not self.restored and self.task != EDIT_TASK:
                        raise
                    # The steps stay in the session: a person saves, corrects or continues them.
                    self.steps = copy.deepcopy(self.base_steps)
                    self.status = "idle"
                    self._say("system", f"{e}. Записанные шаги сохранены в сессии: их можно сохранить как тест, "
                                        "исправить или продолжить с текущей страницы («Продолжить ИИ» или вручную).")
                    return
                if self.task == EDIT_TASK:
                    # Editing: the person leads. The model is asked only when they write in the chat.
                    self.status = "idle"
                    self._say("system", f"Тест воспроизведён в браузере (шагов: {len(self.steps)}). Правьте шаги в "
                                        "списке, добавляйте новые кнопкой «+ шаг», через Element Picker или попросите "
                                        "агента в чате, затем «Сохранить тест» — изменения уйдут в тот же тест "
                                        "новой версией.")
                    return
                if not self.restored:
                    self._say("user", self.task)
                done = "\n".join(f"{i + 1}. [{s['action']}] {s['description']}"
                                 + (f" | value: {s['value']}" if s.get("value") else "")
                                 for i, s in enumerate(self.steps))
                text = (f"Application under test: {self.url}\nTest scenario: {self.scenario}\n\n"
                        f"{self._credentials_note()}{self._context_note()}The recorded test has been replayed in "
                        f"the browser:\n{done}\n\n{self.task}\n\nCurrent page state:")
            else:
                step = new_step("navigate", f"Open {self.url}", self.url, source="system")
                await self._run_step(step)
                self._say("user", self.scenario)
                text = (f"Application under test: {self.url}\nTest scenario: {self.scenario}\n\n"
                        f"{self._credentials_note()}{self._context_note()}"
                        "The browser has already opened the application. Current page state:")
            await self._think([{"type": "text", "text": text}] + await self._page_state())

    async def _login_state(self) -> dict | None:
        """The project's saved login (its login test), unless this session records a login or module."""
        from . import pipeline      # the pipeline imports this module
        if not self.use_login_state or not pipeline.uses_login_state(self.project, {"id": self.test_id or "",
                                                                                     "role": ""}):
            return None
        try:
            state = await pipeline.ensure_login_state(self.project, self.credentials or projects.app_credentials(
                self.project_id), headless=True)
        except Exception as e:
            self._say("system", f"Не удалось войти тестом входа проекта: {llm.api_error_text(e)}")
            return None
        if state:
            self.logged_in = True
            self._say("system", "Сессия начата с сохранённым входом (тест входа проекта): шаги входа записывать "
                                "не нужно.")
        return state

    def _context_note(self) -> str:
        """What else the agent can use: login state, files, modules, the mailbox."""
        parts = []
        if self.logged_in:
            parts.append("The browser is ALREADY LOGGED IN to the application (the project's login test ran before "
                         "this session): do not record login steps.")
        if self.files:
            parts.append("Test files for upload_file: " + ", ".join(self.files) + ".")
        if self.modules:
            lines = []
            for m in self.modules:
                params = sorted({k[7:] for s in m["steps"] for k in re.findall(r"\{\{(params\.\w+)\}\}", s.get("value", ""))})
                lines.append(f"- {m['id']}: «{m['name']}»" + (f", params: {', '.join(params)}" if params else ""))
            parts.append("Modules of the project (use_module runs one as a single step):\n" + "\n".join(lines))
        if self.mailbox:
            parts.append("The project has a test mailbox: read_email waits for a letter and saves its code as "
                         "{{vars.<name>}}.")
        model = knowledge.prompt(self.project_id)
        if model:
            parts.append(model + "\nBefore a step that needs other data (an object it depends on, a role, a state), "
                                 "make sure that data exists: reuse the test data listed above (do not create a "
                                 "duplicate), or prepare it first as part of the test (through the UI or api_request). "
                                 "If a precondition cannot be met, finish with status \"blocked\" and name it.")
        parts.append("Record the test data the scenario finds or creates with `test_data` (an object, its state, what it "
                     "requires): later tests of the project reuse it.")
        return "\n\n".join(parts) + ("\n\n" if parts else "")

    async def approve(self) -> None:
        async with self.lock:
            await self._execute_pending()
            await self._think()

    async def resume(self) -> None:
        """Hand control back to the AI (after an error or manual steps)."""
        async with self.lock:
            await self._think([{"type": "text", "text": self._lead() + "Continue the scenario. Current page state:"}]
                              + await self._page_state())

    def _lead(self, task: str = RESUME_TASK) -> str:
        """The context of a conversation that has not started yet (a restored session whose replay
        stopped, or one started over to keep requests short): the scenario and the steps recorded so far."""
        if self.messages:
            return ""
        done = "\n".join(f"{i + 1}. [{s['action']}] {s['description']}"
                         + (f" | value: {s['value']}" if s.get("value") else "")
                         + (f" | FAILED: {s['error']}" if s.get("status") == "failed" else "")
                         for i, s in enumerate(recorded_steps(self.steps)))
        extra = (f"Your task in this session: {self.task}\n\n"
                 if self.task and self.task not in (EDIT_TASK, RESUME_TASK) else "")
        return (f"Application under test: {self.url}\nTest scenario: {self.scenario}\n\n{extra}"
                f"{self._credentials_note()}{self._context_note()}The test steps recorded so far:\n{done}\n\n"
                f"{self.task if self.task == EDIT_TASK else task}\n\n")

    def _fresh_due(self) -> bool:
        return bool(self.messages) and (self.last_input >= FRESH_INPUT_TOKENS or self.turns >= FRESH_TURNS)

    def _start_over(self, content: list[dict]) -> list[dict]:
        """A clean conversation instead of the grown one: the lead (scenario, recorded steps) and the
        results of the last answer's calls as plain text - their tool calls are not in it any more."""
        self.messages, self.turns, self.last_input = [], 0, 0
        out = [{"type": "text", "text": self._lead(FRESH_TASK)}]
        for b in content:
            if b.get("type") != "tool_result":
                out.append(b)
                continue
            head = "Result of your last call" + (" (error)" if b.get("is_error") else "") + ": "
            body = b.get("content")
            if isinstance(body, str):
                out.append({"type": "text", "text": head + body})
            else:
                out.append({"type": "text", "text": head})
                out += body or []
        return out

    async def reject(self, feedback: str) -> None:
        async with self.lock:
            if not self.pending:
                return
            p = self.pending
            self.pending = None
            self.edits += 1
            self._say("user", f"Rejected: {feedback}" if feedback else "Rejected")
            self.unanswered.append({
                "type": "tool_result", "tool_use_id": p["id"], "is_error": True,
                "content": "The user rejected this step without running it. " +
                           (f"Their feedback: {feedback}" if feedback else "Propose a different step."),
            })
            self._drop_batch(p, "the user rejected the step before it")
            await self._think()

    async def send_chat(self, text: str) -> None:
        if self.pending:
            await self.reject(text)
            return
        async with self.lock:
            self._say("user", text)
            await self._think([{"type": "text", "text": self._lead() + text + "\n\nCurrent page state:"}]
                              + await self._page_state())

    def stop(self) -> None:
        """Stop generating: Auto-Pilot off and the request to the model in flight cancelled. A browser
        step being executed finishes (stopping it halfway would leave the page in between); a
        proposed step stays for the person. "Continue with AI" goes on from here."""
        self.autopilot = False
        if self._llm_task and not self._llm_task.done():
            self._llm_task.cancel()
        elif self.status == "executing":
            self._say("system", "Остановлено: текущий шаг доделается, дальше агент не пойдёт.")
        elif self.status not in ("done", "error"):
            self._say("system", "Остановлено.")

    def set_autopilot(self, on: bool) -> None:
        self.autopilot = on
        self.starts_in_autopilot = False
        if on and (self._auto_task is None or self._auto_task.done()):
            self._auto_task = asyncio.create_task(self._autopilot_loop())

    async def manual_step(self, step: dict) -> dict:
        """A non-element step added by hand (navigate, wait, assert text/url...)."""
        async with self.lock:
            await self._run_manual(step)
            return step

    async def pick(self, x: float, y: float, action: str, value: str, description: str,
                   source: str, target: tuple[float, float] | None = None) -> dict:
        """Element picker / recorder: act on the element at viewport point (x, y); drag_to drops it
        onto the element at `target`."""
        if not isinstance(self.bs, BrowserSession):
            raise ValueError("Element Picker и запись доступны только со встроенным движком Playwright")
        async with self.lock:
            drop = None
            if action == "drag_to":
                if target is None:
                    raise ValueError("Для перетаскивания укажите, куда бросить элемент")
                info, drop = await self.bs.pick_at(x, y, target)
                if not drop:
                    raise ValueError("Нет элемента в месте, куда нужно перетащить")
            else:
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
                    "double_click": f"Double-click '{name}'",
                    "fill": f"Enter '{value}' into '{name}'",
                    "hover": f"Hover over '{name}'",
                    "upload_file": f"Upload '{value}' into '{name}'",
                    "drag_to": f"Drag '{name}' onto '{(drop or {}).get('name') or 'the target'}'",
                    "select_option": f"Select '{value}' in '{name}'",
                    "assert_visible": f"Verify '{name}' is visible",
                    "assert_text_present": f"Verify text '{value}' is shown",
                    "assert_element_text": f"Verify '{name}' shows the expected text",
                    "assert_value": f"Verify the value of '{name}'",
                    "assert_checked": f"Verify the state of '{name}'",
                    "assert_enabled": f"Verify '{name}' is enabled or disabled as expected",
                    "assert_count": f"Verify the number of items like '{name}'",
                    "assert_screenshot": f"Visual check of '{name}'",
                }.get(action, action)
            step = new_step(action, description, value, source=source)
            step["ref"] = ref if action != "assert_text_present" else ""
            if drop:
                step["target_ref"] = drop["ref"]
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
            self._drop_batch(self.pending, "the user took over and performed steps manually")
            self.pending = None
        await self._run_step(step)
        if step["status"] == "passed":
            self.notes.append(f"The user manually performed a step: {step['description']}"
                              + (f" (value: {step['value']})" if step.get("value") else ""))
        self.status = "idle"

    async def refresh_screenshot(self) -> None:
        if self.bs and not self.lock.locked():
            async with self.lock:
                self.screenshot = await self.bs.screenshot_b64()
                self.page_url = self.bs.url

    def checkpoint(self) -> None:
        """The session's progress in the database: after a restart of the studio it is continued
        (restored) or saved as a test. The LLM conversation is not kept - a restored session replays
        the steps. Real credentials typed for this session go to the vault, never here."""
        if self.status == "closed":
            return
        own = bool(self.credentials) and not self.account             and self.credentials != projects.app_credentials(self.project_id)
        chat = self.chat
        for secret in (self.credentials.get("password"), self.credentials.get("totp_secret")):
            if secret:
                chat = [m | {"text": m["text"].replace(secret, "***")} for m in chat]
        try:
            if own and not self._creds_kept:
                vault.save(projects.secrets_kind(self.project_id), f"session-{self.id}", self.credentials)
                self._creds_kept = True
            fs.write_json(_checkpoints(self.project_id) / f"{self.id}.json", {
                "id": self.id, "project_id": self.project_id, "name": self.name, "url": self.url,
                "scenario": self.scenario, "engine": self.engine, "headless": self.headless,
                "test_id": self.test_id, "task_id": self.task_id, "origin": self.origin, "account": self.account,
                "steps": self.steps, "chat": chat[-300:], "summary": self.summary,
                "finish_status": self.finish_status, "status": self.status, "own_credentials": own,
                "created": self.created, "updated": time.time()}, indent=None)
        except Exception as e:      # the checkpoint must not break the session
            logging.getLogger("testgen.agent").warning("Не удалось сохранить снимок сессии %s: %s", self.id, e)

    async def close(self, discard: bool = False) -> None:
        """Stop the browser. `discard`: the session is done with (closed by a person, its test
        saved by the pipeline) - its checkpoint goes too; otherwise it stays for a restore."""
        if discard:
            drop_checkpoint(self.project_id, self.id)
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

    async def _replay(self) -> None:
        """Run the saved steps (base_steps) and record them as passed (modules included, no healing)."""
        from . import runner
        self.status = "executing"
        steps = [st | {"status": "pending", "error": ""} for st in self.base_steps]
        report = {"passed": True, "healed": 0, "proposals": []}
        results: list[dict] = []
        self.bs.follow_new_tabs = not any(s["action"] == "switch_tab" for s in steps)
        await runner.run_steps(self.bs, {"id": self.test_id or "", "project_id": self.project_id, "steps": steps},
                               steps, {"self_heal": False}, report, results, shots=False, heal_ok=False)
        self.bs.follow_new_tabs = True
        if not report["passed"]:
            failed = results[-1]
            raise ValueError(f"Не удалось воспроизвести сохранённый тест на шаге «{failed['description']}»: "
                             f"{failed['error'][:200]}")
        for step in steps:
            step["status"] = "passed"
            self.steps.append(step)
        self.bs.new_tabs = 0
        self.screenshot = await self.bs.screenshot_b64()
        self.page_url = self.bs.url

    def _credentials_note(self) -> str:
        if not self.credentials:
            return "No login credentials were provided.\n\n"
        parts = []
        if "username" in self.credentials:
            parts.append(f"username (type {{{{username}}}}, its value is '{self.credentials['username']}')")
        if "password" in self.credentials:
            parts.append("password (type {{password}})")
        if self.credentials.get("totp_secret"):
            parts.append("the current one-time 2FA code (type {{totp}})")
        for p in self.credentials.get("params") or []:
            parts.append(f"login parameter '{p['name']}' (type {{{{auth.{p['name']}}}}}"
                         + (")" if p.get("secret") else f", its value is '{p['value']}')"))
        return ("Login credentials for this application are available: " + ", ".join(parts) +
                ". Log in with them when the scenario or the site requires it.\n\n")

    def _mask(self, step: dict) -> None:
        """Recorded steps never contain real credentials, only placeholders."""
        username = self.credentials.get("username")
        for secret, placeholder in testdata.secret_pairs(self.credentials):
            step["value"] = (step.get("value") or "").replace(secret, placeholder)
            step["description"] = step["description"].replace(secret, "***")
            step["error"] = (step.get("error") or "").replace(secret, "***")
        if username and step.get("value") == username:
            step["value"] = "{{username}}"

    def _say(self, role: str, text: str) -> None:
        if text:
            self.chat.append({"role": role, "text": text})
            self.checkpoint()

    async def _page_state(self) -> list[dict]:
        t0 = time.monotonic()
        text = await self.bs.describe()
        self.screenshot = await self.bs.screenshot_b64()
        self.page_url = self.bs.url
        self.timing["browser"] += time.monotonic() - t0
        if not self.screenshot or self.screenshots != "always":
            return [{"type": "text", "text": text}]
        return [{"type": "text", "text": text}, self._image()]

    def _image(self) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": self.screenshot}}

    async def _helper(self, name: str, inp: dict):
        """A tool that informs the agent without becoming a step -> tool_result content."""
        if name == "look":
            self.screenshot = await self.bs.screenshot_b64()
            if not self.screenshot:
                return "No screenshot is available."
            return [{"type": "text", "text": f"Screenshot of {self.bs.url}:"}, self._image()]
        if name == "remember":
            fact = str(inp.get("fact") or "")
            for secret in testdata.secret_values(self.credentials):
                fact = fact.replace(secret, "***")
            try:
                knowledge.remember(self.project_id, fact, source=f"Studio: {self.name}")
            except ValueError as e:
                return str(e)
            self._say("system", f"🧠 Запомнено: {fact[:300]}")
            return "Saved to the project's memory."
        if name == "test_data":
            item = {k: str(inp.get(k) or "") for k in ("entity", "name", "details", "state", "lifecycle", "create")}
            for secret in testdata.secret_values(self.credentials):
                item = {k: v.replace(secret, "***") for k, v in item.items()}
            item["depends_on"] = [x.strip() for x in str(inp.get("depends_on") or "").split(",") if x.strip()]
            try:
                knowledge.record(self.project_id, item, source=f"Studio: {self.name}")
            except ValueError as e:
                return str(e)
            self._say("system", f"🗂 Тестовые данные: {item['entity']} «{item['name']}»"
                                + (f" — {item['state']}" if item["state"] else ""))
            return "Recorded in the application model of the project."
        if name == "find_elements":
            if not isinstance(self.bs, BrowserSession):
                return "find_elements works with the built-in browser engine only."
            found = self.bs.find_text(inp.get("text", ""))
            if not found:
                return f"No element of the page contains {inp.get('text', '')!r}."
            return "Found (refs are valid until the next action):\n" + "\n".join(describe_element(e) for e in found)
        return f"Unknown helper {name}"

    def _invalid(self, call: dict) -> str:
        """What is wrong with a proposed tool call ("" if nothing): unknown tool, bad arguments,
        a ref that is not on the current page."""
        problem = check_call(call, self.tools + (self.toolbox.tools if self.toolbox else []))
        if problem:
            return problem
        for key in ("ref", "target_ref"):
            ref = call["input"].get(key)
            if ref and isinstance(self.bs, BrowserSession) and ref not in self.bs.elements:
                return (f"There is no element with ref {ref!r} on the current page. Use a ref from the latest page "
                        "state.")
        if call["name"] == "upload_file" and call["input"].get("file") not in self.files:
            return f"Unknown file {call['input'].get('file')!r}. Test files: {', '.join(self.files)}."
        if call["name"] == "use_module" and call["input"].get("module") not in {m["id"] for m in self.modules}:
            return "Unknown module id. Modules: " + ", ".join(f"{m['id']} ({m['name']})" for m in self.modules)
        return ""

    async def _run_step(self, step: dict, shot: bool = True) -> None:
        """Execute a step on the live page and record it. A tab opened by the step is followed
        and recorded as a switch_tab step; a dialog nobody prepared for is reported to the agent.
        `shot=False`: the caller takes the page state (with a screenshot) right after."""
        self.status = "executing"
        t0 = time.monotonic()
        self._mask(step)
        builtin = isinstance(self.bs, BrowserSession)
        dialogs = len(self.bs.dialogs) if builtin else 0
        if builtin:
            self.bs.step_index = len(self.steps)
            self.bs.new_tabs = 0
        try:
            if step["action"] == "use_module":
                await self._run_module(step)
            else:
                await self.bs.execute(step)
            step["status"] = "passed"
        except Exception as e:
            step["status"] = "failed"
            step["error"] = (self.bs.mask(str(e)) if builtin else str(e)).splitlines()[0][:300]
        # Assertions recorded without a value take the page's current one: mask it too.
        self._mask(step)
        step.pop("ref", None)
        step.pop("target_ref", None)
        self.steps.append(step)
        if builtin:
            for d in self.bs.dialogs[dialogs:]:
                if d["action"] == "dismissed":
                    self.notes.append(f"A {d['type']} dialog appeared: {d['message'][:200]!r}. It was dismissed. If "
                                      "the scenario needs to accept it, call handle_dialog and then repeat the step.")
            if step["action"] == "api_request" and self.bs.last_response:
                r, self.bs.last_response = self.bs.last_response, None
                self.notes.append(f"Response of the API request (HTTP {r['status']}):\n{r['body'] or '(empty)'}")
            if self.bs.new_tabs and step["status"] == "passed" and step["action"] != "switch_tab":
                tab = new_step("switch_tab", "Перейти на открывшуюся вкладку", "last", source="system")
                tab["status"] = "passed"
                self.steps.append(tab)
                self.bs.new_tabs = 0
                self.notes.append("The step opened a new tab; the browser switched to it (recorded as a switch_tab "
                                  "step).")
        if shot:
            self.screenshot = await self.bs.screenshot_b64()
        self.page_url = self.bs.url
        self.timing["browser"] += time.monotonic() - t0
        self.checkpoint()

    async def _run_module(self, step: dict) -> None:
        """use_module while authoring: the module's steps run in place with its parameters."""
        from . import runner
        report = {"passed": True, "healed": 0, "proposals": []}
        res = {"id": step["id"], "description": step["description"], "status": "passed", "error": ""}
        await runner._module(self.bs, step, {"self_heal": False}, report, res, run_dir=None, prefix="", depth=0,
                             stack=(self.test_id or "",), heal_ok=False)

    async def _execute_pending(self) -> None:
        """The proposed step, then the rest of its batch (Auto-Pilot) while the page stays the same;
        one page state for the model at the end."""
        p = self.pending
        if not p:
            return
        self.pending = None
        step = p["step"]
        await self._run_step(step, shot=False)
        done = [(p["id"], step)]
        url = self.bs.url
        skip = ""
        for call in p.get("batch") or []:
            prev = done[-1][1]
            if not skip:
                skip = ("Auto-Pilot is off: the person approves steps one by one" if not self.autopilot
                        else "an earlier action of this turn failed" if prev["status"] != "passed"
                        else "the page may have changed after the previous action" if prev["action"] not in BATCH_SAFE
                        or self.bs.url != url or getattr(self.bs, "new_tabs", 0)
                        else self._invalid(call))
            if skip:
                self.unanswered.append({"type": "tool_result", "tool_use_id": call["id"], "is_error": True,
                                        "content": f"Not executed: {skip}. Propose it again if it is still needed."})
                continue
            nxt = tool_to_step(call["name"], call["input"])
            await self._run_step(nxt, shot=False)
            done.append((call["id"], nxt))
        state = await self._page_state()
        for i, (tid, st) in enumerate(done):
            result = "Step executed successfully." if st["status"] == "passed" else f"Step FAILED: {st['error']}"
            last = i == len(done) - 1
            self.unanswered.append({
                "type": "tool_result", "tool_use_id": tid, "is_error": st["status"] != "passed",
                "content": [{"type": "text", "text": result + " New page state:"}] + state if last else result,
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
            if content and self._fresh_due() and self.messages[-1]["role"] == "assistant":
                content = self._start_over(content)
                self._say("system", "Контекст агента вырос — следующий запрос идёт с чистого контекста: сценарий, "
                                    "записанные шаги и текущая страница.")
            if content:
                self.messages.append({"role": "user", "content": content})
            if not self.messages or self.messages[-1]["role"] != "user":
                self.status = "idle"
                return
            self.status = "thinking"
            self._llm_task = asyncio.current_task()
            t0 = time.monotonic()
            try:
                # Tools + rules + skills are cached for the whole session; old screenshots
                # and snapshots are dropped: they are useless once the page moved on.
                # Auto-Pilot with the built-in engine may batch actions on one page (BATCH_SAFE); a person
                # approving steps sees them one at a time.
                reply = await llm.chat(self.cfg, system=self.system,
                                       tools=self.tools + (self.toolbox.tools if self.toolbox else []),
                                       messages=self.messages, max_tokens=16000, one_tool=not self._batching(),
                                       cache_all=True,
                                       keep_images=KEEP_STATES, usage=self.usage, project_id=self.project_id,
                                       stage_name="authoring")
            except llm.ProviderError as e:
                self.status = "error"
                self.autopilot = False
                self._say("system", llm.api_error_text(e))
                # Put the unsent user turn back in the queue so Retry can resend it.
                self.unanswered = self.messages.pop()["content"]
                return
            except asyncio.CancelledError:
                # Stop: the turn goes back to the queue, "Continue with AI" sends it again.
                self.unanswered = self.messages.pop()["content"]
                self.status, self.autopilot = "idle", False
                self._say("system", "Генерация остановлена. Можно поправить шаги, подсказать агенту в чате или "
                                    "нажать «Продолжить с AI».")
                raise
            finally:
                self._llm_task = None
                self.timing["model"] += time.monotonic() - t0
                self.timing["turns"] += 1

            self.messages.append({"role": "assistant", "content": reply.content})
            u = reply.usage or {}
            self.turns += 1
            self.last_input = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                               + u.get("cache_read_input_tokens", 0))
            if reply.stop == "refusal":
                self.status = "error"
                self._say("system", "ИИ отклонил этот запрос.")
                return

            if reply.text:
                self._say("agent", reply.text)
            calls = list(reply.tool_calls)
            tool = calls[0] if calls else None
            if tool is None and not reply.text:
                # Some models (through gateways) now and then answer with nothing at all.
                self.repairs += 1
                if self.repairs <= MAX_REPAIRS:
                    self.notes.append("Your reply was empty. Continue the scenario: call exactly one tool.")
                    continue
                self.autopilot = False
                self._say("system", "Модель несколько раз подряд вернула пустой ответ. Подскажите ей в чате "
                                    "или нажмите «Продолжить с AI».")
            if tool is None:
                self.status = "idle"   # the agent is waiting for the user
                return
            tid, name, inp = tool["id"], tool["name"], tool.get("input")
            # Every tool call of the answer gets a result: the rest of a batch of actions is kept for
            # _execute_pending, anything else is sent back.
            batch = []
            for extra in calls[1:]:
                if self._is_action(tool) and self._is_action(extra) and len(batch) < MAX_BATCH:
                    batch.append(extra)
                else:
                    self.unanswered.append({"type": "tool_result", "tool_use_id": extra["id"], "is_error": True,
                                            "content": "Not executed: only browser actions on the same page can go "
                                                       "in one turn. Call it again on its own."})
            problem = self._invalid(tool)
            if problem:
                self.repairs += 1
                self.unanswered.append({"type": "tool_result", "tool_use_id": tid, "is_error": True,
                                        "content": f"Invalid call, nothing was done: {problem}"})
                self.unanswered += [{"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                                     "content": "Not executed: an earlier call of this turn was invalid."}
                                    for c in batch]
                if self.repairs > MAX_REPAIRS:
                    self.status = "idle"
                    self.autopilot = False
                    self._say("system", f"Модель {MAX_REPAIRS + 1} раза подряд предложила некорректный шаг "
                                        f"({problem}). Подскажите ей в чате или добавьте шаг вручную.")
                    return
                continue
            self.repairs = 0
            if self.toolbox and self.toolbox.owns(name):
                self._say("system", f"🔧 {name.replace('__', ' → ', 1)}")
                text, is_error = await self.toolbox.call(name, inp)
                self.unanswered.append({"type": "tool_result", "tool_use_id": tid,
                                        "is_error": is_error, "content": text})
                continue
            if name in HELPERS:
                self.unanswered.append({"type": "tool_result", "tool_use_id": tid,
                                        "content": await self._helper(name, inp)})
                continue
            if name == "finish":
                if inp.get("status") == "passed" and not has_assertion(self.steps):
                    # A test without a passing assertion is green whatever the app does.
                    self._say("system", "Агент хотел завершить тест, но в нём ещё нет проверки ожидаемого "
                                        "результата: тест без проверки зелёный при любом поведении приложения. "
                                        "Агент добавит проверку.")
                    self.unanswered.append({
                        "type": "tool_result", "tool_use_id": tid, "is_error": True,
                        "content": "Not finished: the test has no passing assertion of the result yet. Add an "
                                   "assertion of the scenario's expected result, then call finish again.",
                    })
                    continue
                self.finish_status = inp.get("status", "")
                self.summary = f"[{inp.get('status')}] {inp.get('summary', '')}"
                self.finish = {"status": self.finish_status, "summary": inp.get("summary", ""),
                               "evidence": inp.get("evidence", ""),
                               "assertions": [i + 1 for i, st in enumerate(self.steps)
                                              if st["action"].startswith("assert") and st.get("status") == "passed"]}
                self._say("agent", self.summary + (f"\nПодтверждение: {inp['evidence']}" if inp.get("evidence") else ""))
                self.unanswered.append({"type": "tool_result", "tool_use_id": tid,
                                        "content": "Authoring finished."})
                self.status = "done"
                self.autopilot = False
                return
            step = tool_to_step(name, inp)
            self.pending = {"id": tid, "name": name, "input": inp, "step": step, "batch": batch}
            self.status = "awaiting_approval"
            return
        self.status = "idle"
        self._say("system", "Агент слишком много ходов подряд не предлагал действий в браузере "
                            "(инструменты подключений или завершение без проверки).")

    def _drop_batch(self, pending: dict, reason: str) -> None:
        """The actions batched after a step that will not run: each gets its "not executed" result."""
        self.unanswered += [{"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                             "content": f"Not executed: {reason}."} for c in pending.get("batch") or []]

    def _batching(self) -> bool:
        return (self.autopilot or self.starts_in_autopilot) and isinstance(self.bs, BrowserSession)

    def _is_action(self, call: dict) -> bool:
        """A browser action (a test step), not a helper, a connection tool or finish."""
        name = call["name"]
        return name not in HELPERS and name != "finish" and not (self.toolbox and self.toolbox.owns(name))

    async def _autopilot_loop(self) -> None:
        start = len(self.steps)
        n = 0
        while self.autopilot and n < self.max_steps:
            if self.status == "awaiting_approval" and self.pending:
                await self.approve()
                n = len(self.steps) - start      # a batch of actions counts every step
            elif self.status in ("done", "error", "idle"):
                break
            else:
                await asyncio.sleep(0.3)
        if self.autopilot and n >= self.max_steps and self.status != "done":
            self._say("system", f"Auto-Pilot остановлен: достигнут лимит шагов ({self.max_steps}, «Проект → Процесс "
                                "генерации»). Агент не завершил сценарий — продолжите с подтверждением шагов или "
                                "включите Auto-Pilot снова.")
        self.autopilot = False
