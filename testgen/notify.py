"""Notifications of the team: Telegram, Mattermost, e-mail.

Events (project setting "notify.events"):
    suite      a suite run finished: "always" | "failed" (only when it failed) | "off"
    streak     the suite failed this many times in a row (0 = off)
    proposals  self-healing proposed new locators that wait for a person's review
    budget     the project spent 80% of its monthly language-model budget

Channels (project setting "notify"): Telegram (a bot and a chat id), Mattermost (an incoming
webhook), e-mail (SMTP). Secrets - the bot token, the webhook address, the SMTP password -
live in secrets/projects/<id>/notify.json. TESTGEN_PUBLIC_URL (the studio's address for
people) adds links. Sending never breaks the work that triggered it: errors are logged.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

import httpx

from . import llm, projects, vault

log = logging.getLogger("testgen.notify")
TRANSPORT = None                  # tests: an httpx MockTransport
SMTP = smtplib.SMTP               # tests replace these
SMTP_SSL = smtplib.SMTP_SSL
DEFAULT = {"telegram": {"enabled": False, "chat_id": ""},
           "mattermost": {"enabled": False},
           "email": {"enabled": False, "host": "", "port": 587, "user": "", "sender": "", "to": "", "tls": True},
           "events": {"suite": "failed", "streak": 3, "proposals": True, "budget": True}}
SECRET_KEYS = ("telegram_token", "mattermost_webhook", "smtp_password")


def settings(project: dict) -> dict:
    given = project.get("notify") or {}
    out = {}
    for k, v in DEFAULT.items():
        out[k] = v | {kk: vv for kk, vv in (given.get(k) or {}).items() if kk in v}
    return out


def secrets(pid: str) -> dict:
    return vault.load(projects.secrets_kind(pid), "notify") or {}


def save(pid: str, cfg: dict, new_secrets: dict) -> dict:
    p = projects.get(pid)
    clean = {}
    for k, v in DEFAULT.items():
        given = cfg.get(k) or {}
        clean[k] = {kk: type(vv)(given[kk]) if kk in given and given[kk] is not None else vv for kk, vv in v.items()}
    clean["events"]["suite"] = clean["events"]["suite"] if clean["events"]["suite"] in ("always", "failed", "off") \
        else "failed"
    p["notify"] = clean
    projects.save(p)
    s = secrets(pid)
    for k in SECRET_KEYS:
        if new_secrets.get(k):
            s[k] = new_secrets[k].strip()
        elif new_secrets.get(k) == "":
            s.pop(k, None)
    vault.save(projects.secrets_kind(pid), "notify", s) if s else vault.delete(projects.secrets_kind(pid), "notify")
    return clean


def public_view(project: dict) -> dict:
    s = secrets(project["id"])
    return settings(project) | {"secrets_set": sorted(k for k in SECRET_KEYS if s.get(k))}


# ---------- sending ----------

async def _telegram(cfg: dict, sec: dict, text: str) -> None:
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
        r = await c.post(f"https://api.telegram.org/bot{sec['telegram_token']}/sendMessage",
                         json={"chat_id": cfg["chat_id"], "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True})
        r.raise_for_status()


async def _mattermost(sec: dict, text: str) -> None:
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
        r = await c.post(sec["mattermost_webhook"], json={"text": text, "username": "AI Test Generator"})
        r.raise_for_status()


def _email(cfg: dict, sec: dict, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, cfg["sender"] or cfg["user"]
    msg["To"] = ", ".join(a.strip() for a in cfg["to"].replace(";", ",").split(",") if a.strip())
    msg.set_content(text)
    port = int(cfg["port"] or 587)
    if port == 465:
        with SMTP_SSL(cfg["host"], port, context=ssl.create_default_context(), timeout=30) as s:
            if cfg["user"]:
                s.login(cfg["user"], sec.get("smtp_password", ""))
            s.send_message(msg)
    else:
        with SMTP(cfg["host"], port, timeout=30) as s:
            if cfg.get("tls", True):
                s.starttls(context=ssl.create_default_context())
            if cfg["user"]:
                s.login(cfg["user"], sec.get("smtp_password", ""))
            s.send_message(msg)


def _plain(text: str) -> str:
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def send(project: dict, subject: str, text: str) -> list[str]:
    """Send to every enabled channel -> the channels that failed."""
    cfg, sec = settings(project), secrets(project["id"])
    failed = []
    jobs = []
    if cfg["telegram"]["enabled"] and sec.get("telegram_token") and cfg["telegram"]["chat_id"]:
        jobs.append(("telegram", _telegram(cfg["telegram"], sec, f"<b>{html.escape(subject)}</b>\n{text}")))
    if cfg["mattermost"]["enabled"] and sec.get("mattermost_webhook"):
        jobs.append(("mattermost", _mattermost(sec, f"**{subject}**\n{_plain(text)}")))
    if cfg["email"]["enabled"] and cfg["email"]["host"] and cfg["email"]["to"]:
        jobs.append(("email", asyncio.to_thread(_email, cfg["email"], sec, subject, _plain(text))))
    for name, job in jobs:
        try:
            await job
        except Exception as e:
            log.warning("notification to %s failed: %s", name, e)
            failed.append(name)
    return failed


def _fire(coro) -> None:
    """Run in the background: on the current loop, or in a thread when there is none."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=asyncio.run, args=(coro,), daemon=True).start()
        return
    task = loop.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


_pending: set = set()


def _link(path: str) -> str:
    base = os.environ.get("TESTGEN_PUBLIC_URL", "").rstrip("/")
    return f'\n<a href="{html.escape(base + path)}">Открыть в студии</a>' if base else ""


def message(project: dict, kind: str, **kw) -> tuple[str, str] | None:
    """(subject, text) of an event, or None when the project does not want it."""
    ev = settings(project)["events"]
    name = project["name"]
    if kind == "suite":
        s = kw["suite"]
        c = s.get("summary") or {}
        if ev["suite"] == "off" or (ev["suite"] == "failed" and s.get("passed")):
            return None
        verdict = "успешно" if s.get("passed") else "есть падения"
        failed = [i for i in s["items"] if i["status"] in ("failed", "error") and not i["quarantined"]]
        lines = [f"Набор {' '.join('#' + t for t in s.get('tags') or []) or '(все тесты)'}: {verdict}",
                 f"прошло {c.get('passed', 0)}, нестабильных {c.get('flaky', 0)}, упало {c.get('failed', 0)}, "
                 f"ошибок {c.get('error', 0)} из {c.get('total', 0)}"]
        lines += [f"• {html.escape(i['name'])}: {html.escape((i.get('failed_step') or '') + ' ' + (i.get('error') or ''))[:200]}"
                  for i in failed[:10]]
        return f"{name}: набор — {verdict}", "\n".join(lines) + _link(f"/#suite={s['id']}")
    if kind == "streak":
        return (f"{name}: набор падает {kw['n']} раз подряд",
                f"Последние {kw['n']} запусков набора завершились с падениями." + _link("/#tests"))
    if kind == "proposals":
        if not ev["proposals"]:
            return None
        t, run = kw["test"], kw["run"]
        return (f"{name}: самолечение ждёт ревью",
                f"Тест «{html.escape(t['name'])}»: новых локаторов на ревью — {run['proposals']}. Без решения человека "
                f"они в тест не попадут." + _link(f"/#test={t['id']}"))
    if kind == "budget":
        if not ev["budget"]:
            return None
        return f"{name}: расход на модели", html.escape(kw["text"])
    return None


def event(project: dict | None, kind: str, **kw) -> None:
    """Notify about an event in the background (never raises)."""
    if not project:
        return
    try:
        msg = message(project, kind, **kw)
        if msg:
            _fire(send(project, *msg))
        if kind == "suite":
            n = int(settings(project)["events"].get("streak") or 0)
            if n and _streak(project["id"]) == n:
                streak_msg = message(project, "streak", n=n)
                _fire(send(project, *streak_msg))
    except Exception as e:           # a notification must never break a run
        log.warning("notification failed: %s", e)


def _streak(pid: str) -> int:
    """Failed suite runs in a row, the newest first."""
    from . import suite
    n = 0
    for s in suite.list_suites(pid, limit=20):
        if s["status"] == "running":
            continue
        if s.get("passed"):
            break
        n += 1
    return n


llm.MONTH_WARN_HOOKS.append(lambda pid, text: event(projects.get(pid), "budget", text=text))
