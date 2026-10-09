"""The application model of a project (knowledge.py keeps the logic, this - the storage).

`km_items`: a row per entity, role, stand data record, fact of the memory and pending update, with the
normalized name for the search, the entity of a data record (the group of an entity) and its status;
the record itself in `body`. `km_meta`: the summary, the confirmation and the other lists of the model.
A write stores only the rows that changed.
"""
from __future__ import annotations

from sqlalchemy import or_, select

from .. import db

KINDS = ("entities", "roles", "data", "memory", "pending")


class Sql:
    t, m = db.km_items, db.km_meta

    @classmethod
    def load(cls, pid: str) -> dict | None:
        """The model as one document (None - the project has none in the tables)."""
        with db.engine().connect() as c:
            meta = c.execute(select(cls.m.c.body).where(cls.m.c.project_id == pid)).first()
            if meta is None:
                return None
            doc = dict(meta.body) | {k: [] for k in KINDS}
            for r in c.execute(select(cls.t.c.kind, cls.t.c.body).where(cls.t.c.project_id == pid)
                               .order_by(cls.t.c.kind, cls.t.c.pos)):
                if r.kind in doc:
                    doc[r.kind].append(dict(r.body))
        return doc

    @classmethod
    def store(cls, pid: str, doc: dict, rows: dict[str, list[dict]]) -> None:
        """The model: `doc` - the meta document, `rows` - kind -> [{id, name, entity, status, body}] in their
        order. Only the rows that changed are written; the ones that are gone are deleted."""
        t = cls.t
        with db.engine().begin() as c:
            have = {(r.kind, r.id): (r.pos, r.body) for r in
                    c.execute(select(t.c.kind, t.c.id, t.c.pos, t.c.body).where(t.c.project_id == pid))}
            keep = set()
            for kind, items in rows.items():
                for pos, row in enumerate(items):
                    k = (kind, row["id"])
                    keep.add(k)
                    if have.get(k) != (pos, row["body"]):
                        db.upsert(c, t, {"project_id": pid, "kind": kind, "pos": pos} | row)
            for kind, rid in set(have) - keep:
                c.execute(t.delete().where(t.c.project_id == pid, t.c.kind == kind, t.c.id == rid))
            db.upsert(c, cls.m, {"project_id": pid, "updated": doc.get("updated"), "body": doc})

    @classmethod
    def search(cls, pid: str, words: list[str], kinds: tuple[str, ...] = ("entities", "data", "roles"),
               limit: int = 60) -> list[tuple[str, dict]]:
        """Records whose name (other names, entity of a data record) contains any of the normalized `words`
        -> [(kind, record)]; the pg_trgm index makes it fast when the database has it."""
        words = [w for w in words if len(w) >= 3][:20]
        if not words:
            return []
        t = cls.t
        pats = ["%" + w.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%" for w in words]
        cond = or_(*[t.c.name.like(p, escape="\\") for p in pats], *[t.c.entity.like(p, escape="\\") for p in pats])
        with db.engine().connect() as c:
            return [(r.kind, dict(r.body)) for r in c.execute(
                select(t.c.kind, t.c.body).where(t.c.project_id == pid, t.c.kind.in_(kinds), cond)
                .order_by(t.c.kind, t.c.pos).limit(limit))]

    @classmethod
    def drop(cls, c, pid: str) -> None:
        c.execute(cls.t.delete().where(cls.t.c.project_id == pid))
        c.execute(cls.m.delete().where(cls.m.c.project_id == pid))

    @classmethod
    def export(cls, c):
        """Each project's model as the document of the old layout."""
        pids = [r.project_id for r in c.execute(select(cls.m.c.project_id))]
        for pid in pids:
            meta = c.execute(select(cls.m.c.body).where(cls.m.c.project_id == pid)).first()
            doc = dict(meta.body) | {k: [] for k in KINDS}
            for r in c.execute(select(cls.t.c.kind, cls.t.c.body).where(cls.t.c.project_id == pid)
                               .order_by(cls.t.c.kind, cls.t.c.pos)):
                if r.kind in doc:
                    doc[r.kind].append(dict(r.body))
            yield f"data/projects/{pid}/knowledge.json", doc
