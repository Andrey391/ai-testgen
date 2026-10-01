"""Where requirements come from: a Jira issue or a Confluence page, fetched by
link through the project's Atlassian MCP connection (mcp-atlassian); an issue of
YouTrack, Yandex Tracker or Kaiten through their REST APIs (trackers.py); a
specification file - .docx (Word, the usual form of a Russian ТЗ), .pdf, .md, .txt.

The Atlassian MCP server runs as a short-lived stdio subprocess per request with
the project's Atlassian Cloud credentials (email + API token), in read-only mode.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

from . import mcp_hub, trackers
from .mcp_hub import normalize_site

TRACKERS = ("youtrack", "yandex_tracker", "kaiten")

MAX_CHARS = 200_000
TIMEOUT = 90

_ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")


class SourceError(Exception):
    """A message that can be shown to the user as is."""


def connection(project: dict) -> dict | None:
    cid = project["pipeline"]["requirements"].get("connection", "")
    return mcp_hub.find(project, cid=cid) if cid else mcp_hub.find(project, "atlassian")


def parse_link(link: str, default_site: str = "") -> tuple[str, str, str]:
    """-> (kind, site, id): ("jira", site, "PROJ-123") or ("confluence", site, page url/id)."""
    link = link.strip()
    u = urlparse(link if "://" in link else "")
    site = f"{u.scheme}://{u.netloc}" if u.netloc else normalize_site(default_site)
    if u.netloc and u.path.startswith("/wiki"):
        return "confluence", site, link
    if u.netloc:
        q = parse_qs(u.query)
        m = _ISSUE_KEY.search((q.get("selectedIssue") or [""])[0]) or _ISSUE_KEY.search(u.path)
        if m:
            return "jira", site, m.group(1)
        raise SourceError("Не удалось распознать ссылку: нужна ссылка на задачу Jira "
                          "(…/browse/PROJ-123) или страницу Confluence (…/wiki/spaces/…/pages/…)")
    if _ISSUE_KEY.fullmatch(link.upper()):
        return "jira", site, link.upper()
    if link.isdigit():
        return "confluence", site, link
    raise SourceError("Укажите ссылку на Jira/Confluence, ключ задачи (PROJ-123) или ID страницы")


def _tracker_for(project: dict, link: str) -> dict | None:
    """A tracker connection for this link: the one the link points to, or the requirements connection
    of the project when it is a tracker and the link is a bare key."""
    conns = [c for c in project.get("connections", []) if c.get("enabled", True) and c["preset"] in TRACKERS]
    for c in conns:
        if trackers.matches(c["preset"], c.get("fields") or {}, link):
            return c
    chosen = connection(project)
    if chosen and chosen["preset"] in TRACKERS and "://" not in link:
        return chosen
    if "://" not in link and not any(c["preset"] == "atlassian" for c in project.get("connections", [])) and conns:
        return conns[0]
    return None


async def fetch(link: str, project: dict) -> dict:
    """Fetch a Jira issue / Confluence page / tracker issue as markdown-ish text."""
    tracker = _tracker_for(project, link)
    if tracker:
        fields, secrets = mcp_hub.fields_of(project["id"], tracker)
        try:
            title, text = await asyncio.wait_for(trackers.fetch(tracker["preset"], fields, secrets, link), TIMEOUT)
        except asyncio.TimeoutError:
            raise SourceError(f"{tracker['name']} не ответил вовремя")
        except trackers.TrackerError as e:
            raise SourceError(str(e))
        return {"kind": tracker["preset"], "source": link.strip(), "title": title, "text": text[:MAX_CHARS],
                "truncated": len(text) > MAX_CHARS}
    conn = connection(project)
    if conn and conn["preset"] in TRACKERS:
        conn = mcp_hub.find(project, "atlassian")
    if not conn:
        raise SourceError("В проекте нет подключения Jira/Confluence: добавьте его в настройках проекта "
                          "(Подключения → Jira / Confluence)")
    fields = (conn.get("fields") or {}) | mcp_hub.load_secrets(project["id"], conn["id"])
    if not fields.get("email") or not fields.get("token"):
        raise SourceError(f"Заполните email и API-токен в подключении «{conn['name']}»")
    kind, site, ident = parse_link(link, fields.get("site", ""))
    if not site:
        raise SourceError("Не указан адрес Atlassian (https://your-company.atlassian.net)")

    if kind == "jira":
        tool, args = "jira_get_issue", {"issue_key": ident, "fields": "*all", "comment_limit": 20,
                                        "use_display_names": True, "update_history": False}
    else:
        tool, args = "confluence_get_page", {"page_id": ident, "convert_to_markdown": True,
                                             "include_metadata": True}
    # The link may point to another site than the saved one: use the link's.
    raw = await _call(project["id"], conn | {"fields": (conn.get("fields") or {}) | {"site": site}},
                      tool, args)
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        raise SourceError(f"Atlassian: {data['error']}")
    title, text = (_format_issue if kind == "jira" else _format_page)(data) if isinstance(data, dict) else ("", raw)
    return {"kind": kind, "source": link.strip(), "title": title,
            "text": text[:MAX_CHARS], "truncated": len(text) > MAX_CHARS}


async def _call(project_id: str, conn: dict, tool: str, args: dict) -> str:
    async def run() -> str:
        async with mcp_hub.connect(project_id, conn) as session:
            res = await session.call_tool(tool, args)
        if res.isError:
            raise SourceError(f"Atlassian: {mcp_hub.error_text(res)}")
        return mcp_hub.result_text(res)

    try:
        return await asyncio.wait_for(run(), TIMEOUT)
    except asyncio.TimeoutError:
        raise SourceError("MCP-сервер Atlassian не ответил вовремя")
    except mcp_hub.McpError as e:
        raise SourceError(str(e))


def _name(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("display_name") or v.get("value") or "")
    return "" if v is None else str(v)


def _format_issue(d: dict) -> tuple[str, str]:
    key, summary = d.get("key", ""), d.get("summary", "")
    lines = [f"# {key}: {summary}".strip(": ")]
    meta = [f"{label}: {_name(d.get(field))}" for label, field in
            (("Тип", "issue_type"), ("Статус", "status"), ("Приоритет", "priority"))
            if _name(d.get(field))]
    if d.get("labels"):
        meta.append("Метки: " + ", ".join(map(str, d["labels"])))
    if meta:
        lines.append(" · ".join(meta))
    if d.get("description"):
        lines += ["", "## Описание", str(d["description"])]
    # Long text custom fields: acceptance criteria, business rules and the like.
    for fid, f in d.items():
        if fid.startswith("customfield_") and isinstance(f, dict):
            v = f.get("value")
            if isinstance(v, str) and len(v) >= 30 and " " in v:
                lines += ["", f"## {f.get('name') or fid}", v]
    for sub in d.get("subtasks") or []:
        if isinstance(sub, dict):
            lines.append(f"- Подзадача {sub.get('key', '')}: {sub.get('summary') or _name(sub.get('fields'))}")
    comments = [c for c in d.get("comments") or [] if isinstance(c, dict) and c.get("body")]
    if comments:
        lines += ["", "## Комментарии"]
        lines += [f"- {_name(c.get('author'))}: {c['body']}" for c in comments]
    return f"{key} {summary}".strip(), "\n".join(lines)


def _format_page(d: dict) -> tuple[str, str]:
    page = d.get("metadata", d)
    content = page.get("content") or d.get("content") or ""
    body = content.get("value", "") if isinstance(content, dict) else str(content)
    title = page.get("title", "")
    return title, (f"# {title}\n\n{body}" if title else body)


# ---------- specification files ----------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(data: bytes) -> str:
    """Word: paragraphs (headings become Markdown headings, lists "- ") and tables (Markdown rows)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    out: list[str] = []

    def text_of(el) -> str:
        parts = []
        for node in el.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag in (f"{_W}tab",):
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        return "".join(parts).strip()

    for el in body if body is not None else []:
        if el.tag == f"{_W}p":
            t = text_of(el)
            if not t:
                continue
            style = el.find(f"{_W}pPr/{_W}pStyle")
            name = (style.get(f"{_W}val") if style is not None else "") or ""
            level = re.search(r"(?:Heading|Заголовок|heading)\s*(\d)", name)
            if level:
                out.append("#" * min(int(level.group(1)) + 1, 6) + " " + t)
            elif el.find(f"{_W}pPr/{_W}numPr") is not None or "List" in name:
                out.append("- " + t)
            else:
                out.append(t)
        elif el.tag == f"{_W}tbl":
            rows = [[text_of(c).replace("\n", " ") for c in r.findall(f"{_W}tc")] for r in el.findall(f"{_W}tr")]
            for i, r in enumerate(rows):
                out.append("| " + " | ".join(r) + " |")
                if i == 0:
                    out.append("|" + " --- |" * len(r))
    return "\n\n".join(out)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SourceError("Для чтения PDF установите пакет pypdf (pip install pypdf)")
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()


def from_file(name: str, data: bytes) -> dict:
    """A specification file -> {kind, source, title, text, truncated}."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if ext == "docx":
            text = _docx(data)
        elif ext == "pdf":
            text = _pdf(data)
        elif ext in ("md", "markdown", "txt"):
            text = data.decode("utf-8-sig", "replace")
        else:
            raise SourceError("Поддерживаются файлы .docx, .pdf, .md и .txt")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise SourceError(f"Не удалось прочитать файл {name}: он повреждён или это не {ext.upper()}")
    if not text.strip():
        raise SourceError(f"В файле {name} нет текста (скан без распознавания?)")
    title = name.rsplit(".", 1)[0]
    return {"kind": ext, "source": name, "title": title, "text": text[:MAX_CHARS], "truncated": len(text) > MAX_CHARS}
