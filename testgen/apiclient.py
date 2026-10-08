"""API tests without a browser.

An API (backend) scenario is authored and run by the same agent and runner as a UI test, through
ApiClient instead of BrowserSession: the same interface (describe, screenshot_b64, execute, url,
close, expand, mask, options, vars...), but no page. Every step is an api_request, sent with a
Playwright APIRequestContext to the project's API address («Проект → API», else the application's
URL) with the project's authorization:

    cookies  the saved login of the project's login test (its storage state), like the UI tests;
    none     no authorization;
    bearer   Authorization: Bearer <token>;
    header   <header>: <token> (an API key);
    login    a login request with the account's {{username}} / {{password}}; the token from its
             response goes into login.header (Authorization: Bearer <token> by default).

The token never reaches the model, the steps or the artifacts: it lives in the headers of the
request context and is masked in everything the client returns (secrets()).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from . import projects, traffic
from .browser import expand
from .steps import json_path, perform
from .testdata import DataValues, secret_values

# What an API test consists of: requests, and modules made of requests.
API_ACTIONS = {"api_request", "use_module"}
AUTH_TEXT = {"cookies": "the login of the project's login test (cookies)", "none": "none",
             "bearer": "a bearer token (already in the headers)", "header": "an API key header (already set)",
             "login": "a token from the login request (already in the headers)"}


async def check(project: dict) -> dict:
    """Sign in the way the project's API tests do (its default account) and call the API address."""
    from . import pipeline          # the pipeline imports the agent, which imports this module
    creds = projects.app_credentials(project["id"])
    state = None
    if projects.api_settings(project)["auth"] == "cookies" and pipeline.login_test(project["id"]):
        state = await pipeline.ensure_login_state(project, creds)
        if not state:
            raise ValueError("Тест входа проекта не прошёл: войти в API с его cookies нельзя")
    client = await ApiClient.launch(project, creds, state)
    try:
        return await client.check()
    finally:
        await client.close()


class ApiClient:
    engine = "api"

    def __init__(self, project: dict):
        self.project = project
        self.base_url = projects.api_base(project)
        self.cfg = projects.api_settings(project)
        self._pw = None
        self.context = SimpleNamespace(request=None, pages=[])
        self.page = None
        self.credentials: dict = {}
        self.testdata = DataValues()
        self.vars: dict = {}
        self.params: list[dict] = []
        self.events: list[dict] = []
        self.traffic: list[dict] | None = None
        self.step_index = -1
        self.options: dict = {}
        self.follow_new_tabs = True
        self.new_tabs = 0
        self.downloads: list[dict] = []
        self.dialog_plan = None
        self.dialog_error = ""
        self.dialogs: list[dict] = []
        self.last_response: dict | None = None
        self.elements: dict = {}
        self.history: list[str] = []        # the requests of this session, for describe()
        self.responses: list[dict] = []     # the same with the responses, for the Studio (masked)
        self.endpoints: list[dict] = []     # the API catalog of the project (traffic.catalog)
        self.token = ""

    @classmethod
    async def launch(cls, project: dict, credentials: dict | None = None,
                     storage_state: dict | None = None) -> "ApiClient":
        s = cls(project)
        s.credentials = credentials or {}
        if not s.base_url.startswith("http"):
            raise ValueError("Не задан адрес API: укажите его в «Проект → API» или URL приложения в проекте")
        s.options = {"project_id": project["id"], "base_url": s.base_url, "own_vars": []}
        s._pw = await async_playwright().start()
        try:
            headers = await s._auth_headers()
            state = storage_state if s.cfg["auth"] == "cookies" else None
            s.context.request = await s._pw.request.new_context(extra_http_headers=headers, storage_state=state)
        except Exception:
            await s._pw.stop()
            raise
        try:
            s.endpoints = traffic.catalog(project["id"])
        except Exception:
            s.endpoints = []
        return s

    async def _auth_headers(self) -> dict:
        auth = self.cfg["auth"]
        if auth in ("bearer", "header"):
            self.token = projects.api_token(self.project["id"])
            if not self.token:
                raise ValueError("Не задан токен API: укажите его в «Проект → API»")
            return ({"Authorization": f"Bearer {self.token}"} if auth == "bearer"
                    else {self.cfg["header"] or "X-API-Key": self.token})
        if auth == "login":
            self.token = await self._login(self.cfg["login"])
            name = self.cfg["login"]["header"] or "Authorization"
            return {name: f"Bearer {self.token}" if name.lower() == "authorization" else self.token}
        return {}

    async def _login(self, login: dict) -> str:
        if not login["path"]:
            raise ValueError("Вход в API: не задан адрес запроса входа («Проект → API»)")
        ctx = await self._pw.request.new_context()
        try:
            body = login["body"]
            try:
                data = json.loads(self.expand(json.dumps(json.loads(body), ensure_ascii=False))) if body else None
            except ValueError:
                data = self.expand(body)
            resp = await ctx.fetch(urljoin(self.base_url + "/", self.expand(login["path"])), method=login["method"],
                                   data=data, timeout=30000)
            if not resp.ok:
                raise ValueError(f"Вход в API не выполнен: {login['method']} {login['path']} ответил {resp.status}")
            try:
                token = json_path(await resp.json(), login["token"])
            except Exception:
                raise ValueError(f"Вход в API: в ответе нет токена {login['token']}")
            if not isinstance(token, str) or not token:
                raise ValueError(f"Вход в API: {login['token']} в ответе — не строка")
            return token
        finally:
            await ctx.dispose()

    async def close(self) -> None:
        try:
            if self.context.request:
                await self.context.request.dispose()
        finally:
            if self._pw:
                await self._pw.stop()

    # ---------- the interface of a browser session ----------

    def secrets(self) -> list[str]:
        return secret_values(self.credentials) + ([self.token] if self.token else [])

    def mask(self, text: str) -> str:
        for s in self.secrets():
            if text and s:
                text = text.replace(s, "***")
        return text

    def expand(self, value: str) -> str:
        return expand(self.credentials, value, self.testdata, self.vars, self.params[-1] if self.params else {})

    @property
    def url(self) -> str:
        return self.base_url

    async def screenshot_b64(self) -> str:
        return ""

    async def settle(self) -> None:
        return None

    def settled_quietly(self) -> None:
        return None

    def just_settled(self) -> bool:
        return True

    def console_errors(self) -> list[dict]:
        return []

    def find_text(self, text: str, limit: int = 20) -> list[dict]:
        return []

    async def find(self, locator: list[dict], wait: float = 0):
        return None, None

    async def pick_at(self, *args, **kwargs):
        raise ValueError("В API-тесте нет страницы: шаги — запросы к API")

    def by_ref(self, ref: str):
        raise ValueError("В API-тесте нет элементов страницы: используйте api_request")

    async def execute(self, step: dict) -> None:
        step.pop("ref", None)
        step.pop("target_ref", None)
        if step["action"] not in API_ACTIONS:
            raise ValueError("В API-тесте нет браузера и страницы: каждый шаг — запрос к API (api_request)")
        self.last_response = None
        error = ""
        try:
            return await perform(self, step)
        except Exception as e:
            error = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
            raise
        finally:
            self._remember(step, error)

    def _remember(self, step: dict, error: str) -> None:
        try:
            spec = json.loads(step.get("value") or "{}")
        except ValueError:
            spec = {}
        last = self.last_response or {}
        request = self.mask(f"{spec.get('method', 'GET')} {spec.get('url', '')}")
        self.history = (self.history + [f"{request} -> {last.get('status', '—')}"
                                        + (f" (step failed: {error})" if error else "")])[-20:]
        self.responses = (self.responses + [{"request": request, "status": last.get("status"),
                                             "body": last.get("body", "")[:1500], "error": self.mask(error)}])[-10:]

    async def check(self) -> dict:
        """«Проверить» in «Проект → API»: the authorization worked and the API answers."""
        resp = await self.context.request.get(self.base_url, timeout=20000)
        return {"status": resp.status, "url": self.base_url, "auth": self.cfg["auth"],
                "endpoints": len(self.endpoints)}

    async def describe(self) -> str:
        """The state for the model: the API, its authorization, the requests made, the last response
        and the endpoints the project knows."""
        last = self.last_response
        text = (f"API under test: {self.base_url}\nAuthorization: {AUTH_TEXT[self.cfg['auth']]}\n"
                "There is no browser and no page: every step is an api_request.\n\n"
                "Requests of this session:\n" + ("\n".join(self.history) or "(none yet)") + "\n\n")
        if last:
            text += f"Last response: status {last['status']}\n{last['body'][:2500]}\n\n"
        text += ("Known endpoints of the application (from the traffic recorded by its UI tests and the site map; "
                 "{id} stands for an object id):\n" + (traffic.catalog_text(self.endpoints) or
                                                        "(none recorded yet: take the paths from the scenario)"))
        return self.mask(text)
