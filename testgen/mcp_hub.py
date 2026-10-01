"""MCP connections of a project: Jira/Confluence (mcp-atlassian), Zephyr Scale,
Playwright MCP, or any other MCP server.

A connection is created from a preset. Its public part (preset, name, command,
non-secret fields) lives in project.json; secret fields (API tokens) live in
secrets/projects/<id>/conn-<cid>.json.

Arbitrary command lines and environment variables mean arbitrary code on the
server, so only studio admins may set them (see server.py). Regular users fill
in the fields a preset declares: site, email, token and the like.

Two ways to talk to a server:
- `connect()`: a short-lived session inside one task (fetch one issue, test).
- `McpClient`: a long-lived session owned by its own task, usable from any task
  on the same loop (the authoring agent's tools, the Playwright MCP browser).
  anyio cancel scopes must be exited by the task that entered them, hence the
  dedicated task.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from . import vault
from .paths import OFFLINE

STARTUP_TIMEOUT = 120      # npx may download the package on first start
CALL_TIMEOUT = 120
MAX_RESULT_CHARS = 30_000


class McpError(Exception):
    """A message that can be shown to the user as is."""


def normalize_site(site: str) -> str:
    site = site.strip().rstrip("/")
    if site and "://" not in site:
        site = "https://" + site
    u = urlparse(site)
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


# ---------- presets ----------

def _atlassian_command() -> tuple[str, list[str]]:
    custom = os.environ.get("TESTGEN_ATLASSIAN_MCP")
    if custom:
        cmd, *args = shlex.split(custom, posix=os.name != "nt")
        return cmd, args
    # Installed from requirements.txt next to this interpreter; else try uvx.
    exe = (shutil.which("mcp-atlassian", path=os.path.dirname(sys.executable))
           or shutil.which("mcp-atlassian"))
    return (exe, []) if exe else ("uvx", ["mcp-atlassian"])


def _atlassian_env(f: dict) -> dict:
    site = normalize_site(f.get("site", ""))
    return {"JIRA_URL": site, "JIRA_USERNAME": f.get("email", ""), "JIRA_API_TOKEN": f.get("token", ""),
            "CONFLUENCE_URL": f"{site}/wiki" if site else "", "CONFLUENCE_USERNAME": f.get("email", ""),
            "CONFLUENCE_API_TOKEN": f.get("token", ""),
            # Requirements are only read: the server hides every write tool.
            "READ_ONLY_MODE": "true", "FASTMCP_SHOW_CLI_BANNER": "false"}


# kind: what the studio uses the connection for.
PRESETS: dict[str, dict] = {
    "atlassian": {
        "title": "Jira / Confluence",
        "kind": "requirements",
        "hint": "MCP-сервер mcp-atlassian (Atlassian Cloud), только чтение. Токен: "
                "id.atlassian.com → Security → API tokens.",
        "fields": [
            {"key": "site", "label": "Адрес", "placeholder": "https://company.atlassian.net", "required": True},
            {"key": "email", "label": "Email", "required": True},
            {"key": "token", "label": "API-токен", "secret": True, "required": True},
            {"key": "defect_project", "label": "Проект Jira для дефектов (ключ)", "placeholder": "QA"},
            {"key": "defect_type", "label": "Тип задачи дефекта", "placeholder": "Bug"},
        ],
        "env": _atlassian_env,
    },
    "zephyr": {
        "title": "Zephyr Scale",
        "kind": "test_management",
        "hint": "MCP-сервер mcp-zephyr-scale (Zephyr Scale Cloud, нужен Node.js). Токен: Jira → "
                "профиль → Zephyr API keys.",
        "command": "npx", "args": ["-y", "mcp-zephyr-scale"],
        "fields": [
            {"key": "project_key", "label": "Ключ проекта Jira", "placeholder": "PROJ", "required": True},
            {"key": "token", "label": "Zephyr API-токен", "secret": True, "required": True},
        ],
        "env": lambda f: {"JIRA_PROJECT_KEY": f.get("project_key", ""),
                          "ZEPHYR_API_TOKEN": f.get("token", "")},
    },
    "playwright": {
        "title": "Playwright MCP",
        "kind": "browser",
        "hint": "Официальный MCP-сервер Playwright (@playwright/mcp, нужен Node.js). Используется как "
                "движок браузера при генерации тестов. По умолчанию берёт Chromium, установленный "
                "командой playwright install.",
        "command": "npx", "args": ["-y", "@playwright/mcp@latest"],
        "fields": [],
        "env": lambda f: {},
    },
    "allure": {
        "title": "Allure TestOps",
        "kind": "test_management",
        "hint": "Встроенный MCP-сервер Allure TestOps (с версии 26.1.1), HTTP. Токен: профиль → API tokens. "
                "Публикация тест-кейсов, результаты прогонов, импорт ручных кейсов. Если адрес MCP у вашей "
                "установки другой — администратор меняет его в «Запуск сервера».",
        "transport": "http",
        "fields": [
            {"key": "site", "label": "Адрес Allure TestOps", "placeholder": "https://allure.company.ru", "required": True},
            {"key": "project_id", "label": "ID проекта", "placeholder": "12", "required": True},
            {"key": "token", "label": "API-токен", "secret": True, "required": True},
        ],
        "url": lambda f: f"{normalize_site(f.get('site', ''))}/api/mcp" if f.get("site") else "",
        "headers": lambda f: {"Authorization": f"Api-Token {f['token']}"} if f.get("token") else {},
        "env": lambda f: {},
    },
    "testit": {
        "title": "Test IT",
        "kind": "test_management",
        "hint": "Test IT через REST API v2 (MCP-сервера с записью у Test IT нет). Токен: профиль → Приватный "
                "токен. Автотесты с привязкой к ручным кейсам, результаты прогонов, импорт ручных кейсов.",
        "rest": True,
        "fields": [
            {"key": "site", "label": "Адрес Test IT", "placeholder": "https://testit.company.ru", "required": True},
            {"key": "project_id", "label": "ID проекта (UUID)", "required": True},
            {"key": "configuration_id", "label": "ID конфигурации (необязательно)"},
            {"key": "token", "label": "Приватный токен", "secret": True, "required": True},
        ],
        "env": lambda f: {},
    },
    "youtrack": {
        "title": "YouTrack",
        "kind": "tracker",
        "hint": "YouTrack через REST API: требования из задач (только чтение) и дефекты — только по нажатию "
                "человека. Токен: профиль → Account Security → Tokens.",
        "rest": True,
        "fields": [
            {"key": "site", "label": "Адрес YouTrack", "placeholder": "https://company.youtrack.cloud", "required": True},
            {"key": "defect_project", "label": "Проект для дефектов (краткое имя)", "placeholder": "QA"},
            {"key": "token", "label": "Постоянный токен", "secret": True, "required": True},
        ],
        "env": lambda f: {},
    },
    "yandex_tracker": {
        "title": "Яндекс Трекер",
        "kind": "tracker",
        "hint": "Яндекс Трекер через REST API: требования из задач (только чтение) и дефекты — только по нажатию "
                "человека. OAuth-токен или IAM-токен и идентификатор организации.",
        "rest": True,
        "fields": [
            {"key": "org_id", "label": "ID организации", "required": True},
            {"key": "cloud", "label": "Организация Yandex Cloud (yes/no)", "placeholder": "no"},
            {"key": "queue", "label": "Очередь для дефектов", "placeholder": "QA"},
            {"key": "token", "label": "OAuth-токен", "secret": True, "required": True},
        ],
        "env": lambda f: {},
    },
    "kaiten": {
        "title": "Kaiten",
        "kind": "tracker",
        "hint": "Kaiten через REST API: требования из карточек (только чтение) и дефекты — только по нажатию "
                "человека. Токен: профиль → API/Интеграции.",
        "rest": True,
        "fields": [
            {"key": "site", "label": "Адрес Kaiten", "placeholder": "https://company.kaiten.ru", "required": True},
            {"key": "board_id", "label": "Доска для дефектов (ID)"},
            {"key": "column_id", "label": "Колонка для дефектов (ID)"},
            {"key": "token", "label": "API-токен", "secret": True, "required": True},
        ],
        "env": lambda f: {},
    },
    "custom": {
        "title": "Другой MCP-сервер",
        "kind": "tools",
        "hint": "Любой MCP-сервер (stdio-команда или HTTP-адрес). Его инструменты чтения можно дать "
                "агенту на этапе генерации, а все, кроме удаления, — на этапе публикации. "
                "Настраивает только администратор студии.",
        "admin_only": True,
        "fields": [
            {"key": "secret_env", "label": "Секретные переменные окружения (KEY=VALUE, по одной в строке)",
             "secret": True, "multiline": True},
            {"key": "authorization", "label": "Заголовок Authorization (для HTTP-сервера)",
             "placeholder": "Bearer …", "secret": True},
        ],
        "env": lambda f: parse_env(f.get("secret_env", "")),
    },
}


def parse_env(text: str) -> dict:
    """KEY=VALUE lines -> dict (blank lines and # comments skipped)."""
    env = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
                env[k.strip()] = v.strip()
    return env


def presets_public() -> list[dict]:
    return [{"id": k, "title": p["title"], "kind": p["kind"], "hint": p["hint"],
             "admin_only": p.get("admin_only", False), "rest": p.get("rest", False),
             "fields": p["fields"], "command": p.get("command", ""), "args": p.get("args", [])}
            for k, p in PRESETS.items()]


def new_connection(preset: str, name: str = "") -> dict:
    p = PRESETS[preset]
    return {"id": uuid.uuid4().hex[:8], "preset": preset, "name": name or p["title"],
            "enabled": True, "transport": p.get("transport", "stdio"), "command": "", "args": [], "url": "",
            "env": {}, "fields": {}}


def is_rest(conn: dict) -> bool:
    """Connections the studio calls over REST itself (Test IT, trackers): no MCP server."""
    return bool(PRESETS.get(conn.get("preset"), {}).get("rest"))


def fields_of(project_id: str, conn: dict) -> tuple[dict, dict]:
    """(non-secret fields, secrets) of a connection."""
    return dict(conn.get("fields") or {}), load_secrets(project_id, conn["id"])


def secret_keys(conn: dict) -> set[str]:
    return {f["key"] for f in PRESETS[conn["preset"]]["fields"] if f.get("secret")}


def _secrets_kind(project_id: str) -> str:
    return f"projects/{project_id}"


def load_secrets(project_id: str, cid: str) -> dict:
    return vault.load(_secrets_kind(project_id), f"conn-{cid}") or {}


def save_secrets(project_id: str, cid: str, data: dict) -> None:
    data = {k: v for k, v in data.items() if v}
    if data:
        vault.save(_secrets_kind(project_id), f"conn-{cid}", data)
    else:
        vault.delete(_secrets_kind(project_id), f"conn-{cid}")


def missing_fields(conn: dict, secrets: dict) -> list[str]:
    """Labels of the required preset fields that are still empty."""
    return [f["label"] for f in PRESETS[conn["preset"]]["fields"] if f.get("required") and not (
        secrets.get(f["key"]) if f.get("secret") else (conn.get("fields") or {}).get(f["key"]))]


def public_view(project_id: str, conn: dict) -> dict:
    """What the UI sees: never secret values, only whether they are set.
    `check` is the result of the last "test" (reset when the settings change)."""
    s = load_secrets(project_id, conn["id"])
    return conn | {"secrets_set": sorted(k for k in secret_keys(conn) if s.get(k)),
                   "missing": missing_fields(conn, s), "check": conn.get("check"),
                   "title": PRESETS[conn["preset"]]["title"], "kind": PRESETS[conn["preset"]]["kind"]}


def find(project: dict, preset: str | None = None, cid: str = "", enabled_only: bool = True) -> dict | None:
    """A connection by id, or the first one made from `preset`."""
    for c in project.get("connections", []):
        if enabled_only and not c.get("enabled", True):
            continue
        if (cid and c["id"] == cid) or (not cid and preset and c["preset"] == preset):
            return c
    return None


def _command(conn: dict) -> tuple[str, list[str]]:
    if conn.get("command"):
        return conn["command"], list(conn.get("args") or [])
    p = PRESETS[conn["preset"]]
    if conn["preset"] == "atlassian":
        cmd, args = _atlassian_command()
        return cmd, args + list(conn.get("args") or [])
    if not p.get("command"):
        raise McpError(f"Для подключения «{conn['name']}» не задана команда запуска")
    args = list(p.get("args", []))
    if OFFLINE and p["command"] == "npx":
        # Without internet npx must take the package from the npm cache (filled when the image is built).
        args = ["--offline"] + [a.removesuffix("@latest") for a in args]
    return p["command"], args + list(conn.get("args") or [])


def _env(project_id: str, conn: dict) -> dict:
    fields = dict(conn.get("fields") or {}) | load_secrets(project_id, conn["id"])
    env = PRESETS[conn["preset"]]["env"](fields)
    env.update(conn.get("env") or {})
    return {k: str(v) for k, v in env.items() if v not in (None, "")}


# ---------- sessions ----------

def _first_error(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return str(e) or type(e).__name__


@asynccontextmanager
async def connect(project_id: str, conn: dict, extra_args: list[str] | None = None,
                  cwd: str | None = None):
    """An initialized ClientSession. Must be entered and exited in the same task.

    Startup failures become McpError with the tail of the server's stderr; errors
    raised by the caller's code inside the block pass through unchanged."""
    # The server's stderr goes to a file: when it exits on bad settings (no token...),
    # its last lines are the only useful explanation.
    if is_rest(conn):
        raise McpError(f"«{conn['name']}» — подключение по REST API, а не MCP-сервер")
    fields = dict(conn.get("fields") or {}) | load_secrets(project_id, conn["id"])
    missing = [f["label"] for f in PRESETS[conn["preset"]]["fields"] if f.get("required") and not fields.get(f["key"])]
    if missing and not conn.get("command"):
        raise McpError(f"Подключение «{conn['name']}»: заполните {', '.join(missing)}")
    errlog = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    started = False
    preset = PRESETS[conn["preset"]]
    try:
        if conn.get("transport") == "http":
            url = conn.get("url") or (preset["url"](fields) if preset.get("url") else "")
            if not url:
                raise McpError(f"Для подключения «{conn['name']}» не задан адрес")
            auth = load_secrets(project_id, conn["id"]).get("authorization")
            headers = {"Authorization": auth} if auth else (preset["headers"](fields) if preset.get("headers") else None)
            async with create_mcp_http_client(headers=headers or None) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), STARTUP_TIMEOUT)
                        started = True
                        yield session
        else:
            cmd, args = _command(conn)
            params = StdioServerParameters(command=cmd, args=args + (extra_args or []), cwd=cwd,
                                           env=dict(os.environ) | _env(project_id, conn))
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), STARTUP_TIMEOUT)
                    started = True
                    yield session
    except McpError:
        raise
    except BaseException as e:
        if started or not isinstance(e, Exception):
            leaf = _leaf(e)
            if leaf is e:
                raise
            raise leaf from None
        if isinstance(_leaf(e), (asyncio.TimeoutError, TimeoutError)):
            raise McpError(f"MCP-сервер «{conn['name']}» не ответил вовремя")
        stderr = _tail(errlog)
        raise McpError(f"Не удалось подключиться к MCP-серверу «{conn['name']}»: "
                       + (f"{stderr}" if stderr else f"{_first_error(e)}. Проверьте команду запуска"
                          + (" и что установлен Node.js" if _command_name(conn).startswith("npx") else "")))
    finally:
        errlog.close()


def _leaf(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


def _tail(f, lines: int = 6) -> str:
    try:
        f.seek(0)
        text = re.sub(r"\x1b\[[0-9;]*m", "", f.read()[-4000:])
    except (OSError, ValueError):
        return ""
    rows = [r.strip() for r in text.splitlines() if r.strip() and not r.strip().startswith("at ")]
    return " ".join(rows[-lines:])[:800]


def _command_name(conn: dict) -> str:
    try:
        return os.path.basename(_command(conn)[0]).lower()
    except McpError:
        return ""


def result_text(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


def error_text(res) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", result_text(res).strip())
    text = re.sub(r"^(### Error\s*)?(Error calling tool [^:]+:|Error:)?", "", text)
    return text.strip() or "ошибка MCP-инструмента"


class McpClient:
    """A long-lived MCP session run by its own task; call it from any task on this loop."""

    def __init__(self, project_id: str, conn: dict, extra_args: list[str] | None = None,
                 cwd: str | None = None):
        self.project_id, self.conn = project_id, conn
        self.extra_args, self.cwd = extra_args, cwd
        self.session: ClientSession | None = None
        self.tools: list = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> "McpClient":
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._main(ready))
        await ready
        return self

    async def _main(self, ready: asyncio.Future) -> None:
        try:
            async with connect(self.project_id, self.conn, self.extra_args, self.cwd) as session:
                self.tools = (await session.list_tools()).tools
                self.session = session
                ready.set_result(True)
                await self._stop.wait()
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, McpError) else McpError(_first_error(e)))
        finally:
            self.session = None

    async def call(self, tool: str, args: dict, timeout: float = CALL_TIMEOUT):
        if not self.session:
            raise McpError(f"MCP-сервер «{self.conn['name']}» не запущен")
        return await self.session.call_tool(tool, args, read_timeout_seconds=timedelta(seconds=timeout))

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, 15)
            except Exception:
                pass


async def test(project_id: str, conn: dict) -> list[dict]:
    """Start the server and list its tools (a REST connection: a read of its project / profile)."""
    if is_rest(conn):
        fields, secrets = fields_of(project_id, conn)
        missing = missing_fields(conn, secrets)
        if missing:
            raise McpError(f"Подключение «{conn['name']}»: заполните {', '.join(missing)}")
        try:
            if conn["preset"] == "testit":
                from . import testit
                return await testit.check(fields, secrets)
            from . import trackers
            return await trackers.check(conn["preset"], fields, secrets)
        except Exception as e:
            raise McpError(str(e)) from e
    async with connect(project_id, conn) as session:
        tools = (await session.list_tools()).tools
    return [{"name": t.name, "description": (t.description or "").strip().split("\n")[0][:200],
             "access": access(t.name)} for t in tools]


# ---------- tools for Claude ----------

_READ = {"get", "list", "search", "read", "find", "fetch", "query", "describe", "view", "show",
         "download", "export", "count", "browse", "lookup", "check"}
_DESTRUCTIVE = {"delete", "remove", "destroy", "drop", "purge", "erase", "archive", "unlink",
                "wipe", "truncate", "reset", "revoke", "close", "trash"}


def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", name.lower()) if w]


def access(tool_name: str) -> str:
    """"read", "write" or "destructive", guessed from the tool name."""
    words = _words(tool_name)
    if any(w in _DESTRUCTIVE for w in words):
        return "destructive"
    # "jira_get_issue", "getTestCase", "confluence_search": the verb is among the first words.
    if any(w in _READ for w in words[:2]):
        return "read"
    return "write"


class Toolbox:
    """MCP tools of several connections, offered to Claude as regular tools.

    mode="read": only tools that look read-only (authoring agent context);
    mode="write": everything except destructive tools (publishing to Zephyr).
    Tools that delete or remove data are never offered.
    """

    def __init__(self, project_id: str, conns: list[dict], mode: str = "read"):
        self.project_id, self.conns, self.mode = project_id, conns, mode
        self.clients: list[McpClient] = []
        self.tools: list[dict] = []
        self._route: dict[str, tuple[McpClient, str]] = {}

    async def start(self) -> "Toolbox":
        """Start every connection; one that fails is skipped and listed in `errors`."""
        self.errors: list[str] = []
        for conn in self.conns:
            if is_rest(conn):
                continue        # REST connections (Test IT, trackers) have no tools for the model
            try:
                client = await McpClient(self.project_id, conn).start()
            except McpError as e:
                self.errors.append(str(e))
                continue
            self.clients.append(client)
            prefix = re.sub(r"[^A-Za-z0-9]+", "_", conn["preset"] if conn["preset"] != "custom"
                            else conn["name"]).strip("_")[:20] or "mcp"
            for t in client.tools:
                a = access(t.name)
                if a == "destructive" or (self.mode == "read" and a != "read"):
                    continue
                name = f"{prefix}__{t.name}"[:64]
                name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
                schema = {k: v for k, v in (t.inputSchema or {}).items() if k != "$schema"}
                schema.setdefault("type", "object")
                self.tools.append({"name": name, "description": (
                    f"[{conn['name']}] " + (t.description or t.name))[:1000], "input_schema": schema})
                self._route[name] = (client, t.name)
        return self

    def owns(self, name: str) -> bool:
        return name in self._route

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """-> (text, is_error)"""
        client, tool = self._route[name]
        try:
            res = await client.call(tool, args)
        except Exception as e:
            return f"Tool failed: {_first_error(e)}", True
        text = result_text(res) or "(empty result)"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n…(truncated)"
        return text, bool(res.isError)

    async def close(self) -> None:
        for c in self.clients:
            await c.close()
        self.clients = []
