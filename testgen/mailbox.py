"""Codes from e-mails (2FA, registration confirmation): the step read_email waits for a
letter in the project's test mailbox and saves what a regular expression finds into a
run variable, e.g. {{vars.code}} for the next "fill" step.

The mailbox is a project setting ("Проект → Общие → Тестовый почтовый ящик"):
    mailpit  a Mailpit server (catches all mail of a test stand), its HTTP API: {"url"}
    imap     any mailbox over IMAP: {"host", "port", "user", "ssl"}, the password in secrets
Only reading: letters are never deleted or marked.

CAPTCHA is never solved: switch it off on the test stand or use the provider's test keys.
"""
from __future__ import annotations

import asyncio
import datetime
import email
import email.utils
import html
import imaplib
import re
import time
from email.header import decode_header, make_header

import httpx

from . import projects, vault

DEFAULT_PATTERN = r"\b(\d{4,8})\b"
MAX_AGE = 600            # seconds: older letters are not "the code we just asked for"


def settings(pid: str) -> dict:
    p = projects.get(pid) or {}
    return p.get("mailbox") or {}


def password(pid: str) -> str:
    return (vault.load(projects.secrets_kind(pid), "mailbox") or {}).get("password", "")


def set_mailbox(pid: str, cfg: dict, secret: str = "") -> dict:
    p = projects.get(pid)
    kind = cfg.get("kind") if cfg.get("kind") in ("mailpit", "imap", "") else ""
    p["mailbox"] = {"kind": kind, "url": str(cfg.get("url") or "").strip().rstrip("/"),
                    "host": str(cfg.get("host") or "").strip(), "port": int(cfg.get("port") or 993),
                    "user": str(cfg.get("user") or "").strip(), "ssl": bool(cfg.get("ssl", True))} if kind else {}
    projects.save(p)
    if secret:
        vault.save(projects.secrets_kind(pid), "mailbox", {"password": secret})
    elif not kind:
        vault.delete(projects.secrets_kind(pid), "mailbox")
    return p["mailbox"]


def _text_of(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        parts.append(_strip_html(text) if ctype == "text/html" else text)
    return "\n".join(parts)


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


async def _mailpit(cfg: dict, to: str, subject: str, since: float) -> list[tuple[float, str, str]]:
    query = " ".join(filter(None, [f'to:"{to}"' if to else "", f'subject:"{subject}"' if subject else ""]))
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{cfg['url']}/api/v1/search", params={"query": query or "*", "limit": 10})
        r.raise_for_status()
        out = []
        for m in r.json().get("messages") or []:
            created = _ts(m.get("Created"))
            if created < since:
                continue
            body = (await c.get(f"{cfg['url']}/api/v1/message/{m['ID']}")).json()
            out.append((created, m.get("Subject", ""), (body.get("Text") or "") + "\n" + _strip_html(body.get("HTML"))))
    return out


def _ts(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _imap(cfg: dict, secret: str, to: str, subject: str, since: float) -> list[tuple[float, str, str]]:
    cls = imaplib.IMAP4_SSL if cfg.get("ssl", True) else imaplib.IMAP4
    box = cls(cfg["host"], int(cfg.get("port") or (993 if cfg.get("ssl", True) else 143)))
    try:
        box.login(cfg["user"], secret)
        box.select("INBOX", readonly=True)
        day = datetime.datetime.fromtimestamp(since).strftime("%d-%b-%Y")
        criteria = ["SINCE", day] + (["TO", f'"{to}"'] if to and to.isascii() else [])
        status, data = box.search(None, *criteria)
        ids = (data[0] or b"").split()[-10:] if status == "OK" else []
        out = []
        for i in reversed(ids):
            status, parts = box.fetch(i, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            date = email.utils.parsedate_to_datetime(msg["Date"]).timestamp() if msg.get("Date") else 0
            if date < since or (subject and subject.lower() not in subj.lower()):
                continue
            if to and to.lower() not in (msg.get("To") or "").lower():
                continue
            out.append((date, subj, _text_of(msg)))
        return out
    finally:
        try:
            box.logout()
        except Exception:
            pass


async def read_code(bs, s: dict) -> dict:
    """read_email: wait (up to s["timeout"], 60 s) for the newest matching letter and save the code."""
    pid = bs.options.get("project_id") or ""
    cfg = settings(pid)
    if not cfg.get("kind"):
        raise ValueError("В проекте не настроен тестовый почтовый ящик (Проект → Общие)")
    to, subject = bs.expand(s.get("to") or ""), bs.expand(s.get("subject") or "")
    pattern = s.get("pattern") or DEFAULT_PATTERN
    name = s.get("save") or "code"
    since = time.time() - float(s.get("max_age") or MAX_AGE)
    deadline = time.monotonic() + min(float(s.get("timeout") or 60), 300)
    while True:
        if cfg["kind"] == "mailpit":
            letters = await _mailpit(cfg, to, subject, since)
        else:
            letters = await asyncio.to_thread(_imap, cfg, password(pid), to, subject, since)
        for _, subj, text in sorted(letters, reverse=True):
            m = re.search(pattern, text)
            if m:
                bs.vars[name] = m.group(1) if m.groups() else m.group(0)
                return {"email": {"subject": subj[:120], "saved": name}}
        if time.monotonic() >= deadline:
            raise AssertionError(f"Письмо{' для ' + to if to else ''}{' с темой «' + subject + '»' if subject else ''} "
                                 f"с кодом не пришло")
        await asyncio.sleep(2)
