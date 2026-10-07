"""Test steps: the unit the agent, the recorder and the replayer all share.

Adding an action: put it into ELEMENT_ACTIONS / ALL_ACTIONS and `perform`, then
into the agent's tools (agent.py), the Playwright MCP engine (mcp_browser.py)
and the exporters.

Values of some actions are JSON:
    handle_dialog   {"action": "accept" | "dismiss", "prompt_text": "", "expect": "text in the message"}
                    arms the handler for the NEXT dialog (alert/confirm/prompt), so it goes before
                    the step that opens the dialog
    assert_download {"name": "report*.csv", "min_bytes": 1}   the file downloaded by the previous step
    api_request     {"method", "url", "headers", "body", "save": {"var": "$.path"}, "expect_status",
                     "expect": {"$.path": "value"}}
                    a test's "before" / "after" blocks (written by a person) and the steps of an API
                    (backend) test, which the agent writes with its api_request tool; only to the
                    application under test, DELETE only in "after" (see run_api)
    read_email      {"to", "subject", "pattern", "save": "var", "timeout"}  a code from a test mailbox
    use_module      {"module": "<test id>", "params": {"name": "value"}}   another test as one step
    mock_route      {"url", "method", "status", "content_type", "body"}
"""
from __future__ import annotations

import fnmatch
import json
import re
import uuid
from urllib.parse import urljoin, urlparse

from playwright.async_api import expect

from . import checks
from .browser import BrowserSession

# Actions that target an element (need a locator).
ELEMENT_ACTIONS = {"click", "double_click", "fill", "select_option", "hover", "upload_file", "drag_to",
                   "assert_visible", "assert_value", "assert_checked", "assert_enabled", "assert_count",
                   "assert_element_text"}
# The element is optional: without a locator the whole page is used.
OPTIONAL_ELEMENT = {"assert_screenshot"}
ALL_ACTIONS = ELEMENT_ACTIONS | OPTIONAL_ELEMENT | {
    "navigate", "press_key", "scroll", "wait", "switch_tab", "handle_dialog", "assert_text_present",
    "assert_url_contains", "assert_no_console_errors", "assert_accessible", "assert_download", "mock_route",
    "api_request", "read_email", "use_module",
}
ASSERTIONS = {a for a in ALL_ACTIONS if a.startswith("assert_")}
# Checks that look at the page as a whole rather than at the scenario's result.
AUXILIARY_ASSERTIONS = {"assert_no_console_errors", "assert_accessible", "assert_screenshot"}
# The only action of the before / after blocks of a test.
DATA_ACTIONS = {"api_request"}
TIMEOUT = 7000
MAX_MODULE_DEPTH = 3


def _flag(value: str) -> bool:
    """assert_checked / assert_enabled value: "true" / "false"."""
    return str(value).strip().lower() not in ("false", "0", "no", "off", "нет")


def new_step(action: str, description: str, value: str = "", locator: list | None = None,
             source: str = "ai", press_enter: bool = False) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "action": action,
        "description": description,
        "value": value,
        "press_enter": press_enter,
        "locator": locator or [],
        "source": source,
        "status": "pending",
        "error": "",
        "healed": False,
    }


def needs_element(step: dict) -> bool:
    return step["action"] in ELEMENT_ACTIONS or (step["action"] in OPTIONAL_ELEMENT and bool(step.get("locator")))


def spec(step: dict) -> dict:
    """The JSON value of an action (handle_dialog, assert_download, api_request...)."""
    v = step.get("value") or "{}"
    try:
        d = json.loads(v)
    except ValueError:
        raise ValueError(f"Шаг «{step.get('description', step['action'])}»: значение должно быть JSON")
    if not isinstance(d, dict):
        raise ValueError(f"Шаг «{step.get('description', step['action'])}»: значение должно быть JSON-объектом")
    return d


async def perform(bs: BrowserSession, step: dict, loc=None) -> dict | None:
    """Run one step. `loc` is a Playwright Locator for element actions.
    Returns details for the run report (accessibility, visual checks, requests) or None."""
    page = bs.page
    a = step["action"]
    raw = a in ("mock_route", "handle_dialog", "assert_download", "api_request", "read_email", "use_module")
    v = step.get("value", "") if raw else bs.expand(step.get("value", ""))
    details = None
    if a in ELEMENT_ACTIONS and a != "assert_count" and loc is None:
        raise ValueError("No element for this step")
    if a == "navigate":
        await page.goto(v, wait_until="domcontentloaded", timeout=45000)
    elif a == "click":
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.click(timeout=10000)
    elif a == "double_click":
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.dblclick(timeout=10000)
    elif a == "fill":
        await loc.fill(v, timeout=10000)
        if step.get("press_enter"):
            await loc.press("Enter")
    elif a == "select_option":
        try:
            await loc.select_option(label=v, timeout=5000)
        except Exception:
            await loc.select_option(value=v, timeout=5000)
    elif a == "hover":
        await loc.hover(timeout=10000)
    elif a == "upload_file":
        await loc.set_input_files(project_file(bs, v), timeout=10000)
    elif a == "drag_to":
        target, _ = await bs.find(step.get("target") or [], wait=5)
        if target is None:
            raise ValueError("Не найден элемент, на который нужно перетащить")
        await loc.drag_to(target, timeout=10000)
    elif a == "press_key":
        await page.keyboard.press(v)
    elif a == "scroll":
        await page.mouse.wheel(0, -700 if v == "up" else 700)
    elif a == "wait":
        await page.wait_for_timeout(min(float(v or 1), 15) * 1000)
    elif a == "switch_tab":
        await bs.switch_tab(v)
        await bs.page.bring_to_front()
    elif a == "handle_dialog":
        bs.dialog_plan = spec(step)
    elif a == "assert_visible":
        await expect(loc).to_be_visible(timeout=TIMEOUT)
    elif a == "assert_text_present":
        await expect(page.get_by_text(v).first).to_be_visible(timeout=TIMEOUT)
    elif a == "assert_url_contains":
        await expect(page).to_have_url(re.compile(re.escape(v)), timeout=TIMEOUT)
    elif a == "assert_value":
        await expect(loc).to_have_value(v, timeout=TIMEOUT)
    elif a == "assert_checked":
        await expect(loc).to_be_checked(checked=_flag(v), timeout=TIMEOUT)
    elif a == "assert_enabled":
        await expect(loc).to_be_enabled(enabled=_flag(v), timeout=TIMEOUT)
    elif a == "assert_count":
        await expect(await bs.find_all(step["locator"])).to_have_count(int(v or 0), timeout=TIMEOUT)
    elif a == "assert_element_text":
        await expect(loc).to_contain_text(v, timeout=TIMEOUT)
    elif a == "assert_no_console_errors":
        errors = bs.console_errors()
        if errors:
            raise AssertionError(f"Ошибки в консоли браузера: {len(errors)} — "
                                 + " | ".join(e["text"][:150] for e in errors[:3]))
    elif a == "assert_accessible":
        details = await checks.accessibility(bs, step)
    elif a == "assert_screenshot":
        await bs.settle()
        details = await checks.screenshot(bs, step, loc)
    elif a == "assert_download":
        details = await assert_download(bs, spec(step))
    elif a == "mock_route":
        await bs.mock(json.loads(v))
    elif a == "api_request":
        details = await run_api(bs, step, phase=step.get("phase", "before"))
    elif a == "read_email":
        from . import mailbox
        details = await mailbox.read_code(bs, spec(step))
    elif a == "use_module":
        raise ValueError("Шаг-модуль выполняет раннер (runner.run_steps)")
    else:
        raise ValueError(f"Unknown action {a}")
    await bs.settle()
    if bs.dialog_error:
        error, bs.dialog_error = bs.dialog_error, ""
        raise AssertionError(error)
    return details


def project_file(bs: BrowserSession, name: str) -> str:
    """A file of the project for upload_file: data/projects/<id>/files/<name>."""
    from . import fs, projects
    pid = bs.options.get("project_id") or ""
    name = (name or "").strip()
    if not pid or not re.fullmatch(r"[^/\\:*?\"<>|]{1,200}", name) or name in (".", ".."):
        raise ValueError(f"Файл «{name}» не найден: загрузите его в «Проект → Файлы для тестов»")
    f = fs.local_path(projects.path(pid) / "files" / name)     # the browser needs it on the local disk
    if not f.is_file():
        raise ValueError(f"Файл «{name}» не найден в файлах проекта")
    return str(f)


async def assert_download(bs: BrowserSession, s: dict) -> dict:
    new = await bs.new_downloads()
    if not new:
        raise AssertionError("Файл не скачивался")
    d = new[-1]
    name = s.get("name") or "*"
    if not fnmatch.fnmatch(d["name"].lower(), name.lower()):
        raise AssertionError(f"Скачан файл «{d['name']}», ожидался «{name}»")
    low, high = int(s.get("min_bytes") or 0), int(s.get("max_bytes") or 0)
    if d["size"] < low or (high and d["size"] > high):
        raise AssertionError(f"Размер файла «{d['name']}» {d['size']} байт вне допуска "
                             f"{low}–{high or '∞'}")
    return {"download": {"name": d["name"], "size": d["size"]}}


# ---------- before / after: data preparation through the application's API ----------

def json_path(data, path: str):
    """A value by a simple JSON path: $.a.b[0].c"""
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", re.sub(r"^\$\.?", "", path or "")):
        if part.startswith("["):
            cur = cur[int(part[1:-1])]
        else:
            cur = cur[part]
    return cur


def check_api(step: dict, base_url: str, phase: str, own_vars: set[str] | None = None) -> None:
    """The rules of before/after requests: only to the application under test; DELETE only in
    "after" and only for an object this run created (its URL holds a {{vars.x}} set in "before")."""
    s = spec(step)
    method = (s.get("method") or "GET").upper()
    url = s.get("url") or ""
    target = urlparse(urljoin(base_url.rstrip("/") + "/", url))
    base = urlparse(base_url)
    if not base.netloc or (target.scheme, target.netloc) != (base.scheme, base.netloc):
        raise ValueError(f"Запрос {method} {url}: разрешены только запросы к тестируемому приложению ({base_url})")
    if method == "DELETE":
        if phase != "after":
            raise ValueError("DELETE разрешён только в блоке «после теста» (after)")
        used = set(re.findall(r"\{\{vars\.([A-Za-z_]\w*)\}\}", url))
        if not used or (own_vars is not None and not used <= own_vars):
            raise ValueError("DELETE разрешён только для объекта, созданного в этом прогоне: адрес должен "
                             "содержать {{vars.…}}, сохранённую запросом из блока «до теста»")


async def run_api(bs: BrowserSession, step: dict, phase: str = "before") -> dict:
    """api_request through the browser context (it shares the cookies of the logged-in page).
    The response body is never kept: only the status and the names of the saved variables."""
    base_url = bs.options.get("base_url") or bs.url
    check_api(step, base_url, phase, set(bs.options.get("own_vars") or []))
    s = spec(step)
    method = (s.get("method") or "GET").upper()
    url = urljoin(base_url.rstrip("/") + "/", bs.expand(s.get("url") or ""))
    headers = {k: bs.expand(str(v)) for k, v in (s.get("headers") or {}).items()}
    body = s.get("body")
    kw: dict = {"method": method, "headers": headers, "timeout": 30000}
    if body not in (None, ""):
        if isinstance(body, (dict, list)):
            kw["data"] = json.loads(bs.expand(json.dumps(body, ensure_ascii=False)))
        else:
            kw["data"] = bs.expand(str(body))
    resp = await bs.context.request.fetch(url, **kw)
    want = s.get("expect_status")
    ok = resp.status == int(want) if want else 200 <= resp.status < 300
    safe_url = bs.mask(url)
    if not ok:
        raise AssertionError(f"{method} {safe_url} ответил {resp.status}" + (f", ожидался {want}" if want else ""))
    try:
        text = await resp.text()
    except Exception:
        text = ""
    # What the authoring agent sees of the response (secrets masked): to write checks of its fields.
    bs.last_response = {"status": resp.status, "body": bs.mask(text[:3000])}
    saved = []
    data = None
    if s.get("save") or s.get("expect"):
        try:
            data = json.loads(text)
        except ValueError:
            raise AssertionError(f"{method} {safe_url}: ответ не JSON, проверить поля и сохранить переменные нельзя")
    for path, want in (s.get("expect") or {}).items():
        try:
            got = json_path(data, path)
        except (KeyError, IndexError, TypeError):
            raise AssertionError(f"{method} {safe_url}: в ответе нет {path}")
        got_text = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
        if got_text != bs.expand(str(want)):
            raise AssertionError(f"{method} {safe_url}: {path} = {bs.mask(got_text)[:200]}, ожидалось "
                                 f"{bs.mask(bs.expand(str(want)))[:200]}")
    if s.get("save"):
        for name, path in s["save"].items():
            try:
                bs.vars[name] = json_path(data, path)
            except (KeyError, IndexError, TypeError):
                raise AssertionError(f"{method} {safe_url}: в ответе нет {path}")
            saved.append(name)
            if phase == "before":
                bs.options.setdefault("own_vars", []).append(name)
    return {"api": {"method": method, "url": safe_url, "status": resp.status, "saved": saved}}
