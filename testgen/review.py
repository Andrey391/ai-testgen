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
in the pipeline from the reviewed requirements. It never changes the application model itself. A check is
kept as soon as it is finished (`keep`): a review cut off by a restart of the studio asks only the other one again.

A person works through the findings (items()): answers how it should be (or takes the reviewer's suggestion)
or marks a finding as not relevant; revise() - an editor on a clean context applies the answered findings to
the document as edits of exact fragments, a theme per request (FIX_BATCH), FIX_PARALLEL requests at a time, each
batch kept as it is done.
A finding either blocks test design (a question, a defect of the content) or is the quality of the document (its form, minor findings: worth fixing, not required). The findings
of both checks on the same thing are grouped into themes (group()) and settled with one answer; the answers of an
analyst pasted as one text are matched to them by a model (distribute(), up to ANSWERS_MAX characters, the themes
in batches). Each theme has a number a person and an analyst answer by (`no`: the document's 1, 2…, the lifecycle's
Ж1, Ж2…). The application model learns from the requirements only when every finding that blocks test design is
fixed or marked not relevant (resolved()): the lifecycle of the system does not take in what the authors have not
settled. Reviewed again - the same text or a new version the analyst sent - the reviewer is told what the authors
decided and answered (notes()).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, ValidationError

from . import answers, knowledge, llm, projects, skills, validation
from .providers.base import check_call

_log = logging.getLogger("testgen.review")

MAX_TURNS = 8           # lookups of the model and the report; the last turn may only report
WAIT = 300              # seconds of silence in a streamed answer of the reviewer before it is given up (a gateway
                        # that waits for a whole answer of a model thinking for minutes cuts it off: 504)
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

When a part of the model you need is not shown, ask for it with model_lookup (an entity or role name). Finish with the `report` tool. Write in the language of the document."""

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
    if not document.strip():
        raise ValueError("Нет текста требований для сверки с жизненным циклом")
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
    # The document goes with the task, not as a second block of the system prompt: a gateway that keeps only
    # the first block of it (with tools) left the reviewer with no document - it answered "provide the document".
    messages = [{"role": "user", "content": f"The document:\n{document[:150_000]}\n\n"
                                            "Review the document against the lifecycle of the system." + notes}]
    for turn in range(MAX_TURNS):
        offer = [REPORT_TOOL] if turn == MAX_TURNS - 1 else tools
        reply = await llm.chat(cfg, system=system, context=head, messages=messages, tools=offer, max_tokens=16000,
                               project_id=pid, stage_name="requirements", stream=True, timeout=WAIT, cache_all=True)
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
    """ready | needs_work | not_ready of the two checks together: the worse of them, by what blocks test design
    (items()). Not ready only for a critical finding that blocks it: the form of the document (numbering, sections
    of the standard) makes it need work at most."""
    its = [it for it in items(report) if it["blocking"]]
    if any(it["severity"] == "critical" for it in its):
        return "not_ready"
    worst = (report.get("validation") or {}).get("verdict") or "ready"
    if worst == "not_ready" or any(it["kind"] == "lifecycle" and it["severity"] == "major" for it in its):
        worst = "needs_work"
    return worst if worst in ("ready", "needs_work") else "needs_work"


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


def tally(report: dict | None) -> dict:
    """How the findings are settled by now (themes()): settled - both checks finished and nothing that blocks test
    design is left (the quality of the document may be); counts of the themes fixed, not relevant, waiting for the
    editor, open and the blocking ones left."""
    ts = themes(report)
    left = [t for t in ts if t["blocking"] and t["status"] in (OPEN, TODO)]
    return {"settled": bool(report) and not report.get("errors") and not left, "themes": len(ts),
            "fixed": sum(t["status"] == FIXED for t in ts), "dismissed": sum(t["status"] == DISMISSED for t in ts),
            "todo": sum(t["status"] == TODO for t in ts), "open": sum(t["status"] == OPEN for t in ts),
            "blocking_left": len(left)}


def settled_summary(report: dict) -> str:
    """One line for logs: how the findings were settled."""
    t = tally(report)
    if not t["themes"]:
        return "замечаний нет"
    rest = t["open"] + t["todo"]
    return (f"замечаний {t['themes']}: исправлено в ТЗ {t['fixed']}, не актуально {t['dismissed']}"
            + (f", не обязательных не разобрано {rest}" if rest else ""))


CHECKS = ("validation", "lifecycle")


def parts_key(project: dict, document: str, standard: str = "", notes: str = "") -> str:
    """What both checks depend on: a check finished by a cut-off review is reused only for the same document,
    standard, notes and settings of the stage (analyses.review_parts)."""
    from . import analyses
    cfg = json.dumps(project["pipeline"]["requirements"], sort_keys=True, ensure_ascii=False, default=str)
    return analyses.parts_key(document, standard, notes, cfg)


async def review(project: dict, document: str, standard: str = "", log=None, notes: str = "",
                 done: dict | None = None, keep=None) -> dict:
    """Both checks of the subagent at once. {validation, lifecycle, errors, verdict, at}: a check that failed
    is in `errors` (by name) and the other one is kept; both failed - the error is raised. notes - what the
    authors decided on the findings of the previous review of the document (notes()). done - the checks a
    cut-off review of the same finished ({name: result}, CHECKS): they are not asked again; keep(name, result) -
    a check is finished (kept to go on from if this review is cut off)."""
    say = log or (lambda text: None)
    done = {k: v for k, v in (done or {}).items() if k in CHECKS and v}
    # what the team has answered before (answers.py): facts, not questions to ask again
    notes += answers.prompt(project["id"], document)

    async def check(name: str, work):
        if name in done:
            return done[name]
        out = await work()
        if keep:
            keep(name, out)
        return out

    if done:
        say("Ревьюер требований: продолжаю прерванную проверку, уже готово и не повторяется: "
            + ", ".join({"validation": "проверка документа по стандарту", "lifecycle": "сверка с жизненным циклом"}[k]
                        for k in CHECKS if k in done))
    else:
        say("Ревьюер требований: проверка документа по стандарту и сверка с жизненным циклом системы…")
    v, lc = await asyncio.gather(
        check("validation", lambda: validation.validate(project, document, standard, notes)),
        check("lifecycle", lambda: lifecycle(project, document, log=log, notes=notes)), return_exceptions=True)
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
    try:
        out["groups"] = await group(project, out)
    except asyncio.CancelledError:
        raise
    except Exception as e:      # the findings stay one by one
        say(f"Замечания не сведены в темы: {llm.api_error_text(e)}")
    if out.get("groups"):
        say(f"Замечания сведены в темы: {len(themes(out))} вместо {len(items(out))}")
    return out


def with_lifecycle(report: dict, lc: dict) -> dict:
    """The report with the check against the lifecycle made again (it failed, or the model changed): the check of
    the document and the decisions on it stay; the decisions on the earlier conflicts and questions of the
    lifecycle (l<i>, lq<i>) are about other findings now and go, with them in themes."""
    out = dict(report)
    out["lifecycle"] = lc
    out["errors"] = {k: v for k, v in (report.get("errors") or {}).items() if k != "жизненный цикл"}
    out["resolutions"] = {k: v for k, v in (report.get("resolutions") or {}).items() if not k.startswith("l")}
    groups = [g | {"ids": [i for i in g["ids"] if not i.startswith("l")]} for g in report.get("groups") or []]
    out["groups"] = [g for g in groups if len(g["ids"]) > 1]
    out["verdict"] = verdict(out)
    return out


def blocking(report: dict | None) -> bool:
    """The review found what blocks test design: critical findings or the document is not ready."""
    return bool(report) and report.get("verdict") == "not_ready"


# ---------- a person works through the findings ----------

OPEN, TODO, FIXED, DISMISSED = "open", "todo", "fixed", "dismissed"
ANSWER_MAX = 20_000     # characters of a person's answer to a finding
NOTE_MAX = 5_000        # of why a finding is not relevant
ANSWERS_MAX = 200_000   # of the answers of an analyst pasted as one text (as long as a specification)


# The form of the document, not its content: a test is designed without them, so they do not block it.
DOCUMENT_FORM = ("traceable", "singular", "section")


def _blocks(it: dict) -> bool:
    """The finding blocks test design: a question to the authors, or a defect of the content that is not minor."""
    if it["kind"] == "question":
        return True
    return it["severity"] != "minor" and it.get("criterion") not in DOCUMENT_FORM


def items(report: dict | None) -> list[dict]:
    """The findings a person has to settle, each with a stable id within the report: defects of the document
    (d<i>), conflicts with the lifecycle (l<i>; "new" ones are not defects and are left out) and the questions
    to the authors (vq<i>, lq<i>). `blocking` - it blocks test design (_blocks), the others are the quality of
    the document: worth fixing, not required. `theme` - the group it is in (groups, group()). With the person's
    decision: status, answer, note."""
    if not report:
        return []
    out = []
    v, lc = report.get("validation") or {}, report.get("lifecycle") or {}
    for i, f in enumerate(v.get("findings") or []):
        out.append({"id": f"d{i}", "kind": "defect", "severity": f["severity"], "location": f["location"],
                    "problem": f["problem"], "suggestion": f["suggestion"], "criterion": f.get("criterion", "")})
    for i, c in enumerate(lc.get("conflicts") or []):
        if c["kind"] != "new":
            out.append({"id": f"l{i}", "kind": "lifecycle", "severity": c["severity"], "location": c["location"],
                        "problem": c["problem"], "suggestion": c["suggestion"], "model_rule": c.get("model_rule", ""),
                        "criterion": c["kind"]})
    for prefix, qs in (("vq", v.get("questions")), ("lq", lc.get("questions"))):
        for i, q in enumerate(qs or []):
            out.append({"id": f"{prefix}{i}", "kind": "question", "severity": "", "location": "", "problem": q,
                        "suggestion": "", "criterion": ""})
    decided = report.get("resolutions") or {}
    theme = {i: g["id"] for g in report.get("groups") or [] for i in g["ids"]}
    return [it | {"blocking": _blocks(it), "theme": theme.get(it["id"], it["id"]), "status": OPEN, "answer": "",
                  "note": ""} | decided.get(it["id"], {}) for it in out]


def unresolved(report: dict | None, everything: bool = False) -> list[dict]:
    """Findings that block test design (everything - all findings) neither fixed in the document nor marked not
    relevant."""
    return [it for it in items(report) if (everything or it["blocking"]) and it["status"] not in (FIXED, DISMISSED)]


def resolved(report: dict | None) -> bool:
    """Every finding that blocks test design is settled: the application model may learn from the requirements.
    The quality of the document (its form, minor findings) does not hold it back."""
    return not unresolved(report)


# ---------- themes: the findings of both checks on the same thing, settled with one answer ----------

GROUP_MIN = 6       # fewer findings are read one by one


class Theme(BaseModel):
    title: str
    items: list[str]


class Themes(BaseModel):
    themes: list[Theme]


GROUP = """Two reviewers checked a specification independently: one for defects of the document, the other against the lifecycle of the system; both asked the authors questions. They often report the same thing several times. Group the findings that are about the same thing - the same requirement, rule, field or behaviour - so that the authors answer it once.

A theme has a short title (what has to be decided, a few words) and the ids of its findings. Put findings together only when one answer settles all of them; a finding unlike the others stays out of every theme. Every id is in one theme at most."""


def _brief(it: dict) -> str:
    kind = {"question": "question", "lifecycle": "lifecycle conflict"}.get(it["kind"], "defect")
    return f"[{it['id']}] {kind}" + (f" ({it['location'][:160]})" if it["location"] else "") + f": {it['problem'][:400]}"


async def group(project: dict, report: dict) -> list[dict]:
    """The findings of the report grouped into themes by a model on a clean context: [{id: t<i>, title, ids}],
    only the themes of two findings or more. Few findings - none."""
    its = items(report)
    if len(its) < GROUP_MIN:
        return []
    reply = await llm.parse(project["pipeline"]["requirements"], system=GROUP + projects.language_rule(project),
                            messages=[{"role": "user", "content": "The findings:\n" + "\n".join(map(_brief, its))}],
                            schema=Themes, max_tokens=8000, project_id=project["id"], stage_name="requirements")
    if reply.parsed is None:
        return []
    known, seen, out = {it["id"] for it in its}, set(), []
    for t in reply.parsed.themes:
        ids = [i for i in dict.fromkeys(t.items) if i in known and i not in seen]
        if len(ids) < 2:
            continue
        seen |= set(ids)
        out.append({"id": f"t{len(out)}", "title": t.title.strip()[:200], "ids": ids})
    return out


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "": 3}


def lc_only(findings: list[dict]) -> bool:
    """Findings of the lifecycle alone (its conflicts and questions, l<i>/lq<i>): settled with the lifecycle."""
    return all(m["id"].startswith("l") for m in findings)


def themes(report: dict | None) -> list[dict]:
    """What a person settles: a theme of findings (groups) or a finding of its own, in the order of the findings.
    With what it takes from its findings: blocking (any), the worst severity, question (one of them asks the
    authors: it needs an answer) and the decision - open while any is open, todo while any waits for the editor,
    dismissed when all are not relevant, otherwise fixed."""
    its = items(report)
    titles = {g["id"]: g["title"] for g in (report or {}).get("groups") or []}
    order, members = [], {}
    for it in its:
        if it["theme"] not in members:
            order.append(it["theme"])
        members.setdefault(it["theme"], []).append(it)
    out, count = [], {"doc": 0, "lc": 0}
    for tid in order:
        ms = members[tid]
        sts = {m["status"] for m in ms}
        status = OPEN if OPEN in sts else TODO if TODO in sts else DISMISSED if sts == {DISMISSED} else FIXED
        first = next((m for m in ms if m["answer"] or m["note"]), ms[0])
        # the number a person and an analyst answer by: the document's 1, 2…, the lifecycle's Ж1, Ж2… (lc_only)
        side = "lc" if lc_only(ms) else "doc"
        count[side] += 1
        out.append({"id": tid, "no": f"{'Ж' if side == 'lc' else ''}{count[side]}", "title": titles.get(tid, ""),
                    "ids": [m["id"] for m in ms],
                    "blocking": any(m["blocking"] for m in ms),
                    "severity": min((m["severity"] for m in ms), key=lambda s: SEVERITY_ORDER.get(s, 3)),
                    "question": any(m["kind"] == "question" for m in ms), "status": status,
                    "answer": first["answer"], "note": first["note"],
                    "error": next((m["error"] for m in ms if m.get("error")), "")})
    return out


def public(report: dict | None) -> dict | None:
    """The report as the studio shows it: with its findings (items) and what a person settles (themes)."""
    if not report:
        return report
    return report | {"items": items(report), "themes": themes(report)}


# ---------- the answers of an analyst, pasted as one text ----------

class Answer(BaseModel):
    id: str
    answer: str              # how it should be ("" - when not relevant)
    not_relevant: bool
    note: str                # why it is not relevant


class Answers(BaseModel):
    answers: list[Answer]


DISTRIBUTE = """The authors of a specification answered the reviewer's findings in one text: a list, a letter, notes in any order. Match each answer to its finding by the id in square brackets, by the number of the finding (as the list gives it: 1, 2… or Ж1, Ж2…) or by what it is about, and give it as said, in full - do not shorten or invent answers. A finding the text says is not relevant, not needed or out of scope: not_relevant, `note` - why. A finding the text does not answer is left out. You are given a part of the findings: answer only them, the text may answer others as well."""
DISTRIBUTE_BATCH = 15       # themes per request: the answers come back whole however long the text is
DISTRIBUTE_PARALLEL = 3


def _key(ref: str) -> str:
    """What an answer calls its finding by, comparable: «[t3]», «№ Ж1», «5.» -> t3, ж1, 5."""
    return re.sub(r"[\s\[\]()№#.:,;*«»\"']", "", str(ref or "")).lower().replace("zh", "ж")


def _theme_of(report: dict) -> dict[str, str]:
    """What an answer may call a theme by -> its id: the id, the number a person and an analyst answer by
    (themes() `no`: 3, Ж1) and the id of any finding of it - a model answers by any of them."""
    out = {}
    for t in themes(report):
        for ref in (t["id"], t["no"], *t["ids"]):
            out.setdefault(_key(ref), t["id"])
    return out


async def distribute(project: dict, report: dict, text: str, log=None, missed: list | None = None) -> list[dict]:
    """The answers in `text` matched to the unsettled themes (themes()): [{id, answer, not_relevant, note}]. The
    text goes whole (up to ANSWERS_MAX characters) with a batch of themes per request (DISTRIBUTE_BATCH), the
    requests side by side: a long text answering many findings does not cut the answers off. An answer the
    model gave to the id, the number or a finding of a theme goes to the theme; the rest - to `missed` and the
    log (`log` and the studio's log): {id, answer, why}."""
    say = log or (lambda line: None)
    left = [t for t in themes(report) if t["status"] in (OPEN, TODO)]
    if not left:
        raise ValueError("Все замечания уже разобраны")
    by = {it["id"]: it for it in items(report)}
    batches = [left[i:i + DISTRIBUTE_BATCH] for i in range(0, len(left), DISTRIBUTE_BATCH)]
    gate, done = asyncio.Semaphore(DISTRIBUTE_PARALLEL), [0]
    content = f"The answers of the authors:\n{text[:ANSWERS_MAX]}"
    aid = project["id"]      # the log of the studio: which project

    async def one(n: int, batch: list[dict]) -> list[Answer]:
        lines = [f"{t['no']}. [{t['id']}] {t['title'] or by[t['ids'][0]]['problem'][:300]}"
                 + "".join(f"\n   - {by[i]['problem'][:300]}" for i in t["ids"] if t["title"] or i != t["ids"][0])
                 for t in batch]
        async with gate:
            reply = await llm.parse(project["pipeline"]["requirements"],
                                    system=DISTRIBUTE + projects.language_rule(project),
                                    context="The findings:\n" + "\n".join(lines),
                                    messages=[{"role": "user", "content": content}], schema=Answers,
                                    max_tokens=32000, project_id=project["id"], stage_name="requirements",
                                    stream=True)
        if reply.parsed is None:
            _log.warning("distribute %s: part %s of %s - no answer (stop %s): %s", aid, n, len(batches),
                         reply.stop, (reply.text or "")[:2000])
            raise RuntimeError(f"Модель не смогла разобрать ответы (часть {n} из {len(batches)}, "
                               f"остановка: {reply.stop or 'нет'})")
        answers = reply.parsed.answers
        done[0] += 1
        _log.info("distribute %s: part %s of %s - %s answers: %s", aid, n, len(batches), len(answers),
                  [(a.id, a.not_relevant, len(a.answer)) for a in answers])
        say(f"Часть {n} из {len(batches)} (замечания {batch[0]['no']}–{batch[-1]['no']}): модель нашла ответов "
            f"{len(answers)}" + (f" — готово частей {done[0]} из {len(batches)}" if len(batches) > 1 else ""))
        return answers

    say(f"Агент сопоставляет ответы аналитика ({len(text):,} символов) с замечаниями: {len(left)}".replace(",", " "))
    known, theme_of, out, lost = {t["id"] for t in left}, _theme_of(report), {}, []
    for answers in await asyncio.gather(*(one(n, b) for n, b in enumerate(batches, 1))):
        for a in answers:
            tid = theme_of.get(_key(a.id))
            why = ("пустой ответ" if not (a.not_relevant or a.answer.strip()) else
                   "нет такого замечания" if not tid else "замечание уже разобрано" if tid not in known else "")
            if why:
                lost.append({"id": a.id, "answer": (a.note if a.not_relevant else a.answer)[:300], "why": why})
                continue
            if tid in out:
                _log.info("distribute %s: %s answered again as %s", aid, tid, a.id)
            out[tid] = a.model_dump() | {"id": tid}
    if lost:
        _log.warning("distribute %s: not matched %s of %s answers: %s", aid, len(lost), len(lost) + len(out), lost)
        say("Не разнесены ответы модели: " + "; ".join(f"{x['id'] or '(без кода)'} — {x['why']}" for x in lost[:20])
            + (f" и ещё {len(lost) - 20}" if len(lost) > 20 else ""))
    if missed is not None:
        missed.extend(lost)
    return list(out.values())


def unsettled_ids(report: dict, theme_id: str) -> list[str]:
    """The findings of a theme still to settle (open or waiting for the editor)."""
    return [it["id"] for it in items(report) if it["theme"] == theme_id and it["status"] in (OPEN, TODO)]


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
        ans = (prev.get("answer", "") if answer is None else answer).strip()[:ANSWER_MAX]
        if status == TODO and known[i]["kind"] == "question" and not ans:
            raise ValueError("На вопрос нужен ответ: по нему агент исправит ТЗ")
        if status == OPEN:
            res.pop(i, None)
            continue
        res[i] = {"status": status, "answer": ans,
                  "note": (prev.get("note", "") if note is None else note).strip()[:NOTE_MAX], "by": user,
                  "at": time.time()}
    return report


def fixed(report: dict, ids: list[str], user: str = "") -> dict:
    """The editor applied these findings to the document (revise()). Changes `report` in place."""
    res = report.setdefault("resolutions", {})
    for i in ids:
        res[i] = res.get(i, {}) | {"status": FIXED, "fixed_by": user, "fixed_at": time.time()}
    return report


def notes(report: dict | None) -> str:
    """For the next review of the requirements - the same text or a new version the analyst sent: what the authors
    decided. Findings marked not relevant are not to be reported again; the answers are what the document now says
    (one answered but not applied yet - the new version should say it)."""
    lines = []
    for it in items(report):
        if it["status"] == DISMISSED:
            lines.append(f"- not relevant, do not report it again: {it['problem']}"
                         + (f" (why: {it['note']})" if it["note"] else ""))
        elif it["status"] == FIXED and it["answer"]:
            lines.append(f"- settled by the authors: {it['problem']} -> {it['answer']}")
        elif it["status"] == FIXED:
            lines.append(f"- fixed in the document by the reviewer's suggestion: {it['problem']}")
        elif it["status"] == TODO and it["answer"]:
            lines.append(f"- answered by the authors, the document should say so now (report it only if it does "
                         f"not): {it['problem']} -> {it['answer'][:2000]}")
    return ("\n\nDecisions of the authors on the findings of the previous review:\n" + "\n".join(lines)) if lines else ""


class Edit(BaseModel):
    item: str                # the id of the finding the edit settles
    find: str                # an exact fragment of the document
    replace: str             # what it becomes


class Revision(BaseModel):
    summary: str
    edits: list[Edit]


REVISE = """You are the editor of a specification (ТЗ, SRS, user stories). A reviewer found defects in it and the authors answered how it should be. Apply each finding you are given to the document as edits.

Every edit replaces an exact fragment of the document: `find` - the shortest fragment copied verbatim from the document that occurs in it once (a sentence, a list item, a requirement - never a whole section); `replace` - the new text of that fragment. To add text, find the fragment it goes after (the end of the previous item) and replace it with that fragment followed by the new text. `item` - the id of the finding the edit settles; a finding may need several edits.

The answer of the authors is how it must be: follow it over the reviewer's suggestion; with no answer, apply the suggestion. Change only what the findings need; keep the numbering, the language and the style of the document; do not invent requirements beyond the answers. Every finding gets at least one edit; give the edits finding by finding, in the order the findings are listed. `summary` - one or two sentences on what changed."""

ATTEMPTS = 2        # a batch whose fragment was not found or whose answer was lost is asked once more
FIX_BATCH = 1       # themes (or findings of their own) per request: a model that thinks long gets a short task
FIX_PARALLEL = 3    # requests of the editor at a time: the themes of a document seldom touch the same fragment
FIX_WAIT = 300      # seconds of silence in the editor's streamed answer before the request is given up
FIX_THINK = 60_000  # characters of thinking with no answer begun: the request is given up (GLM behind a gateway
                    # drafts every edit in its thinking until the output limit - 5 minutes and nothing)
FIX_TOKENS = 32000  # the output limit of a request of the editor, its thinking included
FIX_TIME = 900      # seconds for a batch, its attempts included


def _locate(document: str, fragment: str) -> tuple[int, int] | None:
    """Where the fragment is: verbatim, or with any whitespace between its words (line breaks of the model)."""
    if not fragment.strip():
        return None
    i = document.find(fragment)
    if i >= 0:
        return i, i + len(fragment)
    m = re.search(r"\s+".join(map(re.escape, fragment.split())), document)
    return (m.start(), m.end()) if m else None


def _finding(it: dict, pad: str) -> str:
    line = ""
    if it.get("location"):
        line += f"\n{pad}where: {it['location']}"
    if it.get("model_rule"):
        line += f"\n{pad}the lifecycle of the system says: {it['model_rule']}"
    if it.get("suggestion"):
        line += f"\n{pad}the reviewer suggests: {it['suggestion']}"
    return line


def _revise_task(units: list[tuple[str, list[dict]]], titles: dict | None = None) -> str:
    """units - (id, findings): a finding of its own, or a theme of findings settled by one answer."""
    out = []
    for uid, ms in units:
        if len(ms) == 1:
            line = f"[{uid}] {ms[0]['problem']}" + _finding(ms[0], "  ")
        else:
            line = f"[{uid}] {(titles or {}).get(uid) or 'findings on the same thing'} - settle them together:" + "".join(
                f"\n  - {m['problem']}" + _finding(m, "    ") for m in ms)
        answer = next((m["answer"] for m in ms if m["answer"]), "")
        line += f"\n  the answer of the authors: {answer or 'none - apply the suggestion'}"
        out.append(line)
    return "Apply these findings to the document:\n\n" + "\n\n".join(out)


def _edits(reply) -> tuple[list[Edit], str, str]:
    """The edits of an answer and its summary; an answer cut off by the output limit (or broken) - its complete
    edits, but those of the finding it was cut on (it may need more of them). The third - why edits are missing."""
    if reply.parsed is not None:
        return reply.parsed.edits, reply.parsed.summary.strip(), ""
    edits, cut = llm.salvage(reply.text, "edits", Edit), reply.stop == "max_tokens"
    if cut and edits:
        edits = [e for e in edits if e.item != edits[-1].item]
    return edits, "", "ответ агента оборван на лимите длины" if cut else "ответ агента не разобран"


async def _revise_batch(project: dict, system: str, state: dict, units: dict[str, list[dict]], titles: dict,
                        status=None) -> tuple[list[dict], str, dict[str, str]]:
    """One batch of themes applied to the document: state["doc"] - the text now (the batches in work at the same
    time apply theirs to it as each is done). (the edits, the summary, {theme: why not}). An answer lost in
    thinking is not asked again at once: the batch's next attempt is the only repeat."""
    edits, summaries, failed, pending = [], [], {}, list(units)
    for attempt in range(ATTEMPTS):
        def progress(thought: int, said: int, names=list(pending), attempt=attempt) -> None:
            if status:
                status(f"{', '.join(_unit_name(u, units[u], titles) for u in names)}: "
                       + (f"пишет правки ({said // 1000 or 1} тыс. символов)" if said else
                          f"модель рассуждает ({thought // 1000} тыс. символов)")
                       + (", вторая попытка" if attempt else ""))
        reply = await llm.parse(project["pipeline"]["requirements"], system=system,
                                context=f"The document:\n{state['doc'][:150_000]}",
                                messages=[{"role": "user", "content": _revise_task([(u, units[u]) for u in pending],
                                                                                   titles)}],
                                schema=Revision, max_tokens=FIX_TOKENS, project_id=project["id"],
                                stage_name="requirements", stream=True, timeout=FIX_WAIT, think_limit=FIX_THINK,
                                progress=progress, again=False)
        got, summary, why = _edits(reply)
        if summary:
            summaries.append(summary)
        asked, done, reasons = set(pending), set(), {}
        doc = state["doc"]
        for e in got:
            if e.item not in asked:
                continue
            at = _locate(doc, e.find)
            if not at:
                reasons[e.item] = f"не найден фрагмент «{e.find[:120]}»"
                continue
            doc = doc[:at[0]] + e.replace + doc[at[1]:]
            edits.append(e.model_dump())
            done.add(e.item)
        state["doc"] = doc
        failed = {u: why_not for u, why_not in failed.items() if u not in asked}
        failed |= {u: reasons.get(u) or why or "агент не предложил правку" for u in pending if u not in done}
        pending = [u for u in pending if u in failed]
        if not pending:
            break
    return edits, " ".join(summaries), failed


def _unit_name(uid: str, ms: list[dict], titles: dict) -> str:
    name = titles.get(uid) or ms[0]["problem"]
    return f"«{name[:70]}{'…' if len(name) > 70 else ''}»"


async def revise(project: dict, document: str, report: dict, log=None, keep=None, status=None) -> dict:
    """The findings a person sent to be fixed (status todo) applied to the document by an editor on a clean
    context; the findings of a theme are given and fixed together, FIX_BATCH themes per request, FIX_PARALLEL
    requests at a time - the answers stay short. keep(part) - every batch as it is done ({requirements: the whole
    text so far, fixed, failed, edits, summary} of the batch): a fix cut off loses only the batches in work.
    status(line) - how the requests in work go (the model thinks / writes), not for the log. A batch with no
    answer in FIX_TIME fails alone; an error of the model stops the fix: no batch begins, the ones in work and
    done stay.
    {requirements: the new text, fixed: [ids], failed: {id: why}, edits: [{item, find, replace}], summary}."""
    say = log or (lambda line: None)
    todo = [it for it in items(report) if it["status"] == TODO]
    if not todo:
        raise ValueError("Нет замечаний для исправления: ответьте на замечания или примите предложения ревьюера")
    units: dict[str, list[dict]] = {}
    for it in todo:
        units.setdefault(it["theme"], []).append(it)
    titles = {g["id"]: g["title"] for g in report.get("groups") or []}
    system = REVISE + projects.language_rule(project)
    order = list(units)
    batches = [order[i:i + FIX_BATCH] for i in range(0, len(order), FIX_BATCH)]
    state = {"doc": document}
    fixed, failed, edits, summaries = [], {}, [], []
    gate = asyncio.Semaphore(FIX_PARALLEL)

    errors: list[Exception] = []

    async def one(n: int, batch: list[str]) -> None:
        async with gate:
            if errors:          # an error of the model: the batches not begun wait for the next fix
                return
            say(f"Пакет {n} из {len(batches)}: " + ", ".join(_unit_name(u, units[u], titles) for u in batch))
            try:
                got, summary, missed = await asyncio.wait_for(
                    _revise_batch(project, system, state, {u: units[u] for u in batch}, titles, status), FIX_TIME)
            except asyncio.TimeoutError:
                got, summary, missed = [], "", {u: f"агент не исправил за {FIX_TIME // 60} мин" for u in batch}
            except Exception as e:      # the batches in work at the same time are kept as they finish
                errors.append(e)
                return
            part = {"requirements": state["doc"], "fixed": [m["id"] for u in batch if u not in missed for m in units[u]],
                    "failed": {m["id"]: why for u, why in missed.items() for m in units[u]}, "edits": got,
                    "summary": summary}
            if keep:
                keep(part)
            fixed.extend(part["fixed"])
            failed.update(part["failed"])
            edits.extend(got)
            summaries.append(summary)
            done = len(batch) - len(missed)
            say(f"Пакет {n} из {len(batches)} готов: исправлено {done}"
                + (f", не удалось {len(missed)}: {next(iter(missed.values()))}" if missed else ""))

    tasks = [asyncio.ensure_future(one(n, batch)) for n, batch in enumerate(batches, 1)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
    if errors:
        raise errors[0]
    return {"requirements": state["doc"], "fixed": fixed, "failed": failed, "edits": edits,
            "summary": " ".join(s for s in summaries if s)}
