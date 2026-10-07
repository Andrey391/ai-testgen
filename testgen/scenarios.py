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
lifecycles, test data) turns into preconditions the scenarios state explicitly.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from . import knowledge, llm, projects, skills
from .catalog import DEFAULT_TECHNIQUES, DEFAULT_TYPES, LAYERS, TECHNIQUES, TYPES

ScenarioType = Literal[tuple(TYPES)]
Layer = Literal["ui", "api"]
Priority = Literal["high", "medium", "low"]

BATCH = 6          # scenarios detailed per request
PARALLEL = 4       # detail requests in flight


class Scenario(BaseModel):
    title: str
    type: ScenarioType
    layer: Layer = "ui"
    priority: Priority
    preconditions: str
    instructions: str        # plain-language scenario to hand to the authoring agent
    expected_result: str
    gherkin: str


class ScenarioSet(BaseModel):
    feature: str
    assumptions: list[str]
    scenarios: list[Scenario]


class PlannedScenario(BaseModel):
    title: str
    type: ScenarioType
    layer: Layer = "ui"
    priority: Priority
    covers: str              # which requirement / rule this checks, one line


class ScenarioPlan(BaseModel):
    feature: str
    assumptions: list[str]
    scenarios: list[PlannedScenario]


class ScenarioBatch(BaseModel):
    scenarios: list[Scenario]


SYSTEM = """You are a senior QA analyst. From the requirements you are given, design a set of test scenarios with good coverage: the main happy paths, negative cases (invalid input, errors), edge and boundary cases, and the other kinds of checks you are asked for. Do not pad the list with near-duplicates; each scenario must test something distinct. There is no limit on the number of scenarios: include every scenario the requirements call for, and no more.

Every scenario has a `layer`: "ui" - a test in the browser, as a user; "api" - a test of the backend API of the same application (HTTP requests and their responses: status, fields, rules), without the user interface. Design a scenario at the layer that checks its rule most directly, and only at the layers you are asked for.

For each scenario, `instructions` is what a test automation agent will be told, so write concrete, self-contained steps ending with what must be verified: for "ui", the steps a user takes on the site; for "api", the requests (method, path, body) and the expected status and response fields. `preconditions` lists what must exist before the test - data, accounts with their roles, states of objects - taken from the application model when it has them; a scenario never silently relies on data it does not name. `gherkin` is the scenario in Given/When/Then form (just the Scenario block). List any assumption you had to make about unclear requirements. Write in the language of the requirements."""

PLAN_TASK = ("First step: plan the complete list of scenarios. For each give only the title, type, "
             "priority and a one-line note of what it covers. Order them by importance.")


def _context(requirements: str, url: str, cfg: dict, model: str = "") -> str:
    text = f"Requirements:\n{requirements}\n\n"
    if model:
        text += f"{model}\n\n"
    if url:
        text += f"Application URL: {url}\n"
    types = [t for t in cfg.get("types") or [] if t in TYPES]
    if types:
        text += ("Kinds of checks to cover (the scenario `type`); do not produce other types:\n"
                 + "".join(f"- {t}: {TYPES[t][1]}\n" for t in types))
    layers = [x for x in cfg.get("layers") or [] if x in LAYERS] or ["ui"]
    text += f"Layers: {', '.join(layers)}. Do not produce scenarios of other layers.\n"
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


async def _parse(cfg: dict, system: str, context: str, task: str, fmt: type[BaseModel], project_id: str = ""):
    # The requirements are the same in every request of one generation: they are cached separately.
    reply = await llm.parse(cfg, system=system, context=context, messages=[{"role": "user", "content": task}],
                            schema=fmt, max_tokens=16000, project_id=project_id, stage_name="scenarios")
    if reply.stop == "max_tokens":
        raise RuntimeError("Ответ модели не поместился в лимит: разделите требования на части.")
    if reply.stop == "refusal" or reply.parsed is None:
        raise RuntimeError("Модель не смогла составить сценарии по этим требованиям.")
    return reply.parsed


async def generate(requirements: str, url: str = "", project: dict | None = None,
                   log=None, progress=None, cfg: dict | None = None) -> ScenarioSet:
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
    context = _context(requirements, url, cfg, knowledge.prompt(project["id"]) if project else "")

    pid = project["id"] if project else ""
    if progress:
        say(f"Анализ требований ({len(requirements)} символов) и план покрытия…")
    plan: ScenarioPlan = await _parse(cfg, system, context, PLAN_TASK, ScenarioPlan, pid)
    if progress:
        progress({"type": "plan", "feature": plan.feature, "assumptions": plan.assumptions,
                  "scenarios": [s.model_dump() for s in plan.scenarios]})
    say(f"Сценариев в плане: {len(plan.scenarios)}, детализация…")
    if not plan.scenarios:
        return ScenarioSet(feature=plan.feature, assumptions=plan.assumptions, scenarios=[])

    listing = "\n".join(f"{i + 1}. [{s.layer}, {s.type}, {s.priority}] {s.title} — {s.covers}"
                        for i, s in enumerate(plan.scenarios))
    gate = asyncio.Semaphore(PARALLEL)

    async def detail(start: int, count: int, retry: bool = True) -> list[Scenario]:
        part = plan.scenarios[start:start + count]
        task = (f"The full plan of scenarios:\n{listing}\n\n"
                f"Write out in full only scenarios {start + 1}–{start + len(part)} of this plan, "
                "in the same order, keeping their titles, layers, types and priorities.")
        async with gate:
            if progress:
                say(f"Детализация сценариев {start + 1}–{start + len(part)}: {part[0].title}…")
            batch: ScenarioBatch = await _parse(cfg, system, context, task, ScenarioBatch, pid)
        # The plan is authoritative for what the scenario is; the batch adds the details.
        out = [full.model_copy(update={"title": planned.title, "type": planned.type, "layer": planned.layer,
                                       "priority": planned.priority})
               for planned, full in zip(part, batch.scenarios)]
        if progress and out:
            progress({"type": "batch", "start": start, "scenarios": [s.model_dump() for s in out]})
            say(f"Готовы сценарии {start + 1}–{start + len(out)}" + "".join(f"\n  ✓ {s.title}" for s in out))
        if len(out) < len(part) and retry:
            out += await detail(start + len(out), len(part) - len(out), retry=False)
        elif len(out) < len(part):
            say(f"Не удалось детализировать сценариев: {len(part) - len(out)}")
        return out

    batches = await asyncio.gather(*(detail(i, BATCH) for i in range(0, len(plan.scenarios), BATCH)))
    return ScenarioSet(feature=plan.feature, assumptions=plan.assumptions,
                       scenarios=[s for b in batches for s in b])
