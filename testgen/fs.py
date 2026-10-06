"""Where the studio's data lives: the PostgreSQL database (db.py) and, for binary files, optionally
S3-compatible storage. Nothing of it is kept only on the local disk.

Modules address data by paths: DATA/projects/<id>/project.json, SECRETS/users.json and so on
(paths.py). A path under DATA or SECRETS is the key of a row of `docs`: "data/<rel>" or
"secrets/<rel>" (tests, tasks, runs and model spending have tables of their own: repo/). JSON and
text go into the row; binary files (screenshots, traces, visual baselines, files for upload steps)
too, or - with TESTGEN_S3_BUCKET - into S3-compatible storage (MinIO, Yandex Object Storage, AWS S3)
under the same key, the row keeping their size and time. Only DATA/cache stays local.

A browser reads and writes binary files on the local disk, so the local folders are a cache of
the database: local_path() brings a file there (when the store has a newer copy), push() sends
files written there to the store.

lock(path) serializes read-modify-write of a document across threads, processes and instances
(a PostgreSQL advisory lock).

S3:  TESTGEN_S3_BUCKET, TESTGEN_S3_ENDPOINT (e.g. http://minio:9000, https://storage.yandexcloud.net),
     TESTGEN_S3_ACCESS_KEY, TESTGEN_S3_SECRET_KEY, TESTGEN_S3_REGION (default us-east-1),
     TESTGEN_S3_PREFIX (inside the bucket, default none).
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from . import db
from .paths import DATA, SECRETS

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".har", ".txt", ".yaml", ".yml", ".csv", ".xml"}
LOCAL_ONLY = ("data/cache/",)
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_held = threading.local()
_s3 = None


# ---------- paths <-> keys ----------

def _roots() -> tuple[tuple[str, Path], ...]:
    return (("data", Path(os.path.abspath(DATA))), ("secrets", Path(os.path.abspath(SECRETS))))


def key(p: Path | str) -> str | None:
    """The row key of a path, or None for a path that stays on the local disk (DATA/cache, outside DATA)."""
    ap = Path(os.path.abspath(p))
    for name, root in _roots():
        try:
            rel = ap.relative_to(root).as_posix()
        except ValueError:
            continue
        k = name if rel == "." else f"{name}/{rel}"
        return None if any((k + "/").startswith(x) for x in LOCAL_ONLY) else k
    return None


def _path_of(k: str) -> Path:
    name, _, rel = k.partition("/")
    return dict(_roots())[name] / rel


def _is_blob(k: str) -> bool:
    parts = k.split("/")
    if parts[0] == "data" and len(parts) > 3 and parts[1] == "projects":
        if parts[3] in ("files", "baselines"):
            return True
        if parts[3] == "runs" and len(parts) > 6:        # projects/<p>/runs/<test>/<run>/<file>
            return True
    return PurePosixPath(k).suffix.lower() not in TEXT_SUFFIXES


def _project_of(k: str) -> str | None:
    parts = k.split("/")
    if len(parts) > 2 and parts[1] == "projects":
        return parts[2][:64]
    return None


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------- the object storage ----------

def s3_enabled() -> bool:
    return bool(os.environ.get("TESTGEN_S3_BUCKET", "").strip())


def _s3_client():
    global _s3
    cfg = tuple(os.environ.get(v, "") for v in ("TESTGEN_S3_ENDPOINT", "TESTGEN_S3_ACCESS_KEY", "TESTGEN_S3_SECRET_KEY",
                                                "TESTGEN_S3_REGION"))
    if _s3 is None or _s3[0] != cfg:
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", endpoint_url=cfg[0] or None, aws_access_key_id=cfg[1] or None,
                              aws_secret_access_key=cfg[2] or None, region_name=cfg[3] or "us-east-1",
                              config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}))
        _s3 = (cfg, client)
    return _s3[1]


def _s3_key(k: str) -> str:
    prefix = os.environ.get("TESTGEN_S3_PREFIX", "").strip("/")
    return f"{prefix}/{k}" if prefix else k


def _bucket() -> str:
    return os.environ["TESTGEN_S3_BUCKET"].strip()


def s3_ping() -> bool:
    try:
        _s3_client().head_bucket(Bucket=_bucket())
        return True
    except Exception:
        return False


# ---------- rows ----------

def _row(k: str, body: bool = False):
    from sqlalchemy import select
    t = db.docs
    cols = [t.c.path, t.c.kind, t.c.size, t.c.updated] + ([t.c.body, t.c.data] if body else [])
    with db.engine().connect() as c:
        return c.execute(select(*cols).where(t.c.path == k)).first()


def _put(k: str, *, body: str | None = None, data: bytes | None = None) -> float:
    now = time.time()
    if data is not None and s3_enabled():
        _s3_client().put_object(Bucket=_bucket(), Key=_s3_key(k), Body=data)
        values = {"kind": "s3", "body": None, "data": None, "size": len(data)}
    elif data is not None:
        values = {"kind": "bin", "body": None, "data": data, "size": len(data)}
    else:
        values = {"kind": "json" if k.endswith(".json") else "text", "body": body, "data": None,
                  "size": len(body.encode())}
    values |= {"path": k, "name": k.rsplit("/", 1)[-1][:255], "project_id": _project_of(k), "updated": now}
    with db.engine().begin() as c:
        db.upsert(c, db.docs, values)
    return now


def _content(k: str) -> bytes | str | None:
    """A row's content: str for text, bytes for binary; None if there is no such row."""
    r = _row(k, body=True)
    if r is None:
        return None
    if r.kind == "s3":
        return _s3_client().get_object(Bucket=_bucket(), Key=_s3_key(k))["Body"].read()
    if r.kind == "bin":
        return bytes(r.data or b"")
    return r.body or ""


def _keys(prefix: str, name: str | None = None, limit: int | None = None) -> list:
    """Rows under a prefix ("data/projects/x/tests/"), optionally with a file name."""
    from sqlalchemy import select
    t = db.docs
    q = select(t.c.path, t.c.kind, t.c.size, t.c.updated).where(t.c.path.like(_like(prefix), escape="\\"))
    if name is not None:
        q = q.where(t.c.name == name)
    if limit:
        q = q.limit(limit)
    with db.engine().connect() as c:
        return list(c.execute(q))


def _delete_keys(rows: list) -> None:
    from sqlalchemy import delete
    if not rows:
        return
    s3 = [r.path for r in rows if r.kind == "s3"]
    if s3 and s3_enabled():
        client = _s3_client()
        for i in range(0, len(s3), 1000):
            client.delete_objects(Bucket=_bucket(), Delete={"Objects": [{"Key": _s3_key(k)} for k in s3[i:i + 1000]],
                                                            "Quiet": True})
    with db.engine().begin() as c:
        paths = [r.path for r in rows]
        for i in range(0, len(paths), 500):
            c.execute(delete(db.docs).where(db.docs.c.path.in_(paths[i:i + 500])))


# ---------- the file operations ----------

def read_text(p: Path | str) -> str:
    k = key(p)
    if k is None:
        return Path(p).read_text("utf-8")
    v = _content(k)
    if v is None:
        raise FileNotFoundError(str(p))
    return v.decode("utf-8") if isinstance(v, bytes) else v


def read_json(p: Path | str, default=None):
    """The parsed document, or `default` when there is none (ValueError for a broken one)."""
    try:
        return json.loads(read_text(p))
    except FileNotFoundError:
        return default


def write_text(p: Path | str, text: str) -> None:
    k = key(p)
    if k is None:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(text, "utf-8")
    elif _is_blob(k):
        _put(k, data=text.encode("utf-8"))
    else:
        _put(k, body=text)


def write_json(p: Path | str, obj, indent: int | None = 2) -> None:
    write_text(p, json.dumps(obj, ensure_ascii=False, indent=indent))


def read_bytes(p: Path | str) -> bytes:
    k = key(p)
    if k is None:
        return Path(p).read_bytes()
    v = _content(k)
    if v is None:
        raise FileNotFoundError(str(p))
    return v if isinstance(v, bytes) else v.encode("utf-8")


def write_bytes(p: Path | str, data: bytes) -> None:
    k = key(p)
    if k is None:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_bytes(data)
    else:
        _put(k, data=data)


def exists(p: Path | str) -> bool:
    k = key(p)
    if k is None:
        return Path(p).exists()
    return _row(k) is not None or bool(_keys(k + "/", limit=1))


def is_file(p: Path | str) -> bool:
    k = key(p)
    return Path(p).is_file() if k is None else _row(k) is not None


def is_dir(p: Path | str) -> bool:
    k = key(p)
    return Path(p).is_dir() if k is None else bool(_keys(k + "/", limit=1))


def mtime(p: Path | str) -> float:
    k = key(p)
    if k is None:
        return Path(p).stat().st_mtime
    r = _row(k)
    if r is None:
        raise FileNotFoundError(str(p))
    return r.updated


def size(p: Path | str) -> int:
    k = key(p)
    if k is None:
        return Path(p).stat().st_size
    r = _row(k)
    if r is None:
        raise FileNotFoundError(str(p))
    return int(r.size)


def unlink(p: Path | str) -> bool:
    k = key(p)
    if k is None:
        if Path(p).is_file():
            Path(p).unlink()
            return True
        return False
    rows = [r for r in _keys(k) if r.path == k]
    _delete_keys(rows)
    Path(p).unlink(missing_ok=True)       # the local copy
    return bool(rows)


def rmtree(p: Path | str) -> None:
    k = key(p)
    if k is not None:
        _delete_keys(_keys(k + "/"))
    shutil.rmtree(p, ignore_errors=True)


def glob(d: Path | str, pattern: str) -> list[Path]:
    """Like Path.glob for patterns of plain segments ("*.json", "*/runs/*/<id>.json"); no "**"."""
    k = key(d)
    if k is None:
        return list(Path(d).glob(pattern)) if Path(d).is_dir() else []
    segments = pattern.split("/")
    last = segments[-1]
    rows = _keys(k + "/", name=None if any(ch in last for ch in "*?[") else last)
    out = []
    for r in rows:
        rel = r.path[len(k) + 1:].split("/")
        if len(rel) == len(segments) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(rel, segments)):
            out.append(_path_of(r.path))
    return out


def documents(d: Path | str, pattern: str = "*.json") -> list[tuple[Path, str, float]]:
    """Every text file of a folder that matches `pattern`: (path, text, modified), newest first -
    one query instead of one per file."""
    k = key(d)
    out = []
    if k is None:
        for p in Path(d).glob(pattern) if Path(d).is_dir() else []:
            try:
                out.append((p, p.read_text("utf-8"), p.stat().st_mtime))
            except OSError:          # removed meanwhile
                continue
    else:
        from sqlalchemy import select
        t = db.docs
        segments = pattern.split("/")
        q = select(t.c.path, t.c.body, t.c.updated).where(t.c.path.like(_like(k + "/"), escape="\\"),
                                                          t.c.kind.in_(("json", "text")))
        with db.engine().connect() as c:
            for r in c.execute(q):
                rel = r.path[len(k) + 1:].split("/")
                if len(rel) == len(segments) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(rel, segments)):
                    out.append((_path_of(r.path), r.body or "", r.updated))
    return sorted(out, key=lambda x: x[2], reverse=True)


def iterdir(d: Path | str) -> list[Path]:
    """Children of a folder: files and sub-folders."""
    k = key(d)
    if k is None:
        return list(Path(d).iterdir()) if Path(d).is_dir() else []
    names = {r.path[len(k) + 1:].split("/")[0] for r in _keys(k + "/")}
    return [Path(d) / n for n in sorted(names)]


# ---------- the local cache of binary files ----------

def local_path(p: Path | str) -> Path:
    """The file on the local disk, brought from the store if the store has a newer copy."""
    p = Path(p)
    k = key(p)
    if k is None:
        return p
    r = _row(k)
    if r is None:
        return p
    if not p.is_file() or p.stat().st_mtime < r.updated - 0.001:
        data = read_bytes(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:6]}")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        os.utime(p, (r.updated, r.updated))
    return p


def push(p: Path | str) -> int:
    """Send a local file, or every file of a local folder, that the store does not have yet. -> files sent."""
    p = Path(p)
    if key(p) is None or not p.exists():
        return 0
    files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and not f.name.startswith(".")]
    stored = {r.path: r.updated for r in _keys(key(p) + "/")} if p.is_dir() else \
        ({key(p): _row(key(p)).updated} if _row(key(p)) else {})
    n = 0
    for f in files:
        k = key(f)
        if k in stored and f.stat().st_mtime <= stored[k] + 0.001:
            continue
        at = _put(k, data=f.read_bytes()) if _is_blob(k) else _put(k, body=f.read_text("utf-8"))
        os.utime(f, (at, at))
        n += 1
    return n


# ---------- locks ----------

def _local_lock(name: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(name, threading.RLock())


@contextlib.contextmanager
def lock(p: Path | str, timeout: float = 120):
    """Exclusive access to a document for a read-modify-write, in this process and in every process
    of every instance (a path outside the database: in this process). Re-entrant in one thread."""
    k = key(p)
    name = str(k or os.path.abspath(p))
    held = getattr(_held, "names", None)
    if held is None:
        held = _held.names = {}
    with _local_lock(name):
        if held.get(name) or k is None:
            held[name] = held.get(name, 0) + 1
            try:
                yield
            finally:
                held[name] -= 1
            return
        with _db_lock(name, timeout):
            held[name] = 1
            try:
                yield
            finally:
                held[name] = 0


@contextlib.contextmanager
def _db_lock(name: str, timeout: float):
    from sqlalchemy import text
    conn = db.engine().connect()
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": db.lock_id(name)})
        conn.commit()
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": db.lock_id(name)})
            conn.commit()
        finally:
            conn.close()


# ---------- moving an installation ----------

def import_tree(src: Path, root: Path) -> int:
    """Copy every file of a local folder (data/ or secrets/ of an older version, kept in folders) into
    the store, as if it were at `root`; tests, tasks, runs and spending go into their tables, the
    audit log (data/audit/*.jsonl) into audit_log. -> files copied."""
    from . import audit, repo
    n = 0
    logs = []
    for f in sorted(Path(src).rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.suffix == ".tmp":
            continue
        target = root / f.relative_to(src)
        k = key(target)
        if k is None:
            continue
        if k.startswith("data/audit/") and k.endswith(".jsonl"):
            logs.append(f)
            continue
        if repo.owns(k):
            repo.import_text(k, f.read_text("utf-8"))
        elif _is_blob(k):
            _put(k, data=f.read_bytes())
        else:
            _put(k, body=f.read_text("utf-8"))
        n += 1
    if logs:
        n += audit.import_files(logs)
    return n


def export_tree(root: Path, dest: Path) -> int:
    k = key(root)
    n = 0
    for r in _keys(k + "/") if k else []:
        f = Path(dest) / r.path[len(k) + 1:]
        f.parent.mkdir(parents=True, exist_ok=True)
        v = _content(r.path)
        f.write_bytes(v if isinstance(v, bytes) else v.encode("utf-8"))
        n += 1
    if k:
        from . import repo
        with db.engine().connect() as c:
            for path, text in repo.export_docs(c, k):
                f = Path(dest) / path[len(k) + 1:]
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, "utf-8")
                n += 1
    return n
