"""Russian and other issue trackers over their REST APIs: YouTrack, Yandex Tracker, Kaiten.

- requirements: an issue / card by link or key, read-only (sources.fetch);
- defects: a draft built from a failed run (defects.py) is created in the tracker only when a
  person presses "Создать" - never by a model.

Connection fields come from the mcp_hub presets "youtrack", "yandex_tracker", "kaiten".
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

TIMEOUT = httpx.Timeout(60, connect=15)
TRANSPORT = None          # tests: an httpx MockTransport
YANDEX_API = "https://api.tracker.yandex.net/v3"


class TrackerError(Exception):
    """A message that can be shown to the user as is."""


async def _call(method: str, url: str, headers: dict, title: str, **kw):
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT) as c:
            r = await c.request(method, url, headers=headers | {"Accept": "application/json"}, **kw)
    except httpx.HTTPError as e:
        raise TrackerError(f"{title} недоступен: {e}") from e
    if r.status_code in (401, 403):
        raise TrackerError(f"{title}: нет доступа (проверьте токен)")
    if r.status_code == 404:
        raise TrackerError(f"{title}: не найдено")
    if r.status_code >= 400:
        raise TrackerError(f"{title}: HTTP {r.status_code} — {r.text[:300]}")
    return r.json() if r.content else None


def _site(fields: dict) -> str:
    site = (fields.get("site") or "").strip().rstrip("/")
    if site and "://" not in site:
        site = "https://" + site
    return site


# ---------- YouTrack ----------

def _yt_headers(secrets: dict) -> dict:
    return {"Authorization": f"Bearer {secrets.get('token', '')}"}


async def _youtrack_issue(fields, secrets, key):
    q = "idReadable,summary,description,customFields(name,value(name,text)),comments(text,author(fullName))"
    d = await _call("GET", f"{_site(fields)}/api/issues/{key}", _yt_headers(secrets), "YouTrack", params={"fields": q})
    lines = [f"# {d.get('idReadable', key)}: {d.get('summary', '')}"]
    meta = []
    for f in d.get("customFields") or []:
        v = f.get("value")
        v = v.get("name") or v.get("text") if isinstance(v, dict) else v
        if isinstance(v, str) and v:
            meta.append(f"{f.get('name')}: {v}")
    if meta:
        lines.append(" · ".join(meta))
    if d.get("description"):
        lines += ["", "## Описание", d["description"]]
    comments = [c for c in d.get("comments") or [] if c.get("text")]
    if comments:
        lines += ["", "## Комментарии"] + [f"- {(c.get('author') or {}).get('fullName', '')}: {c['text']}" for c in comments]
    return f"{d.get('idReadable', key)} {d.get('summary', '')}", "\n".join(lines)


async def _youtrack_create(fields, secrets, title, text):
    if not fields.get("defect_project"):
        raise TrackerError("YouTrack: укажите проект для дефектов в подключении")
    project = await _call("GET", f"{_site(fields)}/api/admin/projects", _yt_headers(secrets), "YouTrack",
                          params={"fields": "id,shortName", "query": fields["defect_project"]})
    pid = next((p["id"] for p in project or [] if p.get("shortName") == fields["defect_project"]), None)
    if not pid:
        raise TrackerError(f"YouTrack: проект {fields['defect_project']} не найден")
    d = await _call("POST", f"{_site(fields)}/api/issues", _yt_headers(secrets), "YouTrack",
                    params={"fields": "idReadable"}, json={"project": {"id": pid}, "summary": title, "description": text})
    return d["idReadable"], f"{_site(fields)}/issue/{d['idReadable']}"


# ---------- Yandex Tracker ----------

def _ya_headers(fields: dict, secrets: dict) -> dict:
    org = "X-Cloud-Org-ID" if str(fields.get("cloud", "")).lower() in ("yes", "true", "1", "да") else "X-Org-ID"
    token = secrets.get("token", "")
    scheme = "Bearer" if token.startswith("t1.") else "OAuth"      # IAM tokens start with t1.
    return {"Authorization": f"{scheme} {token}", org: fields.get("org_id", "")}


async def _yandex_issue(fields, secrets, key):
    h = _ya_headers(fields, secrets)
    d = await _call("GET", f"{YANDEX_API}/issues/{key}", h, "Яндекс Трекер")
    comments = await _call("GET", f"{YANDEX_API}/issues/{key}/comments", h, "Яндекс Трекер") or []
    lines = [f"# {d.get('key', key)}: {d.get('summary', '')}"]
    meta = [f"{label}: {(d.get(f) or {}).get('display', '')}" for label, f in
            (("Тип", "type"), ("Статус", "status"), ("Приоритет", "priority")) if isinstance(d.get(f), dict)]
    if meta:
        lines.append(" · ".join(meta))
    if d.get("description"):
        lines += ["", "## Описание", d["description"]]
    if comments:
        lines += ["", "## Комментарии"] + [f"- {(c.get('createdBy') or {}).get('display', '')}: {c.get('text', '')}"
                                          for c in comments if c.get("text")]
    return f"{d.get('key', key)} {d.get('summary', '')}", "\n".join(lines)


async def _yandex_create(fields, secrets, title, text):
    if not fields.get("queue"):
        raise TrackerError("Яндекс Трекер: укажите очередь для дефектов в подключении")
    d = await _call("POST", f"{YANDEX_API}/issues", _ya_headers(fields, secrets), "Яндекс Трекер",
                    json={"queue": fields["queue"], "summary": title, "description": text, "type": "bug"})
    return d["key"], f"https://tracker.yandex.ru/{d['key']}"


# ---------- Kaiten ----------

def _kaiten_headers(secrets: dict) -> dict:
    return {"Authorization": f"Bearer {secrets.get('token', '')}"}


async def _kaiten_issue(fields, secrets, card_id):
    h = _kaiten_headers(secrets)
    d = await _call("GET", f"{_site(fields)}/api/latest/cards/{card_id}", h, "Kaiten")
    comments = await _call("GET", f"{_site(fields)}/api/latest/cards/{card_id}/comments", h, "Kaiten") or []
    lines = [f"# {d.get('title', '')}"]
    if d.get("description"):
        lines += ["", "## Описание", d["description"]]
    if comments:
        lines += ["", "## Комментарии"] + [f"- {(c.get('author') or {}).get('full_name', '')}: {c.get('text', '')}"
                                          for c in comments if c.get("text")]
    return f"#{card_id} {d.get('title', '')}", "\n".join(lines)


async def _kaiten_create(fields, secrets, title, text):
    if not fields.get("board_id") or not fields.get("column_id"):
        raise TrackerError("Kaiten: укажите доску и колонку для дефектов в подключении")
    d = await _call("POST", f"{_site(fields)}/api/latest/cards", _kaiten_headers(secrets), "Kaiten",
                    json={"board_id": int(fields["board_id"]), "column_id": int(fields["column_id"]),
                          "title": title, "description": text})
    return str(d["id"]), f"{_site(fields)}/space/0/card/{d['id']}"


# ---------- dispatch ----------

def parse_link(preset: str, link: str) -> str:
    """The issue key / card id in a link (or the link itself if it is one)."""
    link = link.strip()
    if preset == "kaiten":
        m = re.search(r"/card/(\d+)", link) or re.fullmatch(r"#?(\d+)", link)
    else:
        m = re.search(r"/issue/([A-Za-z][A-Za-z0-9_]*-\d+)", link) or \
            re.search(r"tracker\.yandex\.\w+/([A-Za-z][A-Za-z0-9_]*-\d+)", link) or \
            re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*-\d+)", link)
    if not m:
        raise TrackerError("Не удалось распознать задачу: нужна ссылка на задачу или её ключ (QA-123)")
    return m.group(1).upper() if preset != "kaiten" else m.group(1)


def matches(preset: str, fields: dict, link: str) -> bool:
    """Does this link point to this tracker?"""
    host = urlparse(link if "://" in link else "").netloc
    if preset == "yandex_tracker":
        return "tracker.yandex" in host
    site = urlparse(_site(fields)).netloc
    return bool(host) and host == site


async def fetch(preset: str, fields: dict, secrets: dict, link: str) -> tuple[str, str]:
    key = parse_link(preset, link)
    return await {"youtrack": _youtrack_issue, "yandex_tracker": _yandex_issue, "kaiten": _kaiten_issue}[preset](
        fields, secrets, key)


async def create(preset: str, fields: dict, secrets: dict, title: str, text: str) -> tuple[str, str]:
    """A defect in the tracker -> (key, url). Called only on a person's click."""
    return await {"youtrack": _youtrack_create, "yandex_tracker": _yandex_create, "kaiten": _kaiten_create}[preset](
        fields, secrets, title, text)


async def check(preset: str, fields: dict, secrets: dict) -> list[dict]:
    if preset == "youtrack":
        me = await _call("GET", f"{_site(fields)}/api/users/me", _yt_headers(secrets), "YouTrack",
                         params={"fields": "login,fullName"})
        who = me.get("fullName") or me.get("login")
    elif preset == "yandex_tracker":
        me = await _call("GET", f"{YANDEX_API}/myself", _ya_headers(fields, secrets), "Яндекс Трекер")
        who = me.get("display") or me.get("login")
    else:
        me = await _call("GET", f"{_site(fields)}/api/latest/users/current", _kaiten_headers(secrets), "Kaiten")
        who = me.get("full_name") or me.get("username")
    return [{"name": f"Пользователь: {who}", "description": "Чтение задач; дефекты — по нажатию человека",
             "access": "read"}]
