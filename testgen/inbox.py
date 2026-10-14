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
JOBS = 30           # the newest runs whose items "нужно подтверждение" are looked at, as the list of runs shows them

# The bell polls every 30 s in every open tab, and «Другие проекты» counts every project: a document is read and
# parsed again only when it changed (its modified time), the model's entries are counted again only when the
# model changed (repo.knowledge.Sql.signature) - the duplicates of a model of thousands of records are not cheap.
_SEEN: dict[str, dict[str, tuple[float, object]]] = {}     # folder -> document -> (modified, its digest)
_MODEL: dict[str, tuple[tuple, list[dict]]] = {}            # project -> (signature, entries)


def _entry(kind: str, title: str, route: str, count: int = 1, since: float | None = None, **more) -> dict:
    return {"kind": kind, "title": title, "count": count, "since": since, "route": route} | more


def _when(ts: float | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else ""


def _scan(d, digest) -> list[tuple[str, float, object]]:
    """Each document of a folder, newest first: (name, modified, digest(document)); digest None - skipped. An
    unchanged document is not read again."""
    folder = str(d)
    seen, now, out = _SEEN.get(folder, {}), {}, []
    for path, modified in fs.stamps(d):
        k = str(path)
        hit = seen.get(k)
        if hit is None or hit[0] != modified:
            try:
                hit = (modified, digest(json.loads(fs.read_text(path))))
            except (ValueError, FileNotFoundError, KeyError, AttributeError, TypeError):
                hit = (modified, None)
        now[k] = hit
        if hit[1] is not None:
            out.append((path.stem, modified, hit[1]))
    _SEEN[folder] = now
    return out


def _job_digest(j: dict) -> dict:
    need = sum(1 for i in j.get("items") or []
               if i.get("status") == "needs_attention" and not i.get("test_id") and not i.get("skipped"))
    return {k: j.get(k) for k in ("id", "status", "feature", "created", "finished", "waiting_since")} | {"need": need}


def _jobs(pid: str) -> list[dict]:
    """The runs to look at: the newest ones and every run waiting for a person, however old (a run that waits
    is not saved again, newer ones would push it out). In memory here - as they are, the others - as saved (a
    "live" one no instance works on was cut off by a restart - not waiting for anyone)."""
    out = {j.id: _job_digest(j.state()) for j in pipeline.JOBS.values() if j.project_id == pid}
    for n, (jid, _, j) in enumerate(_scan(pipeline._jobs_dir(pid), _job_digest)):
        if jid in out or (n >= JOBS and j["status"] not in notify.WAITS):
            continue
        if j["status"] in pipeline.LIVE and not pipeline._job_elsewhere(jid):
            j = j | {"status": "error"}
        out[jid] = j | {"id": j.get("id") or jid}
    jobs = sorted(out.values(), key=lambda j: j.get("created") or 0, reverse=True)
    return [j for n, j in enumerate(jobs) if n < JOBS or j["status"] in notify.WAITS]


def _job_entries(pid: str) -> list[dict]:
    out = []
    for n, j in enumerate(_jobs(pid)):
        route = f"/p/{pid}/pipe/{j['id']}"
        name = j.get("feature") or f"запуск от {_when(j.get('created'))}"
        if j.get("status") in notify.WAITS:
            out.append(_entry("job", f"{name}: ждёт {notify.WAITS[j['status']]}", route,
                              since=j.get("waiting_since") or j.get("created"), status=j["status"]))
        if j["need"] and n < JOBS:
            out.append(_entry("attention", f"{name}: нужно подтверждение", route, count=j["need"],
                              since=j.get("finished") or j.get("created")))
    return out


def _review_digest(a: dict) -> tuple | bool:
    """The blocking findings left in a requirements analysis: (id, title, left, since), False - none."""
    rv = a.get("review")
    if not rv or a.get("status") == "running":
        return False
    left = [t for t in review.themes(rv) if t["blocking"] and t["status"] in (review.OPEN, review.TODO)]
    return bool(left) and (a["id"], a.get("title") or a.get("feature") or "Требования", len(left),
                           rv.get("at") or a.get("created"))


def _review_entries(pid: str) -> list[dict]:
    return [_entry("review", f"{title}: замечания ревьюера", f"/p/{pid}/req/{aid}", count=left, since=since)
            for _, _, d in _scan(analyses._dir(pid), _review_digest) if d
            for aid, title, left, since in [d]]


def _model_entries(pid: str, project: dict) -> list[dict]:
    cfg = (project.get("pipeline") or {}).get("requirements") or {}
    sig = knowledge.Sql.signature(pid)
    key = (sig, bool(cfg.get("confirm_model")))
    hit = _MODEL.get(pid)
    if sig is not None and hit and hit[0] == key:
        return hit[1]
    out = _count_model(pid, key[1])
    if sig is not None:
        _MODEL[pid] = (key, out)
    return out


def _count_model(pid: str, confirm: bool) -> list[dict]:
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
    if confirm and (doc["entities"] or doc["roles"]):
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


def _checkpoint_digest(cp: dict) -> dict:
    return {k: cp.get(k) for k in ("id", "name", "scenario", "origin", "updated")}


def _session_entries(pid: str, alive) -> list[dict]:
    # A session of a pipeline run goes on with the run («Продолжить» in the run): the run is the place for it.
    out = []
    for sid, modified, cp in _scan(agent._checkpoints(pid), _checkpoint_digest):
        cp = cp | {"id": cp.get("id") or sid}
        if not (cp.get("origin") or {}).get("job") and not alive(cp):
            out.append(_entry("session", f"Прерванная сессия Studio: «{cp.get('name') or cp.get('scenario') or cp['id']}»",
                              f"/p/{pid}/studio/{cp['id']}", count=1, since=cp.get("updated") or modified))
    return out


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
