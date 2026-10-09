"""Requirements -> test scenarios (the article's "data interpretation" and
"scenario formulation" stages): a user story, Jira ticket or spec goes in,
a prioritized list of positive, negative and edge-case scenarios comes out.
Each scenario can then be sent to the Studio for generation.

There is no cap on the number of scenarios: coverage of the requirements decides
it. To keep that independent of the output limit of one response, generation has
two phases: a compact plan of every scenario (title, type, priority, what it
covers), then the full scenarios, detailed in parallel batches.

What to design is chosen for every generation (or taken from the project's "scenarios" settings):
the kinds of checks (TYPES: positive, negative, boundary... by the principles of testing), the
layers (UI tests in the browser, API tests of the backend) and the test design techniques
(TECHNIQUES). The project's application model (knowledge.py: entities, their dependencies and
lifecycles, roles with their capabilities, test data) turns into preconditions the scenarios state
explicitly: every scenario names the role it acts as and the test data it needs (`test_data`), and
that need is recorded in the model (knowledge.need) so the tests reuse one set of data. A large model
does not fit a request: the requests get the slice of it the requirements name (knowledge.prompt with a
focus); the plan names the groups of the model its scenarios need, and each batch of details gets the
slice of those groups and of its scenarios. The plan is
compared with the scenarios and tests the project already has (reuse.py): a scenario that repeats or
extends an earlier one carries a `match`, and a person decides to reuse, refine or create a new one.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from . import knowledge, llm, projects, reuse, skills, traffic
from .catalog import DEFAULT_TECHNIQUES, DEFAULT_TYPES, LAYERS, TECHNIQUES, TYPES

ScenarioType = Literal[tuple(TYPES)]
Layer = Literal["ui", "api"]
Priority = Literal["high", "medium", "low"]

BATCH = 3          # scenarios detailed per request: short answers
PARALLEL = 4       # detail requests in flight


class DataNeed(BaseModel):
    entity: str              # Product, Order, Customer…
    name: str                # a reusable name: «Test product A», «a customer with a delivery address»
    details: str             # what matters: price, settings, links to other objects
    state: str               # the state of its lifecycle the scenario needs: in stock, paid…
    role: str                # the role whose object it is


class Scenario(BaseModel):
    title: str
    type: ScenarioType
    layer: Layer = "ui"
    priority: Priority
    role: str = ""           # the role of the application model the test acts as ("" - no login)
    preconditions: str
    instructions: str        # plain-language scenario to hand to the authoring agent
    expected_result: str
    gherkin: str
    test_data: list[DataNeed] = []


class DesignedScenario(Scenario):
    match: dict | None = None    # an earlier test or scenario it repeats or extends (reuse.py); not asked of the model


class ScenarioSet(BaseModel):
    feature: str
    assumptions: list[str]
    scenarios: list[DesignedScenario]


class PlannedScenario(BaseModel):
    title: str
    type: ScenarioType
    layer: Layer = "ui"
    priority: Priority
    role: str = ""
    covers: str              # which requirement / rule this checks, one line
    existing: str = ""       # the ref of an earlier test (T…) or scenario (S…) it repeats or extends
    relation: Literal["", "same", "refine"] = ""


class ScenarioPlan(BaseModel):
    feature: str
    assumptions: list[str]
    scenarios: list[PlannedScenario]
    more: bool = False       # the plan goes on: the next part is asked for
    groups: list[str] = []   # groups of a large application model the scenarios need (its index names them)


class ScenarioBatch(BaseModel):
    scenarios: list[Scenario]


SYSTEM = """You are a senior QA analyst. From the requirements you are given, design a set of test scenarios with good coverage: the main happy paths, negative cases (invalid input, errors), edge and boundary cases, and the other kinds of checks you are asked for. Do not pad the list with near-duplicates; each scenario must test something distinct. There is no limit on the number of scenarios: include every scenario the requirements call for, and no more.

Every scenario has a `layer`: "ui" - a test in the browser, as a user; "api" - a test of the backend API of the same application (HTTP requests and their responses: status, fields, rules), without the user interface. Design a scenario at the layer that checks its rule most directly, and only at the layers you are asked for.

For each scenario, `instructions` is what a test automation agent will be told, so write concrete, self-contained steps ending with what must be verified: for "ui", the steps a user takes on the site; for "api", the requests (method, path, body) and the expected status and response fields. `preconditions` lists what must exist before the test - data, accounts with their roles, states of objects - taken from the application model when it has them; a scenario never silently relies on data it does not name.

Roles and the lifecycle of the system decide the scenarios. `role` is the role the test acts as - a role of the application model by its exact name ("" only when the application has no login). A scenario acts as a role that may do what it checks (its capabilities); checks of rights ("roles", "security") act as a role that may not, and expect a refusal. An action available to several roles with different rules is a scenario per role that behaves differently. Objects are taken in the state of their lifecycle the scenario needs, and that state is reached by the transitions the lifecycle allows (a "shipped" order needs a paid one first). `test_data` lists every object the test needs before its first step: entity, a reusable name (an object of the model's test data when there is one - its exact name - otherwise a descriptive one such as "a customer with a delivery address"), the details that matter, the state of its lifecycle and the role whose object it is. Scenarios that need the same data name it the same way: the studio records it in the project's test data and the tests reuse one set of data. `gherkin` is the scenario in Given/When/Then form (just the Scenario block). List any assumption you had to make about unclear requirements. Write in the language of the requirements; write its letters as they are, never as \\u escapes."""

PLAN_TASK = ("First step: plan the complete list of scenarios. For each give only the title, type, "
             "priority, the role it acts as and a one-line note of what it covers. Order them by importance. "
             "Give at most {page} scenarios in this answer; if the plan needs more, set `more` to true "
             "and you will be asked for the rest.")
PLAN_GROUPS = ("\n\nThe application model is large and shown in part (its index names the rest by group). In `groups` "
               "name the groups of the model the planned scenarios need, as the model names them; [] when what they "
               "need is all shown.")
PLAN_NEXT = ("Already planned (titles):\n{listing}\n\nContinue the plan: give the next scenarios (at most {page}) "
             "that are not in this list, in the same format. Set `more` to true if even more remain after them.")
# Short answers: the plan and the details come in small parts, every request on a clean context (the
# requirements and one task, no conversation), so no single answer depends on the output limit.
PLAN_PAGE = 15     # planned scenarios per answer
PLAN_PAGES = 60    # a guard against a model that always answers `more`


def _context(requirements: str, url: str, cfg: dict, model: str = "", earlier: str = "", api: str = "") -> str:
    text = f"Requirements:\n{requirements}\n\n"
    if model:
        text += f"{model}\n\n"
    if earlier:
        text += f"{earlier}\n\n"
    if url:
        text += f"Application URL: {url}\n"
    types = [t for t in cfg.get("types") or [] if t in TYPES]
    if types:
        text += ("Kinds of checks to cover (the scenario `type`); do not produce other types:\n"
                 + "".join(f"- {t}: {TYPES[t][1]}\n" for t in types))
    layers = [x for x in cfg.get("layers") or [] if x in LAYERS] or ["ui"]
    text += f"Layers: {', '.join(layers)}. Do not produce scenarios of other layers.\n"
    if "api" in layers and api:
        text += ("Known endpoints of the application's API (seen in the traffic of its UI tests and the site map; "
                 "{id} is an object id). API scenarios use these paths, methods and fields; an endpoint the "
                 "requirements need but the list lacks may be named as the requirements describe it:\n" + api + "\n")
    techniques = [t for t in cfg.get("techniques") or [] if t in TECHNIQUES]
    if techniques:
        text += ("Test design techniques to apply when deriving the scenarios:\n"
                 + "".join(f"- {TECHNIQUES[t][0]}: {TECHNIQUES[t][1]}\n" for t in techniques))
    return text


def settings(project: dict | None, types: list[str] | None = None, layers: list[str] | None = None,
             techniques: list[str] | None = None) -> dict:
    """The "scenarios" stage settings of a project, with the choice made for one generation (None = the project's)."""
    cfg = dict(project["pipeline"]["scenarios"]) if project else {"types": DEFAULT_TYPES, "layers": ["ui"],
                                                                   "techniques": DEFAULT_TECHNIQUES}
    if types is not None:
        cfg["types"] = [t for t in types if t in TYPES] or cfg.get("types") or DEFAULT_TYPES
    if layers is not None:
        cfg["layers"] = [x for x in layers if x in LAYERS] or cfg.get("layers") or ["ui"]
    if techniques is not None:
        cfg["techniques"] = [t for t in techniques if t in TECHNIQUES]
    return cfg


class TooLong(RuntimeError):
    """The answer was cut off by the output limit; `text` is what came before the cut."""
    def __init__(self, message: str, text: str = ""):
        super().__init__(message)
        self.text = text


async def _parse(cfg: dict, system: str, context: str, task: str, fmt: type[BaseModel], project_id: str = ""):
    # The requirements are the same in every request of one generation: they are cached separately.
    reply = await llm.parse(cfg, system=system, context=context, messages=[{"role": "user", "content": task}],
                            schema=fmt, max_tokens=16000, project_id=project_id, stage_name="scenarios")
    if reply.stop == "max_tokens":
        raise TooLong("Ответ модели не поместился в лимит: разделите требования на части.", reply.text or "")
    if reply.stop == "refusal" or reply.parsed is None:
        raise RuntimeError("Модель не смогла составить сценарии по этим требованиям.")
    return reply.parsed


def _salvage(text: str, item: type[BaseModel]) -> list:
    """The complete scenarios of an answer cut off by the output limit: they are kept, not asked again."""
    m = re.search(r'"scenarios"\s*:\s*\[', text or "")
    if not m:
        return []
    decoder, pos, out = json.JSONDecoder(), m.end(), []
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        try:
            obj, pos = decoder.raw_decode(text, pos)
            out.append(item.model_validate(obj))
        except (ValueError, ValidationError):
            return out


def _field(text: str, key: str, default):
    """A top-level field of a cut-off answer, if it came before the cut."""
    m = re.search(rf'"{key}"\s*:\s*', text or "")
    try:
        return json.JSONDecoder().raw_decode(text, m.end())[0] if m else default
    except ValueError:
        return default


def _listing(planned: list[PlannedScenario], first: int = 0) -> str:
    return "\n".join(f"{first + i + 1}. [{s.layer}, {s.type}, {s.priority}{', ' + s.role if s.role else ''}] "
                     f"{s.title} — {s.covers}"
                     for i, s in enumerate(planned))


async def _plan(cfg: dict, system: str, context: str, pid: str, say, extra: str = "") -> ScenarioPlan:
    """The plan part by part: at most `page` scenarios per answer. The next part gets only the titles
    planned so far; a cut-off answer keeps its complete scenarios and the plan goes on from there."""
    plan: ScenarioPlan | None = None
    page = PLAN_PAGE
    for _ in range(PLAN_PAGES):
        task = (PLAN_TASK.format(page=page) if plan is None else PLAN_NEXT.format(
            listing="\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(plan.scenarios)), page=page)) + extra
        try:
            part: ScenarioPlan = await _parse(cfg, system, context, task, ScenarioPlan, pid)
        except TooLong as e:
            got = _salvage(e.text, PlannedScenario)
            if not got and page <= 3:
                raise
            page = max(3, page // 2)
            if not got:
                say(f"План не поместился в ответ модели, прошу частями по {page}…")
                continue
            say(f"Ответ модели оборвался на лимите: сохранено сценариев {len(got)}, продолжаю частями по {page}…")
            part = ScenarioPlan(feature=_field(e.text, "feature", ""), assumptions=_field(e.text, "assumptions", []),
                                scenarios=got, more=True)
        if plan is None:
            plan = part
        else:
            seen = {s.title.strip().lower() for s in plan.scenarios}
            plan.scenarios += [s for s in part.scenarios if s.title.strip().lower() not in seen]
            plan.groups = list(dict.fromkeys(plan.groups + part.groups))
        if not part.more or not part.scenarios:
            break
        say(f"В плане {len(plan.scenarios)} сценариев, продолжаю план…")
    plan.more = False
    return plan


async def generate(requirements: str, url: str = "", project: dict | None = None,
                   log=None, progress=None, cfg: dict | None = None, record: bool = True) -> ScenarioSet:
    """`project` (optional) supplies the "scenarios" stage settings: skills, scenario
    types to cover, model and effort; `cfg` - those settings with the choice of one generation
    (settings()). `log(text)` (optional) reports progress;
    `progress(event)` (optional) gets the intermediate results for a live view:
    {"type": "log", "text"}, {"type": "plan", "feature", "assumptions", "scenarios"} (the planned
    titles), {"type": "batch", "start", "scenarios"} (detailed scenarios from position `start`)."""
    def say(text: str) -> None:
        if log:
            log(text)
        if progress:
            progress({"type": "log", "text": text})

    cfg = cfg or settings(project)
    system = SYSTEM + projects.language_rule(project) + (skills.prompt(project["id"], cfg.get("skills", []))
                                                         if project else "")
    pid = project["id"] if project else ""
    earlier = reuse.candidates(pid) if project else []
    layers = cfg.get("layers") or []
    api = traffic.catalog_text(traffic.catalog(pid)) if project and "api" in layers else ""
    # A large application model: the part the requirements name, an index of the rest (knowledge.prompt).
    model = knowledge.prompt(pid, knowledge.focus(text=requirements)) if project else ""
    large = bool(model) and not knowledge.fits(pid)
    context = _context(requirements, url, cfg, model, reuse.listing(earlier), api)

    if progress:
        say(f"Анализ требований ({len(requirements)} символов) и план покрытия…")
    plan = await _plan(cfg, system, context, pid, say, PLAN_GROUPS if large else "")
    matches = [reuse.resolve(earlier, s.existing, s.relation) for s in plan.scenarios]
    if any(matches):
        say(f"Похожи на созданные ранее: {sum(1 for m in matches if m)} — решите, переиспользовать, доработать "
            "или создать новые")
    if progress:
        progress({"type": "plan", "feature": plan.feature, "assumptions": plan.assumptions,
                  "scenarios": [s.model_dump() | {"match": m} for s, m in zip(plan.scenarios, matches)]})
    say(f"Сценариев в плане: {len(plan.scenarios)}, детализация…")
    if not plan.scenarios:
        return ScenarioSet(feature=plan.feature, assumptions=plan.assumptions, scenarios=[])

    gate = asyncio.Semaphore(PARALLEL)

    async def detail(start: int, count: int) -> list[DesignedScenario]:
        # Only the scenarios of this batch go into the request, not the whole plan: a short request.
        part = plan.scenarios[start:start + count]
        task = (f"Write out in full these scenarios {start + 1}–{start + len(part)} of the test plan, in this order, "
                f"keeping their titles, layers, types, priorities and roles:\n{_listing(part, start)}")
        if large:       # the part of the model these scenarios need, beyond what the requirements named
            task += "\n\nFor these scenarios - " + knowledge.prompt(
                pid, knowledge.focus(text=_listing(part, start), groups=plan.groups))
        async with gate:
            if progress:
                say(f"Детализация сценариев {start + 1}–{start + len(part)}: {part[0].title}…")
            try:
                got = (await _parse(cfg, system, context, task, ScenarioBatch, pid)).scenarios
            except TooLong as e:
                got = _salvage(e.text, Scenario)        # the complete ones are kept, the rest asked again
                if not got and len(part) == 1:
                    raise
        if not got and len(part) > 1:
            half = len(part) // 2
            return await detail(start, half) + await detail(start + half, len(part) - half)
        # The plan is authoritative for what the scenario is; the batch adds the details.
        out = [DesignedScenario(**full.model_dump() | {
                   "title": planned.title, "type": planned.type, "layer": planned.layer, "priority": planned.priority,
                   "role": planned.role or full.role, "match": matches[start + k]})
               for k, (planned, full) in enumerate(zip(part, got))]
        if progress and out:
            progress({"type": "batch", "start": start, "scenarios": [s.model_dump() for s in out]})
            say(f"Готовы сценарии {start + 1}–{start + len(out)}" + "".join(f"\n  ✓ {s.title}" for s in out))
        if out and len(out) < len(part):
            out += await detail(start + len(out), len(part) - len(out))
        elif len(out) < len(part):
            say(f"Не удалось детализировать сценариев: {len(part) - len(out)}")
        return out

    batches = await asyncio.gather(*(detail(i, BATCH) for i in range(0, len(plan.scenarios), BATCH)))
    res = ScenarioSet(feature=plan.feature, assumptions=plan.assumptions, scenarios=[s for b in batches for s in b])
    if record and project and project["pipeline"]["requirements"].get("learn_model"):
        doc = knowledge.need(pid, [sc.model_dump() for sc in res.scenarios])
        say(f"Тестовые данные сценариев записаны в модель приложения: записей {len(doc['data'])}, "
            f"ролей {len(doc['roles'])}")
    return res
