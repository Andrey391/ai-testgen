"""Manual test cases of a test management system (Test IT, Allure TestOps, Zephyr Scale) -> automated tests.

    fetch      the cases of a connection, imported for a person to look at and choose («Импорт кейсов» in
               the Tests tab): kept in data/projects/<id>/case-imports/<import>.json, the run of the pipeline
               takes the chosen ones from there (nothing is read from the system twice)
    review     the reviewer reads each chosen case on a clean context of its own (the case, the slice of the
               application model it touches): the test data the case uses (the role, the objects, the users) and
               the findings that keep an agent from automating it as the authors meant (an ambiguous step, no
               expected result, data the case does not give, a step against the lifecycle of the system...)
    settle     a person settles the findings: an answer «как должно быть», the reviewer's proposal, or «не
               актуально»; the answers reach the agent with the case (`clarifications`, pipeline.scenario_text)

A run of the pipeline with cases waits for the person (`awaiting_cases`) while a blocking finding (not minor)
is not settled; the test data of the cases joins the application model (knowledge.need) after that.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Literal

from pydantic import BaseModel

from . import fs, knowledge, llm, projects, storage

BATCH = 5           # cases in one request to the reviewer
PARALLEL = 3        # requests at once
KEEP = 20           # imports a project keeps
ACTIONS = ("answer", "accept", "dismiss")
BLOCKING = ("blocker", "major")
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
   severity: blocker - the case cannot be automated as written; major - the agent will most likely guess wrong; minor - worth fixing, the agent copes. `step` - the number of the step (0 - the whole case), `quote` - the words of the case, `question` - what to ask the authors, `suggestion` - how the step should read, when you can propose it from the case and the model.
Do not report style, wording or the form of the case. A case with nothing to report gets an empty list of findings. Return every case by its index. Write everything in Russian."""


def _text(i: int, c: dict) -> str:
    return (f"## Case {i}: {c['title']}\n" + (f"Preconditions: {c['preconditions']}\n" if c.get("preconditions") else "")
            + f"Steps:\n{c.get('instructions') or '(no steps)'}\n"
            + (f"Expected result: {c['expected_result']}\n" if c.get("expected_result") else ""))


async def _review_batch(project: dict, batch: list[tuple[int, dict]]) -> dict[int, XCase]:
    pid = project["id"]
    text = "\n".join(_text(i, c) for i, c in batch)
    model = knowledge.prompt(pid, knowledge.focus(text=text))
    reply = await llm.parse(project["pipeline"]["requirements"], system=SYSTEM + projects.language_rule(project),
                            context=model, messages=[{"role": "user", "content": text[:60000]}], schema=XCases,
                            max_tokens=8000, project_id=pid, stage_name="cases")
    if reply.parsed is None:
        raise RuntimeError("Модель не смогла проверить кейсы")
    return {c.index: c for c in reply.parsed.cases}


def _result(index: int, x: XCase | None) -> dict:
    findings = [f.model_dump() | {"id": f"c{index}f{n}", "decision": None}
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


def settle(cases: list[dict], decisions: list[dict], user: str = "") -> int:
    """A person's decisions [{id, action, text}] on the findings: answer (text - how it should be), accept (the
    reviewer's suggestion), dismiss (text - why not relevant). -> how many were settled. The answers become the
    case's `clarifications` for the agent."""
    by_id = {f["id"]: f for f in findings(cases)}
    n = 0
    for d in decisions:
        f = by_id.get(str(d.get("id") or ""))
        action, text = d.get("action"), " ".join(str(d.get("text") or "").split())[:4000]
        if not f or action not in ACTIONS:
            continue
        if action == "answer" and not text:
            continue
        if action == "accept":
            if not f.get("suggestion"):
                continue
            text = f["suggestion"]
        f["decision"] = {"action": action, "text": text, "user": user, "at": time.time()}
        n += 1
    for c in cases:
        c["clarifications"] = [f"{x['problem']} — {x['decision']['text']}"
                               for x in (c.get("case_review") or {}).get("findings") or []
                               if x.get("decision") and x["decision"]["action"] in ("answer", "accept")]
    return n


def tally(cases: list[dict]) -> dict:
    fs_ = findings(cases)
    return {"cases": sum(1 for c in cases if c.get("case_review")), "findings": len(fs_),
            "blocking": sum(1 for f in fs_ if f["severity"] in BLOCKING),
            "unsettled": len(unsettled(cases)),
            "answered": sum(1 for f in fs_ if (f.get("decision") or {}).get("action") in ("answer", "accept")),
            "dismissed": sum(1 for f in fs_ if (f.get("decision") or {}).get("action") == "dismiss")}
