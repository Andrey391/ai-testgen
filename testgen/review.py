"""The reviewer of requirements: a subagent that checks a specification (ТЗ, SRS, user stories) before any
test is designed from it - the first job of the Requirements tab, and the first stage of the pipeline.

It works outside the common context: every check starts a clean conversation of its own with the document,
the documentation standard and the lifecycle of the system (the application model, knowledge.py) - nothing of
the Studio sessions, the scenarios or the other stages. Two checks run side by side:

- defects of the document (validation.validate): the sections the standard asks for, the quality of every
  requirement, the team's checklist, questions to the authors;
- the lifecycle (lifecycle()): where the document disagrees with the lifecycle of the system - statuses and
  transitions the model does not allow, dependencies it ignores, roles doing what their restrictions forbid -
  and where the lifecycle the document itself describes is broken (a status with no way out, no one to make
  a transition, no rule for dependent entities). The model is given as the slice the document names
  (knowledge.prompt); a bigger model is looked up by the subagent with `model_lookup`.

The report (review()) is kept with the analysis; a person reads it, and the scenarios and tests are designed
in the pipeline from the reviewed requirements. It never changes the application model itself.

A person works through the findings (items()): answers how it should be (or takes the reviewer's suggestion)
or marks a finding as not relevant; revise() - an editor on a clean context applies the answered findings to
the document as edits of exact fragments. The application model learns from the requirements only when every
finding is fixed or marked not relevant (resolved()): the lifecycle of the system does not take in what the
authors have not settled. Reviewed again, the reviewer is told what the authors decided (notes()).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Literal

from pydantic import BaseModel, ValidationError

from . import knowledge, llm, projects, skills, validation
from .providers.base import check_call

MAX_TURNS = 8           # lookups of the model and the report; the last turn may only report
KINDS = ("contradiction", "role", "gap", "new")
SEVERITIES = ("critical", "major", "minor")


class Conflict(BaseModel):
    kind: Literal["contradiction", "role", "gap", "new"]
    severity: Literal["critical", "major", "minor"]
    location: str            # where in the document: a section, a requirement number, a quote
    model_rule: str          # the rule of the model it disagrees with (a quote); "" - the model says nothing
    problem: str
    suggestion: str


class LifecycleReport(BaseModel):
    summary: str
    conflicts: list[Conflict]
    questions: list[str]


LIFECYCLE = """You are an independent reviewer of requirements. You work alone, on a clean context: the specification and the model of the application under test - its lifecycle: entities, what each depends on, their statuses and transitions, who makes them, rules; roles with their capabilities and restrictions. Before any test is designed, find where the specification and the lifecycle of the system disagree, and where the lifecycle the specification itself describes is broken. Each finding has a kind:

- contradiction: the document requires a status, a transition, an order of actions or a dependency the model does not allow (or forbids one the model has): editing an order after it is shipped, creating an entity without the one it depends on, a status the lifecycle has no way into;
- role: the document lets a role do what its restrictions forbid or what is not among its capabilities, or gives one action to different roles in different places;
- gap: the lifecycle the document describes is incomplete or inconsistent: an entity with statuses but no transitions, a status with no way in or out, no final status, no one who makes a transition, a transition with no condition, no rule for the dependent entities when the one they depend on is cancelled or deleted, two requirements that put an entity in different statuses after the same action;
- new: an entity, status, transition or role the document introduces that the model does not know - not a defect, the model will learn it; report only the ones that change the lifecycle.

`location` quotes the document (a section, a requirement number or a short quote); `model_rule` quotes the model (empty when the model says nothing on it). severity: critical - blocks development or testing, a scenario of the document cannot be carried out; major - will cause defects or rework; minor - a detail. Do not invent rules the model does not state and do not report style or wording of the document: another reviewer checks those. `questions` - what the authors must answer about the lifecycle before tests can be designed. With an empty model, check the lifecycle the document describes (gap) and list what it introduces (new).

When a part of the model you need is not shown, ask for it with model_lookup (an entity or role name). Finish with the `report` tool."""

LOOKUP_TOOL = {
    "name": "model_lookup",
    "description": "The part of the application model a query names: entities with their lifecycle, dependencies "
                   "and rules, roles with capabilities and restrictions. Use it for what the model above does not show.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "entity or role names"}},
                     "required": ["query"]},
}

_STR = {"type": "string"}
REPORT_TOOL = {
    "name": "report",
    "description": "The result of the review: call once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "two or three sentences: how the document fits the lifecycle"},
            "conflicts": {"type": "array", "items": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                               "severity": {"type": "string", "enum": list(SEVERITIES)},
                               "location": _STR, "model_rule": _STR, "problem": _STR, "suggestion": _STR},
                "required": ["kind", "severity", "location", "model_rule", "problem", "suggestion"]}},
            "questions": {"type": "array", "items": _STR},
        },
        "required": ["summary", "conflicts", "questions"],
    },
}


def _model_state(doc: dict) -> dict:
    c = knowledge.confirmation(doc)
    return {"entities": len(doc["entities"]), "roles": len(doc["roles"]), "state": c["state"]}


async def lifecycle(project: dict, document: str, log=None, notes: str = "") -> dict:
    """The document against the lifecycle of the system: LifecycleReport as a dict, with the model it was
    checked against (how many entities and roles, confirmed or not)."""
    pid = project["id"]
    cfg = project["pipeline"]["requirements"]
    doc = knowledge.get(pid)
    model = knowledge.prompt(pid, knowledge.focus(text=document))
    state = _model_state(doc)
    tools = [REPORT_TOOL] + ([LOOKUP_TOOL] if model and not knowledge.fits(pid) else [])
    head = ("The application model is empty: check the lifecycle the document describes.\n" if not model else
            f"The application model ({'confirmed by a person' if state['state'] == 'confirmed' else 'not confirmed yet'}):\n"
            f"{model}\n")
    system = LIFECYCLE + projects.language_rule(project) + skills.prompt(pid, cfg.get("skills", []))
    messages = [{"role": "user", "content": "Review the document against the lifecycle of the system." + notes}]
    for turn in range(MAX_TURNS):
        offer = [REPORT_TOOL] if turn == MAX_TURNS - 1 else tools
        reply = await llm.chat(cfg, system=system, context=f"{head}\nThe document:\n{document[:150_000]}",
                               messages=messages, tools=offer, max_tokens=16000, project_id=pid,
                               stage_name="requirements")
        messages.append({"role": "assistant", "content": reply.content})
        if reply.stop == "refusal":
            raise RuntimeError("Модель отказалась проверять документ")
        calls = reply.tool_calls
        if not calls:
            if reply.stop == "max_tokens":
                raise RuntimeError("Отчёт не поместился в лимит ответа: проверьте документ по частям.")
            messages.append({"role": "user", "content": "Finish with the report tool."})
            continue
        results = []
        for c in calls:
            problem = check_call(c, offer)
            if not problem and c["name"] == "report":
                try:
                    out = LifecycleReport.model_validate(c["input"]).model_dump()
                except ValidationError as e:
                    problem = f"The report does not fit its schema: {e.errors()[:3]}. Call report again."
                else:
                    out["conflicts"] = [x for x in out["conflicts"] if x["problem"].strip()]
                    return out | {"model": state}
            if problem:
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": problem, "is_error": True})
                continue
            query = str(c["input"].get("query") or "")[:300]
            if log:
                log(f"Ревьюер смотрит модель приложения: «{query}»")
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": knowledge.lookup(pid, query)})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("Проверка по жизненному циклу не завершилась за отведённое число шагов")


def verdict(report: dict) -> str:
    """ready | needs_work | not_ready of the two checks together: the worse of them."""
    order = ("ready", "needs_work", "not_ready")
    worst = (report.get("validation") or {}).get("verdict") or "ready"
    conflicts = [c for c in (report.get("lifecycle") or {}).get("conflicts") or [] if c["kind"] != "new"]
    if any(c["severity"] == "critical" for c in conflicts):
        worst = "not_ready"
    elif any(c["severity"] == "major" for c in conflicts) and worst == "ready":
        worst = "needs_work"
    return worst if worst in order else "needs_work"


def summary(report: dict) -> str:
    """One line for logs: the score and the findings of both checks."""
    v, lc = report.get("validation"), report.get("lifecycle")
    parts = []
    if v:
        parts.append(f"стандарт и качество {v['score']}/100, замечаний {len(v['findings'])}")
    if lc:
        bad = [c for c in lc["conflicts"] if c["kind"] != "new"]
        parts.append(f"жизненный цикл: расхождений {len(bad)}"
                     + (f", критичных {sum(c['severity'] == 'critical' for c in bad)}" if bad else ""))
    parts += [f"{k}: не удалось — {e}" for k, e in (report.get("errors") or {}).items()]
    return "; ".join(parts)


async def review(project: dict, document: str, standard: str = "", log=None, notes: str = "") -> dict:
    """Both checks of the subagent at once. {validation, lifecycle, errors, verdict, at}: a check that failed
    is in `errors` (by name) and the other one is kept; both failed - the error is raised. notes - what the
    authors decided on the findings of the previous review of the document (notes())."""
    say = log or (lambda text: None)
    say("Ревьюер требований: проверка документа по стандарту и сверка с жизненным циклом системы…")
    v, lc = await asyncio.gather(validation.validate(project, document, standard, notes),
                                 lifecycle(project, document, log=log, notes=notes), return_exceptions=True)
    for e in (v, lc):
        if isinstance(e, asyncio.CancelledError):
            raise e
    if isinstance(v, BaseException) and isinstance(lc, BaseException):
        raise v
    errors = {name: llm.api_error_text(e) if isinstance(e, Exception) else str(e)
              for name, e in (("документ", v), ("жизненный цикл", lc)) if isinstance(e, BaseException)}
    out = {"validation": None if isinstance(v, BaseException) else v,
           "lifecycle": None if isinstance(lc, BaseException) else lc, "errors": errors, "at": time.time()}
    out["verdict"] = verdict(out)
    say(f"Ревьюер требований: {summary(out)}")
    return out


def blocking(report: dict | None) -> bool:
    """The review found what blocks test design: critical findings or the document is not ready."""
    return bool(report) and report.get("verdict") == "not_ready"


# ---------- a person works through the findings ----------

OPEN, TODO, FIXED, DISMISSED = "open", "todo", "fixed", "dismissed"


def items(report: dict | None) -> list[dict]:
    """The findings a person has to settle, each with a stable id within the report: defects of the document
    (d<i>), conflicts with the lifecycle (l<i>; "new" ones are not defects and are left out) and the questions
    to the authors (vq<i>, lq<i>). With the person's decision: status, answer, note."""
    if not report:
        return []
    out = []
    v, lc = report.get("validation") or {}, report.get("lifecycle") or {}
    for i, f in enumerate(v.get("findings") or []):
        out.append({"id": f"d{i}", "kind": "defect", "severity": f["severity"], "location": f["location"],
                    "problem": f["problem"], "suggestion": f["suggestion"]})
    for i, c in enumerate(lc.get("conflicts") or []):
        if c["kind"] != "new":
            out.append({"id": f"l{i}", "kind": "lifecycle", "severity": c["severity"], "location": c["location"],
                        "problem": c["problem"], "suggestion": c["suggestion"], "model_rule": c.get("model_rule", "")})
    for prefix, qs in (("vq", v.get("questions")), ("lq", lc.get("questions"))):
        for i, q in enumerate(qs or []):
            out.append({"id": f"{prefix}{i}", "kind": "question", "severity": "", "location": "", "problem": q,
                        "suggestion": ""})
    decided = report.get("resolutions") or {}
    return [it | {"status": OPEN, "answer": "", "note": ""} | decided.get(it["id"], {}) for it in out]


def unresolved(report: dict | None) -> list[dict]:
    """Findings neither fixed in the document nor marked not relevant."""
    return [it for it in items(report) if it["status"] not in (FIXED, DISMISSED)]


def resolved(report: dict | None) -> bool:
    """Every finding of the review is settled: the application model may learn from the requirements."""
    return not unresolved(report)


def decide(report: dict, ids: list[str], status: str, answer: str | None = None, note: str | None = None,
           user: str = "") -> dict:
    """A person's decision on findings: todo - fix the document (by the answer; empty - by the reviewer's
    suggestion; a question needs an answer), dismissed - not relevant (note - why), open - undecided again.
    Changes `report` in place."""
    if status not in (OPEN, TODO, DISMISSED):
        raise ValueError("Решение по замечанию: open, todo или dismissed")
    known = {it["id"]: it for it in items(report)}
    missing = [i for i in ids if i not in known]
    if missing or not ids:
        raise KeyError(", ".join(missing))
    res = report.setdefault("resolutions", {})
    for i in ids:
        prev = res.get(i, {})
        ans = (prev.get("answer", "") if answer is None else answer).strip()[:4000]
        if status == TODO and known[i]["kind"] == "question" and not ans:
            raise ValueError("На вопрос нужен ответ: по нему агент исправит ТЗ")
        if status == OPEN:
            res.pop(i, None)
            continue
        res[i] = {"status": status, "answer": ans,
                  "note": (prev.get("note", "") if note is None else note).strip()[:2000], "by": user, "at": time.time()}
    return report


def fixed(report: dict, ids: list[str], user: str = "") -> dict:
    """The editor applied these findings to the document (revise()). Changes `report` in place."""
    res = report.setdefault("resolutions", {})
    for i in ids:
        res[i] = res.get(i, {}) | {"status": FIXED, "fixed_by": user, "fixed_at": time.time()}
    return report


def notes(report: dict | None) -> str:
    """For the next review of the same requirements: what the authors decided. Findings marked not relevant are
    not to be reported again; the answers are what the document now says."""
    lines = []
    for it in items(report):
        if it["status"] == DISMISSED:
            lines.append(f"- not relevant, do not report it again: {it['problem']}"
                         + (f" (why: {it['note']})" if it["note"] else ""))
        elif it["status"] == FIXED and it["answer"]:
            lines.append(f"- settled by the authors: {it['problem']} -> {it['answer']}")
    return ("\n\nDecisions of the authors on the findings of the previous review:\n" + "\n".join(lines)) if lines else ""


class Edit(BaseModel):
    item: str                # the id of the finding the edit settles
    find: str                # an exact fragment of the document
    replace: str             # what it becomes


class Revision(BaseModel):
    summary: str
    edits: list[Edit]


REVISE = """You are the editor of a specification (ТЗ, SRS, user stories). A reviewer found defects in it and the authors answered how it should be. Apply each finding you are given to the document as edits.

Every edit replaces an exact fragment of the document: `find` - a fragment copied verbatim from the document, long enough to occur once (a sentence, a list item, a requirement); `replace` - the new text of that fragment. To add text, find the fragment it goes after and replace it with that fragment followed by the new text. `item` - the id of the finding the edit settles; a finding may need several edits.

The answer of the authors is how it must be: follow it over the reviewer's suggestion; with no answer, apply the suggestion. Change only what the findings need; keep the numbering, the language and the style of the document; do not invent requirements beyond the answers. Every finding gets at least one edit. `summary` - one or two sentences on what changed."""

ATTEMPTS = 2        # a finding whose fragment was not found is asked once more


def _locate(document: str, fragment: str) -> tuple[int, int] | None:
    """Where the fragment is: verbatim, or with any whitespace between its words (line breaks of the model)."""
    if not fragment.strip():
        return None
    i = document.find(fragment)
    if i >= 0:
        return i, i + len(fragment)
    m = re.search(r"\s+".join(map(re.escape, fragment.split())), document)
    return (m.start(), m.end()) if m else None


def _revise_task(todo: list[dict]) -> str:
    out = []
    for it in todo:
        line = f"[{it['id']}] {it['problem']}"
        if it.get("location"):
            line += f"\n  where: {it['location']}"
        if it.get("model_rule"):
            line += f"\n  the lifecycle of the system says: {it['model_rule']}"
        if it.get("suggestion"):
            line += f"\n  the reviewer suggests: {it['suggestion']}"
        line += f"\n  the answer of the authors: {it['answer'] or 'none - apply the suggestion'}"
        out.append(line)
    return "Apply these findings to the document:\n\n" + "\n\n".join(out)


async def revise(project: dict, document: str, report: dict) -> dict:
    """The findings a person sent to be fixed (status todo) applied to the document by an editor on a clean
    context. {requirements: the new text, fixed: [ids], failed: {id: why}, edits: [{item, find, replace}],
    summary}."""
    todo = [it for it in items(report) if it["status"] == TODO]
    if not todo:
        raise ValueError("Нет замечаний для исправления: ответьте на замечания или примите предложения ревьюера")
    cfg = project["pipeline"]["requirements"]
    system = REVISE + projects.language_rule(project)
    doc, edits, summaries, failed, pending = document, [], [], {}, todo
    for _ in range(ATTEMPTS):
        reply = await llm.parse(cfg, system=system, context=f"The document:\n{doc[:150_000]}",
                                messages=[{"role": "user", "content": _revise_task(pending)}], schema=Revision,
                                max_tokens=16000, project_id=project["id"], stage_name="requirements")
        if reply.parsed is None:
            raise RuntimeError("Модель не смогла исправить документ")
        summaries.append(reply.parsed.summary.strip())
        asked, done = {it["id"] for it in pending}, set()
        for e in reply.parsed.edits:
            if e.item not in asked:
                continue
            at = _locate(doc, e.find)
            if not at:
                failed[e.item] = f"не найден фрагмент «{e.find[:120]}»"
                continue
            doc = doc[:at[0]] + e.replace + doc[at[1]:]
            edits.append(e.model_dump())
            done.add(e.item)
        for i in asked:
            if i in done:
                failed.pop(i, None)
            else:
                failed.setdefault(i, "агент не предложил правку")
        pending = [it for it in pending if it["id"] in failed]
        if not pending:
            break
    return {"requirements": doc, "fixed": [it["id"] for it in todo if it["id"] not in failed], "failed": failed,
            "edits": edits, "summary": " ".join(s for s in summaries if s)}
