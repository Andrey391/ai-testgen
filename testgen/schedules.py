"""Scheduled suite runs: a project's regression goes by itself - every day at given times on given weekdays
(nightly runs) or every N hours.

A schedule is what "Запустить набор" takes (tags or tests, drafts, screen) plus when. The schedules of a
project are one document, data/projects/<id>/schedules.json; each keeps `next` (the time of its next run)
and `last` (what its last turn did: the suite it started, or why it started none).

tick() runs in every instance of the studio (server._schedules, every TICK seconds): under the lock of the
project's document it takes the schedules whose time came and moves their `next` on, so a turn starts once
however many instances there are. A turn missed by more than GRACE seconds (the studio was stopped) is not
caught up - it is recorded as missed and the schedule waits for its next time. A turn whose previous suite
still runs is skipped: two runs of one regression would only fight for the stand.
"""
from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import fs, projects, storage, suite

TICK = 30
GRACE = 3600            # a turn later than this (the studio was down) is skipped, not caught up
MIN_HOURS, MAX_HOURS = 1, 168
MAX_TIMES = 24
DEFAULT_TZ = "Europe/Moscow"
TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class Invalid(ValueError):
    pass


def _file(pid: str):
    return projects.path(pid) / "schedules.json"


def list_for(pid: str) -> list[dict]:
    return fs.read_json(_file(pid), {}).get("items", [])


def get(pid: str, sid: str) -> dict | None:
    return next((s for s in list_for(pid) if s["id"] == sid), None)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid(f"Неизвестный часовой пояс: {name}") from None


def clean(body: dict) -> dict:
    """The fields of a schedule a person sets, checked."""
    name = str(body.get("name") or "").strip()[:120]
    mode = body.get("mode") or "daily"
    if mode not in ("daily", "interval"):
        raise Invalid("Режим расписания: daily или interval")
    out = {"name": name, "enabled": bool(body.get("enabled", True)), "mode": mode,
           "tags": storage.normalize_tags(body.get("tags") or []),
           "test_ids": [str(t) for t in body.get("test_ids") or []][:500],
           "include_drafts": bool(body.get("include_drafts")),
           "device": str(body.get("device") or "").strip()[:60],
           "tz": str(body.get("tz") or DEFAULT_TZ).strip()}
    zone(out["tz"])
    if mode == "daily":
        days = sorted({int(d) for d in body.get("days") or [] if str(d).isdigit() and 0 <= int(d) <= 6})
        times = sorted({t.strip() for t in body.get("times") or [] if TIME.fullmatch(str(t).strip())})
        if not days:
            raise Invalid("Выберите хотя бы один день недели")
        if not times:
            raise Invalid("Укажите время запуска в виде ЧЧ:ММ")
        if len(times) > MAX_TIMES:
            raise Invalid(f"Не больше {MAX_TIMES} запусков в день")
        out.update(days=days, times=times)
    else:
        try:
            hours = int(body.get("every_hours") or 0)
        except (TypeError, ValueError):
            hours = 0
        if not MIN_HOURS <= hours <= MAX_HOURS:
            raise Invalid(f"Интервал — от {MIN_HOURS} до {MAX_HOURS} часов")
        out["every_hours"] = hours
    if not out["name"]:
        out["name"] = describe(out)
    return out


def describe(s: dict) -> str:
    """When the schedule runs, in words: «пн–пт в 02:00», «каждые 4 ч»."""
    if s.get("mode") == "interval":
        return f"каждые {s['every_hours']} ч"
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    days = s.get("days") or []
    when = ("каждый день" if len(days) == 7 else "пн–пт" if days == [0, 1, 2, 3, 4]
            else "сб, вс" if days == [5, 6] else ", ".join(names[d] for d in days))
    return f"{when} в {', '.join(s.get('times') or [])}"


def next_time(s: dict, after: float) -> float | None:
    """The first time of the schedule later than `after` (epoch seconds)."""
    if s.get("mode") == "interval":
        return after + s["every_hours"] * 3600
    tz = zone(s.get("tz"))
    start = dt.datetime.fromtimestamp(after, tz)
    best = None
    for shift in range(8):
        day = start.date() + dt.timedelta(days=shift)
        if day.weekday() not in s.get("days", []):
            continue
        for t in s.get("times", []):
            h, m = map(int, t.split(":"))
            at = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()
            if at > after and (best is None or at < best):
                best = at
        if best is not None:
            return best
    return best


def _write(pid: str, items: list[dict]) -> None:
    fs.write_json(_file(pid), {"items": items})


def save(pid: str, body: dict, user: str, sid: str = "") -> dict:
    """Create (no `sid`) or change a schedule; its next time is counted anew."""
    fields = clean(body)
    with fs.lock(_file(pid)):
        items = list_for(pid)
        if sid:
            s = next((x for x in items if x["id"] == sid), None)
            if not s:
                raise KeyError(sid)
            s.update(fields, changed=time.time(), changed_by=user)
        else:
            s = fields | {"id": uuid.uuid4().hex[:10], "user": user, "created": time.time(), "last": None}
            items.append(s)
        if s["mode"] == "daily":
            s.pop("every_hours", None)
        else:
            for k in ("days", "times"):
                s.pop(k, None)
        s["next"] = next_time(s, time.time()) if s["enabled"] else None
        _write(pid, items)
    return s


def delete(pid: str, sid: str) -> bool:
    with fs.lock(_file(pid)):
        items = list_for(pid)
        rest = [s for s in items if s["id"] != sid]
        if len(rest) == len(items):
            return False
        _write(pid, rest)
    return True


def view(s: dict) -> dict:
    return s | {"when": describe(s)}


def _running(s: dict) -> bool:
    last = (s.get("last") or {}).get("suite_id")
    return bool(last and (suite.get(last) or {}).get("status") == "running")


def fire(project: dict, s: dict, start, now: float) -> dict:
    """One turn of a schedule: a suite run like «Запустить набор» from the schedule's author. -> its `last`."""
    if _running(s):
        return {"at": now, "status": "skipped", "suite_id": s["last"]["suite_id"],
                "note": "Предыдущий запуск набора по этому расписанию ещё идёт"}
    tests = storage.select(project["id"], tags=s.get("tags") or None, test_ids=s.get("test_ids") or None,
                           include_drafts=s.get("include_drafts", False))
    if not tests:
        return {"at": now, "status": "empty",
                "note": "Нет тестов для прогона" + (f" с тегами {', '.join(s['tags'])}" if s.get("tags") else "")}
    run = suite.new(project, tests, tags=s.get("tags") or [], trigger="schedule", user=s.get("user") or "",
                    devices=[s["device"]] if s.get("device") else None)
    run["schedule"] = {"id": s["id"], "name": s.get("name") or describe(s)}
    suite.save(run)
    start(project, run, tests)
    return {"at": now, "status": "started", "suite_id": run["id"]}


def run_now(project: dict, sid: str, start) -> dict:
    """«Запустить сейчас»: a turn out of time; the schedule keeps its next time."""
    with fs.lock(_file(project["id"])):
        items = list_for(project["id"])
        s = next((x for x in items if x["id"] == sid), None)
        if not s:
            raise KeyError(sid)
        s["last"] = fire(project, s, start, time.time())
        _write(project["id"], items)
    return s


def tick(start, now: float | None = None) -> list[dict]:
    """Start the schedules whose time came in every project. `start(project, suite, tests)` sends the suite to
    run (the browser loop or the queue). -> the `last` of each turn taken."""
    now = time.time() if now is None else now
    done = []
    for f in fs.glob(projects.ROOT, "*/schedules.json"):
        pid = f.parent.name
        doc = fs.read_json(f, {})
        if not any(s.get("enabled") and s.get("next") and s["next"] <= now for s in doc.get("items", [])):
            continue        # nothing due: no lock taken
        project = projects.get(pid)
        if not project:
            continue
        with fs.lock(_file(pid)):
            items = list_for(pid)
            for s in items:
                if not s.get("enabled") or not s.get("next") or s["next"] > now:
                    continue
                if now - s["next"] > GRACE:
                    s["last"] = {"at": now, "status": "missed", "planned": s["next"],
                                 "note": "Студия не работала в это время — запуск пропущен"}
                else:
                    try:
                        s["last"] = fire(project, s, start, now)
                    except Exception as e:      # one schedule must not stop the others
                        s["last"] = {"at": now, "status": "error", "note": str(e).splitlines()[0][:300] if str(e)
                                     else type(e).__name__}
                s["next"] = next_time(s, now)
                done.append(s["last"] | {"project_id": pid, "schedule_id": s["id"]})
            _write(pid, items)
    return done
