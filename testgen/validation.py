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
from .explorer import MAP_TITLE


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
    document_kind: str = ""          # what the document is: a technical spec of a module, user stories, an API...
    standard_fits: bool = True       # the standard is meant for this kind of document
    suggested_standard: str = ""     # a key of STANDARDS that fits it better ("" - none)
    score: int               # 0..100
    verdict: Literal["ready", "needs_work", "not_ready"]
    sections: list[SectionCheck]
    findings: list[Finding]
    questions: list[str]


SYSTEM = """You are a lead systems analyst who reviews specifications before test design and development. Check the document against the documentation standard and the quality criteria you are given.

0. The kind of document: say what it is (document_kind, a few words: a technical specification of a module, user stories, an API description...). standard_fits - whether the standard is meant for such a document; when it is not, suggested_standard - the key of the standard below that fits it better ("" when none does), and a section of the standard the document lacks is not a defect: report it in `sections` only, not as a finding.
1. Sections: for every section the standard requires, say whether the document has it (present), has it only in part (partial) or lacks it (missing), with a short comment. Judge by content, not by headings: a section may be named differently.
2. Quality of requirements: find the requirements that are incomplete, ambiguous (vague words: "fast", "convenient", "etc."), contradictory, not verifiable (no measurable criterion or expected result), not identified/traceable, infeasible or not atomic. For each finding give the criterion key, the severity (critical: blocks development or testing; major: will cause defects or rework; minor: style), where it is (section, requirement number or a short quote), the problem and a concrete fix.
3. The team's checklist, when given: each rule it breaks is a finding with criterion "checklist".
4. Questions: what the authors must answer before the requirements can be tested.
5. score: 0-100 for how ready the document is for test design; verdict: ready (only minor findings), needs_work, not_ready (critical findings or key sections missing).

Do not invent content of the document; quote it. Write in the language of the document."""

# The Planner's map of the site (explorer.to_requirements) is not written by people.
SITE_MAP = """
The document is a map of the user interface recorded by an automatic crawl (Planner), not a specification written by people. It holds only what the pages declare: fields with their type, required flag, length, format, range, hints and the error texts already present in the markup. What the pages do not declare (server-side rules, error texts shown only after a submission, where the user is taken) cannot be in it and is not an omission of authors: report each such gap once for all the forms it concerns, with severity major at most, and put what must be learned into the questions (to the product team). Field names are the labels of the pages."""


def _task(standard: str, checklist: str, site_map: bool = False) -> str:
    title, sections = STANDARDS.get(standard) or STANDARDS["gost34"]
    text = f"Standard: {title}\nRequired sections:\n" + "".join(f"- {s}\n" for s in sections)
    text += "Other standards (key: title): " + "; ".join(f"{k}: {t}" for k, (t, _) in STANDARDS.items() if k != standard) + "\n"
    text += "Quality criteria (key: meaning):\n" + "".join(f"- {k}: {v[1]}\n" for k, v in QUALITY.items())
    rules = [r.strip() for r in (checklist or "").splitlines() if r.strip()]
    if rules:
        text += "The team's checklist:\n" + "".join(f"- {r}\n" for r in rules)
    return text + (SITE_MAP if site_map else "") + "\nValidate the document."


async def validate(project: dict, document: str, standard: str = "", notes: str = "") -> dict:
    """The report on `document` -> Validation as a dict, with the standard it was checked against."""
    cfg = project["pipeline"]["requirements"]
    standard = standard if standard in STANDARDS else cfg.get("standard") or "gost34"
    system = SYSTEM + projects.language_rule(project) + skills.prompt(project["id"], cfg.get("skills", []))
    task = _task(standard, cfg.get("checklist", ""), document.lstrip().startswith(MAP_TITLE)) + notes
    reply = await llm.parse(cfg, system=system, context=f"The document:\n{document[:150_000]}",
                            messages=[{"role": "user", "content": task}],
                            schema=Validation, max_tokens=16000, project_id=project["id"], stage_name="requirements")
    if reply.stop == "max_tokens":
        raise RuntimeError("Отчёт не поместился в лимит ответа: проверьте документ по частям.")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла проверить документ.")
    out = reply.parsed.model_dump()
    out["score"] = max(0, min(100, out["score"]))
    if out["suggested_standard"] not in STANDARDS or out["suggested_standard"] == standard:
        out["suggested_standard"] = ""
    if not out["standard_fits"]:        # a section the document was never meant to have is not its defect
        out["findings"] = [f for f in out["findings"] if f["criterion"] != "section"]
    return out | {"standard": standard, "standard_title": STANDARDS[standard][0]}
