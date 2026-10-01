"""Helpers for the studio's tests: run coroutines, record steps like the agent does."""
from __future__ import annotations

import asyncio

from testgen.browser import BrowserSession
from testgen.steps import new_step


def arun(coro):
    return asyncio.run(coro)


class Recorder:
    """Drives a BrowserSession by element names and records steps with real locators."""

    def __init__(self, bs: BrowserSession):
        self.bs, self.steps = bs, []

    async def ref(self, name: str) -> str:
        ref = None
        for _ in range(20):      # the page may still be loading, as the agent would see it
            await self.bs.snapshot()
            ref = next((r for r, e in self.bs.elements.items()
                        if name in (e["name"], e.get("placeholder"), e.get("label"))), None)
            if ref:
                break
            await asyncio.sleep(0.25)
        assert ref, f"no element {name!r}: {[e['name'] for e in self.bs.elements.values()]}"
        return ref

    async def do(self, action: str, name: str = "", value: str = "", description: str = "", target: str = "",
                 **extra) -> dict:
        step = new_step(action, description or f"{action} {name}".strip(), value, **extra)
        if name:
            step["ref"] = await self.ref(name)
        if target:
            step["target_ref"] = next(r for r, e in self.bs.elements.items() if target in (e["name"], e.get("testid")))
        await self.bs.execute(step)
        step["status"] = "passed"
        self.steps.append(step)
        return step


async def record(url: str, script, credentials: dict | None = None, traffic: list | None = None,
                 options: dict | None = None) -> list[dict]:
    """Run `await script(recorder)` in a fresh browser at `url` -> recorded steps.
    With `traffic` (a list) the XHR/fetch exchanges are recorded into it; `options`: the
    session's options (project id for upload_file, base_url for api_request)."""
    bs = await BrowserSession.launch(headless=True, record_traffic=traffic is not None)
    bs.credentials = credentials or {}
    bs.options = options or {}
    try:
        rec = Recorder(bs)
        await rec.do("navigate", value=url, description=f"Open {url}")
        await script(rec)
        await bs.settle()
        if traffic is not None:
            traffic += bs.traffic
        return rec.steps
    finally:
        await bs.close()

