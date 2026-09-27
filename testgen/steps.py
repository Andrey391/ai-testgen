"""Test steps: the unit the agent, the recorder and the replayer all share.

Adding an action: put it into ELEMENT_ACTIONS / ALL_ACTIONS and `perform`, then
into the agent's tools (agent.py), the Playwright MCP engine (mcp_browser.py)
and the exporters.
"""
from __future__ import annotations

import json
import re
import uuid

from playwright.async_api import expect

from . import checks
from .browser import BrowserSession

# Actions that target an element (need a locator).
ELEMENT_ACTIONS = {"click", "fill", "select_option", "hover", "assert_visible", "assert_value", "assert_checked",
                   "assert_enabled", "assert_count", "assert_element_text"}
# The element is optional: without a locator the whole page is used.
OPTIONAL_ELEMENT = {"assert_screenshot"}
ALL_ACTIONS = ELEMENT_ACTIONS | OPTIONAL_ELEMENT | {
    "navigate", "press_key", "scroll", "wait", "assert_text_present", "assert_url_contains",
    "assert_no_console_errors", "assert_accessible", "mock_route",
}
ASSERTIONS = {a for a in ALL_ACTIONS if a.startswith("assert_")}
# Checks that look at the page as a whole rather than at the scenario's result.
AUXILIARY_ASSERTIONS = {"assert_no_console_errors", "assert_accessible", "assert_screenshot"}
TIMEOUT = 7000


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


async def perform(bs: BrowserSession, step: dict, loc=None) -> dict | None:
    """Run one step. `loc` is a Playwright Locator for element actions.
    Returns details for the run report (accessibility, visual checks) or None."""
    page = bs.page
    a = step["action"]
    v = step.get("value", "") if a == "mock_route" else bs.expand(step.get("value", ""))
    details = None
    if a in ELEMENT_ACTIONS and a != "assert_count" and loc is None:
        raise ValueError("No element for this step")
    if a == "navigate":
        await page.goto(v, wait_until="domcontentloaded", timeout=45000)
    elif a == "click":
        await loc.scroll_into_view_if_needed(timeout=5000)
        await loc.click(timeout=10000)
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
    elif a == "press_key":
        await page.keyboard.press(v)
    elif a == "scroll":
        await page.mouse.wheel(0, -700 if v == "up" else 700)
    elif a == "wait":
        await page.wait_for_timeout(min(float(v or 1), 15) * 1000)
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
    elif a == "mock_route":
        await bs.mock(json.loads(v))
    else:
        raise ValueError(f"Unknown action {a}")
    await bs.settle()
    return details
