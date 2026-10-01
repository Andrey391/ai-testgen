"""A defect from a failed run: a draft (title, steps to reproduce, expected and actual result,
environment, links), which a person edits and sends to the project's tracker by pressing
"Создать" - the model never creates issues.

Trackers: YouTrack, Yandex Tracker, Kaiten (REST, trackers.py) and Jira (the Atlassian
connection: one call of jira_create_issue with the read-only mode lifted for that call only).
"""
from __future__ import annotations

import os
import time

from . import mcp_hub, trackers

TRACKER_PRESETS = ("youtrack", "yandex_tracker", "kaiten", "atlassian")
VERDICT = {"product_bug": "дефект продукта", "test_issue": "проблема теста", "environment": "окружение",
           "flaky": "нестабильный тест", "unknown": "не ясно"}


def draft(test: dict, run: dict) -> dict:
    """The defect text built from the run: every step that ran, the failure, the analysis."""
    failed = next((r for r in run.get("results") or [] if r["status"] == "failed"), None)
    if failed and not failed.get("description"):
        failed = failed | {"description": next((s["description"] for s in test["steps"] if s["id"] == failed["id"]), "")}
    analysis = run.get("analysis") or {}
    steps = []
    for i, s in enumerate(test["steps"], 1):
        r = next((x for x in run.get("results") or [] if x["id"] == s["id"]), None)
        mark = " ← падение" if r and r["status"] == "failed" else ""
        steps.append(f"{i}. {s['description']}" + (f" (значение: {s['value']})" if s.get("value") and
                                                   "{{password}}" not in s["value"] else "") + mark)
        if r and r["status"] == "failed":
            break
    base = os.environ.get("TESTGEN_PUBLIC_URL", "").rstrip("/")
    title = (analysis.get("summary") or (failed or {}).get("error") or test["name"]).split("\n")[0][:120]
    lines = [f"Тест: {test['name']}", f"Адрес: {(failed or {}).get('url') or test.get('url', '')}",
             f"Браузер: {run.get('browser') or 'chromium'}" + (f", устройство: {run['device']}" if run.get("device") else ""),
             f"Прогон: {time.strftime('%Y-%m-%d %H:%M', time.localtime(run.get('started') or time.time()))}", "",
             "Шаги воспроизведения:", *steps, "",
             f"Ожидаемый результат: {failed['description'] if failed else '—'}",
             f"Фактический результат: {(failed or {}).get('error', '—')}"]
    if analysis:
        lines += ["", f"Анализ падения ({VERDICT.get(analysis.get('verdict'), analysis.get('verdict'))}): "
                      f"{analysis.get('summary', '')}"]
        if analysis.get("suggestion"):
            lines.append(analysis["suggestion"])
    events = [e["text"] for e in (run.get("events") or []) if e["type"] in ("pageerror", "console", "http")][:8]
    if events:
        lines += ["", "События браузера:"] + [f"- {e[:200]}" for e in events]
    if base:
        lines += ["", f"Прогон в студии: {base}/#run={run['id']}"]
        if run.get("trace"):
            lines.append(f"Playwright trace: {base}/api/runs/{run['id']}/files/{run['trace']}")
    return {"title": title, "text": "\n".join(lines)}


def trackers_of(project: dict) -> list[dict]:
    return [c for c in project.get("connections", []) if c.get("enabled", True) and c["preset"] in TRACKER_PRESETS]


async def create(project: dict, conn: dict, title: str, text: str) -> dict:
    """Create the issue in the tracker -> {"key", "url"}. Only on a person's click."""
    fields, secrets = mcp_hub.fields_of(project["id"], conn)
    if conn["preset"] == "atlassian":
        if not fields.get("defect_project"):
            raise trackers.TrackerError("Jira: укажите проект для дефектов в подключении")
        writable = conn | {"env": (conn.get("env") or {}) | {"READ_ONLY_MODE": "false"}}
        async with mcp_hub.connect(project["id"], writable) as session:
            res = await session.call_tool("jira_create_issue", {
                "project_key": fields["defect_project"], "summary": title,
                "issue_type": fields.get("defect_type") or "Bug", "description": text})
        body = mcp_hub.result_text(res)
        if res.isError:
            raise trackers.TrackerError(f"Jira: {mcp_hub.error_text(res)}")
        import json
        try:
            d = json.loads(body)
            issue = d.get("issue") or d
            key = issue.get("key", "")
        except ValueError:
            key = ""
        site = mcp_hub.normalize_site(fields.get("site", ""))
        return {"key": key, "url": f"{site}/browse/{key}" if key and site else ""}
    key, url = await trackers.create(conn["preset"], fields, secrets, title, text)
    return {"key": key, "url": url}
