"""Run history: run records and their summaries (runs.py keeps the logic, this - the storage).

The `runs` table: a row per run, summaries are its columns, history and flaky statistics one query.
The files of a run (screenshots, trace) are files by path: data/projects/<id>/runs/<test>/<run>/.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select

from .. import db, fs, projects

SUMMARY = ("id", "status", "started", "finished", "healed", "proposals", "trigger", "suite_id", "quarantined")


def safe(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s.strip()) or "_"


def test_dir(pid: str, tid: str):
    return projects.path(pid) / "runs" / safe(tid)


def _outcomes(run: dict) -> list[bool]:
    return [bool(a.get("passed")) for a in run.get("attempts") or []] or (
        [bool(run["passed"])] if run.get("passed") is not None else [])


class Sql:
    t = db.runs

    @classmethod
    def row(cls, run: dict) -> dict:
        started, finished = float(run.get("started") or 0), run.get("finished")
        return {"id": run["id"], "project_id": run["project_id"], "test_id": run["test_id"],
                "status": run.get("status") or "running", "trigger": run.get("trigger") or "",
                "suite_id": run.get("suite_id") or "", "started_by": run.get("user") or "",
                "started": started, "finished": finished,
                "duration": round(finished - started, 3) if finished else None,
                "passed": run.get("passed"), "flaky": bool(run.get("flaky")),
                "quarantined": bool(run.get("quarantined")), "healed": int(run.get("healed") or 0),
                "proposals": int(run.get("proposals") or 0), "outcomes": _outcomes(run), "report": run}

    @classmethod
    def put(cls, c, run: dict) -> None:
        db.upsert(c, cls.t, cls.row(run))

    @classmethod
    def save(cls, run: dict) -> None:
        with db.engine().begin() as c:
            cls.put(c, run)

    @classmethod
    def get(cls, rid: str) -> dict | None:
        with db.engine().connect() as c:
            r = c.execute(select(cls.t.c.report).where(cls.t.c.id == rid)).first()
        return dict(r.report) if r else None

    @classmethod
    def _summary(cls, r) -> dict:
        return {k: getattr(r, k) for k in SUMMARY} | {"outcomes": list(r.outcomes or [])}

    @classmethod
    def _cols(cls):
        return [cls.t.c[k] for k in SUMMARY] + [cls.t.c.outcomes]

    @classmethod
    def history(cls, pid: str, tid: str, limit: int) -> list[dict]:
        t = cls.t
        q = select(*cls._cols()).where(t.c.project_id == pid, t.c.test_id == tid, t.c.finished.isnot(None)) \
            .order_by(t.c.finished.desc())
        if limit:
            q = q.limit(limit)
        with db.engine().connect() as c:
            return [cls._summary(r) for r in reversed(c.execute(q).all())]

    @classmethod
    def histories(cls, pid: str, tids: list[str], limit: int) -> dict[str, list[dict]]:
        """The last `limit` summaries of every test of a project in one query (a window function)."""
        t = cls.t
        n = func.row_number().over(partition_by=t.c.test_id, order_by=t.c.finished.desc()).label("n")
        sub = select(*cls._cols(), t.c.test_id.label("tid"), n) \
            .where(t.c.project_id == pid, t.c.finished.isnot(None)).subquery()
        q = select(sub).order_by(sub.c.tid, sub.c.finished)
        if limit:
            q = q.where(sub.c.n <= limit)
        out: dict[str, list[dict]] = {tid: [] for tid in tids}
        with db.engine().connect() as c:
            for r in c.execute(q):
                if r.tid in out:
                    out[r.tid].append(cls._summary(r))
        return out

    @classmethod
    def finished(cls, pid: str, tid: str, summary: dict, keep: int) -> list[str]:
        t = cls.t
        with db.engine().begin() as c:
            old = [r.id for r in c.execute(
                select(t.c.id).where(t.c.project_id == pid, t.c.test_id == tid, t.c.finished.isnot(None))
                .order_by(t.c.finished.desc()).offset(keep))]
            if old:
                c.execute(t.delete().where(t.c.id.in_(old)))
        return old

    @classmethod
    def delete_test(cls, pid: str, tid: str) -> None:
        with db.engine().begin() as c:
            c.execute(cls.t.delete().where(cls.t.c.project_id == pid, cls.t.c.test_id == tid))
        fs.rmtree(test_dir(pid, tid))          # the files of the runs

    @classmethod
    def export(cls, c):
        """Records and index.json of every test, as the files of the old layout."""
        t = cls.t
        index: dict[tuple[str, str], list[dict]] = {}
        for r in c.execute(select(t.c.project_id, t.c.test_id, t.c.report, *cls._cols()[1:])
                           .order_by(t.c.project_id, t.c.test_id, t.c.started)):
            folder = f"data/projects/{r.project_id}/runs/{safe(r.test_id)}"
            yield f"{folder}/{r.report['id']}.json", r.report
            if r.finished is not None:
                index.setdefault((r.project_id, folder), []).append(
                    {k: getattr(r, k) for k in SUMMARY if k != "id"} | {"id": r.report["id"],
                                                                         "outcomes": list(r.outcomes or [])})
        for (_, folder), items in index.items():
            yield f"{folder}/index.json", sorted(items, key=lambda x: x["finished"])


