"""Audit log (stage 5.4): who changed a test, accepted self-healing, started a run, changed a
connection or rights - and when.

Records are only appended: data/audit/<YYYY-MM>.jsonl, one JSON object per line (with a shared
database: the table audit_log, in insertion order). Each record carries the SHA-256 of the
previous one ("prev") and its own ("hash"), across months too, so an edited, inserted or removed
record breaks the chain: `python -m testgen.audit verify`.

A record: at (UTC, ISO 8601), ts, user, action ("test.update", "heal.accept", "run.start",
"connection.update", "project.access", "auth.login", ...), project_id, target (ids of the
object), status (the HTTP status of the request; "ok" for events outside HTTP), via (web |
mcp | cli | system), ip, details (never request bodies: no passwords or tokens get here).

Export to a SIEM, both optional:
    TESTGEN_AUDIT_SYSLOG=host:port[/tcp]   every record as JSON in a syslog message (facility auth)
    TESTGEN_AUDIT_FILE=/var/log/testgen/audit.jsonl   a copy for Filebeat / Fluent Bit; "-" = stdout
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from pathlib import Path

from . import db, filelock, fs
from .paths import DATA

_lock = threading.Lock()
_syslog: tuple[str, logging.Logger] | None = None
ACTIONS = {
    "auth.login": "Вход", "auth.logout": "Выход", "auth.register": "Регистрация",
    "token.create": "Создан API-токен", "token.delete": "Удалён API-токен",
    "user.create": "Пользователь создан", "user.delete": "Пользователь удалён",
    "project.create": "Проект создан", "project.update": "Настройки проекта", "project.delete": "Проект удалён",
    "project.access": "Участники и доступ", "project.credentials": "Учётные данные приложения",
    "project.mailbox": "Почтовый ящик", "project.llm": "Модель ИИ", "project.notify": "Уведомления", "project.export": "Экспорт проекта",
    "connection.create": "Подключение добавлено", "connection.update": "Подключение изменено",
    "connection.delete": "Подключение удалено", "connection.secret_clear": "Секрет подключения удалён",
    "connection.test": "Проверка подключения",
    "skill.save": "Скилл сохранён", "skill.delete": "Скилл удалён",
    "file.upload": "Файл загружен", "file.delete": "Файл удалён",
    "studio.start": "Сессия Studio", "test.save": "Тест сохранён", "test.update": "Тест изменён",
    "test.meta": "Теги, статус, карантин", "test.delete": "Тест удалён", "test.restore": "Откат версии",
    "test.data": "Подготовка данных", "test.credentials": "Учётные данные теста", "test.export": "Экспорт теста",
    "test.verify": "Проверка мутациями", "test.publish": "Публикация", "test.comment": "Комментарий",
    "heal.accept": "Самолечение принято", "heal.reject": "Самолечение отклонено", "heal.auto": "Самолечение без ревью",
    "run.start": "Прогон теста", "suite.start": "Прогон набора", "pipeline.start": "Конвейер",
    "defect.create": "Дефект в трекер", "baseline.accept": "Новый эталон снимка",
    "sso.settings": "Группы каталога → роли",
}


def _dir() -> Path:
    return DATA / "audit"


def _files() -> list[Path]:
    d = _dir()
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def _file_lock():
    """Between the processes that share data/ (studio, workers, CLI): the chain needs the last hash."""
    return filelock.locked(_dir() / ".lock")


def _last_hash() -> str:
    """Of the last record: only the tail of the newest file is read."""
    for f in reversed(_files()):
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = fh.read().splitlines()
        for line in reversed(lines):
            if line.strip():
                return json.loads(line)["hash"]
    return ""


def digest(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def record(action: str, user: str = "", project_id: str = "", target: str | dict = "", status: int | str = "ok",
           details: dict | None = None, via: str = "web", ip: str = "") -> dict:
    now = time.time()
    rec = {"at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="milliseconds"),
           "ts": round(now, 3), "user": user or "", "action": action, "project_id": project_id or "",
           "target": target or "", "status": status, "via": via, "ip": ip or "", "details": details or {}}
    if fs.remote():
        _append_db(rec)
    else:
        with _lock, _file_lock():
            rec["prev"] = _last_hash()
            rec["hash"] = digest(rec)
            f = _dir() / f"{rec['at'][:7]}.jsonl"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _export(rec)
    return rec


def _append_db(rec: dict) -> None:
    from sqlalchemy import insert, select
    t = db.audit_log
    with fs.lock(DATA / "audit" / "chain"):          # one writer at a time across instances: the chain
        with db.engine().begin() as c:
            last = c.execute(select(t.c.rec).order_by(t.c.id.desc()).limit(1)).scalar()
            rec["prev"] = json.loads(last)["hash"] if last else ""
            rec["hash"] = digest(rec)
            c.execute(insert(t).values(month=rec["at"][:7], ts=rec["ts"], project_id=rec["project_id"] or None,
                                       rec=json.dumps(rec, ensure_ascii=False)))


def _db_records(month: str = "", project_id: str = "", newest_first: bool = True):
    from sqlalchemy import select
    t = db.audit_log
    q = select(t.c.rec).order_by(t.c.id.desc() if newest_first else t.c.id)
    if month:
        q = q.where(t.c.month == month)
    if project_id:
        q = q.where(t.c.project_id == project_id)
    with db.engine().connect() as c:
        for (text,) in c.execution_options(yield_per=500).execute(q):
            yield text


def _export(rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False)
    copy = os.environ.get("TESTGEN_AUDIT_FILE", "").strip()
    if copy == "-":
        print(line, file=sys.stdout, flush=True)
    elif copy:
        try:
            Path(copy).parent.mkdir(parents=True, exist_ok=True)
            with open(copy, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as e:
            logging.getLogger("testgen.audit").warning("TESTGEN_AUDIT_FILE: %s", e)
    target = os.environ.get("TESTGEN_AUDIT_SYSLOG", "").strip()
    if target:
        try:
            _syslogger(target).info("testgen-audit: %s", line)
        except OSError as e:
            logging.getLogger("testgen.audit").warning("TESTGEN_AUDIT_SYSLOG: %s", e)


def _syslogger(target: str) -> logging.Logger:
    global _syslog
    if _syslog and _syslog[0] == target:
        return _syslog[1]
    addr, _, proto = target.partition("/")
    host, _, port = addr.rpartition(":")
    handler = logging.handlers.SysLogHandler(
        address=(host or "localhost", int(port or 514)), facility=logging.handlers.SysLogHandler.LOG_AUTH,
        socktype=socket.SOCK_STREAM if proto.lower() == "tcp" else socket.SOCK_DGRAM)
    log = logging.getLogger(f"testgen.audit.syslog.{target}")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.handlers = [handler]
    _syslog = (target, log)
    return log


def read(month: str = "", project_id: str = "", user: str = "", action: str = "", limit: int = 200) -> list[dict]:
    """Records, newest first. month: YYYY-MM (empty = every month, newest first until `limit`)."""
    out = []
    for line in _db_records(month, project_id) if fs.remote() else _file_lines(month):
        if not line.strip():
            continue
        r = json.loads(line)
        if (project_id and r["project_id"] != project_id) or (user and r["user"] != user) or \
                (action and not r["action"].startswith(action)):
            continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def _file_lines(month: str = ""):
    for f in reversed([f for f in _files() if not month or f.stem == month]):
        yield from reversed(f.read_text("utf-8").splitlines())


def export(month: str) -> str:
    if fs.remote():
        return "".join(line + "\n" for line in _db_records(month, newest_first=False))
    f = _dir() / f"{month}.jsonl"
    return f.read_text("utf-8") if f.exists() else ""


def _chain():
    """(where, line) of every record, oldest first."""
    if fs.remote():
        for i, line in enumerate(_db_records(newest_first=False), 1):
            yield f"audit_log #{i}", line
        return
    for f in _files():
        for i, line in enumerate(f.read_text("utf-8").splitlines(), 1):
            yield f"{f.name}:{i}", line


def verify() -> dict:
    """Walk the chain: {"ok", "records", "error": where it breaks}."""
    prev, n = "", 0
    for where, line in _chain():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            return {"ok": False, "records": n, "error": f"{where}: не JSON"}
        if r.get("prev") != prev:
            return {"ok": False, "records": n, "error": f"{where}: цепочка прервана (запись удалена или вставлена)"}
        if digest(r) != r.get("hash"):
            return {"ok": False, "records": n, "error": f"{where}: запись изменена"}
        prev, n = r["hash"], n + 1
    return {"ok": True, "records": n, "error": ""}


def _cli(argv: list[str]) -> None:
    cmd, *rest = argv or ["help"]
    if cmd == "verify":
        res = verify()
        print(f"Записей: {res['records']}. " + ("Цепочка цела." if res["ok"] else f"НАРУШЕНА: {res['error']}"))
        sys.exit(0 if res["ok"] else 1)
    elif cmd == "export" and rest:
        sys.stdout.write(export(rest[0]))
    elif cmd == "tail":
        for r in reversed(read(limit=int(rest[0]) if rest else 20)):
            print(f"{r['at']}  {r['user'] or '-':16} {r['action']:22} {r['project_id']:12} {r['status']}")
    else:
        print(__doc__ + "\nCommands: verify | export YYYY-MM | tail [N]")


if __name__ == "__main__":
    _cli(sys.argv[1:])
