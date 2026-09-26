"""Requirements -> test scenarios (the article's "data interpretation" and
"scenario formulation" stages): a user story, Jira ticket or spec goes in,
a prioritized list of positive, negative and edge-case scenarios comes out.
Each scenario can then be sent to the Studio for generation.

There is no cap on the number of scenarios: coverage of the requirements decides
it. To keep that independent of the output limit of one response, generation has
two phases: a compact plan of every scenario (title, type, priority, what it
covers), then the full scenarios, detailed in parallel batches.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from . import llm, skills

ScenarioType = Literal["positive", "negative", "edge", "boundary", "accessibility", "security"]
Priority = Literal["high", "medium", "low"]

BATCH = 6          # scenarios detailed per request
PARALLEL = 4       # detail requests in flight


class Scenario(BaseModel):
    title: str
    type: ScenarioType
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
    priority: Priority
    covers: str              # which requirement / rule this checks, one line


class ScenarioPlan(BaseModel):
    feature: str
    assumptions: list[str]
    scenarios: list[PlannedScenario]


class ScenarioBatch(BaseModel):
    scenarios: list[Scenario]


SYSTEM = """You are a senior QA analyst. From the requirements you are given, design a set of UI test scenarios with good coverage: the main happy paths, negative cases (invalid input, errors), edge and boundary cases. Do not pad the list with near-duplicates; each scenario must test something distinct. There is no limit on the number of scenarios: include every scenario the requirements call for, and no more.

For each scenario, `instructions` is what a browser automation agent will be told, so write it as concrete, self-contained steps a user would take on the site, ending with what must be verified. `gherkin` is the scenario in Given/When/Then form (just the Scenario block). List any assumption you had to make about unclear requirements. Write in the language of the requirements."""

PLAN_TASK = ("First step: plan the complete list of scenarios. For each give only the title, type, "
             "priority and a one-line note of what it covers. Order them by importance.")


def _context(requirements: str, url: str, cfg: dict) -> str:
    text = f"Requirements:\n{requirements}\n\n"
    if url:
        text += f"Application URL: {url}\n"
    if cfg.get("types"):
        text += f"Scenario types to cover: {', '.join(cfg['types'])}. Do not produce other types.\n"
    return text


async def _parse(cfg: dict, system: str, context: str, task: str, fmt: type[BaseModel]):
    resp = await llm.client().beta.messages.parse(
        **llm.common_params(cfg),
        max_tokens=16000,
        betas=[llm.FALLBACK_BETA],
        # The requirements are the same in every request of one generation: cache them.
        system=[{"type": "text", "text": system},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": task}],
        output_format=fmt,
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Ответ модели не поместился в лимит: разделите требования на части.")
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError("Модель не смогла составить сценарии по этим требованиям.")
    return resp.parsed_output


async def generate(requirements: str, url: str = "", project: dict | None = None,
                   log=None) -> ScenarioSet:
    """`project` (optional) supplies the "scenarios" stage settings: skills, scenario
    types to cover, model and effort. `log(text)` (optional) reports progress."""
    cfg = project["pipeline"]["scenarios"] if project else {}
    system = SYSTEM + (skills.prompt(project["id"], cfg.get("skills", [])) if project else "")
    context = _context(requirements, url, cfg)

    plan: ScenarioPlan = await _parse(cfg, system, context, PLAN_TASK, ScenarioPlan)
    if log:
        log(f"Сценариев в плане: {len(plan.scenarios)}, детализация…")
    if not plan.scenarios:
        return ScenarioSet(feature=plan.feature, assumptions=plan.assumptions, scenarios=[])

    listing = "\n".join(f"{i + 1}. [{s.type}, {s.priority}] {s.title} — {s.covers}"
                        for i, s in enumerate(plan.scenarios))
    gate = asyncio.Semaphore(PARALLEL)

    async def detail(start: int, count: int, retry: bool = True) -> list[Scenario]:
        part = plan.scenarios[start:start + count]
        task = (f"The full plan of scenarios:\n{listing}\n\n"
                f"Write out in full only scenarios {start + 1}–{start + len(part)} of this plan, "
                "in the same order, keeping their titles, types and priorities.")
        async with gate:
            batch: ScenarioBatch = await _parse(cfg, system, context, task, ScenarioBatch)
        # The plan is authoritative for what the scenario is; the batch adds the details.
        out = [full.model_copy(update={"title": planned.title, "type": planned.type,
                                       "priority": planned.priority})
               for planned, full in zip(part, batch.scenarios)]
        if len(out) < len(part) and retry:
            out += await detail(start + len(out), len(part) - len(out), retry=False)
        elif len(out) < len(part) and log:
            log(f"Не удалось детализировать сценариев: {len(part) - len(out)}")
        return out

    batches = await asyncio.gather(*(detail(i, BATCH) for i in range(0, len(plan.scenarios), BATCH)))
    return ScenarioSet(feature=plan.feature, assumptions=plan.assumptions,
                       scenarios=[s for b in batches for s in b])
