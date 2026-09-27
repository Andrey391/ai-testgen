"""Run history of saved tests.

    data/projects/<id>/runs/<test>/<run>.json   the run: attempts, step results, events, analysis
    data/projects/<id>/runs/<test>/<run>/       its files: step screenshots, trace.zip, visual diffs
    data/projects/<id>/runs/<test>/index.json   compact summaries, newest last (strips, flaky stats)

A run has one or two attempts: a failed test is re-run once (run.retry_failed);
failed then passed means "flaky". Status: running | passed | flaky | failed | error.
Only the last run.keep_runs runs of a test are kept.
Runs in progress live in LIVE as well, so the UI can poll them every second.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import projects

LIVE: dict[str, dict] = {}
FLAKY_WINDOW = 20        # runs looked at for the flip rate
_RID = re.compile(r"[0-9a-f]{10}")
_FILE = re.compile(r"[\w.-]{1,120}")
_lock = threading.Lock()


def _safe(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s.strip()) or "_"


def _test_dir(pid: str, tid: str) -> Path:
    return projects.path(pid) / "runs" / _safe(tid)


def files_dir(run: dict) -> Path:
    return _test_dir(run["project_id"], run["test_id"]) / run["id"]


def new(test: dict, trigger: str = "manual", suite_id: str = "", user: str = "") -> dict:
    run = {"id": uuid.uuid4().hex[:10], "project_id": test["project_id"], "test_id": test["id"],
           "test_name": test["name"], "trigger": trigger, "suite_id": suite_id, "user": user,
           "status": "running", "started": time.time(), "finished": None, "passed": None, "flaky": False,
           "quarantined": bool((test.get("quarantine") or {}).get("on")),
           "healed": 0, "proposals": 0, "results": [], "events": [], "trace": "", "analysis": None,
           "attempts": [], "error": ""}
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
    d = _test_dir(run["project_id"], run["test_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run['id']}.json").write_text(json.dumps(public(run), ensure_ascii=False, indent=1), "utf-8")


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
    with _lock:
        index = history(run["project_id"], run["test_id"], limit=0)
        index = [x for x in index if x["id"] != run["id"]] + [summary(run)]
        drop, index = index[:-keep] if len(index) > keep else [], index[-keep:]
        d = _test_dir(run["project_id"], run["test_id"])
        (d / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")
    for old in drop:
        (d / f"{old['id']}.json").unlink(missing_ok=True)
        shutil.rmtree(d / old["id"], ignore_errors=True)


def history(pid: str, tid: str, limit: int = FLAKY_WINDOW) -> list[dict]:
    """Summaries of finished runs, oldest first (the last `limit`; 0 = all)."""
    f = _test_dir(pid, tid) / "index.json"
    try:
        index = json.loads(f.read_text("utf-8")) if f.exists() else []
    except ValueError:
        index = []
    return index[-limit:] if limit else index


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
    for f in projects.ROOT.glob(f"*/runs/*/{rid}.json"):
        run = json.loads(f.read_text("utf-8"))
        if run["status"] == "running":
            run.update(status="error", error="Студия была перезапущена во время прогона")
        return run
    return None


def list_for_test(pid: str, tid: str, limit: int = 30) -> list[dict]:
    return list(reversed(history(pid, tid, limit)))


def file(run: dict, name: str) -> Path | None:
    if not _FILE.fullmatch(name or "") or name.startswith("."):
        return None
    f = files_dir(run) / name
    return f if f.is_file() else None


def delete_test(pid: str, tid: str) -> None:
    shutil.rmtree(_test_dir(pid, tid), ignore_errors=True)
