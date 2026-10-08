"""AI Test Generator - web studio.

Run:  python server.py   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel

from testgen import (access, analyses, apiclient, audit, auth, catalog, checks, db, defects, explorer, exporters, fs, knowledge,
                     llm, mailbox, mcp_hub, mcp_server, metrics, monitoring, mutations, notify, pipeline, projects,
                     publisher, reports, reuse, runs, scenarios, skills, sources, sso, storage, suite, tasks, traffic,
                     trackers, validation, vault, workqueue)
from testgen import worker as worker_mod
from testgen import agent as agent_mod
from testgen.agent import StudioSession
from testgen.browser import screen_size
from testgen.paths import utf8_console
from testgen.steps import ALL_ACTIONS, DATA_ACTIONS, check_api, new_step

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")    # 0.0.0.0 in a container

# Playwright objects are bound to the event loop that created them, so all
# browser work runs on one dedicated background loop; the web server just
# submits coroutines to it. MCP sessions, pipeline jobs and suite runs live there too.
WORKER = asyncio.new_event_loop()
threading.Thread(target=WORKER.run_forever, daemon=True, name="browser-worker").start()


def submit(coro):
    """Fire-and-forget on the worker loop."""
    return asyncio.run_coroutine_threadsafe(coro, WORKER)


async def call(coro):
    """Run on the worker loop and await the result."""
    return await asyncio.wrap_future(submit(coro))


SESSIONS: dict[str, StudioSession] = {}
SESSION_SCENARIOS: dict[str, tuple[str, str, str]] = {}   # session id -> (project, analysis, scenario) of the test
# With several instances of the studio (a shared database): this instance's address for the others.
# Studio sessions and pipeline jobs live in the instance that started them; requests about them that
# reach another instance are passed here (_elsewhere / _proxy).
INSTANCE_URL = os.environ.get("TESTGEN_INSTANCE_URL", "").strip().rstrip("/")
PROXIED = "X-Testgen-Proxied"
PROXY_TRANSPORT: httpx.AsyncBaseTransport | None = None     # tests: the other instance

try:
    db.check()                  # PostgreSQL answers, the schema is current, secrets can be encrypted
except db.NotConfigured as e:
    utf8_console()
    sys.exit(f"Студия не запущена: {e}")
projects.ensure_default()

# The studio as an MCP server for IDE agents, at /mcp (token auth in require_login).
MCP = mcp_server.build(mcp_server.Backend(submit=submit, sessions=SESSIONS, studio_url=f"http://127.0.0.1:{PORT}"))
MCP_APP = MCP.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_app):
    background = []
    if workqueue.enabled():
        # Queued runs: this instance takes them too unless dedicated workers do (TESTGEN_EMBEDDED_WORKER=off).
        if os.environ.get("TESTGEN_EMBEDDED_WORKER", "on").lower() not in ("off", "0", "false", "no"):
            w = worker_mod.Worker(int(os.environ.get("TESTGEN_WORKER_CONCURRENCY", "2")), name=f"web-{INSTANCE_ID}")
            background.append((w, submit(w.serve())))
        background.append((None, submit(_keep_owners())))
    async with MCP.session_manager.run():
        yield
    for w, fut in background:
        if w:
            w.stopping = True
        else:
            fut.cancel()


INSTANCE_ID = os.urandom(3).hex()


async def _keep_owners() -> None:
    """The live sessions and jobs of this instance stay findable for the other instances."""
    while True:
        if INSTANCE_URL:
            try:
                workqueue.touch_owners(INSTANCE_URL)
            except Exception as e:
                print(f"owners: {e}")
        monitoring.SESSIONS.set(len(SESSIONS))
        await asyncio.sleep(30)


PUBLIC = {"/", "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/logout", "/api/health",
          "/api/ready", "/metrics", "/api/auth/oidc/login", "/api/auth/oidc/callback"}
OIDC_COOKIE = "tg_oidc"


async def require_login(request: Request, call_next):
    if request.url.path.rstrip("/") == "/mcp":
        # IDE agents authenticate with an API token instead of the session cookie.
        header = request.headers.get("authorization", "")
        user = (auth.user_for_api_token(header.removeprefix("Bearer ").strip()) if auth.ENABLED
                else auth.ANONYMOUS)
        if not user:
            return JSONResponse({"detail": "Нужен API-токен студии: Authorization: Bearer <token>"}, status_code=401)
        mcp_server.CURRENT_USER.set(user)
        return await call_next(request)
    user = auth.read_token(request.cookies.get(auth.COOKIE)) if auth.ENABLED else auth.ANONYMOUS
    request.state.user = user
    storage.ACTOR.set(user or "")
    access.USER.set(user or "")
    if not user and request.url.path not in PUBLIC:
        return JSONResponse({"detail": "Требуется вход"}, status_code=401)
    target = _elsewhere(request)
    if target:
        return await _proxy(request, target)
    started = time.time()
    response = await call_next(request)
    route = getattr(request.scope.get("route"), "path", "")
    if route.startswith("/api/"):
        monitoring.observe(request.method, route, response.status_code, started)
    _audit(request, response.status_code)
    return response


def _elsewhere(request: Request) -> str | None:
    """The instance holding the Studio session or pipeline job this request is about, if not this one."""
    if not INSTANCE_URL or request.headers.get(PROXIED):
        return None
    m = re.match(r"/api/sessions/([^/]+)", request.url.path)
    if m and m.group(1) not in SESSIONS:
        url = workqueue.owner("session", m.group(1))
    else:
        m = re.match(r"/api/jobs/([^/]+)/(select|cancel|reuse)$", request.url.path)
        url = workqueue.owner("job", m.group(1)) if m and m.group(1) not in pipeline.JOBS else None
    return url if url and url != INSTANCE_URL else None


async def _proxy(request: Request, target: str) -> Response:
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("cookie", "content-type", "accept", "authorization")} | {PROXIED: INSTANCE_URL}
    try:
        async with httpx.AsyncClient(timeout=120, transport=PROXY_TRANSPORT) as client:
            r = await client.request(request.method, target + request.url.path, params=request.query_params,
                                     content=await request.body(), headers=headers)
    except httpx.HTTPError:
        return JSONResponse({"detail": "Экземпляр студии, где идёт эта сессия, недоступен"}, status_code=502)
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "cache-control", "content-disposition")}
    return Response(r.content, status_code=r.status_code, headers=keep)


# ---------- Audit log (audit.py): every change through the API, and exports ----------

AUDIT_ACTIONS = {
    ("POST", "/api/auth/login"): "auth.login",
    ("GET", "/api/auth/oidc/callback"): "auth.login",
    ("POST", "/api/auth/logout"): "auth.logout",
    ("POST", "/api/auth/register"): "auth.register",
    ("POST", "/api/auth/tokens"): "token.create",
    ("DELETE", "/api/auth/tokens/{tid}"): "token.delete",
    ("PUT", "/api/sso"): "sso.settings",
    ("POST", "/api/projects"): "project.create",
    ("PUT", "/api/projects/{pid}"): "project.update",
    ("DELETE", "/api/projects/{pid}"): "project.delete",
    ("PUT", "/api/projects/{pid}/access"): "project.access",
    ("PUT", "/api/projects/{pid}/credentials"): "project.credentials",
    ("POST", "/api/projects/{pid}/accounts"): "account.create",
    ("PUT", "/api/projects/{pid}/accounts/{aid}"): "account.update",
    ("DELETE", "/api/projects/{pid}/accounts/{aid}"): "account.delete",
    ("PUT", "/api/projects/{pid}/mailbox"): "project.mailbox",
    ("PUT", "/api/projects/{pid}/llm"): "project.llm",
    ("PUT", "/api/projects/{pid}/api"): "project.api",
    ("POST", "/api/projects/{pid}/api/endpoints"): "project.api_endpoint",
    ("DELETE", "/api/projects/{pid}/api/endpoints"): "project.api_endpoint",
    ("DELETE", "/api/projects/{pid}/llm/key"): "project.llm",
    ("PUT", "/api/projects/{pid}/notify"): "project.notify",
    ("GET", "/api/projects/{pid}/export"): "project.export",
    ("POST", "/api/projects/{pid}/connections"): "connection.create",
    ("PUT", "/api/projects/{pid}/connections/{cid}"): "connection.update",
    ("DELETE", "/api/projects/{pid}/connections/{cid}"): "connection.delete",
    ("DELETE", "/api/projects/{pid}/connections/{cid}/secrets/{key}"): "connection.secret_clear",
    ("POST", "/api/projects/{pid}/connections/{cid}/test"): "connection.test",
    ("PUT", "/api/projects/{pid}/skills/{name}"): "skill.save",
    ("DELETE", "/api/projects/{pid}/skills/{name}"): "skill.delete",
    ("POST", "/api/projects/{pid}/skills/{name}/clone"): "skill.save",
    ("PUT", "/api/projects/{pid}/skills/{name}/local"): "skill.save",
    ("PUT", "/api/projects/{pid}/skills/{name}/enabled"): "skill.save",
    ("PUT", "/api/projects/{pid}/skills/{name}/stages"): "skill.save",
    ("PUT", "/api/projects/{pid}/knowledge"): "knowledge.save",
    ("POST", "/api/projects/{pid}/knowledge/extract"): "knowledge.save",
    ("POST", "/api/projects/{pid}/knowledge/confirm"): "knowledge.confirm",
    ("POST", "/api/projects/{pid}/knowledge/pending"): "knowledge.save",
    ("POST", "/api/projects/{pid}/knowledge/duplicates"): "knowledge.save",
    ("POST", "/api/projects/{pid}/files"): "file.upload",
    ("DELETE", "/api/projects/{pid}/files/{name}"): "file.delete",
    ("POST", "/api/projects/{pid}/runs"): "suite.start",
    ("POST", "/api/projects/{pid}/jobs"): "pipeline.start",
    ("POST", "/api/sessions"): "studio.start",
    ("POST", "/api/sessions/{sid}/save"): "test.save",
    ("POST", "/api/projects/{pid}/sessions/{sid}/restore"): "studio.restore",
    ("POST", "/api/projects/{pid}/sessions/{sid}/save"): "test.save",
    ("DELETE", "/api/projects/{pid}/sessions/{sid}"): "studio.discard",
    ("POST", "/api/jobs/{jid}/resume"): "pipeline.resume",
    ("POST", "/api/jobs/{jid}/retry"): "pipeline.retry",
    ("PUT", "/api/tests/{tid}"): "test.update",
    ("PUT", "/api/tests/{tid}/steps"): "test.update",
    ("POST", "/api/tests/{tid}/edit"): "studio.start",
    ("PATCH", "/api/tests/{tid}/meta"): "test.meta",
    ("DELETE", "/api/tests/{tid}"): "test.delete",
    ("POST", "/api/tests/{tid}/versions/{n}/restore"): "test.restore",
    ("PUT", "/api/tests/{tid}/data"): "test.data",
    ("PUT", "/api/tests/{tid}/credentials"): "test.credentials",
    ("GET", "/api/tests/{tid}/export"): "test.export",
    ("POST", "/api/tests/{tid}/run"): "run.start",
    ("POST", "/api/tests/{tid}/verify"): "test.verify",
    ("POST", "/api/tests/{tid}/publish"): "test.publish",
    ("POST", "/api/tests/{tid}/comments"): "test.comment",
    ("DELETE", "/api/tests/{tid}/comments/{cid}"): "test.comment",
    ("POST", "/api/tests/{tid}/proposals/{prop_id}/{decision}"): "heal.{decision}",
    ("POST", "/api/runs/{rid}/defect"): "defect.create",
    ("POST", "/api/runs/{rid}/baseline/{step_id}"): "baseline.accept",
    ("POST", "/api/runs/{rid}/retry"): "run.start",
    ("POST", "/api/runs/{rid}/fix"): "studio.start",
    ("POST", "/api/suites/{sid}/retry"): "suite.start",
}
# Frequent and harmless: polling the Studio, the agent's steps, the pipeline's choices.
AUDIT_SKIP = ("/api/sessions/{sid}/", "/api/jobs/{jid}/", "/api/scenarios", "/api/requirements/")


def _audit(request: Request, status: int) -> None:
    path = getattr(request.scope.get("route"), "path", "")
    action = AUDIT_ACTIONS.get((request.method, path))
    if not action and (request.method in ("GET", "HEAD", "OPTIONS") or not path.startswith("/api/")
                       or any(path.startswith(p) for p in AUDIT_SKIP)):
        return
    params = dict(request.scope.get("path_params") or {})
    extra = dict(getattr(request.state, "audit", None) or {})
    action = action.format(**params) if action else f"{request.method} {path}"
    try:
        audit.record(action, user=extra.pop("user", None) or request.state.user or "",
                     project_id=getattr(request.state, "project_id", "") or params.get("pid", ""),
                     target={k: v for k, v in params.items() if k != "pid"}, status=status, details=extra,
                     via="web", ip=request.client.host if request.client else "")
    except OSError as e:     # the log must not take the studio down; the chain shows the gap
        print(f"audit: {e}")


def require_admin(request: Request) -> None:
    if not auth.is_admin(request.state.user):
        raise HTTPException(403, "Это может сделать только администратор студии")


# ---------- Access to projects (roles: access.py) ----------
#
# Every route under a project's resource is checked here, before its handler and before its
# body is read: without access the answer is 404 as for a resource that does not exist, with a
# lower role than needed 403. A route not listed in ROLE_RULES needs a viewer for GET and an
# editor for anything else. Handlers that take the project from the body check it themselves
# (project(pid, need)).

ROLE_RULES = {
    ("POST", "/api/tests/{tid}/run"): "viewer",
    ("POST", "/api/projects/{pid}/runs"): "viewer",
    ("POST", "/api/runs/{rid}/retry"): "viewer",
    ("POST", "/api/suites/{sid}/retry"): "viewer",
    ("GET", "/api/tests/{tid}/credentials"): "editor",
    ("PUT", "/api/projects/{pid}"): "owner",
    ("DELETE", "/api/projects/{pid}"): "owner",
    ("PUT", "/api/projects/{pid}/access"): "owner",
    ("PUT", "/api/projects/{pid}/credentials"): "owner",
    ("POST", "/api/projects/{pid}/accounts"): "owner",
    ("PUT", "/api/projects/{pid}/accounts/{aid}"): "owner",
    ("DELETE", "/api/projects/{pid}/accounts/{aid}"): "owner",
    ("PUT", "/api/projects/{pid}/mailbox"): "owner",
    ("PUT", "/api/projects/{pid}/llm"): "owner",
    ("DELETE", "/api/projects/{pid}/llm/key"): "owner",
    ("POST", "/api/projects/{pid}/llm/test"): "owner",
    ("PUT", "/api/projects/{pid}/notify"): "owner",
    ("POST", "/api/projects/{pid}/notify/test"): "owner",
    ("POST", "/api/projects/{pid}/connections"): "owner",
    ("PUT", "/api/projects/{pid}/connections/{cid}"): "owner",
    ("DELETE", "/api/projects/{pid}/connections/{cid}"): "owner",
    ("DELETE", "/api/projects/{pid}/connections/{cid}/secrets/{key}"): "owner",
    ("POST", "/api/projects/{pid}/connections/{cid}/test"): "owner",
    ("PUT", "/api/projects/{pid}/skills/{name}"): "owner",
    ("DELETE", "/api/projects/{pid}/skills/{name}"): "owner",
    ("POST", "/api/projects/{pid}/skills/{name}/clone"): "owner",
    ("PUT", "/api/projects/{pid}/skills/{name}/local"): "owner",
    ("PUT", "/api/projects/{pid}/skills/{name}/enabled"): "owner",
    ("PUT", "/api/projects/{pid}/skills/{name}/stages"): "owner",
    ("GET", "/api/projects/{pid}/audit"): "owner",
}

# Route prefix -> (path parameter, its project id or None, the "not found" text).
RESOURCES = {
    "/api/projects/{pid}": ("pid", lambda v: v, "Проект не найден"),
    "/api/tests/{tid}": ("tid", lambda v: (storage.load(v) or {}).get("project_id"), "Тест не найден"),
    "/api/tasks/{tid}": ("tid", lambda v: (tasks.load(v) or {}).get("project_id"), "Задача не найдена"),
    "/api/runs/{rid}": ("rid", lambda v: (runs.get(v) or {}).get("project_id"), "Прогон не найден"),
    "/api/suites/{sid}": ("sid", lambda v: (suite.get(v) or {}).get("project_id"), "Прогон набора не найден"),
    "/api/jobs/{jid}": ("jid", lambda v: (pipeline.get_job(v) or {}).get("project_id"), "Запуск не найден"),
    "/api/analyses/{aid}": ("aid", lambda v: (analyses.get(v) or {}).get("project_id"), "Анализ не найден"),
    "/api/sessions/{sid}": ("sid", lambda v: SESSIONS[v].project["id"] if v in SESSIONS else None,
                            "Session not found"),
}


def _allowed(p: dict | None, need: str, missing: str) -> dict:
    if not p:
        raise HTTPException(404, missing)
    try:
        access.check(access.USER.get(), p, need, missing)
    except access.Denied as e:
        raise HTTPException(404 if e.hidden else 403, str(e))
    return p


def route_role(method: str, path: str) -> str:
    return ROLE_RULES.get((method, path)) or ("viewer" if method in ("GET", "HEAD") else "editor")


async def guard(request: Request) -> None:
    path = getattr(request.scope.get("route"), "path", "")
    for prefix, (param, owner_of, missing) in RESOURCES.items():
        if path == prefix or path.startswith(prefix + "/"):
            pid = owner_of(request.path_params[param])
            break
    else:
        if path != "/api/tests":      # the test list of a project: ?project_id=
            return
        pid, missing = request.query_params.get("project_id"), "Проект не найден"
    request.state.project_id = pid       # the audit log records denied attempts too
    _allowed(projects.get(pid) if pid else None, route_role(request.method, path), missing)


app = FastAPI(title="AI Test Generator", lifespan=lifespan, dependencies=[Depends(guard)])
app.middleware("http")(require_login)


def _attachment(name: str, ext: str, inline: bool = False) -> dict:
    # Names are often non-ASCII (e.g. Cyrillic): HTTP headers need RFC 5987 encoding.
    kind = "inline" if inline else "attachment"
    return {"Content-Disposition": f"{kind}; filename=\"file.{ext}\"; filename*=UTF-8''{quote(name)}"}


@app.get("/api/health")
async def health():
    """Liveness for Docker / Kubernetes: the web server answers and the browser worker loop runs."""
    ok = WORKER.is_running()
    return JSONResponse({"ok": ok, "sessions": len(SESSIONS)}, status_code=200 if ok else 503)


@app.get("/api/ready")
async def ready():
    """Readiness: the browser loop, the database and S3 (when configured) answer."""
    checks_ = {"worker_loop": WORKER.is_running(), "database": await asyncio.to_thread(db.ping)}
    if fs.s3_enabled():
        checks_["s3"] = await asyncio.to_thread(fs.s3_ping)
    if workqueue.enabled():
        try:
            checks_["queue"] = await asyncio.to_thread(workqueue.stats)
        except Exception:
            checks_["queue"] = False
    ok = all(v is not False for v in checks_.values())
    return JSONResponse({"ok": ok, "storage": "database"} | checks_, status_code=200 if ok else 503)


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    token = os.environ.get("TESTGEN_METRICS_TOKEN", "").strip()
    if token and request.headers.get("authorization", "") != f"Bearer {token}":
        raise HTTPException(401, "Нужен токен метрик")
    monitoring.SESSIONS.set(len(SESSIONS))
    return Response(await asyncio.to_thread(monitoring.render), media_type=monitoring.CONTENT_TYPE)


# ---------- Studio login ----------

class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: Login, request: Request):
    """A password of the directory (LDAP), or of the studio itself when local passwords are on."""
    if not auth.ENABLED:
        return {"user": auth.ANONYMOUS}
    name = body.username.strip()
    request.state.audit = {"user": name[:64]}
    if sso.ldap_enabled():
        try:
            found = await asyncio.to_thread(sso.ldap_login, name, body.password)
        except sso.SsoError as e:
            if not auth.LOCAL_LOGIN:
                raise HTTPException(502, str(e))
            found = None
        if found:
            try:
                user = auth.sso_login(found[0], found[1], "ldap")
            except sso.SsoError as e:
                raise HTTPException(403, str(e))
            request.state.audit = {"user": user, "method": "ldap", "groups": found[1]}
            return _session_response(user, request, auth.SSO_SESSION_TTL)
    if not auth.LOCAL_LOGIN:
        await asyncio.sleep(1)
        if sso.ldap_enabled():
            raise HTTPException(401, "Неверный логин или пароль")
        raise HTTPException(403, f"Вход по паролю студии выключен: «{sso.oidc_title()}»")
    if not auth.verify(name, body.password):
        await asyncio.sleep(1)   # slow down password guessing
        raise HTTPException(401, "Неверный логин или пароль")
    return _session_response(name, request)


def _login_failed(text: str) -> RedirectResponse:
    resp = RedirectResponse("/?login_error=" + quote(text[:300]), status_code=302)
    resp.delete_cookie(OIDC_COOKIE)
    return resp


@app.get("/api/auth/oidc/login")
async def oidc_login(request: Request):
    """Off to the identity provider (authorization code + PKCE); it comes back to the callback."""
    if not auth.ENABLED or not sso.oidc_enabled():
        raise HTTPException(404, "Вход через OIDC не настроен")
    redirect = (os.environ.get("TESTGEN_OIDC_REDIRECT_URL")
                or f"{request.base_url}".rstrip("/") + "/api/auth/oidc/callback")
    try:
        url, flow = await asyncio.to_thread(sso.start, redirect)
    except sso.SsoError as e:
        return _login_failed(str(e))
    resp = RedirectResponse(url, status_code=302)
    # Lax: the provider sends the browser back with a cross-site GET.
    resp.set_cookie(OIDC_COOKIE, sso.pack_flow(flow, auth._sign), max_age=sso.FLOW_TTL, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.get("/api/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                        error_description: str = ""):
    if not auth.ENABLED or not sso.oidc_enabled():
        raise HTTPException(404, "Вход через OIDC не настроен")
    flow = sso.unpack_flow(request.cookies.get(OIDC_COOKIE), auth._sign)
    try:
        if error:
            raise sso.SsoError(f"Провайдер входа: {error_description or error}")
        username, groups = await asyncio.to_thread(sso.finish, flow, code, state)
        user = auth.sso_login(username, groups, "oidc")
    except sso.SsoError as e:
        request.state.audit = {"error": str(e)[:300]}
        return _login_failed(str(e))
    request.state.audit = {"user": user, "method": "oidc", "groups": groups}
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(OIDC_COOKIE)
    resp.set_cookie(auth.COOKIE, auth.make_token(user, auth.SSO_SESSION_TTL), max_age=auth.SSO_SESSION_TTL,
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/api/auth/register")
async def register(body: Login, request: Request):
    if not auth.SIGNUP:
        raise HTTPException(403, "Регистрация отключена")
    request.state.audit = {"user": body.username.strip()[:64]}
    try:
        auth.register(body.username.strip(), body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _session_response(body.username.strip(), request)


def _session_response(username: str, request: Request, ttl: int | None = None) -> JSONResponse:
    resp = JSONResponse({"user": username})
    resp.set_cookie(auth.COOKIE, auth.make_token(username, ttl), max_age=ttl or auth.SESSION_TTL,
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    admin = bool(request.state.user) and auth.is_admin(request.state.user)
    return {"user": request.state.user, "auth_enabled": auth.ENABLED, "signup": auth.SIGNUP,
            "local_login": auth.LOCAL_LOGIN, "sso": {"oidc": sso.oidc_title(), "ldap": sso.ldap_enabled()},
            "groups": auth.user_groups(request.state.user), "secrets": vault.describe() if admin else None,
            "is_admin": bool(request.state.user) and auth.is_admin(request.state.user),
            "roles": {r: access.LABELS[r] for r in access.ROLES},
            "efforts": llm.EFFORTS, "prompt_cache": llm.PROMPT_CACHE,
            "mcp_url": f"{request.base_url}".rstrip("/") + "/mcp"}


class TokenBody(BaseModel):
    name: str = ""


@app.get("/api/auth/tokens")
async def list_tokens(request: Request):
    return auth.list_api_tokens(request.state.user)


@app.post("/api/auth/tokens")
async def create_token(body: TokenBody, request: Request):
    token, rec = auth.create_api_token(request.state.user, body.name)
    return rec | {"token": token}


@app.delete("/api/auth/tokens/{tid}")
async def delete_token(tid: str, request: Request):
    return {"ok": auth.delete_api_token(request.state.user, tid)}


# ---------- Projects ----------

def project(pid: str, need: str = "viewer") -> dict:
    """The project if the current user has at least the role `need` in it (404 / 403 otherwise)."""
    return _allowed(projects.get(pid), need, "Проект не найден")


def _project_view(p: dict) -> dict:
    role = access.role(access.USER.get(), p)
    c = projects.app_credentials(p["id"])
    editor = access.RANK[role] >= access.RANK["editor"]
    return p | {"role": role, "visibility": p.get("visibility", "open"),
                "connections": [mcp_hub.public_view(p["id"], x) for x in p["connections"]],
                # Secrets are for editors: a viewer does not even see the login.
                "app_username": c.get("username", "") if editor else "",
                "accounts": projects.accounts_view(p["id"], full=editor),
                "app_has_password": bool(c.get("password")),
                "app_has_totp": bool(c.get("totp_secret")), "mailbox_has_password": bool(mailbox.password(p["id"])),
                "files": agent_mod.project_files(p["id"]), "notify": notify.public_view(p),
                "llm": p["llm"] | {"key_set": bool(projects.llm_key(p["id"])),
                                   "env_key": bool(os.environ.get("ANTHROPIC_API_KEY"))},
                "api": projects.api_settings(p) | {"token_set": bool(projects.api_token(p["id"])),
                                                   "effective_url": projects.api_base(p)},
                "catalog": CATALOG}


# What can be chosen for test design: labels and Russian descriptions for the UI (the models get their own).
CATALOG = {"types": {k: {"label": v[0], "hint": catalog.TYPE_INFO[k]} for k, v in catalog.TYPES.items()},
           "techniques": {k: {"label": v[0], "hint": catalog.TECHNIQUE_INFO[k]} for k, v in catalog.TECHNIQUES.items()},
           "layers": catalog.LAYERS,
           "standards": {k: {"title": v[0], "about": catalog.STANDARD_INFO[k], "sections": v[1]}
                         for k, v in catalog.STANDARDS.items()},
           "quality": {k: v[0] for k, v in catalog.QUALITY.items()}}


def _require_model(p: dict) -> None:
    """Fail before starting a browser when the project has no model to drive it."""
    if not p["llm"]["model"]:
        raise HTTPException(400, "Модель не настроена: выберите её в «Проект → Модель».")


def _require_lifecycle(p: dict) -> None:
    """Tests from requirements are generated only after a person confirmed the lifecycle of the system
    (the setting "requirements.confirm_model"; the pipeline waits for it instead)."""
    if p["pipeline"]["requirements"].get("confirm_model") and not knowledge.is_confirmed(p["id"]):
        raise HTTPException(409, "Сначала подтвердите жизненный цикл системы — сущности, статусы, роли и их "
                                 "возможности — в «Проект → Тестовые данные»")


class ProjectBody(BaseModel):
    name: str | None = None
    description: str | None = None
    base_url: str | None = None
    language: str | None = None      # "" | ru | en: steps, scenarios, Gherkin
    pipeline: dict | None = None


@app.get("/api/projects")
async def list_projects(request: Request):
    """Projects the user may see, with a connection summary for the projects page."""
    out = []
    for item in projects.list_projects():
        p = projects.get(item["id"])
        role = access.role(request.state.user, p) if p else None
        if not role:
            continue
        conns = [mcp_hub.public_view(item["id"], c) for c in p["connections"]]
        out.append(item | {"connections": [{k: c[k] for k in ("id", "name", "preset", "title", "enabled",
                                                                "missing", "check")} for c in conns],
                           "model": p["llm"]["model"], "model_check": p["llm"]["check"],
                           "has_app_login": bool(projects.app_credentials(item["id"]).get("username")),
                           "open_tasks": tasks.counts(item["id"])["open"], "role": role,
                           "visibility": p.get("visibility", "open")})
    return out


@app.post("/api/projects")
async def create_project(body: ProjectBody, request: Request):
    """The author becomes the owner; the project is visible to its members only."""
    try:
        p = projects.create(body.name or "", body.description or "", body.base_url or "",
                            owner=request.state.user if auth.ENABLED else "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.state.project_id = p["id"]
    return _project_view(p)


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    return _project_view(project(pid))


@app.put("/api/projects/{pid}")
async def update_project(pid: str, body: ProjectBody):
    project(pid)
    try:
        return _project_view(projects.update(pid, body.model_dump(exclude_none=True)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str, request: Request):
    require_admin(request)
    project(pid)
    return {"ok": projects.delete(pid)}


# ---------- The application's API: address, authorization, endpoints (API tests) ----------

class ApiBody(BaseModel):
    base_url: str | None = None
    auth: str | None = None          # cookies | none | bearer | header | login
    header: str | None = None
    login: dict | None = None        # method, path, body, token (JSON path), header
    token: str = ""                  # empty = keep the saved one
    clear_token: bool = False


@app.get("/api/projects/{pid}/api")
async def get_api(pid: str):
    """The API settings (the token only as "set") and the endpoints the project has seen."""
    p = project(pid)
    return {"settings": _project_view(p)["api"], "endpoints": traffic.catalog(pid)}


@app.put("/api/projects/{pid}/api")
async def update_api(pid: str, body: ApiBody):
    project(pid, "editor")
    try:
        p = projects.update_api(pid, body.model_dump(exclude_none=True, exclude={"token", "clear_token"}),
                                body.token, body.clear_token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _project_view(p)


class EndpointBody(BaseModel):
    method: str
    path: str
    query: list[str] | str = []
    request: list[str] | str = []
    response: list[str] | str = []
    statuses: list[int | str] | str = []
    note: str = ""
    old: str = ""                    # "METHOD /path" of the endpoint being edited; empty = a new one


@app.post("/api/projects/{pid}/api/endpoints")
async def save_endpoint(pid: str, body: EndpointBody):
    """Add an endpoint to the catalog or edit one (a recorded endpoint gets the person's version)."""
    project(pid, "editor")
    try:
        traffic.save_endpoint(pid, body.model_dump(exclude={"old"}), body.old.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return traffic.catalog(pid)


@app.delete("/api/projects/{pid}/api/endpoints")
async def delete_endpoint(pid: str, key: str = ""):
    """Remove an endpoint ("METHOD /path"); a recorded one does not come back from new traffic."""
    project(pid, "editor")
    if not traffic.delete_endpoint(pid, key.strip()):
        raise HTTPException(404, "Эндпоинт не найден")
    return traffic.catalog(pid)


@app.post("/api/projects/{pid}/api/test")
async def test_api(pid: str):
    """Sign in the way API tests do and call the API address."""
    p = project(pid, "editor")
    try:
        return await call(asyncio.wait_for(apiclient.check(p), 90))
    except asyncio.TimeoutError:
        raise HTTPException(400, "API не ответил за 90 секунд")
    except Exception as e:
        raise HTTPException(400, str(e).splitlines()[0][:300] if str(e) else type(e).__name__)


# ---------- The project's model connection ----------

class LlmBody(BaseModel):
    model: str | None = None
    effort: str | None = None
    base_url: str | None = None
    prices: dict[str, list[float]] | None = None
    api_key: str = ""    # empty = keep the saved one


@app.put("/api/projects/{pid}/llm")
async def update_llm(pid: str, body: LlmBody, request: Request):
    """The project's model connection. The API address decides where requests (and the
    key) go, so only an administrator changes it."""
    p = project(pid, "owner")
    patch = body.model_dump(exclude_none=True, exclude={"api_key"})
    if "base_url" in patch and patch["base_url"].strip().rstrip("/") != p["llm"]["base_url"]:
        require_admin(request)
    return _project_view(projects.update_llm(pid, patch, body.api_key))


@app.delete("/api/projects/{pid}/llm/key")
async def clear_llm_key(pid: str):
    project(pid, "owner")
    projects.clear_llm_key(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/llm/test")
async def test_llm(pid: str):
    """Check the connection and list the models it offers; remembered in the project."""
    project(pid, "owner")
    conf = projects.llm_settings(pid)
    try:
        models = await call(asyncio.wait_for(llm.check(conf), 60))
        result = {"ok": True, "models": len(models)}
    except Exception as e:
        models, result = [], {"ok": False, "error": "Нет ответа от API" if isinstance(e, asyncio.TimeoutError)
                              else llm.api_error_text(e)}
    p = projects.get(pid)
    p["llm"]["check"] = result | {"at": time.time()}
    if result["ok"]:
        p["llm"]["models"] = models
    projects.save(p)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return {"models": models}


# ---------- Members of a project; directory groups -> roles (admins) ----------

@app.get("/api/projects/{pid}/access")
async def get_access(pid: str, request: Request):
    p = project(pid)
    return {"visibility": p.get("visibility", "open"), "role": access.role(request.state.user, p),
            "members": [{"user": u, "role": r} for u, r in sorted((p.get("members") or {}).items())],
            "groups": [r for r in auth.sso_settings()["group_roles"] if r.get("project") in ("*", pid)],
            "users": auth.list_users(), "roles": [{"id": r, "title": access.LABELS[r]} for r in access.ROLES]}


class AccessBody(BaseModel):
    visibility: str = "members"
    members: dict[str, str] = {}


@app.put("/api/projects/{pid}/access")
async def set_access(pid: str, body: AccessBody, request: Request):
    p = project(pid, "owner")
    if body.visibility not in access.VISIBILITY:
        raise HTTPException(400, "Видимость проекта: members или open")
    try:
        members = access.normalize_members(body.members)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if "owner" not in members.values():
        raise HTTPException(400, "В проекте должен остаться хотя бы один владелец")
    before = p.get("members") or {}
    request.state.audit = {"visibility": body.visibility, "members": {
        u: [before.get(u), members.get(u)] for u in set(before) | set(members) if before.get(u) != members.get(u)}}
    projects.set_access(pid, body.visibility, members)
    return await get_access(pid, request)


@app.get("/api/projects/{pid}/audit")
async def project_audit(pid: str, user: str = "", action: str = "", month: str = "", limit: int = 200):
    """The project's part of the audit log, for its owners."""
    return audit.read(month, project_id=pid, user=user, action=action, limit=max(1, min(limit, 2000)))


@app.get("/api/audit")
async def studio_audit(request: Request, project_id: str = "", user: str = "", action: str = "", month: str = "",
                       limit: int = 200):
    require_admin(request)
    return audit.read(month, project_id=project_id, user=user, action=action, limit=max(1, min(limit, 5000)))


@app.get("/api/audit/verify")
async def audit_verify(request: Request):
    """Is the hash chain of the log intact (nothing edited, inserted or removed)?"""
    require_admin(request)
    return audit.verify()


@app.get("/api/audit/export")
async def audit_export(month: str, request: Request):
    """A month of the log as JSON lines, for a SIEM or an auditor."""
    require_admin(request)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise HTTPException(400, "Месяц в формате ГГГГ-ММ")
    return Response(audit.export(month), media_type="application/x-ndjson",
                    headers=_attachment(f"audit-{month}.jsonl", "jsonl"))


@app.get("/api/users")
async def list_users():
    """Studio users (to pick members and assignees)."""
    return auth.list_users()


@app.get("/api/sso")
async def get_sso(request: Request):
    require_admin(request)
    return auth.sso_settings()


class SsoBody(BaseModel):
    group_roles: list[dict] = []
    admin_groups: list[str] = []


@app.put("/api/sso")
async def set_sso(body: SsoBody, request: Request):
    """Directory groups (OIDC, LDAP) -> roles in projects, and the groups of studio admins."""
    require_admin(request)
    try:
        rules = access.normalize_rules(body.group_roles)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return auth.save_sso_settings({"group_roles": rules,
                                   "admin_groups": sorted({g.strip() for g in body.admin_groups if g.strip()})})


class Credentials(BaseModel):
    username: str = ""
    password: str = ""   # empty = keep the saved one
    totp_secret: str | None = None   # None = keep, "" = remove (2FA: the base32 secret of the authenticator)
    account: str | None = None       # a test: the project account it runs with ("" = the default one)


def _check_totp(secret: str | None) -> None:
    if secret and not re.fullmatch(r"[A-Za-z2-7\s=]{16,128}", secret):
        raise HTTPException(400, "Секрет TOTP — строка base32 (буквы A–Z и цифры 2–7)")


@app.put("/api/projects/{pid}/credentials")
async def set_project_credentials(pid: str, body: Credentials):
    """The default account's login (the wizard); the accounts themselves: /accounts."""
    project(pid)
    _check_totp(body.totp_secret)
    projects.set_app_credentials(pid, body.username, body.password, body.totp_secret)
    return {"ok": True}


class AuthParam(BaseModel):
    name: str
    value: str = ""      # a secret one: empty = keep the saved value
    secret: bool = False


class AccountBody(BaseModel):
    name: str | None = None
    username: str | None = None
    password: str = ""                # empty = keep the saved one
    totp_secret: str | None = None    # None = keep, "" = remove
    params: list[AuthParam] | None = None   # extra login parameters: {{auth.<name>}}
    default: bool = False
    roles: list[str] | None = None    # ids of the roles of the application model that log in with it; None = keep


def _save_account(pid: str, body: AccountBody, user: str, aid: str = "") -> dict:
    _check_totp(body.totp_secret)
    try:
        acc = projects.save_account(pid, body.model_dump(exclude={"roles"}), aid)
    except KeyError:
        raise HTTPException(404, "Учётная запись не найдена")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.roles is not None:
        knowledge.link_account(pid, acc["id"], body.roles, user)
    return {"id": acc["id"], "accounts": projects.accounts_view(pid)}


@app.post("/api/projects/{pid}/accounts")
async def create_account(pid: str, body: AccountBody, request: Request):
    """Any number of accounts of the application under test, each with its own login parameters and the
    roles of the application model it is for."""
    return _save_account(pid, body, request.state.user)


@app.put("/api/projects/{pid}/accounts/{aid}")
async def update_account(pid: str, aid: str, body: AccountBody, request: Request):
    return _save_account(pid, body, request.state.user, aid)


@app.delete("/api/projects/{pid}/accounts/{aid}")
async def delete_account(pid: str, aid: str, request: Request):
    """Tests of a deleted account run with the default one."""
    if not projects.delete_account(pid, aid):
        raise HTTPException(404, "Учётная запись не найдена")
    knowledge.link_account(pid, aid, [], request.state.user)     # its roles have no account now
    return {"accounts": projects.accounts_view(pid)}


class MailboxBody(BaseModel):
    kind: str = ""        # mailpit | imap | "" (none)
    url: str = ""
    host: str = ""
    port: int = 993
    user: str = ""
    ssl: bool = True
    password: str = ""    # IMAP; empty = keep


@app.put("/api/projects/{pid}/mailbox")
async def set_mailbox(pid: str, body: MailboxBody):
    """The project's test mailbox for read_email steps (codes from letters)."""
    project(pid)
    return mailbox.set_mailbox(pid, body.model_dump(exclude={"password"}), body.password)


# ---------- Project files (upload_file steps) ----------

_FILE_NAME = re.compile(r"[^/\\:*?\"<>|\x00-\x1f]{1,200}")


def _files_dir(pid: str) -> Path:
    return projects.path(pid) / "files"


@app.get("/api/projects/{pid}/files")
async def list_files(pid: str):
    project(pid)
    return [{"name": f.name, "size": fs.size(f)} for f in sorted(fs.glob(_files_dir(pid), "*")) if fs.is_file(f)]


@app.post("/api/projects/{pid}/files")
async def upload_file(pid: str, file: UploadFile):
    project(pid)
    name = Path(file.filename or "").name
    if not _FILE_NAME.fullmatch(name) or name.startswith("."):
        raise HTTPException(400, "Недопустимое имя файла")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ")
    fs.write_bytes(_files_dir(pid) / name, data)
    return {"name": name, "size": len(data)}


@app.delete("/api/projects/{pid}/files/{name}")
async def delete_file(pid: str, name: str):
    project(pid)
    f = _files_dir(pid) / Path(name).name
    if not _FILE_NAME.fullmatch(name) or not fs.is_file(f):
        raise HTTPException(404, "Файл не найден")
    fs.unlink(f)
    return {"ok": True}


def _file_bytes(pid: str):
    def read(name: str) -> bytes | None:
        f = _files_dir(pid) / Path(name).name
        return fs.read_bytes(f) if fs.is_file(f) else None
    return read


@app.get("/api/projects/{pid}/usage")
async def project_usage(pid: str, month: str = ""):
    """Language model spending of the project in a month, by stage and model."""
    project(pid)
    if month and not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "Месяц в формате ГГГГ-ММ")
    return llm.ledger_report(pid, month)


@app.get("/api/projects/{pid}/metrics")
async def project_metrics(pid: str, days: int = 30):
    """The QA lead's dashboard: automation, stability, quality of checks, regression time, saved hours."""
    project(pid)
    return metrics.project_metrics(pid, max(1, min(days, 365)))


@app.get("/api/projects/{pid}/tags")
async def project_tags(pid: str):
    project(pid)
    return sorted({tag for t in storage.all_tests(pid) for tag in t.get("tags") or []})


class NotifyBody(BaseModel):
    settings: dict = {}
    secrets: dict[str, str] = {}      # telegram_token, mattermost_webhook, smtp_password; empty = keep


@app.put("/api/projects/{pid}/notify")
async def set_notify(pid: str, body: NotifyBody):
    project(pid)
    secrets = {k: v for k, v in body.secrets.items() if k in notify.SECRET_KEYS and v.strip()}
    notify.save(pid, body.settings, secrets)
    return notify.public_view(project(pid))


@app.post("/api/projects/{pid}/notify/test")
async def test_notify(pid: str):
    p = project(pid)
    failed = await call(notify.send(p, f"{p['name']}: проверка уведомлений", "Если вы видите это сообщение, "
                                    "уведомления AI Test Generator настроены."))
    if failed:
        raise HTTPException(400, "Не удалось отправить: " + ", ".join(failed) + " (подробности — в журнале студии)")
    return {"ok": True}


@app.get("/api/projects/{pid}/export")
async def export_project(pid: str, tag: str = "", testit: bool = False):
    """All (or tagged) tests as a pytest project in a zip: conftest.py, tests/, features/."""
    p = project(pid)
    tests = storage.select(pid, tags=[tag] if tag else None, include_drafts=True)
    if not tests:
        raise HTTPException(404, "Нет тестов для экспорта")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in exporters.bundle(p, tests, p["pipeline"]["run"]["a11y_impact"], lookup=storage.load,
                                           login=pipeline.login_test(pid), files=_file_bytes(pid),
                                           testit=testit).items():
            z.writestr(path, text)
    name = "".join(c if c.isalnum() else "_" for c in p["name"]).lower() or "tests"
    return Response(buf.getvalue(), media_type="application/zip", headers=_attachment(f"{name}_tests.zip", "zip"))


# ---------- Project MCP connections ----------

@app.get("/api/mcp/presets")
async def mcp_presets(request: Request):
    # Launch commands come from the server's environment: only admins see them.
    return mcp_hub.presets_public(with_commands=bool(request.state.user) and auth.is_admin(request.state.user))


class NewConnection(BaseModel):
    preset: str
    name: str = ""


class ConnectionBody(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    fields: dict[str, str] = {}      # declared by the preset; empty secret = keep the saved one
    # Admin only: they decide what runs on the server.
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: str | None = None           # KEY=VALUE lines, not secret


def _conn(p: dict, cid: str) -> dict:
    c = next((c for c in p["connections"] if c["id"] == cid), None)
    if not c:
        raise HTTPException(404, "Подключение не найдено")
    return c


@app.post("/api/projects/{pid}/connections")
async def add_connection(pid: str, body: NewConnection, request: Request):
    p = project(pid)
    if body.preset not in mcp_hub.PRESETS:
        raise HTTPException(400, "Неизвестный тип подключения")
    if mcp_hub.PRESETS[body.preset].get("admin_only"):
        require_admin(request)
    c = mcp_hub.new_connection(body.preset, body.name.strip())
    p["connections"].append(c)
    projects.save(p)
    return mcp_hub.public_view(pid, c)


@app.put("/api/projects/{pid}/connections/{cid}")
async def update_connection(pid: str, cid: str, body: ConnectionBody, request: Request):
    p = project(pid)
    c = _conn(p, cid)
    preset = mcp_hub.PRESETS[c["preset"]]
    admin_fields = {k: v for k, v in body.model_dump().items()
                    if k in ("transport", "command", "args", "url", "env") and v is not None}
    if admin_fields or preset.get("admin_only"):
        require_admin(request)
    if body.name is not None:
        c["name"] = body.name.strip() or preset["title"]
    if body.enabled is not None:
        c["enabled"] = body.enabled
    declared = {f["key"]: f for f in preset["fields"]}
    secrets = mcp_hub.load_secrets(pid, cid)
    for key, value in body.fields.items():
        if key not in declared:
            raise HTTPException(400, f"Неизвестное поле {key}")
        if declared[key].get("secret"):
            if value.strip():
                secrets[key] = value.strip() if not declared[key].get("multiline") else value
        else:
            c.setdefault("fields", {})[key] = (mcp_hub.normalize_site(value) if key == "site"
                                               else value.strip())
    if "transport" in admin_fields:
        c["transport"] = "http" if admin_fields["transport"] == "http" else "stdio"
    for key in ("command", "url"):
        if key in admin_fields:
            c[key] = admin_fields[key].strip()
    if "args" in admin_fields:
        c["args"] = [a for a in admin_fields["args"] if a.strip()]
    if "env" in admin_fields:
        c["env"] = mcp_hub.parse_env(admin_fields["env"])
    c.pop("check", None)   # settings changed: the last test no longer says anything
    mcp_hub.save_secrets(pid, cid, secrets)
    projects.save(p)
    return mcp_hub.public_view(pid, c)


@app.delete("/api/projects/{pid}/connections/{cid}/secrets/{key}")
async def clear_connection_secret(pid: str, cid: str, key: str, request: Request):
    p = project(pid)
    c = _conn(p, cid)
    if mcp_hub.PRESETS[c["preset"]].get("admin_only"):
        require_admin(request)
    s = mcp_hub.load_secrets(pid, cid)
    s.pop(key, None)
    mcp_hub.save_secrets(pid, cid, s)
    return {"ok": True}


@app.delete("/api/projects/{pid}/connections/{cid}")
async def delete_connection(pid: str, cid: str, request: Request):
    p = project(pid)
    c = _conn(p, cid)
    if mcp_hub.PRESETS[c["preset"]].get("admin_only"):
        require_admin(request)
    p["connections"] = [x for x in p["connections"] if x["id"] != cid]
    mcp_hub.save_secrets(pid, cid, {})
    projects.save(p)
    return {"ok": True}


@app.post("/api/projects/{pid}/connections/{cid}/test")
async def test_connection(pid: str, cid: str):
    p = project(pid)
    c = _conn(p, cid)
    try:
        tools = await call(asyncio.wait_for(mcp_hub.test(pid, c), mcp_hub.STARTUP_TIMEOUT + 30))
    except (asyncio.TimeoutError, mcp_hub.McpError) as e:
        error = str(e) if isinstance(e, mcp_hub.McpError) else f"MCP-сервер «{c['name']}» не ответил вовремя"
        _remember_check(pid, cid, {"ok": False, "error": error})
        raise HTTPException(400, error)
    _remember_check(pid, cid, {"ok": True, "tools": len(tools)})
    return {"tools": tools}


def _remember_check(pid: str, cid: str, result: dict) -> None:
    """Re-read the project: it may have changed while the server was starting."""
    p = projects.get(pid)
    c = next((c for c in (p or {}).get("connections", []) if c["id"] == cid), None)
    if c:
        c["check"] = result | {"at": time.time()}
        projects.save(p)


# ---------- Project skills ----------

@app.get("/api/projects/{pid}/skills")
async def list_skills(pid: str):
    project(pid)
    return skills.list_skills(pid)


@app.get("/api/projects/{pid}/skills/{name}")
async def get_skill(pid: str, name: str, raw: bool = False, version: str = ""):
    """The version the project uses; `version` builtin / local - that one."""
    project(pid)
    s = skills.get(pid, name, version if version in ("builtin", "local") else "")
    if not s:
        raise HTTPException(404, "Скилл не найден")
    if raw:
        return PlainTextResponse(s["text"], headers={
            "Content-Disposition": f"attachment; filename=\"{name}.md\""})
    return s


class SkillBody(BaseModel):
    text: str


@app.put("/api/projects/{pid}/skills/{name}")
async def save_skill(pid: str, name: str, body: SkillBody):
    project(pid)
    try:
        return skills.save(pid, name, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{pid}/skills/{name}")
async def delete_skill(pid: str, name: str):
    project(pid)
    return {"ok": skills.delete(pid, name)}


@app.post("/api/projects/{pid}/skills/{name}/clone")
async def clone_skill(pid: str, name: str):
    """A local copy of a built-in skill: the project edits and uses it instead of the original."""
    project(pid)
    try:
        return skills.clone(pid, name)
    except ValueError as e:
        raise HTTPException(400, str(e))


class LocalBody(BaseModel):
    use: bool


@app.put("/api/projects/{pid}/skills/{name}/local")
async def use_local_skill(pid: str, name: str, body: LocalBody):
    """Which version of a built-in skill the project uses: its local copy or the original."""
    project(pid)
    s = skills.get(pid, name)
    if not s or not s["builtin"] or not s["has_local"]:
        raise HTTPException(400, "У скилла нет локальной копии")
    skills.use_local(pid, name, body.use)
    return skills.get(pid, name)


class EnabledBody(BaseModel):
    on: bool


@app.put("/api/projects/{pid}/skills/{name}/enabled")
async def enable_skill(pid: str, name: str, body: EnabledBody):
    """A skill switched off stays in the stage settings but no stage uses it."""
    project(pid)
    if not skills.get(pid, name):
        raise HTTPException(404, "Скилл не найден")
    skills.set_enabled(pid, name, body.on)
    return skills.get(pid, name)


class StagesBody(BaseModel):
    slots: list[str]     # skills.SLOTS keys: requirements.skills, scenarios.skills, ...


@app.put("/api/projects/{pid}/skills/{name}/stages")
async def skill_stages(pid: str, name: str, body: StagesBody):
    """The stages that use the skill (the skill lists of the pipeline settings)."""
    project(pid)
    if not skills.get(pid, name):
        raise HTTPException(404, "Скилл не найден")
    try:
        skills.attach(pid, name, body.slots)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _project_view(projects.get(pid))


# ---------- Studio sessions ----------

def sess(sid: str) -> StudioSession:
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    return s


async def _guard(s: StudioSession, coro) -> None:
    try:
        await coro
    except Exception as e:
        s.status = "error"
        s.autopilot = False
        text = str(e) if isinstance(e, (mcp_hub.McpError, ValueError)) else llm.api_error_text(e)
        s.chat.append({"role": "system", "text": text})
        s.checkpoint()


class NewSession(BaseModel):
    project_id: str
    name: str = "New test"
    url: str
    scenario: str
    autopilot: bool = False
    headless: bool = True
    engine: str = ""       # builtin | playwright-mcp | api (an API test, no browser); empty = project setting
    username: str = ""     # login for the app under test; empty = the project account
    password: str = ""
    account: str = ""      # the project account to log in with; empty = the default one
    task_id: str = ""      # project task the test is made for: linked on save
    analysis_id: str = ""  # the scenario of a requirements analysis the test is made from: linked on save
    scenario_id: str = ""
    fresh_login: bool = False   # do not start with the project's saved login (e.g. a new login test)


def _start_session(s: StudioSession, autopilot: bool) -> None:
    SESSIONS[s.id] = s
    if INSTANCE_URL:
        workqueue.set_owner("session", s.id, INSTANCE_URL)

    async def boot():
        s.starts_in_autopilot = autopilot
        await _guard(s, s.start())
        if autopilot and s.status != "error":
            s.set_autopilot(True)

    submit(boot())


@app.post("/api/sessions")
async def create_session(body: NewSession, request: Request):
    p = project(body.project_id, "editor")
    url = body.url if "://" in body.url else "https://" + body.url
    creds = {"username": body.username.strip(), "password": body.password}
    account = ""
    if not any(creds.values()):
        if body.account and not projects.account_exists(p["id"], body.account):
            raise HTTPException(400, "Учётная запись не найдена")
        account = body.account
        creds = projects.account_credentials(p["id"], account)
    engine = body.engine if body.engine in ("builtin", "playwright-mcp", "api") else ""
    if body.scenario.strip() or body.autopilot:
        _require_model(p)
    if body.analysis_id:
        _require_lifecycle(p)
    a = analyses.get(body.analysis_id) if body.analysis_id else None
    if a and a["project_id"] != p["id"]:
        a = None
    # A scenario of the API layer is written without a browser.
    sc = next((x for x in (a or {}).get("scenarios") or [] if x.get("id") == body.scenario_id), None)
    if sc and sc.get("layer") == "api":
        engine = "api"
    guest = False
    if sc and p["pipeline"]["requirements"].get("preflight"):
        # The scenario's role has an account and the scenario keeps to the restrictions and lifecycles.
        try:
            check = await knowledge.preflight(p, sc)
        except llm.BudgetExceeded as e:
            raise HTTPException(429, str(e))
        except Exception as e:
            raise HTTPException(502, f"Проверка сценария перед тестом не удалась: {llm.api_error_text(e)}")
        if not check["ok"]:
            raise HTTPException(409, knowledge.preflight_text(check))
        if not body.account and not any(creds.values()):     # no account chosen: the role's one
            account, guest = check["account"], check["guest"]
            creds = {} if guest else projects.account_credentials(p["id"], account)
    s = StudioSession(p, body.name, url, body.scenario, headless=body.headless, credentials=creds,
                      engine=engine, use_login_state=not body.fresh_login and not guest, account=account)
    request.state.project_id = p["id"]
    _start_session(s, body.autopilot)
    t = tasks.load(body.task_id)
    if t and t["project_id"] == p["id"]:
        s.task_id = t["id"]
    if a and body.scenario_id:
        SESSION_SCENARIOS[s.id] = (p["id"], a["id"], body.scenario_id)
    return {"id": s.id}


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    return sess(sid).state()


@app.get("/api/sessions/{sid}/screenshot")
async def screenshot(sid: str):
    s = sess(sid)
    if s.status in ("idle", "awaiting_approval", "done"):
        await call(s.refresh_screenshot())
    if not s.screenshot:
        return Response(status_code=204)
    return Response(base64.b64decode(s.screenshot), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/sessions/{sid}/continue")
async def approve(sid: str):
    s = sess(sid)
    submit(_guard(s, s.approve()))
    return {"ok": True}


class Text(BaseModel):
    text: str = ""


@app.post("/api/sessions/{sid}/reject")
async def reject(sid: str, body: Text):
    s = sess(sid)
    submit(_guard(s, s.reject(body.text)))
    return {"ok": True}


@app.post("/api/sessions/{sid}/chat")
async def chat(sid: str, body: Text):
    s = sess(sid)
    submit(_guard(s, s.send_chat(body.text)))
    return {"ok": True}


@app.post("/api/sessions/{sid}/resume")
async def resume(sid: str):
    s = sess(sid)
    submit(_guard(s, s.resume()))
    return {"ok": True}


class Toggle(BaseModel):
    on: bool


@app.post("/api/sessions/{sid}/stop")
async def stop_session(sid: str):
    """Stop generating: Auto-Pilot off, the request to the model cancelled; the browser stays open."""
    s = sess(sid)
    WORKER.call_soon_threadsafe(s.stop)
    return {"ok": True}


@app.post("/api/sessions/{sid}/autopilot")
async def autopilot(sid: str, body: Toggle):
    s = sess(sid)
    WORKER.call_soon_threadsafe(s.set_autopilot, body.on)
    return {"ok": True}


class Pick(BaseModel):
    x: float
    y: float
    action: str = "click"
    value: str = ""
    description: str = ""
    source: str = "picker"
    target_x: float | None = None     # drag_to: where to drop
    target_y: float | None = None


@app.post("/api/sessions/{sid}/pick")
async def pick(sid: str, body: Pick):
    s = sess(sid)
    target = (body.target_x, body.target_y) if body.target_x is not None and body.target_y is not None else None
    try:
        return await call(s.pick(body.x, body.y, body.action, body.value, body.description,
                                 body.source, target=target))
    except ValueError as e:
        raise HTTPException(400, str(e))


class Manual(BaseModel):
    action: str
    value: str = ""
    description: str = ""


@app.post("/api/sessions/{sid}/manual")
async def manual(sid: str, body: Manual):
    s = sess(sid)
    if body.action not in ALL_ACTIONS or body.action in DATA_ACTIONS:
        raise HTTPException(400, "Unknown action")
    step = new_step(body.action, body.description or f"{body.action} {body.value}".strip(),
                    body.value, source="manual")
    return await call(s.manual_step(step))


class StepsBody(BaseModel):
    steps: list[dict]


@app.put("/api/sessions/{sid}/steps")
async def set_steps(sid: str, body: StepsBody):
    s = sess(sid)
    s.steps = body.steps
    s.edits += 1           # a person corrected the agent (authoring_stats)
    s.checkpoint()
    return {"ok": True}


class SaveBody(BaseModel):
    status: str = ""     # "" = keep (a new test: draft) | draft | review | ready


def _job_session_saved(pid: str, origin: dict | None, sid: str, test: dict) -> None:
    """A person saved the test of a pipeline session: the item of the run that needed them is done."""
    jid = (origin or {}).get("job") or ""
    job = pipeline.JOBS.get(jid)
    if job is not None:
        WORKER.call_soon_threadsafe(job.session_saved, sid, test)
    else:
        pipeline.session_saved(pid, jid, sid, test)


@app.post("/api/sessions/{sid}/save")
async def save_session(sid: str, body: SaveBody | None = None):
    s = sess(sid)
    t, warnings = s.save((body or SaveBody()).status)
    if s.task_id:
        tasks.link_test(s.task_id, t["id"])
    if sid in SESSION_SCENARIOS:
        analyses.link_test(*SESSION_SCENARIOS[sid], t["id"])
    _job_session_saved(s.project_id, s.origin, sid, t)
    return t | {"warnings": warnings}


@app.delete("/api/sessions/{sid}")
async def close_session(sid: str):
    s = SESSIONS.pop(sid, None)
    SESSION_SCENARIOS.pop(sid, None)
    if INSTANCE_URL:
        workqueue.drop_owner("session", sid)
    if s:
        submit(s.close(discard=True))
    return {"ok": True}


# ---------- Interrupted Studio sessions (their checkpoints outlive a restart of the studio) ----------

def _alive(cp: dict) -> bool:
    """The session runs here or on another live instance (or its pipeline run does)."""
    if cp["id"] in SESSIONS:
        return True
    if not INSTANCE_URL:
        return False
    job = (cp.get("origin") or {}).get("job")
    return bool(workqueue.owner("session", cp["id"]) or (job and workqueue.owner("job", job)))


def _checkpoint_or_404(pid: str, sid: str) -> dict:
    cp = agent_mod.load_checkpoint(pid, sid)
    if not cp:
        raise HTTPException(404, "Сессия не найдена")
    return cp


@app.get("/api/projects/{pid}/sessions")
async def interrupted_sessions(pid: str):
    """Sessions of the project that were interrupted (the studio restarted): continue or save them."""
    project(pid)
    return [cp for cp in agent_mod.list_checkpoints(pid) if not _alive(cp)]


class RestoreBody(BaseModel):
    autopilot: bool = False


@app.post("/api/projects/{pid}/sessions/{sid}/restore")
async def restore_session(pid: str, sid: str, body: RestoreBody | None = None):
    p = project(pid)
    cp = _checkpoint_or_404(pid, sid)
    if _alive(cp):
        return {"id": sid}
    _require_model(p)
    _start_session(agent_mod.restored(p, cp), (body or RestoreBody()).autopilot)
    return {"id": sid}


@app.post("/api/projects/{pid}/sessions/{sid}/save")
async def save_interrupted(pid: str, sid: str, body: SaveBody | None = None):
    p = project(pid)
    cp = _checkpoint_or_404(pid, sid)
    if _alive(cp):
        raise HTTPException(409, "Сессия идёт: сохраните тест в Studio")
    if not agent_mod.recorded_steps(cp.get("steps") or []):
        raise HTTPException(400, "В сессии нет выполненных шагов")
    t, warnings = agent_mod.save_checkpoint(p, cp, (body or SaveBody()).status)
    if cp.get("task_id"):
        tasks.link_test(cp["task_id"], t["id"])
    _job_session_saved(pid, cp.get("origin"), sid, t)
    return t | {"warnings": warnings}


@app.delete("/api/projects/{pid}/sessions/{sid}")
async def discard_session(pid: str, sid: str):
    project(pid)
    cp = _checkpoint_or_404(pid, sid)
    if _alive(cp):
        raise HTTPException(409, "Сессия идёт: закройте её в Studio")
    agent_mod.drop_checkpoint(pid, sid)
    return {"ok": True}


# ---------- Saved tests ----------

def test_or_404(tid: str) -> dict:
    t = storage.load(tid)
    if not t:
        raise HTTPException(404, "Тест не найден")
    return t


@app.get("/api/tests")
async def tests(project_id: str, tag: str = ""):
    project(project_id)
    return storage.list_tests(project_id, tag)


@app.get("/api/tests/{tid}")
async def get_test(tid: str):
    return test_or_404(tid)


@app.put("/api/tests/{tid}")
async def update_test(tid: str, body: dict):
    old = test_or_404(tid)
    body.pop("id", None)
    body.pop("project_id", None)
    return storage.update(tid, lambda t: t.update(body)) or old


class StepsBody(BaseModel):
    steps: list[dict]


@app.put("/api/tests/{tid}/steps")
async def edit_test_steps(tid: str, body: StepsBody):
    """The steps edited by a person in the Tests tab (a new version of the test)."""
    test_or_404(tid)
    try:
        return storage.edit_steps(tid, body.steps)
    except ValueError as e:
        raise HTTPException(400, str(e))


class EditBody(BaseModel):
    headless: bool = True


@app.post("/api/tests/{tid}/edit")
async def edit_in_studio(tid: str, body: EditBody, request: Request):
    """A Studio session that replays the saved test and waits for the person: steps are added,
    changed or re-recorded there and saved into the same test."""
    t = test_or_404(tid)
    p = project(t["project_id"], "editor")
    s = StudioSession(p, t["name"], t["url"], t.get("scenario", ""), headless=body.headless,
                      credentials=storage.credentials(t), base_steps=t["steps"], task=agent_mod.EDIT_TASK,
                      account=t.get("account") or "", engine=agent_mod.test_engine(t))
    s.test_id = tid
    request.state.project_id = p["id"]
    _start_session(s, autopilot=False)
    return {"id": s.id}


class MetaBody(BaseModel):
    tags: list[str] | None = None
    quarantine: bool | None = None
    reason: str = ""
    role: str | None = None        # "" | login (the project's login test) | module (used by other tests as a step)
    status: str | None = None      # draft -> review -> ready (the regression suite runs ready tests)


@app.patch("/api/tests/{tid}/meta")
async def test_meta(tid: str, body: MetaBody, request: Request):
    t0 = test_or_404(tid)
    if body.role not in (None, "", "login", "module"):
        raise HTTPException(400, "Роль теста: login, module или пусто")
    if body.status not in (None, *storage.STATUSES):
        raise HTTPException(400, "Статус теста: draft, review или ready")
    if body.role == "login":
        # One login test per project: the previous one becomes a regular test.
        for other in storage.all_tests(t0["project_id"]):
            if other.get("role") == "login" and other["id"] != tid:
                storage.update(other["id"], lambda x: x.update(role=""))

    def change(t: dict) -> None:
        if body.tags is not None:
            t["tags"] = storage.normalize_tags(body.tags)
        if body.quarantine is not None:
            t["quarantine"] = {"on": True, "by": request.state.user, "at": time.time(),
                               "reason": body.reason.strip() or "Вручную"} if body.quarantine else {"on": False}
        if body.role is not None:
            t["role"] = body.role
        if body.status is not None and body.status != storage.status(t):
            t["status"] = body.status
            t["review"] = {"status": body.status, "by": request.state.user, "at": time.time()}
    return storage.update(tid, change)


# ---------- comments on steps and versions of a test ----------

class CommentBody(BaseModel):
    step_id: str = ""      # "" = about the whole test
    text: str


@app.post("/api/tests/{tid}/comments")
async def add_comment(tid: str, body: CommentBody, request: Request):
    test_or_404(tid)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустой комментарий")
    c = {"id": os.urandom(4).hex(), "step_id": body.step_id, "user": request.state.user, "text": text[:4000],
         "at": time.time()}
    return storage.update(tid, lambda t: t.update(comments=(t.get("comments") or []) + [c]))


@app.delete("/api/tests/{tid}/comments/{cid}")
async def delete_comment(tid: str, cid: str, request: Request):
    """Only the author removes a comment."""
    t = test_or_404(tid)
    c = next((c for c in t.get("comments") or [] if c["id"] == cid), None)
    if not c:
        raise HTTPException(404, "Комментарий не найден")
    if c["user"] != request.state.user and not access.can(request.state.user, project(t["project_id"]), "owner"):
        raise HTTPException(403, "Удалить комментарий может только его автор или владелец проекта")
    return storage.update(tid, lambda t: t.update(comments=[x for x in t.get("comments") or [] if x["id"] != cid]))


@app.get("/api/tests/{tid}/versions")
async def test_versions(tid: str):
    return storage.versions(test_or_404(tid))


@app.get("/api/tests/{tid}/versions/{n}")
async def test_version(tid: str, n: int):
    """A version and how the current test differs from it."""
    t = test_or_404(tid)
    v = storage.version(t, n)
    if not v:
        raise HTTPException(404, "Версия не найдена")
    return v | {"diff": storage.diff(v, t)}


@app.post("/api/tests/{tid}/versions/{n}/restore")
async def restore_version(tid: str, n: int):
    test_or_404(tid)
    try:
        return storage.restore(tid, n)
    except KeyError:
        raise HTTPException(404, "Версия не найдена")


class DataBody(BaseModel):
    before: list[dict] = []
    after: list[dict] = []


@app.put("/api/tests/{tid}/data")
async def test_data(tid: str, body: DataBody):
    """The test's data preparation: api_request steps a PERSON writes (the agent never does). Only
    to the application under test; DELETE only after the test and only for what "before" created."""
    t = test_or_404(tid)
    p = project(t["project_id"])
    base = p.get("base_url") or t.get("url", "")
    blocks: dict[str, list[dict]] = {}
    own: set[str] = set()
    for phase in ("before", "after"):
        out = []
        for raw in getattr(body, phase):
            step = new_step("api_request", str(raw.get("description") or "Запрос к API"), source="manual")
            step["id"] = str(raw.get("id") or step["id"])[:16]
            value = raw.get("value")
            step["value"] = value if isinstance(value, str) else json.dumps(value or {}, ensure_ascii=False)
            try:
                check_api(step, base, phase, own)
                s = json.loads(step["value"])
            except ValueError as e:
                raise HTTPException(400, f"«{step['description']}»: {e}")
            if phase == "before":
                own |= set((s.get("save") or {}).keys())
            out.append(step)
        blocks[phase] = out
    return storage.update(tid, lambda x: x.update(blocks))


@app.post("/api/tests/{tid}/traffic/{index}/before")
async def traffic_to_before(tid: str, index: int):
    """A recorded request of the scenario becomes a "before" request (to edit: values, what to save)."""
    t = test_or_404(tid)
    entries = traffic.load(t["project_id"], tid)
    if not 0 <= index < len(entries):
        raise HTTPException(404, "Запрос не найден")
    e = entries[index]
    if e["method"] == "DELETE" or e.get("third_party"):
        raise HTTPException(400, "Удаление и запросы к другим сайтам в подготовку данных не добавляются")
    path = urlparse(e["url"])
    try:
        body = json.loads(e["post_data"]) if e["post_data"] else None
    except ValueError:
        body = e["post_data"]
    step = new_step("api_request", f"{e['method']} {path.path}", json.dumps(
        {"method": e["method"], "url": path.path + (f"?{path.query}" if path.query else ""), "body": body,
         "expect_status": e["status"]}, ensure_ascii=False), source="manual")
    return storage.update(tid, lambda x: x.update(before=(x.get("before") or []) + [step]))


@app.delete("/api/tests/{tid}")
async def delete_test(tid: str):
    return {"ok": storage.delete(tid)}


@app.get("/api/tests/{tid}/credentials")
async def get_credentials(tid: str):
    t = test_or_404(tid)
    c = storage.own_credentials(t)
    return {"username": c.get("username", ""), "has_password": bool(c.get("password")),
            "has_totp": bool(c.get("totp_secret")), "account": t.get("account") or ""}


@app.put("/api/tests/{tid}/credentials")
async def set_credentials(tid: str, body: Credentials):
    """The test's project account (`account`) or its own login (username / password / totp_secret)."""
    t = test_or_404(tid)
    if body.account is not None:
        if body.account and not projects.account_exists(t["project_id"], body.account):
            raise HTTPException(400, "Учётная запись не найдена")
        storage.update(tid, lambda x: x.update(account=body.account))
        if body.account:
            # An own login would take precedence over the chosen account.
            storage.set_own_credentials(t, {})
            return {"ok": True}
    _check_totp(body.totp_secret)
    c = storage.own_credentials(t)
    c["username"] = body.username.strip()
    if body.password:
        c["password"] = body.password
    if body.totp_secret is not None:
        c["totp_secret"] = re.sub(r"\s+", "", body.totp_secret).upper()
    storage.set_own_credentials(t, c)
    return {"ok": True}


@app.get("/api/tests/{tid}/export")
async def export(tid: str, format: str = "playwright", testit: bool = False):
    t = test_or_404(tid)
    p = projects.get(t["project_id"]) or {"pipeline": projects.normalize_pipeline(None)}
    name = "".join(c if c.isalnum() else "_" for c in t["name"]).lower()
    if format == "gherkin":
        text, fname = exporters.to_gherkin(t | {"project": p.get("name", "")}, p.get("language", "")), f"{name}.feature"
    elif format == "api":
        text, fname = exporters.to_api_tests(t, traffic.load(t["project_id"], tid)), f"test_{name}_api.py"
    elif format == "har":
        har = traffic.load_har(t["project_id"], tid)
        if not har:
            raise HTTPException(404, "Трафик этого теста не записан")
        return JSONResponse(har, headers=_attachment(f"{name}.har", "har"))
    else:
        login = pipeline.login_test(t["project_id"]) if p["pipeline"]["run"].get("login_once") else None
        text = exporters.to_playwright(t, p["pipeline"]["run"]["a11y_impact"], lookup=storage.load, login=login,
                                       run_cfg=p["pipeline"]["run"], testit=testit)
        fname = f"test_{name}.py"
    return PlainTextResponse(text, headers=_attachment(fname, fname.rsplit(".", 1)[1], inline=True))


@app.get("/api/tests/{tid}/traffic")
async def test_traffic(tid: str):
    t = test_or_404(tid)
    return [{"index": i, "method": e["method"], "url": e["url"], "status": e["status"], "mime": e["mime"],
             "step": e.get("step", -1), "third_party": e.get("third_party", False), "size": len(e["body"] or "")}
            for i, e in enumerate(traffic.load(t["project_id"], tid))]


class MockBody(BaseModel):
    index: int


@app.post("/api/tests/{tid}/mock")
async def add_mock(tid: str, body: MockBody):
    """Answer a recorded request with its recorded response in every run (mock_route step)."""
    t = test_or_404(tid)
    entries = traffic.load(t["project_id"], tid)
    if not 0 <= body.index < len(entries):
        raise HTTPException(404, "Запрос не найден")
    e = entries[body.index]
    spec = traffic.mock_spec(e)
    step = new_step("mock_route", f"Подменить ответ {e['method']} {spec['url']} записанным ({e['status']})",
                    json.dumps(spec, ensure_ascii=False), source="manual")

    def change(t: dict) -> None:
        n = sum(1 for s in t["steps"] if s["action"] == "mock_route")   # mocks go first, in order
        t["steps"].insert(n, step)
    return storage.update(tid, change)


def _run_request(t: dict, headless: bool | None, request: Request, browser: str = "", device: str | None = None) -> dict:
    p = project(t["project_id"])
    run = runs.new(t, "manual", user=request.state.user, live=not workqueue.enabled())
    worker_mod.start_run(p, t, run, submit, headless=headless, trigger="manual", user=request.state.user,
                         browser=browser, device=device)
    return run


class RunBody(BaseModel):
    headless: bool | None = None   # None = project setting
    browser: str = ""              # chromium | firefox | webkit; empty = the project's first
    device: str | None = None      # a device profile or a screen "1366x768"; None = the project's first


def _check_device(device: str | None) -> None:
    try:
        screen_size(device or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tests/{tid}/run")
async def run(tid: str, body: RunBody, request: Request):
    if body.browser and body.browser not in ("chromium", "firefox", "webkit"):
        raise HTTPException(400, "Браузер: chromium, firefox или webkit")
    _check_device(body.device)
    return {"id": _run_request(test_or_404(tid), body.headless, request, body.browser, body.device)["id"]}


@app.get("/api/tests/{tid}/runs")
async def test_runs(tid: str):
    t = test_or_404(tid)
    return runs.list_for_test(t["project_id"], tid)


def run_or_404(rid: str) -> dict:
    r = runs.get(rid)
    if not r:
        raise HTTPException(404, "Прогон не найден")
    return r


@app.get("/api/runs/{rid}")
async def get_run(rid: str):
    # Screenshots stay on the server as files: the UI polls this every second.
    return runs.public(run_or_404(rid))


@app.post("/api/runs/{rid}/retry")
async def retry_run(rid: str, request: Request):
    """Run the test of a run from the history again, in the same browser and device."""
    r = run_or_404(rid)
    if r["status"] == "running":
        raise HTTPException(409, "Прогон ещё идёт")
    t = storage.load(r["test_id"])
    if not t:
        raise HTTPException(404, "Тест прогона удалён")
    request.state.project_id = t["project_id"]
    return {"id": _run_request(t, None, request, r.get("browser") or "", r.get("device"))["id"]}


class FixBody(BaseModel):
    step_id: str = ""              # the failed step to fix (by default the first failed one)
    headless: bool = True


@app.post("/api/runs/{rid}/fix")
async def fix_run(rid: str, body: FixBody, request: Request):
    """A Studio session that replays the test up to its failed step and asks the agent to fix the step
    and record the rest; the person confirms each step and saves into the same test."""
    r = run_or_404(rid)
    t = storage.load(r["test_id"])
    if not t:
        raise HTTPException(404, "Тест прогона удалён")
    p = project(t["project_id"], "editor")
    _require_model(p)
    ids = [s["id"] for s in t["steps"]]
    failed = [x for x in r.get("results") or [] if x.get("status") != "passed" and x.get("id") in ids]
    res = next((x for x in failed if x["id"] == body.step_id), None) if body.step_id else (failed or [None])[0]
    if not res:
        raise HTTPException(400, "В прогоне нет упавшего шага, который есть в текущей версии теста")
    i = ids.index(res["id"])
    s = StudioSession(p, t["name"], t["url"], t.get("scenario", ""), headless=body.headless,
                      credentials=storage.credentials(t), base_steps=t["steps"][:i],
                      task=agent_mod.fix_task(t["steps"], i, res, r.get("analysis")), account=t.get("account") or "",
                      engine=agent_mod.test_engine(t))
    s.test_id = t["id"]
    request.state.project_id = p["id"]
    _start_session(s, autopilot=False)
    return {"id": s.id}


@app.get("/api/runs/{rid}/files/{name}")
async def run_file(rid: str, name: str):
    f = runs.file(run_or_404(rid), name)
    if not f:
        raise HTTPException(404, "Файл не найден")
    if f.suffix == ".zip":
        return FileResponse(f, media_type="application/zip", headers=_attachment(f"{rid}-{name}", "zip"))
    return FileResponse(f, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/runs/{rid}/defect")
async def defect_draft(rid: str):
    """A defect draft from a failed run, and the trackers it can go to."""
    r = run_or_404(rid)
    t = test_or_404(r["test_id"])
    p = project(r["project_id"])
    return defects.draft(t, r) | {"trackers": [{"id": c["id"], "name": c["name"], "preset": c["preset"]}
                                               for c in defects.trackers_of(p)], "created": r.get("defect")}


class DefectBody(BaseModel):
    connection: str
    title: str
    text: str


@app.post("/api/runs/{rid}/defect")
async def create_defect(rid: str, body: DefectBody, request: Request):
    """The person pressed "Создать": the defect goes to the tracker (never on a model's decision)."""
    r = run_or_404(rid)
    p = project(r["project_id"])
    conn = next((c for c in defects.trackers_of(p) if c["id"] == body.connection), None)
    if not conn:
        raise HTTPException(404, "Трекер не найден")
    if not body.title.strip():
        raise HTTPException(400, "Укажите заголовок дефекта")
    try:
        res = await call(defects.create(p, conn, body.title.strip()[:250], body.text))
    except (trackers.TrackerError, mcp_hub.McpError) as e:
        raise HTTPException(400, str(e))
    stored = runs.get(rid)
    stored["defect"] = res | {"tracker": conn["name"], "by": request.state.user, "at": time.time()}
    runs.save(stored)
    return stored["defect"]


@app.post("/api/runs/{rid}/baseline/{step_id}")
async def accept_baseline(rid: str, step_id: str):
    """The run's screenshot of a visual check becomes its new baseline."""
    r = run_or_404(rid)
    results = [x for a in r.get("attempts") or [] for x in a["results"]] + (r.get("results") or [])
    v = next((((x.get("details") or {}).get("visual") or {}) for x in results
              if x["id"] == step_id and ((x.get("details") or {}).get("visual") or {}).get("actual")), None)
    f = runs.file(r, v["actual"]) if v else None
    if not f:
        raise HTTPException(404, "У этого шага нет снимка для эталона")
    fs.write_bytes(checks.baseline_file(r["project_id"], r["test_id"], step_id), f.read_bytes())
    return {"ok": True}


@app.get("/api/tests/{tid}/baselines/{step_id}")
async def baseline_image(tid: str, step_id: str):
    t = test_or_404(tid)
    f = checks.baseline_file(t["project_id"], tid, step_id)
    if not fs.is_file(f):
        raise HTTPException(404, "Эталона нет: он создаётся при первом прогоне")
    return Response(fs.read_bytes(f), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/tests/{tid}/proposals/{prop_id}/{decision}")
async def heal_decision(tid: str, prop_id: str, decision: str, request: Request):
    """Review of self-healing: accept the new locator, or reject it (never proposed again)."""
    if decision not in ("accept", "reject"):
        raise HTTPException(400, "accept или reject")
    t = test_or_404(tid)
    if not any(p["id"] == prop_id for p in t.get("heal_proposals") or []):
        raise HTTPException(404, "Предложение не найдено")

    def change(t: dict) -> None:
        p = next(p for p in t["heal_proposals"] if p["id"] == prop_id)
        t["heal_proposals"] = [x for x in t["heal_proposals"] if x["id"] != prop_id]
        step = next((s for s in t["steps"] if s["id"] == p["step_id"]), None)
        if not step:
            return
        if decision == "accept":
            step["locator"], step["healed"] = p["new"], True
        else:
            step["heal_rejected"] = ((step.get("heal_rejected") or []) + [p["new"]])[-5:]
        t.setdefault("heal_log", []).append({"at": time.time(), "by": request.state.user, "decision": decision,
                                             "step_id": p["step_id"], "old": p["old"], "new": p["new"]})
        t["heal_log"] = t["heal_log"][-50:]
    return storage.update(tid, change)


@app.post("/api/tests/{tid}/verify")
async def verify_test(tid: str):
    """Mutation testing of the test's assertions (runs in the background)."""
    t = test_or_404(tid)
    if (t.get("verify") or {}).get("status") == "running" and time.time() - t["verify"].get("at", 0) < 1800:
        raise HTTPException(409, "Проверка уже идёт")
    p = project(t["project_id"])
    storage.update(tid, lambda x: x.update(verify={"status": "running", "at": time.time()}))
    worker_mod.start_verify(p, t, submit)
    return {"ok": True}


@app.post("/api/tests/{tid}/strengthen")
async def strengthen(tid: str, request: Request):
    """Open a Studio session that replays the test and asks the agent to add the missing checks."""
    t = test_or_404(tid)
    v = t.get("verify") or {}
    if not v.get("weak"):
        raise HTTPException(400, "Сначала проверьте тест мутациями: усиливать нечего")
    p = project(t["project_id"])
    _require_model(p)
    s = StudioSession(p, t["name"], t["url"], t.get("scenario", ""), headless=True,
                      credentials=storage.credentials(t), base_steps=t["steps"], task=mutations.improvement_task(v),
                      engine=agent_mod.test_engine(t))
    s.test_id = tid
    _start_session(s, autopilot=False)
    return {"id": s.id}


@app.post("/api/tests/{tid}/publish")
async def publish(tid: str):
    t = test_or_404(tid)
    p = project(t["project_id"])
    try:
        res = await call(publisher.publish_test(p, t))
    except (publisher.PublishError, mcp_hub.McpError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, llm.api_error_text(e))
    storage.update(tid, lambda x: x.update(external=t.get("external")))
    return res


# ---------- Suite runs (regression) ----------

class SuiteBody(BaseModel):
    tags: list[str] = []
    test_ids: list[str] = []
    headless: bool | None = None
    parallel: int | None = None
    include_drafts: bool = False     # also drafts and tests under review
    device: str = ""                 # this run's device or screen ("1366x768"); empty = the project's


@app.post("/api/projects/{pid}/runs")
async def run_suite(pid: str, body: SuiteBody, request: Request):
    p = project(pid)
    tags = storage.normalize_tags(body.tags)
    tests = storage.select(pid, tags=tags, test_ids=body.test_ids, include_drafts=body.include_drafts)
    if not tests:
        raise HTTPException(400, "Нет тестов для прогона" + (f" с тегами {', '.join(tags)}" if tags else ""))
    _check_device(body.device)
    s = suite.new(p, tests, tags=tags, trigger="manual", user=request.state.user,
                  devices=[body.device] if body.device else None)
    worker_mod.start_suite(p, s, tests, submit, headless=body.headless, parallel=body.parallel)
    return {"id": s["id"]}


@app.get("/api/projects/{pid}/suites")
async def list_suites(pid: str):
    project(pid)
    return suite.list_suites(pid)


def suite_or_404(sid: str) -> dict:
    s = suite.get(sid)
    if not s:
        raise HTTPException(404, "Прогон набора не найден")
    return s


@app.get("/api/suites/{sid}")
async def get_suite(sid: str):
    return suite_or_404(sid)


@app.post("/api/suites/{sid}/retry")
async def retry_suite(sid: str, request: Request):
    """A new suite run of the tests that failed (or errored, or were flaky) in this one."""
    s = suite_or_404(sid)
    if s["status"] == "running":
        raise HTTPException(409, "Набор ещё выполняется")
    failed = list(dict.fromkeys(i["test_id"] for i in s["items"] if i["status"] in ("failed", "error", "flaky")))
    if not failed:
        raise HTTPException(400, "В этом наборе нет упавших тестов")
    p = project(s["project_id"], "viewer")
    tests = storage.select(p["id"], test_ids=failed, include_drafts=True)
    if not tests:
        raise HTTPException(400, "Упавшие тесты удалены")
    new = suite.new(p, tests, tags=s.get("tags") or [], trigger="manual", user=request.state.user)
    worker_mod.start_suite(p, new, tests, submit)
    return {"id": new["id"]}


@app.get("/api/suites/{sid}/junit")
async def suite_junit(sid: str):
    s = suite_or_404(sid)
    return Response(reports.junit(s), media_type="application/xml", headers=_attachment(f"junit-{sid}.xml", "xml"))


# ---------- Planner: site exploration and coverage ----------

class ExploreBody(BaseModel):
    url: str = ""


@app.post("/api/projects/{pid}/explore")
async def explore(pid: str, body: ExploreBody):
    p = project(pid)
    state = {"id": os.urandom(5).hex(), "project_id": pid, "status": "running", "pages": [], "log": []}
    worker_mod.start_explore(p, state, body.url, submit)
    return {"id": state["id"]}


@app.get("/api/projects/{pid}/explore/{eid}")
async def get_explore(pid: str, eid: str):
    project(pid)
    r = explorer.get(pid, eid)
    if not r:
        raise HTTPException(404, "Исследование не найдено")
    out = r | {"pages": [{k: pg.get(k) for k in ("url", "title", "depth", "error")} for pg in r["pages"]]}
    if r["status"] == "done":
        out["requirements"] = explorer.to_requirements(r)
    return out


@app.get("/api/projects/{pid}/coverage")
async def coverage(pid: str):
    project(pid)
    return explorer.coverage(pid) or {"pages": [], "total": 0, "covered": 0, "at": None}


# ---------- Project tasks ----------

class TaskBody(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    due: str | None = None
    test_ids: list[str] | None = None


def task_or_404(tid: str) -> dict:
    t = tasks.load(tid)
    if not t:
        raise HTTPException(404, "Задача не найдена")
    return t


@app.get("/api/projects/{pid}/tasks")
async def list_tasks(pid: str, status: str = "", assignee: str = ""):
    """status: todo | in_progress | review | done | open (all but done); empty = all."""
    project(pid)
    return {"tasks": tasks.list_tasks(pid, status, assignee), "counts": tasks.counts(pid)}


@app.post("/api/projects/{pid}/tasks")
async def create_task(pid: str, body: TaskBody, request: Request):
    project(pid)
    try:
        return tasks.create(pid, body.model_dump(exclude_none=True), user=request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/tasks/{tid}")
async def get_task(tid: str):
    return task_or_404(tid)


@app.patch("/api/tasks/{tid}")
async def update_task(tid: str, body: TaskBody):
    task_or_404(tid)
    try:
        return tasks.update(tid, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/tasks/{tid}")
async def delete_task(tid: str):
    return {"ok": tasks.delete(tid)}


# ---------- Requirements -> scenarios ----------

class ReqBody(BaseModel):
    project_id: str
    requirements: str
    url: str = ""
    stream: bool = False    # NDJSON events with the intermediate results (scenarios.generate `progress`)
    # This generation's choice (None = the project's "scenarios" settings)
    types: list[str] | None = None
    layers: list[str] | None = None
    techniques: list[str] | None = None
    validate_spec: bool | None = None   # validate the specification first (None = "requirements.validate")


async def _learn(p: dict, requirements: str, user: str, log=None) -> None:
    """The application model learns from analysed requirements (the "requirements.learn_model" setting)."""
    if not p["pipeline"]["requirements"].get("learn_model"):
        return
    if requirements.lstrip().startswith(explorer.MAP_TITLE):
        return      # a map of the site only: the model learned from it when the site was explored
    try:
        doc = await knowledge.extract(p, requirements, user)
        if log:
            log(f"Модель приложения обновлена: сущностей {len(doc['entities'])}, ролей {len(doc['roles'])}")
    except Exception as e:      # the model is a help: scenarios do not wait for it to succeed
        if log:
            log(f"Модель приложения не обновлена: {llm.api_error_text(e)}")


@app.post("/api/scenarios")
async def gen_scenarios(body: ReqBody, request: Request):
    """Requirements -> scenarios; every generation is kept in the project's analysis history."""
    p = project(body.project_id, "editor")
    _require_model(p)
    a = analyses.create(p["id"], body.requirements, body.url, request.state.user)
    cfg = scenarios.settings(p, body.types, body.layers, body.techniques)
    check = p["pipeline"]["requirements"]["validate"] if body.validate_spec is None else body.validate_spec
    user = request.state.user
    if not body.stream:
        try:
            learn = submit(_learn(p, body.requirements, user))
            res = await call(scenarios.generate(body.requirements, body.url, project=p, cfg=cfg))
            await asyncio.wrap_future(learn)
        except Exception as e:
            analyses.finish(p["id"], a["id"], error=llm.api_error_text(e))
            raise HTTPException(502, llm.api_error_text(e))
        analyses.set_plan(p["id"], a["id"], res.feature, res.assumptions, [s.model_dump() for s in res.scenarios])
        analyses.finish(p["id"], a["id"], res.model_dump())
        return res.model_dump() | {"analysis_id": a["id"]}

    # One response streams the whole generation: it stays on this instance, so no job to poll.
    loop, queue = asyncio.get_running_loop(), asyncio.Queue()

    def send(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event | {"at": time.time()})

    def emit(event: dict) -> None:
        """Intermediate results go to the analysis first: the page gets the scenarios with their ids."""
        if event["type"] == "plan":
            event = event | {"scenarios": analyses.set_plan(p["id"], a["id"], event["feature"], event["assumptions"],
                                                            event["scenarios"])}
        elif event["type"] == "batch":
            event = event | {"scenarios": analyses.set_batch(p["id"], a["id"], event["start"], event["scenarios"])}
        send(event)

    def log(text: str) -> None:
        send({"type": "log", "text": text})

    async def work():
        learn = asyncio.create_task(_learn(p, body.requirements, user, log))
        try:
            if check:
                log("Проверка ТЗ на соответствие стандарту документации…")
                try:
                    report = await validation.validate(p, body.requirements)
                    analyses.set_validation(p["id"], a["id"], report)
                    send({"type": "validation", "validation": report})
                    log(f"Проверка ТЗ: {report['score']}/100, замечаний {len(report['findings'])}")
                except Exception as e:
                    log(f"Проверка ТЗ не удалась: {llm.api_error_text(e)}")
            res = await scenarios.generate(body.requirements, body.url, project=p, progress=emit, cfg=cfg)
            await learn
            done = analyses.finish(p["id"], a["id"], res.model_dump())
            send({"type": "done", "analysis": analyses.view(done)})
        except asyncio.CancelledError:
            learn.cancel()
            analyses.finish(p["id"], a["id"], error="Остановлено", status="cancelled")
            raise
        except Exception as e:
            analyses.finish(p["id"], a["id"], error=llm.api_error_text(e))
            send({"type": "error", "text": llm.api_error_text(e)})

    send({"type": "analysis", "analysis": analyses.view(a)})
    future = submit(work())

    async def events():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    yield "\n"      # keeps proxies from closing a quiet connection
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in ("done", "error"):
                    return
        finally:
            future.cancel()      # the page went away: stop spending tokens

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class ValidateBody(BaseModel):
    project_id: str
    requirements: str = ""
    analysis_id: str = ""       # validate (and keep the report with) the requirements of this analysis
    standard: str = ""          # empty = the project's ("requirements.standard")


@app.post("/api/requirements/validate")
async def validate_requirements(body: ValidateBody):
    """The specification against the documentation standard: sections, quality of requirements, questions."""
    p = project(body.project_id, "editor")
    _require_model(p)
    a = analyses.get(body.analysis_id) if body.analysis_id else None
    if body.analysis_id and (not a or a["project_id"] != p["id"]):
        raise HTTPException(404, "Анализ не найден")
    text = body.requirements.strip() or (a or {}).get("requirements", "")
    if not text.strip():
        raise HTTPException(400, "Нет текста требований")
    try:
        report = await call(validation.validate(p, text, body.standard))
    except Exception as e:
        raise HTTPException(502, llm.api_error_text(e))
    if a:
        analyses.set_validation(p["id"], a["id"], report)
    return report


# ---------- The application model: entities, dependencies, lifecycles, test data, memory ----------

@app.get("/api/projects/{pid}/knowledge")
async def get_knowledge(pid: str):
    project(pid)
    return knowledge.view(pid)


@app.put("/api/projects/{pid}/knowledge")
async def save_knowledge(pid: str, body: dict, request: Request):
    project(pid, "editor")
    return knowledge.save(pid, body, request.state.user)


class ExtractBody(BaseModel):
    requirements: str = ""
    analysis_id: str = ""


@app.post("/api/projects/{pid}/knowledge/extract")
async def extract_knowledge(pid: str, body: ExtractBody, request: Request):
    """Entities, dependencies, lifecycles and roles from requirements join the model."""
    p = project(pid, "editor")
    _require_model(p)
    a = analyses.get(body.analysis_id) if body.analysis_id else None
    if body.analysis_id and (not a or a["project_id"] != pid):
        raise HTTPException(404, "Анализ не найден")
    text = body.requirements.strip() or (a or {}).get("requirements", "")
    if not text.strip():
        raise HTTPException(400, "Нет текста требований")
    try:
        await call(knowledge.extract(p, text, request.state.user))
    except Exception as e:
        raise HTTPException(502, llm.api_error_text(e))
    return knowledge.view(pid)


class PendingBody(BaseModel):
    action: str                     # accept | reject | separate
    ids: list[str] = []
    all: bool = False


@app.post("/api/projects/{pid}/knowledge/pending")
async def resolve_knowledge(pid: str, body: PendingBody, request: Request):
    """A person decides on updates of existing records (found again by analyses, Planner, tests or
    added by people): each one or all of them."""
    project(pid, "editor")
    try:
        return knowledge.resolve(pid, None if body.all else body.ids, body.action, request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/projects/{pid}/knowledge/duplicates")
async def knowledge_duplicates(pid: str):
    """Groups of entities, roles and stand data that name the same thing."""
    project(pid)
    return knowledge.duplicates(knowledge.get(pid))


class DuplicateGroup(BaseModel):
    kind: str
    ids: list[str]
    keep: str = ""


class DuplicatesBody(BaseModel):
    groups: list[DuplicateGroup] = []
    all: bool = False
    dismiss: list[str] = []         # ids of groups that are not duplicates


@app.post("/api/projects/{pid}/knowledge/duplicates")
async def merge_knowledge_duplicates(pid: str, body: DuplicatesBody, request: Request):
    """Merges the chosen groups of duplicates (or all found) into the records to keep; `dismiss` -
    groups a person marked as different things."""
    project(pid, "editor")
    if body.dismiss:
        return knowledge.dismiss_duplicates(pid, body.dismiss, request.state.user)
    groups = None if body.all else [g.model_dump() for g in body.groups]
    return knowledge.merge_duplicates(pid, groups, request.state.user)


@app.post("/api/projects/{pid}/knowledge/confirm")
async def confirm_knowledge(pid: str, request: Request):
    """A person confirms the lifecycle of the system: runs of the pipeline waiting for it go on."""
    project(pid, "editor")
    try:
        doc = knowledge.confirm(pid, request.state.user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for job in list(pipeline.JOBS.values()):
        if job.project_id == pid and job.status == "awaiting_model":
            WORKER.call_soon_threadsafe(job.model_confirmed)
    return doc


@app.get("/api/projects/{pid}/analyses")
async def list_analyses(pid: str):
    """The history of requirement analyses, newest first."""
    project(pid)
    return analyses.list_analyses(pid)


@app.get("/api/projects/{pid}/scenarios")
async def list_project_scenarios(pid: str):
    """Every scenario of the analyses history: the pipeline is started on a choice of them."""
    project(pid)
    return analyses.all_scenarios(pid)


def _analysis(aid: str) -> dict:
    a = analyses.get(aid)
    if not a:
        raise HTTPException(404, "Анализ не найден")
    return a


@app.get("/api/analyses/{aid}")
async def get_analysis(aid: str):
    return analyses.view(_analysis(aid))


@app.delete("/api/analyses/{aid}")
async def delete_analysis(aid: str):
    """The analysis only: the tests made from its scenarios stay."""
    a = _analysis(aid)
    return {"ok": analyses.delete(a["project_id"], aid)}


class ScenarioPatch(BaseModel):
    title: str | None = None
    type: str | None = None
    layer: str | None = None
    priority: str | None = None
    role: str | None = None
    decision: str | None = None     # a scenario similar to an earlier one: new | reuse | refine (reuse.py)
    preconditions: str | None = None
    instructions: str | None = None
    expected_result: str | None = None
    gherkin: str | None = None


def _scenario_call(fn):
    try:
        return fn()
    except KeyError:
        raise HTTPException(404, "Сценарий не найден")
    except analyses.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/analyses/{aid}/scenarios")
async def add_scenario(aid: str, body: ScenarioPatch):
    a = _analysis(aid)
    return _scenario_call(lambda: analyses.add_scenario(a["project_id"], aid, body.model_dump()))


@app.put("/api/analyses/{aid}/scenarios/{scid}")
async def update_scenario(aid: str, scid: str, body: ScenarioPatch):
    a = _analysis(aid)
    return _scenario_call(lambda: analyses.update_scenario(a["project_id"], aid, scid, body.model_dump()))


@app.delete("/api/analyses/{aid}/scenarios/{scid}")
async def delete_scenario(aid: str, scid: str):
    a = _analysis(aid)
    if not _scenario_call(lambda: analyses.delete_scenario(a["project_id"], aid, scid)):
        raise HTTPException(404, "Сценарий не найден")
    return {"ok": True}


class FetchBody(BaseModel):
    project_id: str
    link: str


@app.post("/api/requirements/fetch")
async def fetch_requirements(body: FetchBody):
    p = project(body.project_id, "editor")
    try:
        return await call(sources.fetch(body.link, p))
    except sources.SourceError as e:
        raise HTTPException(400, str(e))


@app.post("/api/requirements/file")
async def requirements_file(file: UploadFile):
    """A specification file (.docx, .pdf, .md, .txt) -> text."""
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 30 МБ")
    try:
        return sources.from_file(file.filename or "file", data)
    except sources.SourceError as e:
        raise HTTPException(400, str(e))


# ---------- Pipeline jobs ----------

ScenarioIn = scenarios.DesignedScenario


class ScenarioPick(BaseModel):
    analysis_id: str
    scenario_id: str


class JobBody(BaseModel):
    links: list[str] = []
    text: str = ""
    url: str = ""
    explore: bool = False
    case_connection: str = ""   # automate manual test cases of this connection (Test IT, Allure, Zephyr)
    case_ids: str = ""          # their ids; empty = every test case (Test IT)
    scenarios: list[ScenarioIn] = []   # ready scenarios: straight to authoring
    feature: str = ""
    analysis_id: str = ""               # or scenarios of a requirements analysis (all, or `scenario_ids`)
    scenario_ids: list[str] = []
    picks: list[ScenarioPick] = []      # or scenarios of several analyses (the pipeline's common list)
    types: list[str] | None = None      # this run's kinds of checks, layers, techniques (None = the project's)
    layers: list[str] | None = None
    techniques: list[str] | None = None


@app.post("/api/projects/{pid}/jobs")
async def start_job(pid: str, body: JobBody, request: Request):
    p = project(pid)
    cases = None
    if body.case_connection:
        if not any(c["id"] == body.case_connection for c in p["connections"]):
            raise HTTPException(400, "Подключение для импорта кейсов не найдено")
        cases = {"connection": body.case_connection, "ids": publisher.parse_ids(body.case_ids)}
    ready = [s.model_dump() for s in body.scenarios if s.title.strip() and s.instructions.strip()]
    feature = body.feature
    wanted: dict[str, list[str]] = {}       # analysis -> its chosen scenarios (empty: all of them)
    if body.analysis_id:
        wanted[body.analysis_id] = list(body.scenario_ids)
    for pick in body.picks:
        wanted.setdefault(pick.analysis_id, []).append(pick.scenario_id)
    features = []
    for aid, ids in wanted.items():
        a = analyses.get(aid)
        if not a or a["project_id"] != p["id"]:
            raise HTTPException(404, "Анализ не найден")
        chosen = [s for s in a["scenarios"] if not s.get("pending") and (not ids or s["id"] in ids)]
        empty = [s["title"] or "без названия" for s in chosen if not s["title"].strip() or not s["instructions"].strip()]
        if empty:
            raise HTTPException(400, "Заполните название и шаги сценариев: " + ", ".join(f"«{t}»" for t in empty))
        # The tests made by the run are linked back to their scenarios.
        ready += [{k: s.get(k) or "" for k in analyses.FIELDS} | {"test_data": s.get("test_data") or [],
                                                                    "match": s.get("match"),
                                                                    "analysis_id": a["id"], "scenario_id": s["id"]}
                  for s in chosen]
        if a["feature"] and a["feature"] not in features:
            features.append(a["feature"])
    if wanted:
        feature = feature or "; ".join(features)[:300]
        if not ready:
            raise HTTPException(400, "Нет готовых сценариев" if body.picks else "В анализе нет готовых сценариев")
    if body.scenarios and not ready:
        raise HTTPException(400, "У сценариев нет названия или шагов")
    if not [l for l in body.links if l.strip()] and not body.text.strip() and not body.explore and not cases \
            and not ready:
        raise HTTPException(400, "Укажите ссылки на требования, текст, ручные кейсы или включите исследование сайта")
    _require_model(p)
    job = pipeline.Job(p, body.links, body.text, body.url, SESSIONS, user=request.state.user, explore=body.explore,
                       cases=cases, scenarios=ready, feature=feature,
                       design={k: v for k, v in (("types", body.types), ("layers", body.layers),
                                                 ("techniques", body.techniques)) if v is not None})
    pipeline.JOBS[job.id] = job
    job.save()
    if INSTANCE_URL:
        workqueue.set_owner("job", job.id, INSTANCE_URL)
    submit(job.run())
    return {"id": job.id}


@app.get("/api/projects/{pid}/jobs")
async def list_jobs(pid: str):
    project(pid)
    return pipeline.list_jobs(pid)


@app.post("/api/jobs/{jid}/resume")
async def resume_job(jid: str, request: Request):
    """Go on with a run interrupted by a restart of the studio (or stopped, or failed)."""
    j = pipeline.get_job(jid)
    if not j:
        raise HTTPException(404, "Запуск не найден")
    # A finished run stays in JOBS until a restart: only one still working is in the way.
    running = pipeline.JOBS.get(jid)
    if (running and not running.finished) or j["status"] not in ("error", "cancelled"):
        raise HTTPException(409, "Запуск ещё идёт")
    p = project(j["project_id"])
    _require_model(p)
    job = pipeline.Job.resumed(p, j, SESSIONS, user=request.state.user or "")
    pipeline.JOBS[job.id] = job
    job.save()
    if INSTANCE_URL:
        workqueue.set_owner("job", job.id, INSTANCE_URL)
    submit(job.run())
    return {"id": job.id}


class RetryBody(BaseModel):
    indices: list[int] | None = None    # items to generate again; None = every one that failed (pipeline.retryable)


@app.post("/api/jobs/{jid}/retry")
async def retry_job(jid: str, body: RetryBody, request: Request):
    """Generate again, in one go, the scenarios whose authoring failed (an error of the agent, the agent
    stopped or gave up): each starts a fresh session; the rest of the run goes on as with "Продолжить"."""
    j = pipeline.get_job(jid)
    if not j:
        raise HTTPException(404, "Запуск не найден")
    running = pipeline.JOBS.get(jid)
    if (running and not running.finished) or j["status"] in pipeline.LIVE:
        raise HTTPException(409, "Запуск ещё идёт")
    items = j.get("items") or []
    indices = [i for i in (range(len(items)) if body.indices is None else body.indices)
               if 0 <= i < len(items) and pipeline.retryable(items[i], explicit=body.indices is not None)]
    if not indices:
        raise HTTPException(400, "Нет сценариев с ошибкой генерации")
    p = project(j["project_id"])
    _require_model(p)
    for i in indices:       # the failed sessions go: the scenario starts over
        sid = items[i].get("session_id") or ""
        if sid in SESSIONS:
            await call(SESSIONS.pop(sid).close(discard=True))
        elif sid:
            agent_mod.drop_checkpoint(p["id"], sid)
    job = pipeline.Job.retried(p, j, SESSIONS, indices, user=request.state.user or "")
    pipeline.JOBS[job.id] = job
    job.save()
    if INSTANCE_URL:
        workqueue.set_owner("job", job.id, INSTANCE_URL)
    submit(job.run())
    return {"id": job.id, "retried": len(indices)}


class ReuseBody(BaseModel):
    decisions: dict[int, str]       # item index -> new | reuse | refine (reuse.DECISIONS)


@app.post("/api/jobs/{jid}/reuse")
async def decide_reuse(jid: str, body: ReuseBody):
    """A person decides what to do with scenarios similar to earlier ones: a new test, reuse or refine."""
    job = pipeline.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Запуск не найден")
    if any(d not in reuse.DECISIONS for d in body.decisions.values()):
        raise HTTPException(400, "Решение: new, reuse или refine")
    WORKER.call_soon_threadsafe(job.decide, dict(body.decisions))
    return {"ok": True}


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    j = pipeline.get_job(jid)
    if not j:
        raise HTTPException(404, "Запуск не найден")
    return pipeline.reconcile(jid, j, SESSIONS)


class SelectBody(BaseModel):
    indices: list[int]


@app.post("/api/jobs/{jid}/select")
async def select_scenarios(jid: str, body: SelectBody):
    job = pipeline.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Запуск не найден")
    WORKER.call_soon_threadsafe(job.select, body.indices)
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: str):
    job = pipeline.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Запуск не найден")
    WORKER.call_soon_threadsafe(job.cancel)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


# Last: the MCP app's own route is /mcp; everything else was matched above.
app.mount("/", MCP_APP)


if __name__ == "__main__":
    auth.ensure_admin()
    print(f"AI Test Generator: http://{HOST}:{PORT}  (the model is set per project: Project -> Model)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
