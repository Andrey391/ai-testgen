"""Replay a saved test in a fresh browser, with self-healing locators.

Healing has two layers:
1. Each step stores several locator candidates; if the first no longer matches,
   the next one is tried.
2. If none match, the LLM gets the step description plus the current page's
   element list and picks the element that now plays that role. In "review" mode
   (run.heal_mode, the default) the run goes on with that element, but the saved
   test only gets a proposal - old and new locator, the LLM's reason, a screenshot
   with the element outlined - that a person accepts or rejects; a locator that
   was rejected once is never used again. In "auto" mode the step's locator is
   rewritten right away.

Every attempt records what the page reported (console errors, page errors,
failed and 4xx/5xx requests) and, per run.trace, a Playwright trace (open it in
Trace Viewer: `playwright show-trace trace.zip`) with the app password masked.

After a failed run the LLM can classify the failure (product bug, test issue,
environment, flaky) from the failed step, the error, the screenshot, the page
events and the test's recent history. A failed visual check gets its own verdict
(bug / expected change / noise) from the baseline, the actual image and the diff.

The project's "run" stage settings switch healing and analysis on or off and
choose skills, model and effort. Orchestration (retry, history, quarantine) is
in pipeline.run_and_record.
"""
from __future__ import annotations

import base64
import copy
import json
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel

from . import checks, fs, knowledge, llm, skills, testdata
from .browser import BrowserSession, describe_element, group_candidates, listed
from .steps import MAX_MODULE_DEPTH, needs_element, perform, spec

HEAL_SYSTEM = ("You repair broken UI test locators. The page changed and the recorded locator "
               "no longer matches. Pick the element on the current page that the test step "
               "refers to. Return an empty ref if no element fits - never guess wildly.")

ANALYSIS_SYSTEM = ("You are a senior QA engineer triaging a failed automated UI test. From the test, "
                   "the step that failed, the error, the screenshot taken right after the failure, what "
                   "the browser reported (console errors, failed requests) and the test's recent history, "
                   "decide why it failed. Be concrete and brief. Write in the language of the test "
                   "descriptions.")

VISUAL_SYSTEM = ("You review a failed visual regression check of a web UI test. You get the baseline "
                 "image, the current image and a diff where changed pixels are red. Decide whether the "
                 "difference is a bug (broken layout, missing or overlapping content, wrong styles), an "
                 "expected change (a deliberate redesign or new content: the baseline should be updated), "
                 "or noise (anti-aliasing, fonts, animation, dynamic data). Write in the language of the "
                 "test descriptions.")

OUTLINE_JS = """el => { el.dataset.tgOutline = el.style.outline || '';
  el.style.outline = '3px solid #e8590c'; el.style.outlineOffset = '2px'; }"""
UNOUTLINE_JS = "el => { el.style.outline = el.dataset.tgOutline || ''; delete el.dataset.tgOutline; }"
FIND_WAIT = 5    # seconds a step waits for its element before self-healing


class HealChoice(BaseModel):
    ref: str          # element ref, or "" if nothing on the page fits
    reason: str


class FailureAnalysis(BaseModel):
    verdict: Literal["product_bug", "test_issue", "environment", "flaky", "unknown"]
    summary: str       # what happened, 1-3 sentences
    suggestion: str    # bug report text, or how to fix the test / environment


class VisualVerdict(BaseModel):
    verdict: Literal["bug", "expected_change", "noise"]
    summary: str


async def heal(bs: BrowserSession, step: dict, cfg: dict | None = None,
               project_id: str = "") -> tuple[str, str] | None:
    """-> (ref of the element that now plays the step's role, the LLM's reason) or None."""
    cfg = cfg or {}
    snap = await bs.snapshot()
    elements = "\n".join(describe_element(e) for e in listed(snap["elements"], 250, 60))
    old = ", ".join(f"{c['kind']}={c.get('name') or c.get('value')}" for c in step["locator"])
    system = HEAL_SYSTEM + (skills.prompt(project_id, cfg.get("skills", [])) if project_id else "")
    reply = await llm.parse(cfg, system=system, schema=HealChoice, max_tokens=4000, project_id=project_id,
                            stage_name="run", messages=[{"role": "user", "content": (
                                f"Test step: {step['description']}\nAction: {step['action']}\n"
                                f"Old locator candidates: {old or '(none)'}\n\nCurrent page {snap['url']}\n"
                                f"Elements:\n{elements}")}])
    choice = reply.parsed
    if reply.stop == "refusal" or not choice or choice.ref not in bs.elements:
        return None
    return choice.ref, choice.reason


def _events_text(events: list[dict], limit: int = 20) -> str:
    return "\n".join(f"- [{e['type']}] step {e['step'] + 1}: {e['text'][:300]}" for e in events[-limit:])


async def analyze(test: dict, report: dict, cfg: dict | None = None, project_id: str = "",
                  history: list[dict] | None = None, retry: dict | None = None) -> dict | None:
    """Why did the run fail? -> FailureAnalysis as a dict, or None if the model declined.

    `history`: summaries of the test's previous runs (runs.history); `retry`: the report
    of the immediate re-run, if there was one."""
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
    text = (f"Test: {test['name']}\nScenario: {test.get('scenario', '')}\nURL: {test.get('url', '')}\n\n"
            "Steps:\n" + "\n".join(lines) +
            f"\n\nFailed step: {failed['description']}\nError: {failed['error']}\n")
    if report.get("events"):
        text += f"\nBrowser events (console errors, failed requests, HTTP 4xx/5xx):\n{_events_text(report['events'])}\n"
    if history:
        seq = " ".join("".join("P" if o else "F" for o in x.get("outcomes") or []) or "?" for x in history)
        text += (f"\nRecent runs of this test, oldest first (P = passed, F = failed; two letters = "
                 f"failed and re-run): {seq}\n")
    if retry is not None:
        text += ("\nThe test was re-run immediately and PASSED the second time.\n" if retry["passed"] else
                 f"\nThe test was re-run immediately and failed again: "
                 f"{next((r['error'] for r in retry['results'] if r['status'] == 'failed'), '')}\n")
    # The part of the application model this test needs: its scenario, the failed step, the records it uses.
    model = knowledge.prompt(project_id, knowledge.focus(
        text=f"{test.get('scenario', '')}\n{failed['description']}\n{failed.get('error') or ''}",
        records=[r.get("id") for r in test.get("data_refs") or []])) if project_id else ""
    if model:
        # A missing precondition (no club, no role, an object in another state) is the environment, not a bug.
        text += (f"\n{model}\nConsider whether a dependency of the test (data, role, state of an object) was "
                 "missing or different on the stand: that is an environment problem, not a product bug.\n")
    content: list[dict] = [{"type": "text", "text": text + "Screenshot after the failure:"}]
    if failed.get("screenshot"):
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": failed["screenshot"]}})
    system = ANALYSIS_SYSTEM + (skills.prompt(project_id, cfg.get("skills", [])) if project_id else "")
    reply = await llm.parse(cfg, system=system, messages=[{"role": "user", "content": content}],
                            schema=FailureAnalysis, max_tokens=4000, project_id=project_id, stage_name="run")
    if reply.stop == "refusal" or reply.parsed is None:
        return None
    return reply.parsed.model_dump()


async def judge_visual(test: dict, step: dict, images: dict[str, bytes], cfg: dict,
                       project_id: str = "") -> dict | None:
    """Bug or expected change? `images`: baseline / actual / diff PNGs."""
    content: list[dict] = [{"type": "text", "text": f"Test: {test['name']}\nStep: {step['description']}"}]
    for label in ("baseline", "actual", "diff"):
        if images.get(label):
            content += [{"type": "text", "text": f"{label.capitalize()} image:"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(images[label]).decode()}}]
    system = VISUAL_SYSTEM + (skills.prompt(project_id, cfg.get("skills", [])) if project_id else "")
    reply = await llm.parse(cfg, system=system, messages=[{"role": "user", "content": content}],
                            schema=VisualVerdict, max_tokens=4000, project_id=project_id, stage_name="run")
    if reply.stop == "refusal" or reply.parsed is None:
        return None
    return reply.parsed.model_dump()


def secret_values(credentials: dict, storage_state: dict | None = None) -> list[str]:
    """Values that must not stay in a trace: the password, secret login parameters, the TOTP secret, cookie values of a
    saved login (the suite's login state is a secret too)."""
    out = testdata.secret_values(credentials)
    for c in (storage_state or {}).get("cookies") or []:
        if len(str(c.get("value") or "")) >= 8:
            out.append(str(c["value"]))
    return out


def mask_trace(path: Path, credentials: dict, storage_state: dict | None = None) -> None:
    """The trace records typed values, request bodies and cookies: replace the secrets everywhere."""
    values = secret_values(credentials, storage_state)
    if not values or not path.exists():
        return
    needles = {n for v in values for n in (v.encode(), quote_plus(v).encode(), json.dumps(v)[1:-1].encode())}
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item)
            for n in needles:
                data = data.replace(n, b"***")
            dst.writestr(item, data)
    tmp.replace(path)


def _rejected(step: dict, new: list[dict]) -> bool:
    for old in step.get("heal_rejected") or []:
        if old == new or (old and new and old[0] == new[0]):
            return True
    return False


async def _heal_step(bs: BrowserSession, owner: dict, i: int, step: dict, cfg: dict, report: dict,
                     res: dict, run_dir: Path | None, prefix: str):
    """`owner`: the test the step belongs to - the test itself or a module it uses (a module
    is healed once, for every test that uses it)."""
    if not cfg.get("self_heal", True):
        raise RuntimeError("Element not found (self-healing is off)")
    choice = await heal(bs, step, cfg, owner.get("project_id", ""))
    if not choice:
        raise RuntimeError("Element not found and self-healing found no match")
    ref, reason = choice
    e = bs.elements[ref]
    new = (group_candidates(e) if step["action"] == "assert_count" else None) or await bs.unique_candidates(e)
    if _rejected(step, new):
        raise RuntimeError("Самолечение выбрало элемент, который уже был отклонён при ревью — "
                           "вероятно, элемента больше нет на странице")
    loc = bs.by_ref(ref)
    res["healed"] = True
    report["healed"] += 1
    if cfg.get("heal_mode", "review") == "auto":
        owner["steps"][i]["locator"] = new
        owner["steps"][i]["healed"] = True
        if owner.get("_module"):
            report.setdefault("module_heals", {})[owner["id"]] = owner["steps"]
    else:
        shot = ""
        if run_dir:
            try:
                await loc.evaluate(OUTLINE_JS)
                shot = f"{prefix}heal-{step['id']}.jpg"
                (run_dir / shot).write_bytes(await bs.page.screenshot(type="jpeg", quality=70))
                await loc.evaluate(UNOUTLINE_JS)
            except Exception:
                shot = ""
        report["proposals"].append({"id": uuid.uuid4().hex[:8], "step_id": step["id"],
                                    "test_id": owner.get("id", ""), "module": bool(owner.get("_module")),
                                    "description": step["description"], "action": step["action"],
                                    "old": step["locator"], "new": new, "reason": reason,
                                    "screenshot": shot, "at": time.time()})
    step["locator"] = new    # this run goes on with the element the LLM found
    return loc


def _error(bs: BrowserSession, e: Exception) -> str:
    if llm.is_api_error(e):     # self-healing could not reach the model
        return "Самолечение недоступно: " + llm.api_error_text(e)
    return bs.mask(str(e).splitlines()[0][:300] if str(e) else type(e).__name__)


async def run_steps(bs: BrowserSession, owner: dict, steps: list[dict], cfg: dict, report: dict, results: list,
                    *, run_dir: Path | None = None, prefix: str = "", hooks=None, on_progress=None,
                    depth: int = 0, stack: tuple = (), shots: bool = True, heal_ok: bool = True) -> bool:
    """Run `steps` of `owner` (a test or a module) into `results`; False at the first failure.
    use_module steps run the module's steps in place, with its parameters."""
    for i, step in enumerate(steps):
        if depth == 0:
            bs.step_index = i
        res = {"id": step["id"], "description": step["description"], "status": "passed",
               "error": "", "healed": False}
        try:
            if step["action"] == "use_module":
                await _module(bs, step, cfg, report, res, run_dir=run_dir, prefix=prefix, depth=depth,
                              stack=stack + (owner.get("id", ""),), heal_ok=heal_ok)
            else:
                loc = None
                if needs_element(step) and not (step["action"] == "assert_count"
                                                and str(step.get("value") or "0").strip() == "0"):
                    await bs.settle()
                    loc, _ = await bs.find(step["locator"], wait=FIND_WAIT)
                    if loc is None or not await _usable(loc):
                        if not heal_ok:
                            raise RuntimeError(f"Не найден элемент шага «{step['description']}»")
                        loc = await _heal_step(bs, owner, i, step, cfg, report, res, run_dir, prefix)
                if hooks and depth == 0:
                    await hooks.before_step(bs, i, step, loc)
                details = await perform(bs, step, loc)
                if details:
                    res["details"] = details
                if hooks and depth == 0 and hasattr(hooks, "after_step"):
                    await hooks.after_step(bs, i, step, loc)
        except Exception as e:
            res["status"] = "failed"
            res["error"] = res.get("error") or _error(bs, e)
            report["passed"] = False
            if isinstance(e, checks.CheckFailed):
                res["details"] = e.details
                await _judge(owner, step, res, cfg, run_dir)
        res["url"] = bs.url
        if shots:
            try:
                res["screenshot"] = await bs.screenshot_b64()
            except Exception:
                res["screenshot"] = ""
            if run_dir and res["screenshot"]:
                res["shot"] = f"{prefix}{step['id']}.jpg"
                (run_dir / res["shot"]).write_bytes(base64.b64decode(res["screenshot"]))
        results.append(res)
        if on_progress:
            await on_progress(report)
        if res["status"] == "failed":
            return False
    return True


async def _module(bs: BrowserSession, step: dict, cfg: dict, report: dict, res: dict, *, run_dir, prefix: str,
                  depth: int, stack: tuple, heal_ok: bool) -> None:
    from . import storage
    s = spec(step)
    module = storage.load(str(s.get("module") or ""))
    if not module:
        raise ValueError("Модуль не найден (тест удалён?)")
    if depth + 1 > MAX_MODULE_DEPTH or module["id"] in stack:
        raise ValueError("Модули вложены слишком глубоко или вызывают сами себя")
    module["_module"] = True
    params = {k: bs.expand(str(v)) for k, v in (s.get("params") or {}).items()}
    bs.params.append(params)
    sub: list[dict] = []
    try:
        ok = await run_steps(bs, module, module["steps"], cfg, report, sub, run_dir=run_dir, prefix=prefix,
                             depth=depth + 1, stack=stack, shots=False, heal_ok=heal_ok)
    finally:
        bs.params.pop()
    res["sub"] = sub
    res["healed"] = any(r.get("healed") for r in sub)
    if not ok:
        failed = next(r for r in sub if r["status"] == "failed")
        res["error"] = f"Модуль «{module['name']}», шаг «{failed['description']}»: {failed['error']}"
        raise AssertionError(res["error"])


async def _data_block(bs: BrowserSession, steps: list[dict] | None, phase: str) -> list[dict]:
    """The before / after requests of a test (api_request). "after" runs all of its steps even
    when one fails: it cleans up what "before" created."""
    out = []
    for step in steps or []:
        r = {"id": step.get("id", ""), "description": step.get("description", ""), "status": "passed", "error": ""}
        try:
            if step.get("action") != "api_request":
                raise ValueError("В блоках «до» и «после» теста — только запросы к API (api_request)")
            details = await perform(bs, step | {"phase": phase})
            if details:
                r["details"] = details
        except Exception as e:
            r.update(status="failed", error=_error(bs, e))
            out.append(r)
            if phase == "before":
                break
            continue
        out.append(r)
    return out


def origin(url: str) -> str:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


async def run_test(test: dict, headless: bool = True, on_progress=None,
                   credentials: dict | None = None, cfg: dict | None = None, *,
                   browser=None, run_dir: Path | None = None, attempt: int = 1, hooks=None,
                   engine: str = "chromium", device: str = "", storage_state: dict | None = None,
                   capture_state: bool = False, base_url: str = "") -> dict:
    """Run all steps once. Returns the attempt's report; in heal_mode "auto" healed
    locators are written into `test` (and report["module_heals"] for modules), in "review"
    mode they become report["proposals"].

    `credentials` fill the {{username}} / {{password}} / {{totp}} placeholders in step values.
    `cfg` is the project's "run" stage. `browser`: a shared browser (suite runs) to
    open a context in. `run_dir`: where screenshots, the trace and visual diffs go.
    `engine` / `device`: chromium | firefox | webkit, a device profile ("iPhone 13").
    `storage_state`: a saved login (the suite logs in once); `capture_state`: keep the
    context's state after a passed run in report["storage_state"] (the login test).
    The test's "before" requests run first, its "after" requests always run last.
    `hooks` (mutation testing): `setup(bs)` before the first step,
    `before_step(bs, index, step, locator)` before each one and, if defined,
    `after_step(...)` after each successful one.
    """
    cfg = cfg or {"self_heal": True, "heal_mode": "auto", "analyze_failures": False, "trace": "off"}
    project_id = test.get("project_id", "")
    steps = copy.deepcopy(test["steps"])
    prefix = "" if attempt == 1 else f"a{attempt}-"
    report = {"attempt": attempt, "started": time.time(), "results": [], "healed": 0, "passed": True,
              "events": [], "trace": "", "proposals": [], "browser": engine, "device": device,
              "before": [], "after": []}
    if run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
    api = test.get("layer") == "api"
    if api:
        # An API test: no browser, the requests go to the project's API with its authorization.
        from . import projects
        from .apiclient import ApiClient
        bs = await ApiClient.launch(projects.get(project_id) or {"id": project_id, "base_url": test.get("url", "")},
                                    credentials, storage_state)
        report.update(browser="api", device="")
        base_url = bs.url
    else:
        bs = await BrowserSession.launch(headless=headless, browser=browser, engine=engine, device=device,
                                         locale=cfg.get("locale", ""), timezone=cfg.get("timezone", ""),
                                         storage_state=storage_state)
    bs.credentials = credentials or {}
    bs.options = {"project_id": project_id, "test_id": test.get("id", ""), "run_dir": run_dir,
                  "attempt_prefix": prefix, "a11y_impact": cfg.get("a11y_impact", "serious"),
                  "visual_threshold": cfg.get("visual_threshold", checks.DEFAULT_VISUAL_THRESHOLD),
                  "base_url": base_url or origin(test.get("url", "")), "own_vars": []}
    # A test that switches tabs itself is replayed exactly; older tests follow new tabs.
    bs.follow_new_tabs = not any(s["action"] == "switch_tab" for s in steps)
    trace = cfg.get("trace", "failed") if run_dir and not api else "off"
    capture_state = capture_state and not api
    try:
        if trace != "off":
            await bs.context.tracing.start(screenshots=True, snapshots=True)
        if hooks:
            await hooks.setup(bs)
        try:
            report["before"] = await _data_block(bs, test.get("before"), "before")
            if any(r["status"] == "failed" for r in report["before"]):
                report["passed"] = False
                failed = next(r for r in report["before"] if r["status"] == "failed")
                report["results"].append({"id": "before", "description": "Подготовка данных (до теста)",
                                          "status": "failed", "healed": False, "url": bs.url,
                                          "error": f"{failed['description']}: {failed['error']}"})
            else:
                await run_steps(bs, test, steps, cfg, report, report["results"], run_dir=run_dir, prefix=prefix,
                                hooks=hooks, on_progress=on_progress)
        finally:
            report["after"] = await _data_block(bs, test.get("after"), "after")
        if capture_state and report["passed"]:
            report["storage_state"] = await bs.context.storage_state()
        if trace != "off":
            keep = trace == "always" or not report["passed"]
            if keep:
                path = run_dir / f"{prefix}trace.zip"
                await bs.context.tracing.stop(path=str(path))
                mask_trace(path, bs.credentials, storage_state)
                report["trace"] = path.name
            else:
                await bs.context.tracing.stop()
    finally:
        report["events"] = bs.events
        await bs.close()
    report["finished"] = time.time()
    return report


async def _judge(test: dict, step: dict, res: dict, cfg: dict, run_dir: Path | None) -> None:
    """A failed visual check: ask the LLM whether it is a bug or an expected change."""
    v = (res.get("details") or {}).get("visual")
    if not v or not cfg.get("analyze_failures") or not run_dir:
        return
    base = checks.baseline_file(test["project_id"], test["id"], step["id"])
    images = {"baseline": fs.read_bytes(base) if fs.is_file(base) else b""}
    for key in ("actual", "diff"):
        f = run_dir / v[key] if v.get(key) else None
        images[key] = f.read_bytes() if f and f.exists() else b""
    try:
        v["verdict"] = await judge_visual(test, step, images, cfg, test.get("project_id", ""))
    except Exception as e:
        v["verdict"] = {"verdict": "", "summary": llm.api_error_text(e)}


async def _usable(loc) -> bool:
    try:
        await loc.wait_for(state="attached", timeout=5000)
        return True
    except Exception:
        return False
