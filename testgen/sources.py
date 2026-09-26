"""Where requirements come from: a Jira issue or a Confluence page, fetched by
link through the project's Atlassian MCP connection (mcp-atlassian), or an
uploaded .md file (read in the browser, nothing to do here).

The MCP server runs as a short-lived stdio subprocess per request with the
project's Atlassian Cloud credentials (email + API token), in read-only mode.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse

from . import mcp_hub
from .mcp_hub import normalize_site

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


async def fetch(link: str, project: dict) -> dict:
    """Fetch a Jira issue / Confluence page as markdown-ish text."""
    conn = connection(project)
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
