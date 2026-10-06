"""Run history of saved tests.

    data/projects/<id>/runs/<test>/<run>.json   the run: attempts, step results, events, analysis
    data/projects/<id>/runs/<test>/<run>/       its files: step screenshots, trace.zip, visual diffs
    data/projects/<id>/runs/<test>/index.json   compact summaries, newest last (strips, flaky stats)

With a shared database the records and summaries are rows of the `runs` table instead
(repo/runs.py); the files of a run stay files by path.

A run has one or two attempts: a failed test is re-run once (run.retry_failed);
failed then passed means "flaky". Status: running | passed | flaky | failed | error.
Only the last run.keep_runs runs of a test are kept.
Runs in progress live in LIVE as well, so the UI can poll them every second. With a shared
database (fs.py) a run may go on in a worker: its record is saved as it progresses, its files
are sent to the store as they appear, and the web server reads both from there.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from . import fs
from .repo import runs as repo

LIVE: dict[str, dict] = {}
FLAKY_WINDOW = 20        # runs looked at for the flip rate
STALE = 300              # s: a running run nobody saved for this long was left by a stopped process
_RID = re.compile(r"[0-9a-f]{10}")
_FILE = re.compile(r"[\w.-]{1,120}")


def files_dir(run: dict) -> Path:
    return repo.test_dir(run["project_id"], run["test_id"]) / run["id"]


def new(test: dict, trigger: str = "manual", suite_id: str = "", user: str = "", live: bool = True) -> dict:
    """A run record, saved; `live=False` when another process (a worker) will run it."""
    run = {"id": uuid.uuid4().hex[:10], "project_id": test["project_id"], "test_id": test["id"],
           "test_name": test["name"], "trigger": trigger, "suite_id": suite_id, "user": user,
           "status": "running", "started": time.time(), "finished": None, "passed": None, "flaky": False,
           "quarantined": bool((test.get("quarantine") or {}).get("on")),
           "healed": 0, "proposals": 0, "results": [], "events": [], "trace": "", "analysis": None,
           "attempts": [], "error": ""}
    if live:
        LIVE[run["id"]] = run
    save(run)
    return run


def _strip(results: list[dict]) -> list[dict]:
    """Screenshots are files next to the run; base64 stays out of the JSON."""
    return [{k: v for k, v in r.items() if k != "screenshot"} for r in results]


def public(run: dict) -> dict:
    return run | {"results": _strip(run.get("results") or []),
                  "attempts": [a | {"results": _strip(a.get("results") or [])} for a in run.get("attempts") or []]}


def save(run: dict) -> None:
    run["saved"] = time.time()
    repo.Sql.save(public(run))
    fs.push(files_dir(run))           # screenshots of the steps so far, the trace

def outcomes(run: dict) -> list[bool]:
    """Pass/fail of each attempt, in order."""
    return [bool(a.get("passed")) for a in run.get("attempts") or []] or (
        [bool(run["passed"])] if run.get("passed") is not None else [])


def summary(run: dict) -> dict:
    return {k: run.get(k) for k in ("id", "status", "started", "finished", "healed", "proposals", "trigger",
                                    "suite_id", "quarantined")} | {"outcomes": outcomes(run)}


def finish(run: dict, keep: int = 30) -> None:
    run["finished"] = time.time()
    LIVE.pop(run["id"], None)
    save(run)
    from . import monitoring
    monitoring.RUNS.labels(run.get("status") or "", run.get("trigger") or "").inc()
    d = repo.test_dir(run["project_id"], run["test_id"])
    for old in repo.Sql.finished(run["project_id"], run["test_id"], summary(run), keep):
        fs.rmtree(d / old)


def history(pid: str, tid: str, limit: int = FLAKY_WINDOW) -> list[dict]:
    """Summaries of finished runs, oldest first (the last `limit`; 0 = all)."""
    return repo.Sql.history(pid, tid, limit)


def histories(pid: str, tids: list[str], limit: int = FLAKY_WINDOW) -> dict[str, list[dict]]:
    """history() of many tests of a project at once (one query with the database)."""
    return repo.Sql.histories(pid, tids, limit)


def flip_rate(index: list[dict]) -> float | None:
    """How often the result changes between consecutive attempts (a flaky run counts
    as a change): 0 = stable, 1 = alternates every time. None with too little history."""
    seq = [o for x in index for o in x.get("outcomes") or []]
    if len(seq) < 3:
        return None
    return round(sum(a != b for a, b in zip(seq, seq[1:])) / (len(seq) - 1), 3)


def get(rid: str) -> dict | None:
    if rid in LIVE:
        return LIVE[rid]
    if not _RID.fullmatch(rid or ""):
        return None
    run = repo.Sql.get(rid)
    if run and run["status"] == "running" and _abandoned(run):
        run.update(status="error", error="Студия была перезапущена во время прогона")
    return run


def _abandoned(run: dict) -> bool:
    """A running run found only in the store (not in LIVE): a worker may still be on it - it saves the
    run as it goes."""
    from . import workqueue
    return not workqueue.active(run["id"]) and time.time() - (run.get("saved") or run["started"]) > STALE


def list_for_test(pid: str, tid: str, limit: int = 30) -> list[dict]:
    return list(reversed(history(pid, tid, limit)))


def file(run: dict, name: str) -> Path | None:
    """A file of the run on the local disk (brought from the shared store if needed)."""
    if not _FILE.fullmatch(name or "") or name.startswith("."):
        return None
    f = fs.local_path(files_dir(run) / name)
    return f if f.is_file() else None


def delete_test(pid: str, tid: str) -> None:
    repo.Sql.delete_test(pid, tid)
