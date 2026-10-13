"""«Ждёт вас»: what in a project waits for a person's decision, gathered from where it lives.

An entry: kind, title, count (how many things in it), since (a unix time: how long it has waited), route (the
address of the screen where it is decided, the frontend's router: /p/<project>/…). Nothing here asks the model:
only stored state is read, so the bell can poll it.

    job        a run of the pipeline waits for a decision (awaiting_review / _model / _selection / _reuse)
    attention  items of a run marked "нужно подтверждение" (a test of theirs was not saved yet)
    review     requirements with findings of the reviewer that block test design, not settled yet
    model      the application model: updates to accept, duplicates, parts not confirmed (or changed since)
    heal       tests with self-healing proposals to review
    session    Studio sessions interrupted by a restart of the studio
    task       the user's open tasks overdue or due today
"""
from __future__ import annotations

import datetime
import json

from . import agent, analyses, fs, knowledge, notify, pipeline, projects, review, storage
from .repo import tasks as tasks_repo
from . import tasks as tasks_mod

KINDS = ("job", "attention", "review", "model", "heal", "session", "task")
JOBS = 30           # the newest runs looked at, as the list of runs shows them


def _entry(kind: str, title: str, route: str, count: int = 1, since: float | None = None, **more) -> dict:
    return {"kind": kind, "title": title, "count": count, "since": since, "route": route} | more


def _when(ts: float | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else ""


def _jobs(pid: str) -> list[dict]:
    """The newest runs: the ones in memory here as they are, the others as saved (a "live" one no instance
    works on was cut off by a restart - not waiting for anyone)."""
    out = {j.id: j.state() for j in pipeline.JOBS.values() if j.project_id == pid}
    for f, text, _ in fs.documents(pipeline._jobs_dir(pid))[:JOBS]:
        if f.stem in out:
            continue
        try:
            j = json.loads(text)
        except ValueError:
            continue
        if j.get("status") in pipeline.LIVE and not pipeline._job_elsewhere(f.stem):
            j["status"] = "error"
        out[f.stem] = j
    return sorted(out.values(), key=lambda j: j.get("created") or 0, reverse=True)[:JOBS]


def _job_entries(pid: str) -> list[dict]:
    out = []
    for j in _jobs(pid):
        route = f"/p/{pid}/pipe/{j['id']}"
        name = j.get("feature") or f"запуск от {_when(j.get('created'))}"
        if j.get("status") in notify.WAITS:
            out.append(_entry("job", f"{name}: ждёт {notify.WAITS[j['status']]}", route,
                              since=j.get("waiting_since") or j.get("created"), status=j["status"]))
        need = [i for i in j.get("items") or []
                if i.get("status") == "needs_attention" and not i.get("test_id") and not i.get("skipped")]
        if need:
            out.append(_entry("attention", f"{name}: нужно подтверждение", route, count=len(need),
                              since=j.get("finished") or j.get("created")))
    return out


def _review_entries(pid: str) -> list[dict]:
    out = []
    for _, text, _ in fs.documents(analyses._dir(pid)):
        try:
            a = json.loads(text)
        except ValueError:
            continue
        rv = a.get("review")
        if not rv or a.get("status") == "running":
            continue
        left = [t for t in review.themes(rv) if t["blocking"] and t["status"] in (review.OPEN, review.TODO)]
        if left:
            out.append(_entry("review", f"{a.get('title') or a.get('feature') or 'Требования'}: замечания ревьюера",
                              f"/p/{pid}/req/{a['id']}", count=len(left), since=rv.get("at") or a.get("created")))
    return out


def _model_entries(pid: str, project: dict) -> list[dict]:
    doc = knowledge.get(pid)
    base = f"/p/{pid}/settings/data"
    out = []
    if doc["pending"]:
        out.append(_entry("model", "Модель приложения: обновления ждут решения", f"{base}/pending",
                          count=len(doc["pending"]), since=min(p.get("at") or 0 for p in doc["pending"]) or None))
    dups = knowledge.duplicates(doc)
    if dups:
        out.append(_entry("model", "Модель приложения: похожие записи — проверить дубли", f"{base}/dups",
                          count=len(dups), since=doc.get("updated")))
    cfg = (project.get("pipeline") or {}).get("requirements") or {}
    if cfg.get("confirm_model") and (doc["entities"] or doc["roles"]):
        todo = [u for u in knowledge.confirmation(doc)["units"] if u["state"] != "confirmed"]
        if todo:
            changed = any(u["state"] == "changed" for u in todo)
            out.append(_entry("model", "Жизненный цикл: " + ("изменился после подтверждения" if changed
                                                              else "не подтверждён"), f"{base}/overview",
                              count=len(todo), since=doc.get("updated")))
    return out


def _heal_entries(pid: str) -> list[dict]:
    return [_entry("heal", f"«{t['name']}»: самолечение ждёт ревью", f"/p/{pid}/tests/{t['id']}", count=t["count"],
                   since=t["updated"] or None) for t in storage.healing(pid)]


def _session_entries(pid: str, alive) -> list[dict]:
    # A session of a pipeline run goes on with the run («Продолжить» in the run): the run is the place for it.
    return [_entry("session", f"Прерванная сессия Studio: «{cp.get('name') or cp.get('scenario') or cp['id']}»",
                   f"/p/{pid}/studio/{cp['id']}", count=1, since=cp.get("updated"))
            for cp in agent.list_checkpoints(pid) if not (cp.get("origin") or {}).get("job") and not alive(cp)]


def _task_entries(pid: str, user: str) -> list[dict]:
    if not user:
        return []
    today = datetime.date.today().isoformat()
    out = []
    for t in tasks_repo.Sql.all(pid, sorted(tasks_mod.OPEN), user):
        due = t.get("due") or ""
        if due and due <= today:
            out.append(_entry("task", f"«{t['title']}»: " + ("срок сегодня" if due == today else f"просрочена ({due})"),
                              f"/p/{pid}/tasks/{t['id']}", since=t.get("created"), due=due))
    return out


def project_inbox(pid: str, user: str = "", alive=lambda cp: False) -> list[dict]:
    """Every entry of the project, by kind (KINDS), the oldest first within a kind. `alive` - the session of a
    checkpoint still runs (not interrupted)."""
    project = projects.get(pid) or {}
    entries = (_job_entries(pid) + _review_entries(pid) + _model_entries(pid, project) + _heal_entries(pid)
               + _session_entries(pid, alive) + _task_entries(pid, user))
    return sorted(entries, key=lambda e: (KINDS.index(e["kind"]), e["since"] or 0))


def total(entries: list[dict]) -> int:
    """What the bell counts: the places to decide in (a run with 50 items to confirm is one - its entry tells how
    many), so one big run does not drown the rest."""
    return len(entries)
