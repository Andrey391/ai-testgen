"""Language model spending of a project (llm.py keeps the logic, this - the storage).

A month's ledger is {"requests": n, "stages": {stage: {model: {tokens by kind..., "requests": n}}}}.

The `usage` table, a row per request (no lock: workers and instances only insert); the ledger and
the monthly budget are a SUM ... GROUP BY.
"""
from __future__ import annotations

import calendar
import datetime
import time

from sqlalchemy import func, select

from .. import db

FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def empty() -> dict:
    return {"stages": {}, "requests": 0}


class Sql:
    t = db.usage

    @classmethod
    def add(cls, pid: str, month: str, stage: str, model: str, counts: dict) -> None:
        with db.engine().begin() as c:
            c.execute(cls.t.insert().values(project_id=pid, month=month, at=time.time(), stage=stage[:32],
                                            model=model[:128], requests=1, **{f: int(counts[f]) for f in FIELDS}))

    @classmethod
    def ledger(cls, pid: str, month: str) -> dict:
        t = cls.t
        q = select(t.c.stage, t.c.model, func.sum(t.c.requests).label("requests"),
                   *(func.sum(t.c[f]).label(f) for f in FIELDS)) \
            .where(t.c.project_id == pid, t.c.month == month).group_by(t.c.stage, t.c.model)
        d = empty()
        with db.engine().connect() as c:
            for r in c.execute(q):
                d["stages"].setdefault(r.stage, {})[r.model] = {f: int(getattr(r, f) or 0) for f in FIELDS} | {
                    "requests": int(r.requests or 0)}
                d["requests"] += int(r.requests or 0)
        return d

    @classmethod
    def put_month(cls, c, pid: str, month: str, ledger: dict) -> None:
        """A month's ledger of the files: a row per stage and model, dated the first of the month."""
        try:
            y, m = (int(x) for x in month.split("-"))
            at = calendar.timegm(datetime.date(y, m, 1).timetuple())
        except ValueError:
            return
        c.execute(cls.t.delete().where(cls.t.c.project_id == pid, cls.t.c.month == month))
        for stage, models in (ledger.get("stages") or {}).items():
            for model, n in (models or {}).items():
                c.execute(cls.t.insert().values(project_id=pid, month=month, at=float(at), stage=stage[:32],
                                                model=model[:128], requests=int(n.get("requests") or 0),
                                                **{f: int(n.get(f) or 0) for f in FIELDS}))

    @classmethod
    def export(cls, c):
        t = cls.t
        months: dict[tuple[str, str], dict] = {}
        q = select(t.c.project_id, t.c.month, t.c.stage, t.c.model, func.sum(t.c.requests).label("requests"),
                   *(func.sum(t.c[f]).label(f) for f in FIELDS)).group_by(t.c.project_id, t.c.month, t.c.stage,
                                                                            t.c.model)
        for r in c.execute(q):
            d = months.setdefault((r.project_id, r.month), empty())
            d["stages"].setdefault(r.stage, {})[r.model] = {f: int(getattr(r, f) or 0) for f in FIELDS} | {
                "requests": int(r.requests or 0)}
            d["requests"] += int(r.requests or 0)
        for (pid, month), d in months.items():
            yield f"data/projects/{pid}/usage/{month}.json", d

