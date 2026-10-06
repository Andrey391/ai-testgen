"""Playwright MCP (@playwright/mcp) as the authoring browser engine.

Same interface as browser.BrowserSession (describe / screenshot_b64 / execute /
url / close), so the agent, its tools and the recorded steps do not change:
The LLM still calls click(ref), fill(ref, text)..., only the page snapshot is
Playwright's ARIA snapshot and actions go through the MCP server.

Recorded steps need stable locators for replay and export. Before acting on an
element we probe it with browser_evaluate: the probe returns the same fields as
the built-in snapshot (test id, id, role, name, label...), and the code
Playwright MCP generates for the probe carries Playwright's own unique locator.
Saved tests are replayed by runner.py with the built-in engine as usual.

The password never reaches the LLM: {{password}} is expanded right before the
tool call and masked in everything the server returns.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import ELEMENT_INFO_JS, VIEWPORT, expand, group_candidates, locator_candidates
from .mcp_hub import McpClient, McpError, error_text, result_text
from .testdata import DataValues, secret_values

MAX_SNAPSHOT_CHARS = 40_000
_STATE_JS = {
    "assert_value": "(el) => el.value ?? ''",
    "assert_checked": "(el) => !!el.checked || el.getAttribute('aria-checked') === 'true'",
    "assert_enabled": "(el) => !el.disabled && el.getAttribute('aria-disabled') !== 'true'",
    "assert_element_text": "(el) => el.innerText || el.textContent || ''",
    "assert_count": "",
}
_OWN_BROWSER_FLAGS = ("--browser", "--executable-path", "--cdp-endpoint", "--extension", "--endpoint")
_chromium: str | None = None


async def bundled_chromium(headless: bool) -> str | None:
    """Chromium installed by `playwright install chromium` (what the built-in engine uses):
    the headless shell for headless runs, like Playwright itself does."""
    global _chromium
    if _chromium is None:
        pw = await async_playwright().start()
        try:
            _chromium = pw.chromium.executable_path
        finally:
            await pw.stop()
    exe = Path(_chromium)
    if headless:
        root = next((p.parent for p in exe.parents if p.name.startswith("chromium-")), None)
        if root:
            shells = sorted(f for f in root.glob("chromium_headless_shell-*/*/chrome-headless-shell*")
                            if f.suffix in ("", ".exe"))
            if shells:
                return str(shells[-1])
    return str(exe) if exe.exists() else None


_CODEGEN = re.compile(
    r"page\.(locator|get_by_role|get_by_text|get_by_label|get_by_placeholder|get_by_test_id)"
    r"\((.*?)\)\.(?:evaluate|click|fill|hover|select_option|press|type)\(", re.S)


def codegen_locator(code: str) -> dict | None:
    """Locator from Playwright MCP's generated Python code, if it is a single simple call."""
    m = _CODEGEN.search(code or "")
    if not m:
        return None
    try:
        call = ast.parse(f"f({m.group(2)})", mode="eval").body
    except SyntaxError:
        return None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.args
            and all(isinstance(a, ast.Constant) for a in call.args)):
        return None
    args = [a.value for a in call.args]
    kw = {k.arg: k.value.value for k in call.keywords if isinstance(k.value, ast.Constant)}
    kind = m.group(1)
    if kind == "locator":
        return {"kind": "css", "value": args[0]}
    if kind == "get_by_role":
        return {"kind": "role", "role": args[0], "name": kw["name"]} if kw.get("name") else None
    if kind == "get_by_test_id":
        return {"kind": "testid", "value": args[0]}
    return {"kind": kind.removeprefix("get_by_"), "value": args[0]}


def _section(text: str, title: str) -> str:
    m = re.search(rf"^### {re.escape(title)}\s*\n(.*?)(?=^### |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


class McpBrowser:
    engine = "playwright-mcp"

    def __init__(self, client: McpClient, workdir: str):
        self.client, self.workdir = client, workdir
        self.credentials: dict[str, str] = {}
        self._url = ""
        self._shot = ""
        self.testdata = DataValues()     # {{unique}}, {{faker.email}}...: one value per session
        self._errors_seen = 0            # console errors already reported by assert_no_console_errors
        click = next((t for t in client.tools if t.name == "browser_click"), None)
        props = (click.inputSchema or {}).get("properties", {}) if click else {}
        # Older @playwright/mcp versions called the element parameter "ref".
        self.ref_key = "target" if "target" in props else "ref"

    @classmethod
    async def launch(cls, project_id: str, conn: dict, headless: bool = True) -> "McpBrowser":
        given = list(conn.get("args") or [])
        extra = ["--codegen", "python"]
        if headless and "--headless" not in given:
            extra.append("--headless")
        if not any(a in given for a in ("--isolated", "--user-data-dir")):
            extra.append("--isolated")
        # Like the built-in engine (Playwright's default): with the Chromium sandbox on, the browser
        # crashes on some Windows machines ("Target crashed"). "--sandbox" in the args turns it back on.
        if "--sandbox" not in given and "--no-sandbox" not in given:
            extra.append("--no-sandbox")
        if not any(a.startswith("--viewport-size") for a in given):
            extra += ["--viewport-size", f"{VIEWPORT['width']}x{VIEWPORT['height']}"]
        # Without its own browser flag the server would look for an installed Google Chrome.
        if conn.get("transport") != "http" and not any(a.startswith(_OWN_BROWSER_FLAGS) for a in given):
            exe = await bundled_chromium(headless)
            if exe:
                extra += ["--executable-path", exe]
        workdir = tempfile.mkdtemp(prefix="tg-pwmcp-")
        extra += ["--output-dir", workdir]
        try:
            client = await McpClient(project_id, conn, extra_args=extra, cwd=workdir).start()
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        if not any(t.name == "browser_navigate" for t in client.tools):
            await client.close()
            shutil.rmtree(workdir, ignore_errors=True)
            raise McpError(f"«{conn['name']}» не похож на Playwright MCP: нет инструмента browser_navigate")
        return cls(client, workdir)

    # ---------- engine interface ----------

    @property
    def url(self) -> str:
        return self._url

    def expand(self, value: str) -> str:
        return expand(self.credentials, value, self.testdata)

    async def describe(self) -> str:
        text = result_text(await self._call("browser_snapshot", {}))
        if len(text) > MAX_SNAPSHOT_CHARS:
            text = text[:MAX_SNAPSHOT_CHARS] + "\n…(snapshot truncated)"
        return text + "\n\nTarget elements by the ref values from this snapshot (e.g. e12)."

    async def screenshot_b64(self) -> str:
        try:
            res = await self.client.call("browser_take_screenshot", {"type": "jpeg"})
            img = next((c for c in res.content if getattr(c, "type", "") == "image"), None)
            if img:
                self._shot = img.data
        except Exception:
            pass
        return self._shot

    async def execute(self, step: dict) -> None:
        from .steps import ELEMENT_ACTIONS
        a, v = step["action"], step.get("value", "")
        ref = step.pop("ref", "")
        target = {}
        if a in ELEMENT_ACTIONS:
            if not ref:
                raise ValueError("This action needs an element ref")
            info = await self._probe(ref, step)
            target = {self.ref_key: ref, "element": step["description"][:200]}
            if a == "assert_visible" and not info.get("visible", True):
                raise AssertionError("Element is not visible")
            if a in _STATE_JS:
                await self._assert_state(a, step, target, info)
                return
        if a == "navigate":
            await self._call("browser_navigate", {"url": v})
        elif a == "click":
            await self._call("browser_click", target)
        elif a == "double_click":
            await self._call("browser_click", target | {"doubleClick": True})
        elif a == "drag_to":
            end = step.pop("target_ref", "")
            if not end:
                raise ValueError("drag_to needs the ref of the target element")
            probe = {"description": step["description"]}
            await self._probe(end, probe)
            step["target"] = probe.get("locator", [])
            await self._call("browser_drag", self._drag_args(ref, end, step["description"][:200]))
        elif a == "switch_tab":
            await self._switch_tab(v)
        elif a == "fill":
            await self._call("browser_type", target | {"text": self.expand(v),
                                                       "submit": bool(step.get("press_enter"))})
        elif a == "select_option":
            await self._call("browser_select_option", target | {"values": [self.expand(v)]})
        elif a == "hover":
            await self._call("browser_hover", target)
        elif a == "press_key":
            await self._call("browser_press_key", {"key": v})
        elif a == "scroll":
            await self._evaluate(f"() => window.scrollBy(0, {-700 if v == 'up' else 700})")
        elif a == "wait":
            await self._call("browser_wait_for", {"time": min(float(v or 1), 15)})
        elif a == "assert_text_present":
            await self._call("browser_wait_for", {"text": self.expand(v)})
        elif a == "assert_url_contains":
            url = str(await self._evaluate("() => location.href"))
            if self.expand(v) not in url:
                raise AssertionError(f"URL '{url}' does not contain '{v}'")
        elif a == "assert_no_console_errors":
            text = result_text(await self._call("browser_console_messages", {}))
            errors = [line for line in text.splitlines() if re.match(r"\s*-?\s*\[error\]", line, re.I)
                      and "Failed to load resource" not in line]
            new, self._errors_seen = errors[self._errors_seen:], len(errors)
            if new:
                raise AssertionError(f"Ошибки в консоли браузера: {len(new)} — " + " | ".join(new[:3]))
        elif a in ("assert_accessible", "assert_screenshot", "mock_route", "upload_file", "handle_dialog",
                   "assert_download", "api_request", "read_email", "use_module"):
            raise ValueError("Этот шаг доступен только со встроенным движком Playwright")
        elif a != "assert_visible":
            raise ValueError(f"Unknown action {a}")

    def _schema(self, tool: str) -> dict:
        t = next((t for t in self.client.tools if t.name == tool), None)
        return ((t.inputSchema or {}).get("properties") or {}) if t else {}

    def _drag_args(self, start: str, end: str, element: str) -> dict:
        """browser_drag arguments: their names differ between @playwright/mcp versions."""
        props = self._schema("browser_drag")
        key = "Target" if "startTarget" in props else "Ref"
        return {"startElement": element, f"start{key}": start, "endElement": element, f"end{key}": end}

    async def _switch_tab(self, which: str) -> None:
        which = (which or "last").strip()
        listing = result_text(await self._call("browser_tabs", {"action": "list"})) \
            if self._schema("browser_tabs") else result_text(await self._call("browser_tab_list", {}))
        tabs = re.findall(r"^- (\d+):.*?\((\S*)\)\s*$", listing, re.M) or \
            [(str(i), "") for i, _ in enumerate(re.findall(r"^- ", listing, re.M))]
        if not tabs:
            raise ValueError("Нет открытых вкладок")
        if which == "last":
            index = int(tabs[-1][0])
        elif which == "first":
            index = int(tabs[0][0])
        elif which.isdigit():
            index = int(which) - 1
        else:
            index = next((int(i) for i, url in tabs if which in url), -1)
            if index < 0:
                raise ValueError(f"Нет вкладки с адресом, содержащим «{which}»")
        if self._schema("browser_tabs"):
            await self._call("browser_tabs", {"action": "select", "index": index})
        else:
            await self._call("browser_tab_select", {"index": index})

    async def _assert_state(self, a: str, step: dict, target: dict, info: dict) -> None:
        """Value / checked / enabled / text / count assertions, read with browser_evaluate.
        Without an expected value (recorded by hand) the current one is taken."""
        if a == "assert_count":
            step["locator"] = [c for c in group_candidates(info) if c["kind"] in ("css", "testid")]
            if not step["locator"]:
                raise ValueError("This element is not part of a list or group of similar elements")
            c = step["locator"][0]
            css = c["value"] if c["kind"] == "css" else f'[data-testid="{c["value"]}"]'
            actual = await self._evaluate(f"() => document.querySelectorAll({json.dumps(css)}).length")
        else:
            actual = await self._evaluate(_STATE_JS[a], target)
        actual = str(actual).lower() if isinstance(actual, bool) else str(actual)
        if not step.get("value"):
            step["value"] = actual[:100]
        want = self.expand(step["value"])
        ok = (" ".join(want.split()) in " ".join(actual.split())) if a == "assert_element_text" else actual == want
        if not ok:
            raise AssertionError(f"Expected {want!r}, got {actual[:200]!r}")

    async def close(self) -> None:
        try:
            await self.client.call("browser_close", {}, timeout=15)
        except Exception:
            pass
        await self.client.close()
        shutil.rmtree(self.workdir, ignore_errors=True)

    # ---------- internals ----------

    def _mask(self, text: str) -> str:
        for secret in secret_values(self.credentials):
            text = text.replace(secret, "***")
        return text

    async def _call(self, tool: str, args: dict):
        res = await self.client.call(tool, args)
        for c in res.content:
            if getattr(c, "type", "") == "text":
                c.text = self._mask(c.text)
        text = result_text(res)
        m = re.search(r"^- Page URL: (\S+)", text, re.M)
        if m:
            self._url = m.group(1)
        if res.isError:
            raise RuntimeError(error_text(res))
        return res

    async def _evaluate(self, function: str, extra: dict | None = None):
        res = await self._call("browser_evaluate", {"function": function} | (extra or {}))
        raw = _section(result_text(res), "Result")
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')

    async def _probe(self, ref: str, step: dict) -> dict:
        """Element info + stable locators for the step (also checks that the ref is valid)."""
        res = await self._call("browser_evaluate", {self.ref_key: ref, "element": step["description"][:200],
                                                    "function": ELEMENT_INFO_JS})
        text = result_text(res)
        try:
            info = json.loads(_section(text, "Result"))
        except ValueError:
            info = {}
        cands = [c for c in [codegen_locator(_section(text, "Ran Playwright code"))] if c]
        if isinstance(info, dict) and info.get("tag"):
            cands += locator_candidates(info)
        step["locator"] = [c for i, c in enumerate(cands) if c not in cands[:i]]
        return info if isinstance(info, dict) else {}
