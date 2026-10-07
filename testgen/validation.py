"""Validation of a specification (ТЗ, SRS, user story) against the requirements for software
documentation: the sections the chosen standard asks for (catalog.STANDARDS: ГОСТ 34.602-2020,
ГОСТ 19.201-78, ISO/IEC/IEEE 29148, user stories) and the quality of every requirement (complete,
unambiguous, consistent, verifiable...), plus the team's own checklist ("requirements.checklist").

The result is a report for people: a score, a verdict, the sections found or missing, findings
with where they are and how to fix them, and questions to the authors. It never blocks the
generation of scenarios - a person decides.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from . import llm, projects, skills
from .catalog import QUALITY, STANDARDS


class SectionCheck(BaseModel):
    section: str
    status: Literal["present", "partial", "missing"]
    comment: str


class Finding(BaseModel):
    criterion: str           # a key of QUALITY, "section" or "checklist"
    severity: Literal["critical", "major", "minor"]
    location: str            # where: a section, a requirement number, a quote
    problem: str
    suggestion: str


class Validation(BaseModel):
    summary: str
    score: int               # 0..100
    verdict: Literal["ready", "needs_work", "not_ready"]
    sections: list[SectionCheck]
    findings: list[Finding]
    questions: list[str]


SYSTEM = """You are a lead systems analyst who reviews specifications before test design and development. Check the document against the documentation standard and the quality criteria you are given.

1. Sections: for every section the standard requires, say whether the document has it (present), has it only in part (partial) or lacks it (missing), with a short comment. Judge by content, not by headings: a section may be named differently.
2. Quality of requirements: find the requirements that are incomplete, ambiguous (vague words: "fast", "convenient", "etc."), contradictory, not verifiable (no measurable criterion or expected result), not identified/traceable, infeasible or not atomic. For each finding give the criterion key, the severity (critical: blocks development or testing; major: will cause defects or rework; minor: style), where it is (section, requirement number or a short quote), the problem and a concrete fix.
3. The team's checklist, when given: each rule it breaks is a finding with criterion "checklist".
4. Questions: what the authors must answer before the requirements can be tested.
5. score: 0-100 for how ready the document is for test design; verdict: ready (only minor findings), needs_work, not_ready (critical findings or key sections missing).

Do not invent content of the document; quote it. Write in the language of the document."""


def _task(standard: str, checklist: str) -> str:
    title, sections = STANDARDS.get(standard) or STANDARDS["gost34"]
    text = f"Standard: {title}\nRequired sections:\n" + "".join(f"- {s}\n" for s in sections)
    text += "Quality criteria (key: meaning):\n" + "".join(f"- {k}: {v[1]}\n" for k, v in QUALITY.items())
    rules = [r.strip() for r in (checklist or "").splitlines() if r.strip()]
    if rules:
        text += "The team's checklist:\n" + "".join(f"- {r}\n" for r in rules)
    return text + "\nValidate the document."


async def validate(project: dict, document: str, standard: str = "") -> dict:
    """The report on `document` -> Validation as a dict, with the standard it was checked against."""
    cfg = project["pipeline"]["requirements"]
    standard = standard if standard in STANDARDS else cfg.get("standard") or "gost34"
    system = SYSTEM + projects.language_rule(project) + skills.prompt(project["id"], cfg.get("skills", []))
    reply = await llm.parse(cfg, system=system, context=f"The document:\n{document[:150_000]}",
                            messages=[{"role": "user", "content": _task(standard, cfg.get("checklist", ""))}],
                            schema=Validation, max_tokens=16000, project_id=project["id"], stage_name="requirements")
    if reply.stop == "max_tokens":
        raise RuntimeError("Отчёт не поместился в лимит ответа: проверьте документ по частям.")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла проверить документ.")
    out = reply.parsed.model_dump()
    out["score"] = max(0, min(100, out["score"]))
    return out | {"standard": standard, "standard_title": STANDARDS[standard][0]}
