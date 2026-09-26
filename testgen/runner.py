"""Replay a saved test in a fresh browser, with self-healing locators.

Healing has two layers:
1. Each step stores several locator candidates; if the first no longer matches,
   the next one is tried.
2. If none match, Claude gets the step description plus the current page's
   element list and picks the element that now plays that role. The step's
   locator is rewritten, so the saved test is repaired for next time.

After a failed run Claude can classify the failure (product bug, test issue,
environment) from the failed step, the error and the screenshot.

The project's "run" stage settings switch healing and analysis on or off and
choose skills, model and effort.
"""
from __future__ import annotations

import copy
import time
from typing import Literal

from pydantic import BaseModel

from . import llm, skills
from .browser import BrowserSession, describe_element
from .steps import ELEMENT_ACTIONS, perform

HEAL_SYSTEM = ("You repair broken UI test locators. The page changed and the recorded locator "
               "no longer matches. Pick the element on the current page that the test step "
               "refers to. Return an empty ref if no element fits - never guess wildly.")

ANALYSIS_SYSTEM = ("You are a senior QA engineer triaging a failed automated UI test. From the test, "
                   "the step that failed, the error and the screenshot taken right after the failure, "
                   "decide why it failed. Be concrete and brief. Write in the language of the test "
                   "descriptions.")


class HealChoice(BaseModel):
    ref: str          # element ref, or "" if nothing on the page fits
    reason: str


class FailureAnalysis(BaseModel):
    verdict: Literal["product_bug", "test_issue", "environment", "unknown"]
    summary: str       # what happened, 1-3 sentences
    suggestion: str    # bug report text, or how to fix the test / environment


async def heal(bs: BrowserSession, step: dict, cfg: dict | None = None, project_id: str = "") -> str | None:
    cfg = cfg or {}
    snap = await bs.snapshot()
    elements = "\n".join(describe_element(e) for e in snap["elements"])
    old = ", ".join(f"{c['kind']}={c.get('name') or c.get('value')}" for c in step["locator"])
    system = HEAL_SYSTEM + (skills.prompt(project_id, cfg.get("skills", [])) if project_id else "")
    resp = await llm.client().beta.messages.parse(
        **llm.common_params(cfg),
        max_tokens=4000,
        betas=[llm.FALLBACK_BETA],
        system=system,
        messages=[{"role": "user", "content": (
            f"Test step: {step['description']}\nAction: {step['action']}\n"
            f"Old locator candidates: {old or '(none)'}\n\nCurrent page {snap['url']}\n"
            f"Interactive elements:\n{elements}")}],
        output_format=HealChoice,
    )
    choice = resp.parsed_output
    if resp.stop_reason == "refusal" or not choice or choice.ref not in bs.elements:
        return None
    return choice.ref


async def analyze(test: dict, report: dict, cfg: dict | None = None, project_id: str = "") -> dict | None:
    """Why did the run fail? -> FailureAnalysis as a dict, or None if the model declined."""
    cfg = cfg or {}
    failed = next((r for r in report["results"] if r["status"] == "failed"), None)
    if not failed:
        return None
    lines = []
    for i, s in enumerate(test["steps"]):
        r = next((x for x in report["results"] if x["id"] == s["id"]), None)
        mark = "not run" if not r else r["status"] + (" (self-healed)" if r.get("healed") else "")
        lines.append(f"{i + 1}. [{s['action']}] {s['description']}"
                     + (f" | value: {s['value']}" if s.get("value") else "") + f" -> {mark}")
    content: list[dict] = [{"type": "text", "text": (
        f"Test: {test['name']}\nScenario: {test.get('scenario', '')}\nURL: {test.get('url', '')}\n\n"
        "Steps:\n" + "\n".join(lines) +
        f"\n\nFailed step: {failed['description']}\nError: {failed['error']}\n"
        "Screenshot after the failure:")}]
    if failed.get("screenshot"):
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": failed["screenshot"]}})
    system = ANALYSIS_SYSTEM + (skills.prompt(project_id, cfg.get("skills", [])) if project_id else "")
    resp = await llm.client().beta.messages.parse(
        **llm.common_params(cfg),
        max_tokens=4000,
        betas=[llm.FALLBACK_BETA],
        system=system,
        messages=[{"role": "user", "content": content}],
        output_format=FailureAnalysis,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        return None
    return resp.parsed_output.model_dump()


async def run_test(test: dict, headless: bool = True, on_progress=None,
                   credentials: dict | None = None, cfg: dict | None = None) -> dict:
    """Run all steps. Returns a run report; healed locators are written into `test`.

    `credentials` fill the {{username}} / {{password}} placeholders in step values.
    `cfg` is the project's "run" stage (self_heal, analyze_failures, skills, model).
    """
    cfg = cfg or {"self_heal": True, "analyze_failures": False}
    project_id = test.get("project_id", "")
    steps = copy.deepcopy(test["steps"])
    report = {"started": time.time(), "results": [], "healed": 0, "passed": True, "analysis": None}
    bs = await BrowserSession.launch(headless=headless)
    bs.credentials = credentials or {}
    try:
        for i, step in enumerate(steps):
            res = {"id": step["id"], "description": step["description"], "status": "passed",
                   "error": "", "healed": False}
            try:
                loc = None
                if step["action"] in ELEMENT_ACTIONS:
                    await bs.settle()
                    loc, _ = await bs.find(step["locator"])
                    if loc is None or not await _usable(loc):
                        if not cfg.get("self_heal", True):
                            raise RuntimeError("Element not found (self-healing is off)")
                        ref = await heal(bs, step, cfg, project_id)
                        if not ref:
                            raise RuntimeError("Element not found and self-healing found no match")
                        step["locator"] = await bs.unique_candidates(bs.elements[ref])
                        test["steps"][i]["locator"] = step["locator"]
                        test["steps"][i]["healed"] = True
                        loc = bs.by_ref(ref)
                        res["healed"] = True
                        report["healed"] += 1
                await perform(bs, step, loc)
            except Exception as e:
                res["status"] = "failed"
                res["error"] = str(e).splitlines()[0][:300]
                report["passed"] = False
            res["screenshot"] = await bs.screenshot_b64()
            report["results"].append(res)
            if on_progress:
                await on_progress(report)
            if res["status"] == "failed":
                break
    finally:
        await bs.close()
    if not report["passed"] and cfg.get("analyze_failures"):
        try:
            report["analysis"] = await analyze(test, report, cfg, project_id)
        except Exception as e:
            report["analysis"] = {"verdict": "unknown", "summary": llm.api_error_text(e), "suggestion": ""}
    report["finished"] = time.time()
    return report


async def _usable(loc) -> bool:
    try:
        await loc.wait_for(state="attached", timeout=5000)
        return True
    except Exception:
        return False
