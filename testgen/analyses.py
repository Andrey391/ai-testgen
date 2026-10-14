"""History of requirement analyses (the Requirements tab).

Every review of requirements is kept: the requirements, the report of the reviewer (review.py: defects of
the document and its conflicts with the lifecycle of the system), then the scenarios the pipeline designed
from them (a person may edit and delete them) and, for each scenario, the tests generated from it. A run of
the pipeline without a review keeps its scenarios here too (/api/scenarios as well).
One document per analysis: data/projects/<id>/analyses/<analysis>.json.

The test data of a scenario is recorded in the application model (knowledge.need, record_needs()) with
the scenario's ref "<analysis>:<scenario>", and each item gets the id of its record (`record_id`): an edit
of the scenario updates the model, a deleted scenario or analysis lets its records go (knowledge.release).

Work on an analysis (WORK: a review, a design of scenarios, a fix of the requirements, the answers of an analyst…)
is one at a time: the analysis is "running" with its `work`, and whatever would break that work - another work, a
decision on the findings, deleting it - waits for it to end (Conflict with busy_text(): what goes, who started
it). The work goes on when the page that started it goes away; it is stopped only on purpose (request_stop()).
It beats while it goes (working()): an analysis "running" with no beat for BEAT_STALE seconds was cut off - the
studio restarted - and says so at once, not an hour later. Its progress (`progress`: the stage and the last lines
of its log) is in the analysis, so any page shows it - after another tab, F5, on another computer. What was done
by then is kept to go on from: the finished checks of a review (review_parts(), keep_review_part()) and the plan
of a design with the scenarios detailed by then (`plan`, resume_plan()).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid

from . import fs, projects, reuse
from .catalog import LAYERS, TYPES

FIELDS = ("title", "type", "layer", "priority", "role", "preconditions", "instructions", "expected_result", "gherkin")
# Test data a scenario needs (scenarios.DataNeed) and the record of the application model that holds it.
DATA_FIELDS = ("entity", "name", "details", "state", "role", "record_id")
PRIORITIES = ("high", "medium", "low")
STALE = 3600     # a "running" analysis of an earlier version (no beats) older than this was cut off
BEAT = 15        # seconds between the beats of the work on an analysis
BEAT_STALE = 60  # a "running" analysis without a beat this long was cut off (the studio restarted)
PARTS_TTL = 86400    # the finished checks of an interrupted review are reused this long
PROGRESS_LINES = 80  # the last lines of the log of a work kept in the analysis (`progress`)
# The works on an analysis, for the person: what goes now (busy_text()). review and design end with finish();
# the others leave the analysis as it was before them (start_work() / end_work()).
WORK = {
    "review": "проверка требований ревьюером",
    "design": "проектирование сценариев конвейером",
    "fix": "исправление ТЗ агентом",
    "answers": "разбор ответов аналитика по замечаниям",
    "group": "сведение замечаний в темы",
    "lifecycle": "сверка с жизненным циклом системы",
    "learn": "обучение модели приложения на требованиях",
}
INTERRUPTED = {
    "review": "Проверка прервана перезапуском студии — «Проверить заново» продолжит с готовой части",
    "design": "Проектирование сценариев прервано перезапуском студии — «Продолжить» запуск конвейера продолжит "
              "с места остановки",
    "fix": "Исправление ТЗ прервано перезапуском студии — готовые правки сохранены, «Исправить ТЗ агентом» "
           "продолжит с оставшихся замечаний",
}


class Conflict(ValueError):
    pass


def busy_text(a: dict) -> str:
    """What goes on the analysis now, for a person whose action has to wait for it."""
    what = WORK.get(a.get("work") or "", "работа с этими требованиями")
    who = f" (запустил {a['by']})" if a.get("by") else ""
    return f"Сейчас идёт: {what}{who}. Действие станет доступно после окончания"


def check_idle(a: dict) -> None:
    """Nothing goes on the analysis: a decision, a delete, another work may change it. Conflict - something goes."""
    if _stale(a)["status"] == "running":
        raise Conflict(busy_text(a))


def _dir(pid: str):
    return projects.path(pid) / "analyses"


def _path(pid: str, aid: str):
    return _dir(pid) / f"{aid}.json"


def _id() -> str:
    return uuid.uuid4().hex[:10]


def create(pid: str, requirements: str, url: str = "", user: str = "", work: str = "design") -> dict:
    first = next((line.strip(" #\t") for line in requirements.splitlines() if line.strip(" #\t")), "")
    a = {"id": _id(), "project_id": pid, "created": time.time(), "finished": None, "user": user,
         "title": first[:120], "requirements": requirements[:200_000], "url": url, "status": "running",
         "feature": "", "assumptions": [], "scenarios": [], "error": "", "beat": time.time(), "work": work}
    fs.write_json(_path(pid, a["id"]), a, indent=1)
    return a


def _norm(text: str) -> str:
    return " ".join(text.split())


def same_text(pid: str, requirements: str) -> dict | None:
    """The newest analysis of these very requirements (whitespace aside): checking them again updates it."""
    want, found = _norm(requirements), None
    for _, text, _ in fs.documents(_dir(pid)):
        try:
            a = json.loads(text)
        except ValueError:
            continue
        if _norm(a.get("requirements", "")) == want and (not found or a["created"] > found["created"]):
            found = a
    return _stale(found) if found else None


def get(aid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{10}", aid or ""):
        return None
    for f in fs.glob(projects.ROOT, f"*/analyses/{aid}.json"):
        return _stale(fs.read_json(f))
    return None


def _stale(a: dict) -> dict:
    """An analysis "running" with no work beating on it (working()) was cut off: it is an error at once."""
    if a["status"] == "running":
        if "beat" in a:
            gone = time.time() - (a["beat"] or 0) > BEAT_STALE
        else:       # written by a version without beats
            gone = time.time() - (a.get("started") or a["created"]) > STALE
        was = a.get("before_work") or a.get("before_fix")
        if gone and was:        # a work of start_work(): the analysis is what it was (a fix: the batches done stay)
            work = a.get("work") or ""
            a.update(status=was["status"] or "done", work=was["work"], error=was["error"] or "",
                     work_error=INTERRUPTED.get(work) or f"Прервано перезапуском студии: {WORK.get(work, work)} — "
                                                        "запустите заново")
        elif gone:
            a["status"] = "error"
            a["error"] = a["error"] or INTERRUPTED.get(a.get("work") or "", "Генерация была прервана")
    return a


_TASKS: dict[str, asyncio.Task] = {}     # the work on an analysis that goes on this instance: request_stop()


@contextlib.asynccontextmanager
async def working(pid: str, aid: str):
    """The work on the analysis is alive while this is open: it beats every BEAT seconds. A studio that stops
    stops beating, and the analysis reads as interrupted (_stale) - on this instance and on the others. A stop
    asked on another instance (`stop`) is seen at the next beat: the work is cancelled."""
    me = asyncio.current_task()
    stop = []

    def touch(a):
        if a["status"] == "running":
            a["beat"] = time.time()
            if a.get("stop"):
                stop.append(True)

    async def beat():
        while True:
            await asyncio.sleep(BEAT)
            try:
                if fs.exists(_path(pid, aid)):
                    _change(pid, aid, touch)
            except Exception as e:      # a missed beat: the next one comes
                print(f"analysis {aid}: {e}")
            if stop and me:
                me.cancel()
                return
    task = asyncio.create_task(beat())
    if me:
        _TASKS[aid] = me
    try:
        yield
    finally:
        task.cancel()
        if _TASKS.get(aid) is me:
            _TASKS.pop(aid, None)


def request_stop(pid: str, aid: str) -> bool:
    """Stop the work on the analysis on purpose («Остановить»): at once on this instance, at its next beat on
    another one. False - nothing goes."""
    def fn(a):
        if _stale(a)["status"] != "running":
            return False
        a["stop"] = True
        return True
    if not fs.exists(_path(pid, aid)) or not _change(pid, aid, fn):
        return False
    task = _TASKS.get(aid)
    if task and not task.done():
        task.get_loop().call_soon_threadsafe(task.cancel)
    return True


def progress(pid: str, aid: str, line: str, logged: bool = True) -> None:
    """A line of the log of the work on the analysis: the stage now and the log any page shows (`progress`).
    logged=False - the stage only (how a request in work goes, every few seconds)."""
    line = str(line or "").strip()
    if not line or not fs.exists(_path(pid, aid)):
        return

    def fn(a):
        if a["status"] != "running":
            return
        p = a.get("progress") or {}
        p["stage"] = line[:300]
        if logged:
            p["log"] = (p.get("log") or [])[-(PROGRESS_LINES - 1):] + [{"at": time.time(), "text": line[:2000]}]
        a["progress"] = p
    try:
        _change(pid, aid, fn)
    except Exception as e:      # the progress is for a person: the work goes on without it
        print(f"analysis {aid}: {e}")


def _change(pid: str, aid: str, fn) -> dict:
    with fs.lock(_path(pid, aid)):
        a = fs.read_json(_path(pid, aid))
        out = fn(a)
        fs.write_json(_path(pid, aid), a, indent=1)
    return out if out is not None else a


def list_analyses(pid: str, limit: int = 50) -> list[dict]:
    """The history, newest first; `step` - where each analysis is on the way to test design, `blocked` - what
    holds it back (readiness())."""
    from . import knowledge
    out, project, doc = [], projects.get(pid), knowledge.get(pid)
    for _, text, _ in fs.documents(_dir(pid)):
        try:
            a = _stale(json.loads(text))
        except ValueError:
            continue
        ready = [s for s in a["scenarios"] if not s.get("pending")]
        rv = a.get("review") or {}
        state = readiness(a, project, doc)
        out.append({k: a.get(k) for k in ("id", "created", "finished", "user", "title", "feature", "status", "error",
                                          "url", "work")}
                   | {"scenarios": len(ready), "tests": sum(len(s.get("test_ids") or []) for s in ready),
                      "review": {"verdict": rv.get("verdict"), "at": rv.get("at")} if rv else None,
                      "step": state["step"], "blocked": state["blocked"]})
    return sorted(out, key=lambda x: x["created"], reverse=True)[:limit]


def all_scenarios(pid: str) -> list[dict]:
    """Every ready scenario of the project's analyses, newest analysis first: the pipeline picks from them."""
    out = []
    for _, text, _ in fs.documents(_dir(pid)):
        try:
            a = _stale(json.loads(text))
        except ValueError:
            continue
        for s in a["scenarios"]:
            if s.get("pending"):
                continue
            m = s.get("match") or {}
            if m.get("kind") == "scenario":       # "Не дублировать" automates an earlier scenario without a test
                s = s | {"match": m | {"automated": bool(reuse.scenario_tests(pid, m))}}
            out.append({"layer": "ui"} | {k: s.get(k) for k in (*FIELDS, "id", "test_data", "match")}
                       | {"analysis_id": a["id"], "analysis": a.get("feature") or a.get("title") or "",
                          "created": a["created"], "tests": len(s.get("test_ids") or [])})
    return sorted(out, key=lambda x: x["created"], reverse=True)


# ---------- the generation fills it in (scenarios.generate progress events) ----------

def _fields(data: dict) -> dict:
    out = {k: str(data.get(k) or "") for k in FIELDS}
    out["layer"] = out["layer"] if out["layer"] in LAYERS else "ui"
    out["test_data"] = data_needs(data.get("test_data"))
    out["match"] = reuse.clean(data.get("match"))     # an earlier test or scenario it repeats (reuse.py)
    return out


def data_needs(value) -> list[dict]:
    return [{k: str(d.get(k) or "")[:1000] for k in DATA_FIELDS} for d in (value or [])[:30]
            if isinstance(d, dict) and str(d.get("entity") or "").strip() and str(d.get("name") or "").strip()]


def record_needs(pid: str, aid: str, scids: list[str] | None = None) -> None:
    """The test data of the analysis's scenarios (`scids`; None - all of them) into the application model
    (when the project learns it): the records keep the scenarios that need them, the scenarios - the ids
    of the records."""
    from . import knowledge
    if not projects.get(pid)["pipeline"]["requirements"].get("learn_model") or not fs.exists(_path(pid, aid)):
        return

    def fn(a):
        chosen = [s for s in a["scenarios"] if not s.get("pending") and (scids is None or s["id"] in scids)]
        if chosen:
            knowledge.need(pid, [s | {"ref": f"{aid}:{s['id']}"} for s in chosen])     # sets record_id in place
    _change(pid, aid, fn)


def _new_scenario(data: dict) -> dict:
    return _fields(data) | {"id": _id(), "test_ids": []}


def set_plan(pid: str, aid: str, feature: str, assumptions: list[str], planned: list[dict],
             groups: list[str] | None = None, job: str = "") -> list[dict]:
    """The planned scenarios (to be detailed) go first; the ready ones of an earlier design of the same
    requirements, with their tests, stay after them. The plan itself is kept (`plan`) until the design is done:
    interrupted, the run of the pipeline `job` goes on from it (resume_plan())."""
    def fn(a):
        a["feature"], a["assumptions"] = feature, list(assumptions)
        new = [_new_scenario(s) | {"covers": s.get("covers", "")} for s in planned]
        a["plan"] = {"feature": feature, "assumptions": list(assumptions), "groups": list(groups or []), "job": job,
                     "at": time.time(), "scenarios": new}
        a["scenarios"] = [s | {"pending": True} for s in new] + [s for s in a["scenarios"] if not s.get("pending")]
        return a["scenarios"]
    return _change(pid, aid, fn)


def resume_plan(pid: str, aid: str, job: str) -> dict | None:
    """The plan of a design by the run `job` that was cut off: {feature, assumptions, groups, scenarios (planned),
    done: {position: detailed scenario}} - only the rest is detailed again. The analysis gets the plan back in
    its order: the detailed scenarios where they were planned, the rest pending. None - no such plan."""
    def fn(a):
        p = a.get("plan")
        if not p or not job or p.get("job") != job:
            return {}
        ids = {s["id"] for s in p["scenarios"]}
        ready = {s["id"]: s for s in a["scenarios"] if not s.get("pending")}
        a["scenarios"] = [ready.get(s["id"]) or s | {"pending": True} for s in p["scenarios"]] + \
            [s for s in a["scenarios"] if not s.get("pending") and s["id"] not in ids]
        a["feature"], a["assumptions"] = p["feature"], p["assumptions"]
        return {k: p[k] for k in ("feature", "assumptions", "groups", "scenarios")} | {
            "done": {i: ready[s["id"]] for i, s in enumerate(p["scenarios"]) if s["id"] in ready}}
    if not fs.exists(_path(pid, aid)):
        return None
    return _change(pid, aid, fn) or None


def set_batch(pid: str, aid: str, start: int, detailed: list[dict]) -> list[dict]:
    """Detailed scenarios from position `start` of the plan; one a person already edited stays."""
    def fn(a):
        out = []
        for k, d in enumerate(detailed):
            i = start + k
            if i < len(a["scenarios"]) and a["scenarios"][i].get("pending"):
                old = a["scenarios"][i]
                a["scenarios"][i] = {**_fields(d), "id": old["id"], "test_ids": old["test_ids"],
                                     "match": old.get("match")}     # the plan's match, maybe decided already
                out.append(a["scenarios"][i])
        return out
    return _change(pid, aid, fn)


def finish(pid: str, aid: str, result: dict | None = None, error: str = "", status: str = "") -> dict:
    """Done (`result`: the final set fills what no batch filled) or stopped; plan entries never detailed go
    (stopped or failed, the plan stays for the run that goes on: resume_plan())."""
    def fn(a):
        if result:
            a.pop("plan", None)
            a["feature"], a["assumptions"] = result["feature"], result["assumptions"]
            for i, s in enumerate(a["scenarios"]):
                if s.get("pending") and i < len(result["scenarios"]):
                    a["scenarios"][i] = {**_fields(result["scenarios"][i]), "id": s["id"], "test_ids": s["test_ids"],
                                         "match": s.get("match")}
        a["scenarios"] = [s for s in a["scenarios"] if not s.get("pending")]
        a["status"] = status or ("error" if error else "done")
        a["error"], a["finished"] = error, time.time()
    return _change(pid, aid, fn)


# ---------- a person edits ----------

def clean_fields(data: dict) -> dict:
    out = {k: str(v)[:20_000] for k, v in data.items() if k in FIELDS and v is not None}
    if isinstance(data.get("test_data"), list):
        out["test_data"] = data_needs(data["test_data"])
    if "type" in out and out["type"] not in TYPES:
        raise ValueError("Тип сценария: " + ", ".join(TYPES))
    if "layer" in out and out["layer"] not in LAYERS:
        raise ValueError("Слой сценария: ui или api")
    if "priority" in out and out["priority"] not in PRIORITIES:
        raise ValueError("Приоритет сценария: high, medium или low")
    if data.get("decision") is not None:
        if data["decision"] not in reuse.DECISIONS:
            raise ValueError("Решение по похожему сценарию: new, reuse или refine")
        out["decision"] = data["decision"]
    return out


def update_scenario(pid: str, aid: str, scid: str, data: dict) -> dict:
    fields = clean_fields(data)

    def fn(a):
        s = next((x for x in a["scenarios"] if x["id"] == scid), None)
        if s is None:
            raise KeyError(scid)
        if s.get("pending"):
            raise Conflict("Сценарий ещё детализируется")
        decision = fields.pop("decision", None)
        if decision:
            if not s.get("match"):
                raise Conflict("У сценария нет похожего среди созданных ранее")
            s["match"]["decision"] = decision
            # Reused: the earlier test is this scenario's test as well, nothing is generated.
            if decision == "reuse" and reuse.test_of(pid, s["match"]) and s["match"]["id"] not in s["test_ids"]:
                s["test_ids"].append(s["match"]["id"])
        s.update(fields)
        return s
    out = _change(pid, aid, fn)
    if fields.keys() & {"test_data", "title", "role"}:       # what the scenario needs changed: the model too
        record_needs(pid, aid, [scid])
        out = next((s for s in get(aid)["scenarios"] if s["id"] == scid), out)
    return out


def add_scenario(pid: str, aid: str, data: dict) -> dict:
    s = _new_scenario({"type": "positive", "priority": "medium"} | clean_fields(data)) | {"added": True}

    def fn(a):
        a["scenarios"].append(s)
        return s
    _change(pid, aid, fn)
    if s["test_data"] or s["role"]:
        record_needs(pid, aid, [s["id"]])
        return next((x for x in get(aid)["scenarios"] if x["id"] == s["id"]), s)
    return s


def delete_scenario(pid: str, aid: str, scid: str) -> bool:
    def fn(a):
        if _stale(a)["status"] == "running" and a.get("work") == "design":
            raise Conflict("Дождитесь окончания генерации сценариев")
        n = len(a["scenarios"])
        a["scenarios"] = [s for s in a["scenarios"] if s["id"] != scid]
        return len(a["scenarios"]) < n
    gone = _change(pid, aid, fn)
    if gone:                            # the data only it needed leaves the model
        from . import knowledge
        knowledge.release(pid, f"{aid}:{scid}:")
    return gone


def set_validation(pid: str, aid: str, result: dict) -> None:
    """The validation of the analysis's specification against the documentation standard (validation.py)."""
    if fs.exists(_path(pid, aid)):
        _change(pid, aid, lambda a: a.update(validation=result))


def set_review(pid: str, aid: str, report: dict) -> None:
    """The reviewer's finished report (review.review); its check of the document is the analysis's
    validation as well. A review stopped midway writes only its finished checks (keep_review_part()): the
    previous report stays."""
    if not fs.exists(_path(pid, aid)):
        return

    def fn(a):
        a.pop("review_parts", None)
        a["review"] = report
        if report.get("validation"):
            a["validation"] = report["validation"]
    _change(pid, aid, fn)


def parts_key(*texts: str) -> str:
    """What the checks of a review depend on (the document, the standard, the notes, the settings): a finished
    check is reused only for the same."""
    return hashlib.sha256("\x00".join(texts).encode("utf-8")).hexdigest()[:32]


def review_parts(a: dict | None, key: str) -> dict:
    """The checks a cut-off review of the same (`key`) finished: {name: result} (review.review `done`)."""
    parts = (a or {}).get("review_parts") or {}
    if parts.get("key") != key or time.time() - (parts.get("at") or 0) > PARTS_TTL:
        return {}
    return {k: v for k, v in parts.items() if k not in ("key", "at")}


def keep_review_part(pid: str, aid: str, key: str, name: str, value: dict) -> None:
    """A check of the review is finished: kept until the report is (set_review()), to go on from if cut off."""
    if not fs.exists(_path(pid, aid)):
        return

    def fn(a):
        parts = a.get("review_parts") or {}
        if parts.get("key") != key:
            parts = {"key": key}
        parts[name], parts["at"] = value, time.time()
        a["review_parts"] = parts
    _change(pid, aid, fn)


def decide(pid: str, aid: str, ids: list[str], status: str, answer: str | None = None, note: str | None = None,
           user: str = "", idle: bool = False) -> dict:
    """A person's decision on findings of the review (review.decide): fix by the answer, not relevant, undecided.
    idle - only when nothing goes on the analysis (a fix or a review would lose it or apply half of it): Conflict."""
    from . import review

    def fn(a):
        if idle:
            check_idle(a)
        if not a.get("review"):
            raise KeyError("review")
        review.decide(a["review"], ids, status, answer, note, user)
    return _change(pid, aid, fn)


def set_groups(pid: str, aid: str, groups: list[dict]) -> dict:
    """The findings of the review grouped into themes (review.group) after the review."""
    def fn(a):
        if not a.get("review"):
            raise KeyError("review")
        a["review"]["groups"] = groups
    return _change(pid, aid, fn)


def _begin(a: dict, work: str, user: str) -> None:
    a.update(status="running", started=time.time(), beat=time.time(), work=work, by=user, work_error="",
             work_result="", progress={"stage": "", "log": []})
    a.pop("stop", None)
    a.pop("fix_error", None)        # of a version before work_error


def start_work(pid: str, aid: str, work: str, user: str = "") -> dict:
    """A work on the analysis that leaves it as it was (WORK: fix, answers, group, lifecycle, learn): it is in work,
    what it was is kept for end_work(). Conflict - something goes on it already (busy_text())."""
    def fn(a):
        if _stale(a)["status"] == "running":
            raise Conflict(busy_text(a))
        # kept already - the work before was cut off by a restart of the studio: what it was before that one
        a["before_work"] = a.get("before_work") or a.get("before_fix") or {
            k: a.get(k) for k in ("status", "error", "work", "finished")}
        a.pop("before_fix", None)
        _begin(a, work, user)
    return _change(pid, aid, fn)


def end_work(pid: str, aid: str, error: str = "", result: str = "") -> dict:
    """The work is over: the analysis is what it was before it; `work_error` - why it stopped (a fix: the batches
    done stay), `work_result` - what it did, for a page that did not wait for it."""
    def fn(a):
        was = a.pop("before_work", None) or a.pop("before_fix", None) or {
            "status": "done", "error": "", "work": "review", "finished": None}
        a.update(status=was["status"] or "done", work=was["work"], error=was["error"] or "",
                 finished=was.get("finished"), work_error=error, work_result=result)
        a.pop("stop", None)
    return _change(pid, aid, fn)


def start_fix(pid: str, aid: str, user: str = "") -> dict:
    """The editor fixes the requirements (review.revise)."""
    return start_work(pid, aid, "fix", user)


def end_fix(pid: str, aid: str, error: str = "") -> dict:
    return end_work(pid, aid, error)


def learning(pid: str) -> dict | None:
    """An analysis of the project whose requirements the application model learns from now: the model is about to
    change - it is not confirmed meanwhile."""
    for _, text, _ in fs.documents(_dir(pid)):
        if '"learn"' not in text:
            continue
        try:
            a = _stale(json.loads(text))
        except ValueError:
            continue
        if a["status"] == "running" and a.get("work") == "learn":
            return a
    return None


def revise(pid: str, aid: str, result: dict, user: str = "", run: str = "") -> dict:
    """The editor fixed the document by the findings (review.revise): the requirements become the new text, the
    previous one is kept in `revisions` with the edits; the findings applied are fixed, the others stay to fix.
    run - the fix that made it: its batches (one call each) make one revision."""
    from . import review

    def fn(a):
        if result["fixed"]:
            revs = a.setdefault("revisions", [])
            last = revs[-1] if revs else None
            if run and last and last.get("run") == run:
                last["summary"] = " ".join(s for s in (last["summary"], result["summary"]) if s)
                last["edits"] += result["edits"]
                last["fixed"] += result["fixed"]
            else:
                revs.append({"at": time.time(), "by": user, "requirements": a["requirements"],
                             "summary": result["summary"], "edits": result["edits"], "fixed": result["fixed"]}
                            | ({"run": run} if run else {}))
            a["requirements"] = result["requirements"][:200_000]
            a.pop("learned", None)      # the model learns the corrected text, once every finding is settled
            review.fixed(a["review"], result["fixed"], user)
        res = a["review"].setdefault("resolutions", {})
        for i, why in result["failed"].items():
            if i in res:
                res[i]["error"] = why
        for i in result["fixed"]:
            res[i].pop("error", None)
    return _change(pid, aid, fn)


def set_lifecycle(pid: str, aid: str, lc: dict) -> None:
    """The check against the lifecycle made again (review.with_lifecycle): the decisions on the document stay."""
    from . import review

    def fn(a):
        if not a.get("review"):
            raise KeyError("review")
        a["review"] = review.with_lifecycle(a["review"], lc)
    _change(pid, aid, fn)


def mark_learned(pid: str, aid: str) -> None:
    """The application model has learned from these requirements: the pipeline does not extract them again."""
    if fs.exists(_path(pid, aid)):
        _change(pid, aid, lambda a: a.update(learned=time.time()))


def reviewed(a: dict) -> bool:
    """The reviewer has a report on the requirements of the analysis."""
    return bool(a.get("review"))


STEPS = ("review", "findings", "lifecycle", "scenarios")
LIFECYCLE_CHECK = "жизненный цикл"      # the name of the check against the lifecycle in a report's errors


def readiness(a: dict, project: dict | None = None, doc: dict | None = None) -> dict:
    """The way of the requirements to test design, step by step (STEPS): review - the reviewer checked the
    document and the lifecycle, both checks finished; findings - every finding that blocks test design is
    settled; lifecycle - the application model learned from the requirements ("requirements.learn_model") and a
    person confirmed the parts of it they touch ("requirements.confirm_model"). Scenarios, and tests from them,
    are designed only when all are done: `ready`; `settled` - the review alone (findings and checks, not the
    model) is done; `blocked` - why not, for the person; `step` - the first one
    left. `units` - the parts of the model to confirm (None - all of it) with `parts` - their state."""
    from . import explorer, knowledge, review
    project = project or projects.get(a["project_id"]) or {}
    cfg = (project.get("pipeline") or {}).get("requirements") or {}
    rv = a.get("review")
    blocked: list[str] = []
    errors = (rv or {}).get("errors") or {}
    if not rv:
        blocked.append("требования не проверены ревьюером")
    for name, error in errors.items():
        blocked.append(f"проверка «{name}» не выполнена: {error}")
    # Counted by themes, as a person settles them: a theme of the lifecycle alone (its conflicts and questions,
    # l<i>/lq<i>) is settled with the lifecycle, the others with the document.
    left = [t for t in review.themes(rv) if t["blocking"] and t["status"] in (review.OPEN, review.TODO)]
    lc_left = [t for t in left if all(i.startswith("l") for i in t["ids"])]
    if len(left) > len(lc_left):
        blocked.append(f"не разобраны замечания, блокирующие тест-дизайн: {len(left) - len(lc_left)}")
    if lc_left:
        blocked.append(f"не разобраны расхождения с жизненным циклом: {len(lc_left)}")
    text = a.get("requirements") or ""
    learn = bool(cfg.get("learn_model")) and not text.lstrip().startswith(explorer.MAP_TITLE)
    learned = bool(a.get("learned"))
    if rv and learn and not learned:
        blocked.append("модель приложения ещё не обновлена по этим требованиям")
    confirm = bool(cfg.get("confirm_model"))
    units, parts, state = None, [], "confirmed"
    if confirm and a.get("project_id"):
        doc = doc or knowledge.get(a["project_id"])
        units = knowledge.units_for(a["project_id"], text, doc)
        c = knowledge.confirmation(doc, units)
        state = c["state"]
        parts = [u for u in c["units"] if units is None or u["unit"] in units]
        if state != "confirmed":
            todo = [u["title"] for u in parts if u["state"] != "confirmed"]
            blocked.append("жизненный цикл системы не подтверждён" + (f": {', '.join(todo)}" if todo else "")
                           + (f" (не хватает: {'; '.join(c['missing'])})" if c["missing"] else ""))
    # A failed check of the document holds the review back; a failed check of the lifecycle - the lifecycle.
    done = {"review": bool(rv) and not set(errors) - {LIFECYCLE_CHECK}}
    done["findings"] = done["review"] and len(left) == len(lc_left)
    done["lifecycle"] = (bool(rv) and LIFECYCLE_CHECK not in errors and not lc_left and (not learn or learned)
                         and state == "confirmed")
    step = next((s for s in STEPS[:-1] if not done[s]), "scenarios")
    return {"ready": not blocked, "blocked": blocked, "step": step, "done": done,
            "settled": bool(rv) and not errors and not left,
            "left": len(left) - len(lc_left), "lifecycle_left": len(lc_left),
            "learn": learn, "learned": learned, "confirm": confirm, "units": units, "parts": parts,
            "state": state, "missing": knowledge.missing(doc) if doc else []}


class NotReady(ValueError):
    """Test design from requirements that are not settled yet (readiness())."""


def require_ready(a: dict, project: dict | None = None) -> None:
    r = readiness(a, project)
    if not r["ready"]:
        raise NotReady("Тест-дизайн по этим требованиям начнётся, когда они разобраны и жизненный цикл подтверждён "
                       "(вкладка «Требования»): " + "; ".join(r["blocked"]))


def restart(pid: str, aid: str, force: bool = False, work: str = "review", user: str = "",
            requirements: str = "") -> dict:
    """The analysis is in work again (`work`: review | design): its requirements are reviewed again, or the
    pipeline designs scenarios from them. force - a run of the pipeline that goes on after a restart of the studio
    (its own design may still read as running; any other work is waited for). requirements - a new version of the
    document (the analyst corrected it): it replaces the text, the previous one is kept in `revisions`."""
    def fn(a):
        if _stale(a)["status"] == "running" and not (force and a.get("work") == "design"):
            raise Conflict(busy_text(a))
        a.pop("before_work", None)      # a work cut off: the new work decides what the analysis is
        a.pop("before_fix", None)
        if requirements.strip() and _norm(requirements) != _norm(a["requirements"]):
            a.setdefault("revisions", []).append({
                "at": time.time(), "by": user, "requirements": a["requirements"], "kind": "version",
                "summary": "Загружена новая версия ТЗ: проверяется заново с учётом решений по прежним замечаниям",
                "edits": [], "fixed": []})
            a["requirements"] = requirements[:200_000]
            a.pop("learned", None)      # the model learns the new text, once its findings are settled
        _begin(a, work, user)
        a.update(error="", finished=None)
    return _change(pid, aid, fn)


def delete(pid: str, aid: str) -> bool:
    if not fs.exists(_path(pid, aid)):
        return False
    check_idle(fs.read_json(_path(pid, aid)))
    fs.unlink(_path(pid, aid))
    from . import knowledge
    knowledge.release(pid, f"{aid}:")
    return True


# ---------- tests made from scenarios ----------

def link_test(pid: str, aid: str, scid: str, test_id: str, _seen: frozenset = frozenset()) -> None:
    """The test checks the scenario. A scenario a person decided not to duplicate ("Не дублировать" - the
    earlier scenario checks the same) gives the test to the earlier scenario as well: it is automated."""
    if not fs.exists(_path(pid, aid)) or (aid, scid) in _seen:
        return
    earlier = []

    def fn(a):
        for s in a["scenarios"]:
            if s["id"] == scid:
                if test_id not in s["test_ids"]:
                    s["test_ids"].append(test_id)
                m = s.get("match") or {}
                if m.get("kind") == "scenario" and m.get("decision") == "reuse" and m.get("analysis_id"):
                    earlier.append((m["analysis_id"], m["id"]))
    _change(pid, aid, fn)
    for e_aid, e_scid in earlier:
        link_test(pid, e_aid, e_scid, test_id, _seen | {(aid, scid)})


def with_test(pid: str, test_id: str) -> list[dict]:
    """The scenarios the test checks: {analysis_id, analysis, scenario_id, title}, newest analysis first."""
    out = []
    for _, text, _ in fs.documents(_dir(pid)):
        if test_id not in text:
            continue
        try:
            a = json.loads(text)
        except ValueError:
            continue
        out += [{"analysis_id": a["id"], "analysis": a.get("feature") or a.get("title") or "", "created": a["created"],
                 "scenario_id": s["id"], "title": s.get("title") or ""}
                for s in a.get("scenarios") or [] if test_id in (s.get("test_ids") or [])]
    return sorted(out, key=lambda x: x["created"], reverse=True)


def unlink_test(pid: str, test_id: str) -> None:
    for f, text, _ in fs.documents(_dir(pid)):
        if test_id in text:
            _change(pid, f.stem, lambda a: [s.update(test_ids=[t for t in s.get("test_ids") or [] if t != test_id])
                                            for s in a["scenarios"]])


def view(a: dict) -> dict:
    """The analysis with the tests of each scenario (name, status, last run); deleted tests drop out. The review
    with its findings and themes (review.public)."""
    from . import review, storage
    out = dict(a)
    out["review"] = review.public(a.get("review"))
    out["readiness"] = readiness(a)
    out["scenarios"] = []
    from . import reuse
    for s in a["scenarios"]:
        m = s.get("match") or {}
        if m.get("kind") == "scenario":       # "Не дублировать" automates an earlier scenario without a test
            s = s | {"match": m | {"automated": bool(reuse.scenario_tests(a["project_id"], m))}}
        tests = []
        for tid in s.get("test_ids") or []:
            t = storage.load(tid)
            if t:
                last = t.get("last_run") or {}
                tests.append({"id": t["id"], "name": t["name"], "status": storage.status(t),
                              "last_run": {k: last.get(k) for k in ("status", "passed", "at")} if last else None})
        out["scenarios"].append({"layer": "ui"} | {k: v for k, v in s.items() if k != "added"} | {"tests": tests})
    return out
