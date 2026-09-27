"""Quality check of a test's assertions by mutating the application's UI.

A test written by AI can pass whatever the application does: it clicks through
the scenario and "checks" something that is always on the screen. To catch
that, the saved test is replayed a few times on a deliberately broken page -
a mutant - and must FAIL on each one:

    noop_action   the last action before the final check does nothing
                  (the click is swallowed, the typed text is lost)
    assertion     right before a result check its subject is broken: the element
                  is hidden, the text removed, the value / checkbox / enabled
                  state flipped, one list item removed, the URL changed
    api_500       every XHR/fetch request of the page answers 500

A mutant the test survives means weak assertions ("слабые проверки"): the
pipeline's verify stage then asks the authoring agent to add checks (see
agent.StudioSession `base_steps`) and verifies again. The share of killed
mutants is shown on the test card. Mutation runs use no LLM and no self-healing.

Mutants only ever touch the page inside the test's own browser (DOM changes and
request interception); nothing is sent to the application that the recorded
test would not send itself.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

from . import runner, storage
from .steps import ASSERTIONS, AUXILIARY_ASSERTIONS

ACTIONS = ("click", "fill", "select_option", "press_key")
RUN_CFG = {"self_heal": False, "analyze_failures": False, "trace": "off"}


@dataclass
class Mutant:
    kind: str                   # noop_action | assertion | api_500
    step: int                   # index of the step it is applied at (-1: from the start)
    description: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])


def _q(text: str) -> str:
    return f"«{text[:60]}»"


def _assertion_mutant(i: int, s: dict) -> Mutant | None:
    a, v = s["action"], s.get("value", "")
    what = {
        "assert_visible": "элемент скрыт",
        "assert_text_present": f"текст {_q(v)} убран со страницы",
        "assert_element_text": f"текст {_q(v)} убран из элемента",
        "assert_value": "значение поля изменено",
        "assert_checked": "флажок переключён",
        "assert_enabled": "доступность элемента переключена",
        "assert_count": "из списка удалён один элемент",
        "assert_url_contains": "адрес страницы изменён",
    }.get(a)
    if not what or (a == "assert_count" and str(v).strip() in ("", "0")):
        return None
    return Mutant("assertion", i, f"Перед шагом {i + 1} {_q(s['description'])}: {what}")


def plan(test: dict, limit: int = 5, uses_api: bool = True) -> list[Mutant]:
    steps = test["steps"]
    checks = [i for i, s in enumerate(steps)
              if s["action"] in ASSERTIONS and s["action"] not in AUXILIARY_ASSERTIONS]
    first: list[Mutant] = []
    if checks:
        act = next((i for i in range(checks[-1] - 1, -1, -1) if steps[i]["action"] in ACTIONS), None)
        if act is not None:
            first.append(Mutant("noop_action", act, f"Шаг {act + 1} {_q(steps[act]['description'])} "
                                                    "ничего не делает"))
    asserts = [m for m in (_assertion_mutant(i, steps[i]) for i in reversed(checks)) if m]
    out = first + asserts[:1]
    if uses_api:
        out.append(Mutant("api_500", -1, "Все запросы XHR/fetch отвечают ошибкой 500"))
    out += asserts[1:]
    return out[:limit]


_SWALLOW_CLICK = """el => window.addEventListener('click', e => {
  if (el.contains(e.target)) { e.preventDefault(); e.stopImmediatePropagation(); } }, true)"""
_SWALLOW_KEYS = """() => ['keydown', 'keypress', 'keyup'].forEach(t => window.addEventListener(t, e => {
  e.preventDefault(); e.stopImmediatePropagation(); }, true))"""
_REMOVE_TEXT = """([root, needle]) => {
  const low = needle.toLowerCase(); let n = 0;
  const walk = document.createTreeWalker(root || document.body, NodeFilter.SHOW_TEXT);
  for (let t = walk.nextNode(); t; t = walk.nextNode()) {
    const i = t.nodeValue.toLowerCase().indexOf(low);
    if (i >= 0) { t.nodeValue = t.nodeValue.slice(0, i) + t.nodeValue.slice(i + needle.length); n++; }
  }
  return n;
}"""


# The native setter, so that frameworks tracking the value (React) see the change too.
_RESET_INPUT = """el => {
  if (el.tagName === 'SELECT') el.selectedIndex = 0;
  else Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set.call(el, '');
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}"""


class _Later(Exception):
    """The mutant is applied after the step, not before."""


class ApiProbe:
    """Baseline run hooks: does the page use XHR/fetch at all (is api_500 meaningful)?"""

    def __init__(self):
        self.requests = 0

    async def setup(self, bs) -> None:
        def seen(request):
            if request.resource_type in ("xhr", "fetch"):
                self.requests += 1
        bs.context.on("request", seen)

    async def before_step(self, bs, i, step, loc) -> None:
        pass


class Hooks:
    """runner.run_test hooks that apply one mutant."""

    def __init__(self, mutant: Mutant):
        self.m = mutant
        self.applied = False
        self.error = ""

    async def setup(self, bs) -> None:
        if self.m.kind != "api_500":
            return

        async def handler(route):
            if route.request.resource_type in ("xhr", "fetch"):
                await route.fulfill(status=500, content_type="application/json", body='{"error": "mutant"}')
            else:
                await route.fallback()

        await bs.context.route("**/*", handler)
        self.applied = True

    async def before_step(self, bs, i: int, step: dict, loc) -> None:
        if i != self.m.step or self.m.kind == "api_500":
            return
        try:
            await (self._noop if self.m.kind == "noop_action" else self._break)(bs, step, loc)
            self.applied = True
        except _Later:
            pass
        except Exception as e:
            self.error = str(e).splitlines()[0][:200]

    async def after_step(self, bs, i: int, step: dict, loc) -> None:
        """noop_action for typing and choosing: the input does not stick."""
        if i != self.m.step or self.m.kind != "noop_action" or step["action"] not in ("fill", "select_option"):
            return
        if step["action"] == "fill" and step.get("press_enter"):
            return      # handled before the step: Enter is swallowed
        try:
            await loc.evaluate(_RESET_INPUT)
            self.applied = True
        except Exception as e:
            self.error = str(e).splitlines()[0][:200]

    async def _noop(self, bs, step: dict, loc) -> None:
        a = step["action"]
        if a == "click":
            await loc.evaluate(_SWALLOW_CLICK)
        elif a == "press_key" or (a == "fill" and step.get("press_enter")):
            await bs.page.evaluate(_SWALLOW_KEYS)
        else:
            raise _Later()

    async def _break(self, bs, step: dict, loc) -> None:
        a, v = step["action"], bs.expand(step.get("value", ""))
        if a == "assert_visible":
            await loc.evaluate("el => el.style.setProperty('display', 'none', 'important')")
        elif a == "assert_text_present":
            if not await bs.page.evaluate(_REMOVE_TEXT, [None, v]):
                raise RuntimeError("текст не найден на странице")
        elif a == "assert_element_text":
            await loc.evaluate(f"(el, needle) => ({_REMOVE_TEXT})([el, needle])", v)
        elif a == "assert_value":
            await loc.evaluate("(el, v) => { el.value = v ? v + '~mutant' : 'mutant'; }", v)
        elif a == "assert_checked":
            await loc.evaluate("el => { el.checked = !el.checked; }")
        elif a == "assert_enabled":
            await loc.evaluate("el => { el.disabled = !el.disabled; }")
        elif a == "assert_count":
            group = await bs.find_all(step["locator"])
            await group.first.evaluate("el => el.remove()")
        elif a == "assert_url_contains":
            await bs.page.evaluate("() => history.replaceState(null, '', '/tg-mutant')")


async def verify(project: dict, test: dict, log=None) -> dict:
    """Run the mutants against a saved test; the result is stored in test["verify"]."""
    cfg = project["pipeline"]["verify"]
    creds = storage.credentials(test)
    started = time.time()
    result = {"at": started, "status": "running", "mutants": [], "killed": 0, "total": 0, "score": None,
              "weak": False}
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=cfg["headless"])
    try:
        probe = ApiProbe()
        base = await runner.run_test(test, headless=cfg["headless"], credentials=creds, cfg=RUN_CFG,
                                     browser=browser, hooks=probe)
        if not base["passed"]:
            failed = next(r for r in base["results"] if r["status"] == "failed")
            result.update(status="baseline_failed", error=f"Тест падает без мутаций: {failed['description']} — "
                                                          f"{failed['error']}")
            return _save(test, result)
        mutants = plan(test, cfg["mutants"], uses_api=probe.requests > 0)
        if not mutants:
            result.update(status="no_mutants", error="В тесте нет проверок результата, которые можно мутировать")
            return _save(test, result)
        for m in mutants:
            hooks = Hooks(m)
            rep = await runner.run_test(test, headless=cfg["headless"], credentials=creds, cfg=RUN_CFG,
                                        browser=browser, hooks=hooks)
            failed_at = next((i for i, r in enumerate(rep["results"]) if r["status"] == "failed"), None)
            if not hooks.applied or (failed_at is not None and failed_at < m.step):
                outcome = "invalid"      # not applied, or the run broke before the mutant took effect
            else:
                outcome = "killed" if not rep["passed"] else "survived"
            result["mutants"].append({"id": m.id, "kind": m.kind, "step": m.step, "description": m.description,
                                      "result": outcome, "error": hooks.error})
            if log:
                log(f"Мутант: {m.description} — {({'killed': 'обнаружен', 'survived': 'НЕ обнаружен'}).get(outcome, 'не применим')}")
        valid = [x for x in result["mutants"] if x["result"] != "invalid"]
        result["total"] = len(valid)
        result["killed"] = sum(x["result"] == "killed" for x in valid)
        result["score"] = round(result["killed"] / len(valid), 3) if valid else None
        result["weak"] = result["killed"] < len(valid)
        result["status"] = "done"
    except Exception as e:
        result.update(status="error", error=str(e).splitlines()[0][:300] if str(e) else type(e).__name__)
    finally:
        await browser.close()
        await pw.stop()
    return _save(test, result)


def _save(test: dict, result: dict) -> dict:
    result["finished"] = time.time()
    test["verify"] = result
    storage.update(test["id"], lambda t: t.update(verify=result))
    return result


def improvement_task(result: dict) -> str:
    """What the authoring agent is asked to do about surviving mutants."""
    survived = [m["description"] for m in result["mutants"] if m["result"] == "survived"]
    return ("The recorded test above was replayed on deliberately broken versions of the page, and it still "
            "PASSED in these cases, so its assertions do not verify the scenario's result:\n"
            + "\n".join(f"- {d}" for d in survived) +
            "\n\nAdd assertion steps that would fail in these cases: check the actual outcome of the scenario "
            "(the changed data, the message, the value, the list, the state), not just that some element is "
            "visible. Do not repeat the scenario's actions unless you need to; the page is at the end of the "
            "recorded test. Then call finish.")
