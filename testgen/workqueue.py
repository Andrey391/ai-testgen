"""The queue of background work (stage 5.5), in the shared database (db.py).

With TESTGEN_DATABASE_URL, runs of saved tests, suite runs, mutation checks and Planner
explorations do not run in the web server that got the request: they become items of this
queue, and workers (`python -m testgen.worker`, any number of them on any machines; the web
server itself too unless TESTGEN_EMBEDDED_WORKER=off) take them. A big suite is split into one
item per test, so its tests spread over the workers.

An item is taken with an atomic UPDATE ... WHERE status = 'queued': no two workers get the same
one. A worker sends a heartbeat for its items; an item whose worker went silent for DEAD seconds
is failed by whoever notices (reap), and the run or suite it belonged to gets an error.

Also here: `owners` - which instance of the studio holds a live Studio session or a pipeline job
(the other instances pass requests about it there, server.py), and the workers alive.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from . import db, fs

KINDS = ("run", "suite", "verify", "explore")
DEAD = 90             # seconds without a heartbeat: the worker is gone
OWNER_TTL = 90        # an instance refreshes its owners more often than this


def enabled() -> bool:
    """Is background work queued for workers (a shared database) rather than run in this process?"""
    return fs.remote() and os.environ.get("TESTGEN_QUEUE", "on").lower() not in ("off", "0", "false", "no")


def put(kind: str, payload: dict, project_id: str = "", ref: str = "") -> str:
    from sqlalchemy import insert
    assert kind in KINDS, kind
    wid = uuid.uuid4().hex[:16]
    with db.engine().begin() as c:
        c.execute(insert(db.work).values(id=wid, kind=kind, project_id=project_id or None, ref=ref or None,
                                         payload=json.dumps(payload, ensure_ascii=False), status="queued",
                                         attempts=0, created=time.time()))
    return wid


def _item(r) -> dict:
    return {"id": r.id, "kind": r.kind, "project_id": r.project_id, "ref": r.ref, "payload": json.loads(r.payload),
            "status": r.status, "worker": r.worker, "created": r.created, "started": r.started,
            "heartbeat": r.heartbeat, "error": r.error}


def claim(worker: str, limit: int = 1, kinds: tuple[str, ...] = KINDS) -> list[dict]:
    """Take up to `limit` queued items, oldest first."""
    from sqlalchemy import select, update
    t = db.work
    out = []
    with db.engine().connect() as c:
        candidates = [r.id for r in c.execute(select(t.c.id).where(t.c.status == "queued", t.c.kind.in_(kinds))
                                              .order_by(t.c.created).limit(limit * 4))]
    for wid in candidates:
        if len(out) >= limit:
            break
        now = time.time()
        with db.engine().begin() as c:
            got = c.execute(update(t).where(t.c.id == wid, t.c.status == "queued").values(
                status="running", worker=worker, started=now, heartbeat=now, attempts=t.c.attempts + 1)).rowcount
            if got == 1:
                out.append(_item(c.execute(select(t).where(t.c.id == wid)).first()))
    return out


def heartbeat(worker: str, item_ids: list[str], capacity: int, host: str = "") -> None:
    from sqlalchemy import update
    now = time.time()
    e = db.engine()
    with e.begin() as c:
        if item_ids:
            c.execute(update(db.work).where(db.work.c.id.in_(item_ids), db.work.c.worker == worker)
                      .values(heartbeat=now))
        got = c.execute(update(db.workers).where(db.workers.c.id == worker).values(
            heartbeat=now, running=len(item_ids), capacity=capacity)).rowcount
        if not got:
            from sqlalchemy import insert
            c.execute(insert(db.workers).values(id=worker, host=host, started=now, heartbeat=now,
                                                running=len(item_ids), capacity=capacity))


def leave(worker: str) -> None:
    from sqlalchemy import delete
    with db.engine().begin() as c:
        c.execute(delete(db.workers).where(db.workers.c.id == worker))


def finish(item_id: str, error: str = "") -> None:
    from sqlalchemy import update
    with db.engine().begin() as c:
        c.execute(update(db.work).where(db.work.c.id == item_id).values(
            status="failed" if error else "done", finished=time.time(), error=error[:2000] or None))


def reap() -> list[dict]:
    """Running items of workers that went silent: failed now. -> them (to fail their runs)."""
    from sqlalchemy import select, update
    t = db.work
    limit = time.time() - DEAD
    out = []
    with db.engine().connect() as c:
        rows = list(c.execute(select(t).where(t.c.status == "running", t.c.heartbeat < limit)))
    for r in rows:
        with db.engine().begin() as c:
            got = c.execute(update(t).where(t.c.id == r.id, t.c.status == "running", t.c.heartbeat < limit).values(
                status="failed", finished=time.time(), error="Воркер перестал отвечать")).rowcount
        if got:
            out.append(_item(r))
    return out


def active(ref: str) -> bool:
    """Is there a queued item, or a running one with a live worker, about this run / suite / test?"""
    from sqlalchemy import or_, select
    t = db.work
    with db.engine().connect() as c:
        return c.execute(select(t.c.id).where(t.c.ref == ref, or_(
            t.c.status == "queued", (t.c.status == "running") & (t.c.heartbeat >= time.time() - DEAD))).limit(1)
        ).first() is not None


def stats() -> dict:
    """For /metrics and the health check: items by status, workers alive and their slots."""
    from sqlalchemy import func, select
    now = time.time()
    with db.engine().connect() as c:
        by = dict(c.execute(select(db.work.c.status, func.count()).group_by(db.work.c.status)).all())
        alive = list(c.execute(select(db.workers).where(db.workers.c.heartbeat >= now - DEAD)))
        oldest = c.execute(select(func.min(db.work.c.created)).where(db.work.c.status == "queued")).scalar()
    return {"queued": by.get("queued", 0), "running": by.get("running", 0), "done": by.get("done", 0),
            "failed": by.get("failed", 0), "workers": len(alive), "capacity": sum(w.capacity for w in alive),
            "busy": sum(w.running for w in alive), "oldest_queued_seconds": round(now - oldest, 1) if oldest else 0}


def cleanup(days: int = 7) -> None:
    """Finished items older than `days`; dead workers."""
    from sqlalchemy import delete
    with db.engine().begin() as c:
        c.execute(delete(db.work).where(db.work.c.status.in_(("done", "failed")),
                                        db.work.c.finished < time.time() - days * 86400))
        c.execute(delete(db.workers).where(db.workers.c.heartbeat < time.time() - 3600))
        c.execute(delete(db.owners).where(db.owners.c.updated < time.time() - 3600))


# ---------- which instance holds a live object ----------

def set_owner(kind: str, oid: str, url: str) -> None:
    from sqlalchemy import delete, insert
    with db.engine().begin() as c:
        c.execute(delete(db.owners).where(db.owners.c.kind == kind, db.owners.c.id == oid))
        c.execute(insert(db.owners).values(kind=kind, id=oid, url=url, updated=time.time()))


def touch_owners(url: str) -> None:
    from sqlalchemy import update
    with db.engine().begin() as c:
        c.execute(update(db.owners).where(db.owners.c.url == url).values(updated=time.time()))


def owner(kind: str, oid: str) -> str | None:
    """The address of the instance holding it, if that instance is alive."""
    from sqlalchemy import select
    t = db.owners
    with db.engine().connect() as c:
        r = c.execute(select(t.c.url).where(t.c.kind == kind, t.c.id == oid,
                                            t.c.updated >= time.time() - OWNER_TTL)).first()
    return r.url if r else None


def drop_owner(kind: str, oid: str) -> None:
    from sqlalchemy import delete
    with db.engine().begin() as c:
        c.execute(delete(db.owners).where(db.owners.c.kind == kind, db.owners.c.id == oid))
