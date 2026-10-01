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
import shutil
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from testgen import (auth, checks, explorer, exporters, llm, mcp_hub, mcp_server, mutations, pipeline, projects,
                     publisher, reports, runs, scenarios, skills, sources, storage, suite, traffic)
from testgen.agent import StudioSession
from testgen.steps import ALL_ACTIONS, new_step

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))

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

projects.ensure_default()

# The studio as an MCP server for IDE agents, at /mcp (token auth in require_login).
MCP = mcp_server.build(mcp_server.Backend(submit=submit, sessions=SESSIONS, studio_url=f"http://127.0.0.1:{PORT}"))
MCP_APP = MCP.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with MCP.session_manager.run():
        yield


app = FastAPI(title="AI Test Generator", lifespan=lifespan)

PUBLIC = {"/", "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/logout"}


@app.middleware("http")
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
    if not user and request.url.path not in PUBLIC:
        return JSONResponse({"detail": "Требуется вход"}, status_code=401)
    return await call_next(request)


def require_admin(request: Request) -> None:
    if not auth.is_admin(request.state.user):
        raise HTTPException(403, "Это может сделать только администратор студии")


def _attachment(name: str, ext: str, inline: bool = False) -> dict:
    # Names are often non-ASCII (e.g. Cyrillic): HTTP headers need RFC 5987 encoding.
    kind = "inline" if inline else "attachment"
    return {"Content-Disposition": f"{kind}; filename=\"file.{ext}\"; filename*=UTF-8''{quote(name)}"}


# ---------- Studio login ----------

class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: Login, request: Request):
    if not auth.ENABLED:
        return {"user": auth.ANONYMOUS}
    if not auth.verify(body.username.strip(), body.password):
        await asyncio.sleep(1)   # slow down password guessing
        raise HTTPException(401, "Неверный логин или пароль")
    return _session_response(body.username.strip(), request)


@app.post("/api/auth/register")
async def register(body: Login, request: Request):
    if not auth.SIGNUP:
        raise HTTPException(403, "Регистрация отключена")
    try:
        auth.register(body.username.strip(), body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _session_response(body.username.strip(), request)


def _session_response(username: str, request: Request) -> JSONResponse:
    resp = JSONResponse({"user": username})
    resp.set_cookie(auth.COOKIE, auth.make_token(username), max_age=auth.SESSION_TTL,
                    httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    return {"user": request.state.user, "auth_enabled": auth.ENABLED, "signup": auth.SIGNUP,
            "is_admin": bool(request.state.user) and auth.is_admin(request.state.user),
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

def project(pid: str) -> dict:
    p = projects.get(pid)
    if not p:
        raise HTTPException(404, "Проект не найден")
    return p


def _project_view(p: dict) -> dict:
    c = projects.app_credentials(p["id"])
    return p | {"connections": [mcp_hub.public_view(p["id"], x) for x in p["connections"]],
                "app_username": c.get("username", ""), "app_has_password": bool(c.get("password")),
                "llm": p["llm"] | {"key_set": bool(projects.llm_key(p["id"])),
                                   "env_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}}


def _require_model(p: dict) -> None:
    """Fail before starting a browser when the project has no model to drive it."""
    if not p["llm"]["model"]:
        raise HTTPException(400, "Модель не настроена: выберите её в «Проект → Модель».")


class ProjectBody(BaseModel):
    name: str | None = None
    description: str | None = None
    base_url: str | None = None
    pipeline: dict | None = None


@app.get("/api/projects")
async def list_projects():
    """With a connection summary for the projects page."""
    out = []
    for item in projects.list_projects():
        p = projects.get(item["id"]) or {"connections": []}
        conns = [mcp_hub.public_view(item["id"], c) for c in p["connections"]]
        out.append(item | {"connections": [{k: c[k] for k in ("id", "name", "preset", "title", "enabled",
                                                                "missing", "check")} for c in conns],
                           "model": p.get("llm", {}).get("model", ""),
                           "model_check": p.get("llm", {}).get("check"),
                           "has_app_login": bool(projects.app_credentials(item["id"]).get("username"))})
    return out


@app.post("/api/projects")
async def create_project(body: ProjectBody):
    try:
        return _project_view(projects.create(body.name or "", body.description or "", body.base_url or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    p = project(pid)
    patch = body.model_dump(exclude_none=True, exclude={"api_key"})
    if "base_url" in patch and patch["base_url"].strip().rstrip("/") != p["llm"]["base_url"]:
        require_admin(request)
    return _project_view(projects.update_llm(pid, patch, body.api_key))


@app.delete("/api/projects/{pid}/llm/key")
async def clear_llm_key(pid: str):
    project(pid)
    projects.clear_llm_key(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/llm/test")
async def test_llm(pid: str):
    """Check the connection and list the models it offers; remembered in the project."""
    project(pid)
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


class Credentials(BaseModel):
    username: str = ""
    password: str = ""   # empty = keep the saved one


@app.put("/api/projects/{pid}/credentials")
async def set_project_credentials(pid: str, body: Credentials):
    project(pid)
    projects.set_app_credentials(pid, body.username, body.password)
    return {"ok": True}


@app.get("/api/projects/{pid}/tags")
async def project_tags(pid: str):
    project(pid)
    return sorted({tag for t in storage.all_tests(pid) for tag in t.get("tags") or []})


@app.get("/api/projects/{pid}/export")
async def export_project(pid: str, tag: str = ""):
    """All (or tagged) tests as a pytest project in a zip: conftest.py, tests/, features/."""
    p = project(pid)
    tests = storage.select(pid, tags=[tag] if tag else None)
    if not tests:
        raise HTTPException(404, "Нет тестов для экспорта")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in exporters.bundle(p, tests, p["pipeline"]["run"]["a11y_impact"]).items():
            z.writestr(path, text)
    name = "".join(c if c.isalnum() else "_" for c in p["name"]).lower() or "tests"
    return Response(buf.getvalue(), media_type="application/zip", headers=_attachment(f"{name}_tests.zip", "zip"))


# ---------- Project MCP connections ----------

@app.get("/api/mcp/presets")
async def mcp_presets():
    return mcp_hub.presets_public()


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
async def get_skill(pid: str, name: str, raw: bool = False):
    project(pid)
    s = skills.get(pid, name)
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


class NewSession(BaseModel):
    project_id: str
    name: str = "New test"
    url: str
    scenario: str
    autopilot: bool = False
    headless: bool = True
    engine: str = ""       # builtin | playwright-mcp; empty = project setting
    username: str = ""     # login for the app under test; empty = the project's
    password: str = ""


def _start_session(s: StudioSession, autopilot: bool) -> None:
    SESSIONS[s.id] = s

    async def boot():
        await _guard(s, s.start())
        if autopilot and s.status != "error":
            s.set_autopilot(True)

    submit(boot())


@app.post("/api/sessions")
async def create_session(body: NewSession):
    p = project(body.project_id)
    url = body.url if "://" in body.url else "https://" + body.url
    creds = {"username": body.username.strip(), "password": body.password}
    if not any(creds.values()):
        creds = projects.app_credentials(p["id"])
    engine = body.engine if body.engine in ("builtin", "playwright-mcp") else ""
    if body.scenario.strip() or body.autopilot:
        _require_model(p)
    s = StudioSession(p, body.name, url, body.scenario, headless=body.headless, credentials=creds,
                      engine=engine)
    _start_session(s, body.autopilot)
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


@app.post("/api/sessions/{sid}/pick")
async def pick(sid: str, body: Pick):
    s = sess(sid)
    try:
        return await call(s.pick(body.x, body.y, body.action, body.value, body.description,
                                 body.source))
    except ValueError as e:
        raise HTTPException(400, str(e))


class Manual(BaseModel):
    action: str
    value: str = ""
    description: str = ""


@app.post("/api/sessions/{sid}/manual")
async def manual(sid: str, body: Manual):
    s = sess(sid)
    if body.action not in ALL_ACTIONS:
        raise HTTPException(400, "Unknown action")
    step = new_step(body.action, body.description or f"{body.action} {body.value}".strip(),
                    body.value, source="manual")
    return await call(s.manual_step(step))


class StepsBody(BaseModel):
    steps: list[dict]


@app.put("/api/sessions/{sid}/steps")
async def set_steps(sid: str, body: StepsBody):
    sess(sid).steps = body.steps
    return {"ok": True}


@app.post("/api/sessions/{sid}/save")
async def save_session(sid: str):
    t, warnings = sess(sid).save()
    return t | {"warnings": warnings}


@app.delete("/api/sessions/{sid}")
async def close_session(sid: str):
    s = SESSIONS.pop(sid, None)
    if s:
        submit(s.close())
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


class MetaBody(BaseModel):
    tags: list[str] | None = None
    quarantine: bool | None = None
    reason: str = ""


@app.patch("/api/tests/{tid}/meta")
async def test_meta(tid: str, body: MetaBody, request: Request):
    test_or_404(tid)

    def change(t: dict) -> None:
        if body.tags is not None:
            t["tags"] = storage.normalize_tags(body.tags)
        if body.quarantine is not None:
            t["quarantine"] = {"on": True, "by": request.state.user, "at": time.time(),
                               "reason": body.reason.strip() or "Вручную"} if body.quarantine else {"on": False}
    return storage.update(tid, change)


@app.delete("/api/tests/{tid}")
async def delete_test(tid: str):
    return {"ok": storage.delete(tid)}


@app.get("/api/tests/{tid}/credentials")
async def get_credentials(tid: str):
    c = storage.own_credentials(test_or_404(tid))
    return {"username": c.get("username", ""), "has_password": bool(c.get("password"))}


@app.put("/api/tests/{tid}/credentials")
async def set_credentials(tid: str, body: Credentials):
    t = test_or_404(tid)
    c = storage.own_credentials(t)
    c["username"] = body.username.strip()
    if body.password:
        c["password"] = body.password
    storage.set_own_credentials(t, c)
    return {"ok": True}


@app.get("/api/tests/{tid}/export")
async def export(tid: str, format: str = "playwright"):
    t = test_or_404(tid)
    p = projects.get(t["project_id"]) or {"pipeline": projects.normalize_pipeline(None)}
    name = "".join(c if c.isalnum() else "_" for c in t["name"]).lower()
    if format == "gherkin":
        text, fname = exporters.to_gherkin(t | {"project": p.get("name", "")}), f"{name}.feature"
    elif format == "api":
        text, fname = exporters.to_api_tests(t, traffic.load(t["project_id"], tid)), f"test_{name}_api.py"
    elif format == "har":
        har = traffic.load_har(t["project_id"], tid)
        if not har:
            raise HTTPException(404, "Трафик этого теста не записан")
        return JSONResponse(har, headers=_attachment(f"{name}.har", "har"))
    else:
        text, fname = exporters.to_playwright(t, p["pipeline"]["run"]["a11y_impact"]), f"test_{name}.py"
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


def _run_request(t: dict, headless: bool | None, request: Request) -> dict:
    p = project(t["project_id"])
    run = runs.new(t, "manual", user=request.state.user)
    submit(pipeline.run_and_record(p, t, headless=headless, trigger="manual", user=request.state.user, run=run))
    return run


class RunBody(BaseModel):
    headless: bool | None = None   # None = project setting


@app.post("/api/tests/{tid}/run")
async def run(tid: str, body: RunBody, request: Request):
    return {"id": _run_request(test_or_404(tid), body.headless, request)["id"]}


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


@app.get("/api/runs/{rid}/files/{name}")
async def run_file(rid: str, name: str):
    f = runs.file(run_or_404(rid), name)
    if not f:
        raise HTTPException(404, "Файл не найден")
    if f.suffix == ".zip":
        return FileResponse(f, media_type="application/zip", headers=_attachment(f"{rid}-{name}", "zip"))
    return FileResponse(f, headers={"Cache-Control": "private, max-age=3600"})


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
    dest = checks.baseline_file(r["project_id"], r["test_id"], step_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(f, dest)
    return {"ok": True}


@app.get("/api/tests/{tid}/baselines/{step_id}")
async def baseline_image(tid: str, step_id: str):
    t = test_or_404(tid)
    f = checks.baseline_file(t["project_id"], tid, step_id)
    if not f.exists():
        raise HTTPException(404, "Эталона нет: он создаётся при первом прогоне")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "no-store"})


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
    submit(mutations.verify(p, t))
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
                      credentials=storage.credentials(t), base_steps=t["steps"], task=mutations.improvement_task(v))
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


@app.post("/api/projects/{pid}/runs")
async def run_suite(pid: str, body: SuiteBody, request: Request):
    p = project(pid)
    tags = storage.normalize_tags(body.tags)
    tests = storage.select(pid, tags=tags, test_ids=body.test_ids)
    if not tests:
        raise HTTPException(400, "Нет тестов для прогона" + (f" с тегами {', '.join(tags)}" if tags else ""))
    s = suite.new(p, tests, tags=tags, trigger="manual", user=request.state.user)
    submit(suite.run(p, s, tests, headless=body.headless, parallel=body.parallel))
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
    explorer.LIVE[state["id"]] = state

    async def go():
        try:
            await explorer.explore(p, body.url, log=lambda text: state["log"].append(text), state=state)
        finally:
            explorer.LIVE.pop(state["id"], None)

    submit(go())
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


# ---------- Requirements -> scenarios ----------

class ReqBody(BaseModel):
    project_id: str
    requirements: str
    url: str = ""


@app.post("/api/scenarios")
async def gen_scenarios(body: ReqBody):
    p = project(body.project_id)
    _require_model(p)
    try:
        res = await call(scenarios.generate(body.requirements, body.url, project=p))
    except Exception as e:
        raise HTTPException(502, llm.api_error_text(e))
    return res.model_dump()


class FetchBody(BaseModel):
    project_id: str
    link: str


@app.post("/api/requirements/fetch")
async def fetch_requirements(body: FetchBody):
    p = project(body.project_id)
    try:
        return await call(sources.fetch(body.link, p))
    except sources.SourceError as e:
        raise HTTPException(400, str(e))


# ---------- Pipeline jobs ----------

class JobBody(BaseModel):
    links: list[str] = []
    text: str = ""
    url: str = ""
    explore: bool = False


@app.post("/api/projects/{pid}/jobs")
async def start_job(pid: str, body: JobBody, request: Request):
    p = project(pid)
    if not [l for l in body.links if l.strip()] and not body.text.strip() and not body.explore:
        raise HTTPException(400, "Укажите ссылки на требования, текст или включите исследование сайта")
    _require_model(p)
    job = pipeline.Job(p, body.links, body.text, body.url, SESSIONS, user=request.state.user, explore=body.explore)
    pipeline.JOBS[job.id] = job
    job.save()
    submit(job.run())
    return {"id": job.id}


@app.get("/api/projects/{pid}/jobs")
async def list_jobs(pid: str):
    project(pid)
    return pipeline.list_jobs(pid)


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    j = pipeline.get_job(jid)
    if not j:
        raise HTTPException(404, "Запуск не найден")
    return j


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
    print(f"AI Test Generator: http://127.0.0.1:{PORT}  (the model is set per project: Project -> Model)")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
