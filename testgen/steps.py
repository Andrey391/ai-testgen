"""Test steps: the unit the agent, the recorder and the replayer all share."""
from __future__ import annotations

import re
import uuid

from playwright.async_api import expect

from .browser import BrowserSession

# Actions that target an element (need a locator).
ELEMENT_ACTIONS = {"click", "fill", "select_option", "hover", "assert_visible"}
ALL_ACTIONS = ELEMENT_ACTIONS | {
    "navigate", "press_key", "scroll", "wait", "assert_text_present", "assert_url_contains",
}


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


async def perform(bs: BrowserSession, step: dict, loc=None) -> None:
    """Run one step. `loc` is a Playwright Locator for element actions."""
    page = bs.page
    a, v = step["action"], bs.expand(step.get("value", ""))
    if a in ELEMENT_ACTIONS and loc is None:
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
        await expect(loc).to_be_visible(timeout=7000)
    elif a == "assert_text_present":
        await expect(page.get_by_text(v).first).to_be_visible(timeout=7000)
    elif a == "assert_url_contains":
        await expect(page).to_have_url(re.compile(re.escape(v)), timeout=7000)
    else:
        raise ValueError(f"Unknown action {a}")
    await bs.settle()
