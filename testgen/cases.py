"""Manual test cases of a test management system (Test IT, Allure TestOps, Zephyr Scale) -> automated tests.

    fetch      the cases of a connection, imported for a person to look at and choose («Импорт кейсов» in
               the Tests tab): kept in data/projects/<id>/case-imports/<import>.json, the run of the pipeline
               takes the chosen ones from there (nothing is read from the system twice)
    review     the reviewer reads each chosen case on a clean context of its own (the case, the slice of the
               application model it touches): the test data the case uses (the role, the objects, the users) and
               the findings that keep an agent from automating it as the authors meant (an ambiguous step, no
               expected result, data the case does not give, a step against the lifecycle of the system...)
    group      the findings of many cases one answer settles - themes: «под каким пользователем входить» is asked
               once, not in every case
    settle     a person settles the findings (or a theme of them): an answer «как должно быть», the reviewer's
               proposal, or «не актуально»; the answers reach the agent with the case (`clarifications`,
               pipeline.scenario_text) and the team's memory (answers.py): the same question is not asked again
    auto       what settles without a person - an answer the team gave to the same question before, and (the
               project's `decisions.auto_accept`) the reviewer's proposals for minor findings and for major ones it
               is sure of; each is logged and can be undone («reopen»)

A case of a pipeline run waits for a person while a blocking finding (not minor) of it is not settled; the other
cases go to work at once. The test data of a case joins the application model (knowledge.need) when it is settled.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Literal

from pydantic import BaseModel

from . import answers, fs, knowledge, llm, projects, storage

BATCH = 5           # cases in one request to the reviewer
PARALLEL = 3        # requests at once
KEEP = 20           # imports a project keeps
ACTIONS = ("answer", "accept", "dismiss", "reopen")
BLOCKING = ("blocker", "major")
NEVER_AUTO = ("account", "irreversible")       # an account and a stop before a real action are a person's call
GROUP_MIN = 2       # open findings (of two cases or more) worth grouping into themes
MEMORY = "память ответов"       # who settled a finding by an earlier answer of the team
AUTO = "авто"                   # who settled it by the reviewer's confident proposal
KINDS = {"ambiguous": "неоднозначный шаг", "expected": "нет ожидаемого результата",
         "data": "не хватает тестовых данных", "precondition": "неясное предусловие",
         "account": "учётная запись", "lifecycle": "расходится с моделью приложения",
         "irreversible": "необратимое действие", "other": "другое"}


# ---------- import for a person to choose ----------

def _dir(pid: str):
    return projects.path(pid) / "case-imports"


def automated(pid: str) -> dict[str, dict]:
    """The tests that automate manual cases: "<system>:<case id>" -> {id, name}."""
    out = {}
    for t in storage.all_tests(pid):
        for system, e in (t.get("external") or {}).items():
            case = (e or {}).get("case_id") or (e or {}).get("work_item_id")
            if case:
                out[f"{system}:{case}"] = {"id": t["id"], "name": t["name"]}
    return out


def save_import(pid: str, conn: dict, cases: list[dict], user: str = "") -> dict:
    imp = {"id": uuid.uuid4().hex[:10], "project_id": pid, "connection": conn["id"], "connection_name": conn["name"],
           "created": time.time(), "user": user, "cases": cases}
    fs.write_json(_dir(pid) / f"{imp['id']}.json", imp, indent=1)
    for f, _ in fs.stamps(_dir(pid))[KEEP:]:       # newest first
        fs.unlink(f)
    return imp


def get_import(pid: str, iid: str) -> dict | None:
    imp = fs.read_json(_dir(pid) / f"{iid}.json") if iid.isalnum() else None
    return imp if imp and imp.get("project_id") == pid else None


def import_view(pid: str, imp: dict) -> dict:
    """The import for the studio: each case with its steps and the test that already automates it."""
    done = automated(pid)
    return {k: imp[k] for k in ("id", "connection", "connection_name", "created")} | {"cases": [
        {"index": i, "title": c["title"], "priority": c.get("priority", ""), "preconditions": c.get("preconditions", ""),
         "instructions": c.get("instructions", ""), "expected_result": c.get("expected_result", ""),
         "case": c.get("source_case") or {},
         "test": done.get(f"{(c.get('source_case') or {}).get('system')}:{(c.get('source_case') or {}).get('id')}")}
        for i, c in enumerate(imp["cases"])]}


# ---------- the review ----------

class XData(BaseModel):
    entity: str
    name: str
    details: str = ""
    state: str = ""
    role: str = ""


class XFinding(BaseModel):
    kind: Literal["ambiguous", "expected", "data", "precondition", "account", "lifecycle", "irreversible", "other"]
    severity: Literal["blocker", "major", "minor"]
    step: int = 0
    quote: str = ""
    problem: str
    question: str = ""
    suggestion: str = ""
    confidence: int = 0


class XCase(BaseModel):
    index: int
    role: str = ""
    test_data: list[XData] = []
    findings: list[XFinding] = []


class XCases(BaseModel):
    cases: list[XCase]


SYSTEM = """You review manual test cases written by testers before an AI agent automates them in a real browser. The agent follows the steps of a case literally and can ask nobody: whatever the case leaves to the tester's head, the agent guesses. For every case:
1. `role` - who performs the case, as the application calls the role ("" if the case does not say).
2. `test_data` - the data the case relies on: objects of the application (entity - the kind of object as the application model calls it if it is there, name - which object, state - the state it must be in, details - what matters about it), and users to log in as (entity "Пользователь", role - their role). Never copy a password or a secret token into test_data: say that an account is needed instead.
3. `findings` - only what stops the agent from automating the case as the authors meant:
   - ambiguous: a step the agent can perform in several ways or cannot map to the interface ("проверить корректность", "заполнить форму");
   - expected: a step or the case has no checkable expected result (nothing to assert);
   - data: values the case needs and does not give (which product, which order, what to type);
   - precondition: a precondition the agent cannot set up or check;
   - account: the case needs a user of a role the model has no account for, or holds a password in plain text;
   - lifecycle: the case contradicts the model of the application below (a transition the lifecycle does not allow, a role doing what its restrictions forbid);
   - irreversible: a real payment, an order, a message to real people or a deletion - the agent stops before it and asserts there;
   - other.
   severity: blocker - the case cannot be automated as written; major - the agent will most likely guess wrong; minor - worth fixing, the agent copes. `step` - the number of the step (0 - the whole case), `quote` - the words of the case, `question` - what to ask the authors, `suggestion` - how the step should read, when you can propose it from the case and the model; `confidence` - how sure you are, 0-100, that the suggestion is what the authors meant (100 - the case or the model says so, 50 - a guess, 0 - no suggestion).
Do not report style, wording or the form of the case. A case with nothing to report gets an empty list of findings. Return every case by its index. Write everything in Russian."""


def _text(i: int, c: dict) -> str:
    return (f"## Case {i}: {c['title']}\n" + (f"Preconditions: {c['preconditions']}\n" if c.get("preconditions") else "")
            + f"Steps:\n{c.get('instructions') or '(no steps)'}\n"
            + (f"Expected result: {c['expected_result']}\n" if c.get("expected_result") else ""))


async def _review_batch(project: dict, batch: list[tuple[int, dict]]) -> dict[int, XCase]:
    pid = project["id"]
    text = "\n".join(_text(i, c) for i, c in batch)
    model = knowledge.prompt(pid, knowledge.focus(text=text))
    known = answers.prompt(pid, text)
    reply = await llm.parse(project["pipeline"]["requirements"], system=SYSTEM + known + projects.language_rule(project),
                            context=model, messages=[{"role": "user", "content": text[:60000]}], schema=XCases,
                            max_tokens=8000, project_id=pid, stage_name="cases")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла проверить кейсы")
    return {c.index: c for c in reply.parsed.cases}


def _result(index: int, x: XCase | None) -> dict:
    findings = [f.model_dump() | {"id": f"c{index}f{n}", "decision": None, "confidence": max(0, min(f.confidence, 100))}
                for n, f in enumerate((x.findings if x else [])[:20]) if f.problem.strip()]
    return {"at": time.time(), "role": (x.role if x else "").strip(),
            "test_data": [d.model_dump() for d in (x.test_data if x else [])[:30] if d.entity.strip() and d.name.strip()],
            "findings": findings}


async def review(project: dict, cases: list[tuple[int, dict]], keep=None, log=None) -> None:
    """Each case of `cases` ((index, scenario) pairs) gets `case_review` and its role and test data (in place);
    a case reviewed before (a run that goes on) is not asked again. keep() - after each batch."""
    todo = [(i, c) for i, c in cases if not c.get("case_review")]
    if not todo:
        return
    pid = project["id"]
    team = answers.all_answers(pid)
    sem = asyncio.Semaphore(PARALLEL)
    batches = [todo[k:k + BATCH] for k in range(0, len(todo), BATCH)]

    async def one(batch):
        async with sem:
            got = await _review_batch(project, batch)
        for i, c in batch:
            r = _result(i, got.get(i))
            c["case_review"] = r
            if r["role"] and not c.get("role"):
                c["role"] = r["role"]
            if r["test_data"] and not c.get("test_data"):
                c["test_data"] = [d | {"record_id": ""} for d in r["test_data"]]
            for f in r["findings"]:
                # the team answered the same question before: settled by that answer (shown, can be undone)
                hit = team and answers.known(pid, f["question"] or f["problem"], team)
                if hit:
                    f["decision"] = {"action": "answer", "text": hit["answer"], "user": MEMORY, "auto": "memory",
                                     "at": time.time(), "by": hit.get("user") or ""}
            _clarify([c])
        if keep:
            keep()
        if log:
            log(f"Ревью кейсов: проверено {sum(1 for _, c in todo if c.get('case_review'))} из {len(todo)}")
    await asyncio.gather(*(one(b) for b in batches))


def findings(cases: list[dict]) -> list[dict]:
    return [f for c in cases for f in (c.get("case_review") or {}).get("findings") or []]


def unsettled(cases: list[dict]) -> list[dict]:
    """The blocking findings nobody settled yet: the run waits for them."""
    return [f for f in findings(cases) if f["severity"] in BLOCKING and not f.get("decision")]


def _clarify(cases: list[dict]) -> None:
    for c in cases:
        c["clarifications"] = [f"{x['problem']} — {x['decision']['text']}"
                               for x in (c.get("case_review") or {}).get("findings") or []
                               if x.get("decision") and x["decision"]["action"] in ("answer", "accept")]


def settle(cases: list[dict], decisions: list[dict], user: str = "", themes: list[dict] | None = None,
           pid: str = "") -> list[dict]:
    """A person's decisions [{id, action, text}] on the findings - or on a theme of them (its id: each finding of the
    theme not settled yet): answer (text - how it should be), accept (the reviewer's suggestion; the theme's, if it
    has one), dismiss (text - why not relevant), reopen (undecided again: an automatic decision undone).
    -> the findings settled. The answers become the case's `clarifications` for the agent and, with `pid`, the
    team's memory (answers.record)."""
    by_id = {f["id"]: (f, c) for c in cases for f in (c.get("case_review") or {}).get("findings") or []}
    by_theme = {t["id"]: t for t in themes or []}
    done = []
    for d in decisions:
        key, action = str(d.get("id") or ""), d.get("action")
        text = " ".join(str(d.get("text") or "").split())[:4000]
        theme = by_theme.get(key)
        if theme:
            targets = [by_id[i] for i in theme["ids"]
                       if i in by_id and (action == "reopen") == bool(by_id[i][0].get("decision"))]
        else:
            targets = [by_id[key]] if key in by_id else []
        if action not in ACTIONS or action == "answer" and not text:
            continue
        for f, c in targets:
            if action == "reopen":
                if f.get("decision"):
                    f["decision"] = None
                    done.append(f)
                continue
            said = text
            if action == "accept":
                said = (theme or {}).get("suggestion") or f.get("suggestion") or ""
                if not said:
                    continue
            f["decision"] = {"action": action, "text": said, "user": user, "at": time.time()} | (
                {"theme": theme["id"]} if theme else {})
            done.append(f)
            if pid and action in ("answer", "accept"):
                answers.record(pid, (theme or {}).get("question") or f.get("question") or f["problem"], said,
                               source=f"кейс «{c.get('title', '')}»", role=c.get("role") or "", user=user)
    _clarify(cases)
    return done


def auto_accept(cases: list[dict], confidence: int, everything: bool = False) -> list[tuple[dict, dict]]:
    """The reviewer's proposals that settle by themselves (decisions.auto_accept): a minor finding with a proposal,
    a major one the reviewer is sure of (`confidence` and more); never a blocker, an account or an irreversible
    action. everything - without confirmations (projects.autonomous): every proposal. -> (case, finding) settled."""
    out = []
    for c in cases:
        for f in (c.get("case_review") or {}).get("findings") or []:
            if f.get("decision") or not f.get("suggestion"):
                continue
            if everything or f["kind"] not in NEVER_AUTO and f["severity"] != "blocker" and (
                    f["severity"] == "minor" or (f.get("confidence") or 0) >= confidence):
                f["decision"] = {"action": "accept", "text": f["suggestion"], "user": AUTO,
                                 "auto": "autonomous" if everything else "confidence",
                                 "confidence": f.get("confidence") or 0, "at": time.time()}
                out.append((c, f))
    _clarify(cases)
    return out


def tally(cases: list[dict]) -> dict:
    fs_ = findings(cases)
    return {"cases": sum(1 for c in cases if c.get("case_review")), "findings": len(fs_),
            "blocking": sum(1 for f in fs_ if f["severity"] in BLOCKING),
            "unsettled": len(unsettled(cases)),
            "answered": sum(1 for f in fs_ if (f.get("decision") or {}).get("action") in ("answer", "accept")),
            "dismissed": sum(1 for f in fs_ if (f.get("decision") or {}).get("action") == "dismiss"),
            "auto": sum(1 for f in fs_ if (f.get("decision") or {}).get("auto"))}


# ---------- themes: one answer for many cases ----------

class XTheme(BaseModel):
    title: str
    question: str = ""
    suggestion: str = ""
    items: list[str]


class XThemes(BaseModel):
    themes: list[XTheme]


GROUP = """A reviewer read manual test cases before an AI agent automates them and reported findings in each. The same question often comes up in many cases: which user to log in as, which product to take, what the result should be. Group the findings that one answer settles for all of them, so that the team answers once.

A theme: a short title (what has to be decided, a few words), `question` - the one question to ask the team, `suggestion` - the fix when the findings propose the same one ("" otherwise), and the ids of its findings. Put findings together only when one answer settles all of them; a finding unlike the others stays out of every theme. Every id is in one theme at most."""


async def group(project: dict, cases: list[dict]) -> list[dict]:
    """The open findings of the cases grouped into themes by a model on a clean context: [{id: ct<i>, title,
    question, suggestion, ids}] - only themes of two findings or more of two cases or more; each finding of a theme
    gets its `theme`. Few findings - none."""
    rows = [(f, c) for c in cases for f in (c.get("case_review") or {}).get("findings") or [] if not f.get("decision")]
    if len(rows) < GROUP_MIN or len({id(c) for _, c in rows}) < 2:
        return []
    brief = "\n".join(f"[{f['id']}] case «{c.get('title', '')[:80]}», {f['kind']}, {f['severity']}: {f['problem'][:300]}"
                      + (f" Question: {f['question'][:200]}" if f.get("question") else "") for f, c in rows)
    reply = await llm.parse(project["pipeline"]["requirements"], system=GROUP + projects.language_rule(project),
                            messages=[{"role": "user", "content": "The findings:\n" + brief[:60000]}], schema=XThemes,
                            max_tokens=8000, project_id=project["id"], stage_name="cases")
    if reply.parsed is None:
        return []
    case_of = {f["id"]: (f, c) for f, c in rows}
    seen, out = set(), []
    for t in reply.parsed.themes:
        ids = [i for i in dict.fromkeys(t.items) if i in case_of and i not in seen]
        if len(ids) < 2 or len({id(case_of[i][1]) for i in ids}) < 2:
            continue
        seen |= set(ids)
        tid = f"ct{len(out)}"
        for i in ids:
            case_of[i][0]["theme"] = tid
        out.append({"id": tid, "title": t.title.strip()[:200], "question": t.question.strip()[:1000],
                    "suggestion": t.suggestion.strip()[:2000], "ids": ids})
    return out
