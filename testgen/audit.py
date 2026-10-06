"""Audit log (stage 5.4): who changed a test, accepted self-healing, started a run, changed a
connection or rights - and when.

Records are only appended: rows of the table audit_log (db.py), in insertion order. Each record
carries the SHA-256 of the previous one ("prev") and its own ("hash"), across months too, so an
edited, inserted or removed record breaks the chain: `python -m testgen.audit verify`. The log of
an older version kept in files (data/audit/<YYYY-MM>.jsonl) comes in with `db import-files`.

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
import time
from pathlib import Path

from . import db, fs
from .paths import DATA

_syslog: tuple[str, logging.Logger] | None = None
ACTIONS = {
    "auth.login": "Вход", "auth.logout": "Выход", "auth.register": "Регистрация",
    "token.create": "Создан API-токен", "token.delete": "Удалён API-токен",
    "user.create": "Пользователь создан", "user.delete": "Пользователь удалён",
    "project.create": "Проект создан", "project.update": "Настройки проекта", "project.delete": "Проект удалён",
    "project.access": "Участники и доступ", "project.credentials": "Учётные данные приложения",
    "account.create": "Учётная запись добавлена", "account.update": "Учётная запись изменена",
    "account.delete": "Учётная запись удалена",
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


def digest(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def record(action: str, user: str = "", project_id: str = "", target: str | dict = "", status: int | str = "ok",
           details: dict | None = None, via: str = "web", ip: str = "") -> dict:
    now = time.time()
    rec = {"at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="milliseconds"),
           "ts": round(now, 3), "user": user or "", "action": action, "project_id": project_id or "",
           "target": target or "", "status": status, "via": via, "ip": ip or "", "details": details or {}}
    _append_db(rec)
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


def import_files(files: list[Path]) -> int:
    """The log of an older version (data/audit/<YYYY-MM>.jsonl), as it was, chain included - only into
    an empty audit_log: logs of two installations would break each other's chain. -> records."""
    from sqlalchemy import func, insert, select
    t = db.audit_log
    with fs.lock(DATA / "audit" / "chain"), db.engine().begin() as c:
        if c.execute(select(func.count()).select_from(t)).scalar():
            logging.getLogger("testgen.audit").warning("Журнал действий уже не пуст: записи из %s не перенесены",
                                                       ", ".join(str(f) for f in files))
            return 0
        n = 0
        for f in sorted(files, key=lambda f: f.stem):
            for line in Path(f).read_text("utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    c.execute(insert(t).values(month=r["at"][:7], ts=r["ts"], project_id=r.get("project_id") or None,
                                               rec=line.strip()))
                    n += 1
    return n


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
    for line in _db_records(month, project_id):
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


def export(month: str) -> str:
    return "".join(line + "\n" for line in _db_records(month, newest_first=False))


def _chain():
    """(where, line) of every record, oldest first."""
    for i, line in enumerate(_db_records(newest_first=False), 1):
        yield f"audit_log #{i}", line


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
    from .paths import utf8_console
    utf8_console()
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
