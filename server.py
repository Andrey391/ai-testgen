"""AI Test Generator - web studio.

Run:  python server.py   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from testgen import (auth, exporters, llm, mcp_hub, pipeline, projects, publisher, scenarios, skills,
                     sources, storage)
from testgen.agent import StudioSession, has_assertion
from testgen.steps import ALL_ACTIONS, new_step

ROOT = Path(__file__).resolve().parent

# Playwright objects are bound to the event loop that created them, so all
# browser work runs on one dedicated background loop; the web server just
# submits coroutines to it. MCP sessions and pipeline jobs live there too.
WORKER = asyncio.new_event_loop()
threading.Thread(target=WORKER.run_forever, daemon=True, name="browser-worker").start()


def submit(coro):
    """Fire-and-forget on the worker loop."""
    return asyncio.run_coroutine_threadsafe(coro, WORKER)


async def call(coro):
    """Run on the worker loop and await the result."""
    return await asyncio.wrap_future(submit(coro))


SESSIONS: dict[str, StudioSession] = {}
RUNS: dict[str, dict] = {}

projects.ensure_default()
app = FastAPI(title="AI Test Generator")

PUBLIC = {"/", "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/logout"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    user = auth.read_token(request.cookies.get(auth.COOKIE)) if auth.ENABLED else auth.ANONYMOUS
    request.state.user = user
    if not user and request.url.path not in PUBLIC:
        return JSONResponse({"detail": "Требуется вход"}, status_code=401)
    return await call_next(request)


def require_admin(request: Request) -> None:
    if not auth.is_admin(request.state.user):
        raise HTTPException(403, "Это может сделать только администратор студии")


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
            "model": llm.MODEL, "effort": llm.EFFORT}


# ---------- Projects ----------

def project(pid: str) -> dict:
    p = projects.get(pid)
    if not p:
        raise HTTPException(404, "Проект не найден")
    return p


def _project_view(p: dict) -> dict:
    c = projects.app_credentials(p["id"])
    return p | {"connections": [mcp_hub.public_view(p["id"], x) for x in p["connections"]],
                "app_username": c.get("username", ""), "app_has_password": bool(c.get("password"))}


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


class Credentials(BaseModel):
    username: str = ""
    password: str = ""   # empty = keep the saved one


@app.put("/api/projects/{pid}/credentials")
async def set_project_credentials(pid: str, body: Credentials):
    project(pid)
    projects.set_app_credentials(pid, body.username, body.password)
    return {"ok": True}


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
        text = str(e) if isinstance(e, mcp_hub.McpError) else llm.api_error_text(e)
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


@app.post("/api/sessions")
async def create_session(body: NewSession):
    p = project(body.project_id)
    url = body.url if "://" in body.url else "https://" + body.url
    creds = {"username": body.username.strip(), "password": body.password}
    if not any(creds.values()):
        creds = projects.app_credentials(p["id"])
    engine = body.engine if body.engine in ("builtin", "playwright-mcp") else ""
    s = StudioSession(p, body.name, url, body.scenario, headless=body.headless, credentials=creds,
                      engine=engine)
    SESSIONS[s.id] = s

    async def boot():
        await _guard(s, s.start())
        if body.autopilot and s.status != "error":
            s.set_autopilot(True)

    submit(boot())
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
    s = sess(sid)
    t = s.to_test()
    old = storage.load(t["id"])
    for key in ("external", "last_run", "priority", "scenario_type", "source"):
        if old and key in old:
            t[key] = old[key]
    storage.save(t)
    s.test_id = t["id"]
    # A login typed for this session (not the project's) stays with the test.
    if s.credentials and s.credentials != projects.app_credentials(s.project_id):
        storage.set_own_credentials(t, s.credentials)
    warnings = []
    dropped = len(s.steps) - len(t["steps"])
    if dropped:
        warnings.append(f"Упавшие шаги не сохранены в тест: {dropped}")
    if not has_assertion(t["steps"]):
        warnings.append("В тесте нет ни одной проверки: прогон будет успешным, что бы ни показало приложение")
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
async def tests(project_id: str):
    project(project_id)
    return storage.list_tests(project_id)


@app.get("/api/tests/{tid}")
async def get_test(tid: str):
    return test_or_404(tid)


@app.put("/api/tests/{tid}")
async def update_test(tid: str, body: dict):
    old = test_or_404(tid)
    body["id"], body["project_id"] = tid, old["project_id"]
    return storage.save(body)


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
    p = projects.get(t["project_id"]) or {}
    if format == "gherkin":
        text, ext = exporters.to_gherkin(t | {"project": p.get("name", "")}), "feature"
    else:
        text, ext = exporters.to_playwright(t), "py"
    name = "".join(c if c.isalnum() else "_" for c in t["name"]).lower()
    fname = f"test_{name}.{ext}" if ext == "py" else f"{name}.{ext}"
    # Test names are often non-ASCII (e.g. Cyrillic): HTTP headers need RFC 5987 encoding.
    disposition = f"inline; filename=\"test.{ext}\"; filename*=UTF-8''{quote(fname)}"
    return PlainTextResponse(text, headers={"Content-Disposition": disposition})


class RunBody(BaseModel):
    headless: bool | None = None   # None = project setting


@app.post("/api/tests/{tid}/run")
async def run(tid: str, body: RunBody):
    t = test_or_404(tid)
    p = project(t["project_id"])
    rid = uuid.uuid4().hex[:10]
    RUNS[rid] = {"id": rid, "test_id": tid, "status": "running", "report": None}

    async def progress(report):
        RUNS[rid]["report"] = report

    async def go():
        try:
            report = await pipeline.run_and_record(p, t, headless=body.headless, on_progress=progress)
            RUNS[rid].update(report=report, status="passed" if report["passed"] else "failed")
        except Exception as e:
            RUNS[rid].update(status="error", error=llm.api_error_text(e))

    submit(go())
    return {"id": rid}


@app.get("/api/runs/{rid}")
async def get_run(rid: str):
    r = RUNS.get(rid)
    if not r:
        raise HTTPException(404)
    if not r.get("report"):
        return r
    # Screenshots stay on the server: the UI polls this every second.
    report = r["report"] | {"results": [{k: v for k, v in x.items() if k != "screenshot"}
                                        for x in r["report"]["results"]]}
    return r | {"report": report}


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
    storage.save(t)
    return res


# ---------- Requirements -> scenarios ----------

class ReqBody(BaseModel):
    project_id: str
    requirements: str
    url: str = ""


@app.post("/api/scenarios")
async def gen_scenarios(body: ReqBody):
    p = project(body.project_id)
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


@app.post("/api/projects/{pid}/jobs")
async def start_job(pid: str, body: JobBody, request: Request):
    p = project(pid)
    if not [l for l in body.links if l.strip()] and not body.text.strip():
        raise HTTPException(400, "Укажите ссылки на требования или текст")
    job = pipeline.Job(p, body.links, body.text, body.url, SESSIONS, user=request.state.user)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    auth.ensure_admin()
    print(f"AI Test Generator: http://127.0.0.1:{port}  (model {llm.MODEL}, effort {llm.EFFORT})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
