"""Test IT (testit.software) through its REST API v2: no MCP server with writing exists, so the
studio talks to it directly.

    publish_test   create or update an autotest (externalId = the studio test id) with its steps,
                   setup (before) and teardown (after), labels = tags; link it to the manual test
                   case it automates (work item), if it came from one
    report_run     a test run with the result of the autotest (Passed / Failed), then complete it
    import_cases   manual test cases (work items) -> scenarios for the pipeline: by ids, or every
                   test case of the project

Connection fields (mcp_hub preset "testit"): site, project_id (UUID), configuration_id (optional,
else the project's first configuration), token (PrivateToken, secret).
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone

import httpx

TIMEOUT = httpx.Timeout(60, connect=15)
TRANSPORT = None          # tests: an httpx MockTransport


class TestItError(Exception):
    """A message that can be shown to the user as is."""


def _strip(text) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", str(text or ""), flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


class Client:
    def __init__(self, site: str, token: str):
        if not site or not token:
            raise TestItError("Подключение Test IT: заполните адрес и токен (PrivateToken)")
        self.base = site.rstrip("/") + "/api/v2"
        self.headers = {"Authorization": f"PrivateToken {token}", "Accept": "application/json"}

    async def call(self, method: str, path: str, **kw):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT) as c:
                r = await c.request(method, self.base + path, headers=self.headers, **kw)
        except httpx.HTTPError as e:
            raise TestItError(f"Test IT недоступен: {e}") from e
        if r.status_code == 401:
            raise TestItError("Test IT: неверный токен")
        if r.status_code >= 400:
            raise TestItError(f"Test IT {method} {path}: HTTP {r.status_code} — {r.text[:300]}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


def _client(fields: dict, secrets: dict) -> Client:
    return Client(fields.get("site", ""), secrets.get("token", ""))


async def check(fields: dict, secrets: dict) -> list[dict]:
    """The connection test: the project is readable -> pseudo "tools" for the UI."""
    c = _client(fields, secrets)
    p = await c.call("GET", f"/projects/{fields.get('project_id', '')}")
    return [{"name": f"Test IT: {p.get('name', '')}", "description": "REST API v2 (чтение кейсов, автотесты, прогоны)",
             "access": "write"}]


def _steps(test: dict) -> list[dict]:
    return [{"title": s["description"], "description": f"{s['action']}" + (f": {s['value']}" if s.get("value") else "")}
            for s in test["steps"]]


async def publish_test(fields: dict, secrets: dict, test: dict, namespace: str) -> dict:
    c = _client(fields, secrets)
    ext = (test.get("external") or {}).get("testit") or {}
    body = {"externalId": test["id"], "projectId": fields["project_id"], "name": test["name"],
            "namespace": namespace, "classname": "ai_testgen", "title": test["name"],
            "description": test.get("scenario", ""), "steps": _steps(test),
            "setup": [{"title": s["description"]} for s in test.get("before") or []],
            "teardown": [{"title": s["description"]} for s in test.get("after") or []],
            "labels": [{"name": t} for t in test.get("tags") or []]}
    if ext.get("autotest_id"):
        await c.call("PUT", "/autoTests", json=body | {"id": ext["autotest_id"]})
        auto_id = ext["autotest_id"]
    else:
        auto_id = (await c.call("POST", "/autoTests", json=body))["id"]
    if ext.get("work_item_id") and not ext.get("linked"):
        await c.call("POST", f"/autoTests/{auto_id}/workItems", json={"id": ext["work_item_id"]})
    url = f"{fields['site'].rstrip('/')}/projects/{fields['project_id']}/autotests/{auto_id}"
    return {"status": "ok", "key": str(auto_id), "url": url,
            "summary": "Автотест обновлён" if ext.get("autotest_id") else "Автотест создан",
            "external": ext | {"autotest_id": auto_id, "linked": bool(ext.get("work_item_id")), "url": url,
                               "at": time.time()}}


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


async def report_run(fields: dict, secrets: dict, test: dict, run: dict) -> dict:
    c = _client(fields, secrets)
    conf = fields.get("configuration_id")
    if not conf:
        confs = await c.call("GET", f"/projects/{fields['project_id']}/configurations") or []
        if not confs:
            raise TestItError("В проекте Test IT нет конфигураций")
        conf = confs[0]["id"]
    tr = await c.call("POST", "/testRuns", json={"projectId": fields["project_id"],
                                                 "name": f"AI Test Generator: {test['name']}"})
    failed = next((r for r in run.get("results") or [] if r["status"] == "failed"), None)
    outcome = "Passed" if run.get("passed") else "Failed"
    result = {"configurationId": conf, "autoTestExternalId": test["id"], "outcome": outcome,
              "startedOn": _iso(run.get("started")), "completedOn": _iso(run.get("finished")),
              "duration": int(((run.get("finished") or time.time()) - (run.get("started") or time.time())) * 1000),
              "message": (f"{failed['description']}: {failed['error']}" if failed else "")[:1000],
              "traces": ((run.get("analysis") or {}).get("summary") or "")[:4000],
              "stepResults": [{"title": r["description"], "outcome": "Passed" if r["status"] == "passed" else "Failed"}
                              for r in run.get("results") or []]}
    await c.call("POST", f"/testRuns/{tr['id']}/testResults", json=[result])
    await c.call("POST", f"/testRuns/{tr['id']}/complete")
    return {"status": "ok", "key": str(tr["id"]), "url": "", "summary": f"Результат {outcome} отправлен в Test IT"}


def _case(w: dict) -> dict:
    """A Test IT work item -> a scenario of the pipeline (scenarios.Scenario fields)."""
    steps = w.get("steps") or []
    lines = []
    for i, s in enumerate(steps, 1):
        action, expected = _strip(s.get("action")), _strip(s.get("expected"))
        data = _strip(s.get("testData"))
        lines.append(f"{i}. {action}" + (f" (данные: {data})" if data else "") + (f" — ожидается: {expected}" if expected else ""))
    pre = "; ".join(_strip(s.get("action")) for s in w.get("preconditionSteps") or [] if s.get("action"))
    expected = next((_strip(s.get("expected")) for s in reversed(steps) if s.get("expected")), "")
    priority = {"Highest": "high", "High": "high", "Medium": "medium", "Low": "low", "Lowest": "low"}.get(
        str(w.get("priority")), "medium")
    return {"title": w.get("name") or f"Кейс {w.get('globalId')}", "type": "positive", "priority": priority,
            "preconditions": pre, "instructions": "\n".join(lines) or _strip(w.get("description")),
            "expected_result": expected, "gherkin": "",
            "source_case": {"system": "testit", "id": w.get("id"), "global_id": w.get("globalId"),
                            "name": w.get("name", "")}}


async def import_cases(fields: dict, secrets: dict, ids: list[str] | None = None, limit: int = 500) -> list[dict]:
    """Manual test cases -> scenarios: the given work items (id or global id), else all test cases."""
    c = _client(fields, secrets)
    if not ids:
        # The project's list holds short models without steps: collect ids, then read each case.
        ids, skip = [], 0
        while len(ids) < limit:
            page = await c.call("GET", f"/projects/{fields['project_id']}/workItems",
                                params={"isDeleted": "false", "Skip": skip, "Take": 100}) or []
            ids += [str(w["id"]) for w in page if w.get("entityTypeName", "TestCases") == "TestCases"]
            if len(page) < 100:
                break
            skip += 100
    items = [await c.call("GET", f"/workItems/{str(i).strip()}") for i in ids[:limit] if str(i).strip()]
    return [_case(w) for w in items if w and w.get("entityTypeName", "TestCases") == "TestCases"]
