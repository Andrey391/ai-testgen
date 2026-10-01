"""Publishing to the team's test management system and importing manual test cases from it.

Systems (by the preset of the project's publish connection):
    zephyr   Zephyr Scale through its MCP server
    allure   Allure TestOps through its built-in MCP server
    testit   Test IT through its REST API (testit.py)
    custom   any other MCP server the admin connected

MCP servers differ in tool names and arguments, so instead of hard-coding one API the
model gets the connection's tools (everything except delete/remove tools) plus the
"publish" skills of the system, and drives them itself. It ends with the `done` tool,
which reports the key of the created/updated object.

- publish_test: create or update the test case with its steps.
- report_run:   record a run result as a test execution.
- import_cases: manual test cases -> scenarios of the pipeline, linked to the source case.

The key of a test in a system is kept in test["external"][<system>], so the next publish
updates the same case instead of creating a duplicate. Test steps hold only
{{username}} / {{password}} placeholders, never real credentials.
"""
from __future__ import annotations

import json
import re
import time

from pydantic import BaseModel

from . import exporters, llm, mcp_hub, skills, testit
from .providers.base import check_call

MAX_TURNS = 30

# What each system publishes with. The skills are used when the stage keeps the default (Zephyr) ones.
SYSTEMS = {
    "zephyr": {"title": "Zephyr Scale", "skills": ["zephyr-publish"], "run_skills": ["zephyr-report-run"],
               "import_skills": []},
    "allure": {"title": "Allure TestOps", "skills": ["allure-publish"], "run_skills": ["allure-report-run"],
               "import_skills": ["allure-import"]},
    "testit": {"title": "Test IT", "skills": [], "run_skills": [], "import_skills": []},
    "custom": {"title": "MCP", "skills": [], "run_skills": [], "import_skills": []},
}
PUBLISH_PRESETS = ("zephyr", "allure", "testit", "custom")

SYSTEM = """You publish automated UI test cases to the team's test management system using the tools provided (they come from its MCP server). Work carefully and with as few calls as possible:
- Read before you write when you need identifiers (project, folder, statuses, existing test case).
- Never delete anything. Never change objects other than the ones this task is about.
- Test data may contain the placeholders {{username}} and {{password}}: keep them as is, they are not secrets, and never ask for real credentials.
- If a tool call fails, read the error and fix the arguments; after repeated failures call done with status "failed" and explain.
- When finished, call done with the key (and URL, if known) of the object you created or updated."""

IMPORT_SYSTEM = """You read manual test cases from the team's test management system with the read-only tools provided and return them with the `cases` tool: for each case its id, name, preconditions, steps (action and expected result) and priority. Do not change anything in the system. Return exactly the cases you were asked for; if one cannot be found, skip it."""

DONE_TOOL = {
    "name": "done",
    "description": "Finish the task and report the result.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "failed"]},
            "key": {"type": "string", "description": "Key of the created/updated object, e.g. PROJ-T12; empty if none."},
            "url": {"type": "string", "description": "Link to the object if known, else empty."},
            "summary": {"type": "string", "description": "One or two sentences about what was done."},
        },
        "required": ["status", "key", "url", "summary"],
        "additionalProperties": False,
    },
}

CASES_TOOL = {
    "name": "cases",
    "description": "Return the manual test cases that were read.",
    "input_schema": {
        "type": "object",
        "properties": {"cases": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"}, "name": {"type": "string"}, "preconditions": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "steps": {"type": "array", "items": {"type": "object", "properties": {
                    "action": {"type": "string"}, "expected": {"type": "string"}}, "required": ["action"]}},
            },
            "required": ["id", "name", "steps"]}}},
        "required": ["cases"],
    },
}


class PublishError(Exception):
    """A message that can be shown to the user as is."""


def system_of(conn: dict) -> str:
    return conn["preset"] if conn["preset"] in SYSTEMS else "custom"


def title_of(project: dict) -> str:
    """"Zephyr Scale" / "Allure TestOps" / "Test IT" / the connection's name: for messages."""
    conn = connection(project, required=False)
    if not conn:
        return "систему управления тестами"
    return SYSTEMS[system_of(conn)]["title"] if system_of(conn) != "custom" else conn["name"]


def connection(project: dict, required: bool = True) -> dict | None:
    cfg = project["pipeline"]["publish"]
    conn = mcp_hub.find(project, cid=cfg["connection"]) if cfg.get("connection") else next(
        (mcp_hub.find(project, p) for p in ("zephyr", "allure", "testit") if mcp_hub.find(project, p)), None)
    if not conn and required:
        raise PublishError("В проекте нет включённого подключения системы управления тестами "
                           "(Zephyr Scale, Allure TestOps, Test IT): Настройки проекта → Подключения")
    return conn


def external(test: dict, project: dict) -> dict:
    """The test's record in the project's publish system ({} if not published)."""
    conn = connection(project, required=False)
    return ((test.get("external") or {}).get(_key(conn)) or {}) if conn else {}


def _key(conn: dict) -> str:
    s = system_of(conn)
    return s if s != "custom" else f"mcp-{conn['id']}"


def _skills(cfg_list: list[str], default: list[str], system: str, kind: str) -> list[str]:
    """The stage's skills; when they are the Zephyr defaults and the system is another one, its own."""
    return SYSTEMS[system][kind] if cfg_list == default and system not in ("zephyr", "custom") else cfg_list


def _test_payload(test: dict) -> dict:
    return {
        "name": test["name"], "scenario": test.get("scenario", ""), "url": test.get("url", ""),
        "priority": test.get("priority", ""), "type": test.get("scenario_type", ""),
        "source": test.get("source", ""), "tags": test.get("tags") or [],
        "steps": [{"n": i + 1, "action": s["action"], "description": s["description"],
                   "value": s.get("value", "")} for i, s in enumerate(test["steps"])],
        "gherkin": exporters.to_gherkin(test),
    }


async def _agent(project: dict, conn: dict, stage_cfg: dict, skill_names: list[str], task: str,
                 log=None, *, system: str = SYSTEM, mode: str = "write", final: dict = DONE_TOOL) -> dict:
    toolbox = await mcp_hub.Toolbox(project["id"], [conn], mode=mode).start()
    try:
        if toolbox.errors:
            raise PublishError(toolbox.errors[0])
        if not toolbox.tools:
            raise PublishError(f"У подключения «{conn['name']}» нет подходящих инструментов")
        system = system + skills.prompt(project["id"], skill_names)
        messages = [{"role": "user", "content": task}]
        tools = toolbox.tools + [final]
        for _ in range(MAX_TURNS):
            reply = await llm.chat(stage_cfg, system=system, tools=tools, messages=messages, max_tokens=16000,
                                   one_tool=False, cache_all=True, project_id=project["id"], stage_name="publish")
            messages.append({"role": "assistant", "content": reply.content})
            if reply.stop == "refusal":
                raise PublishError("Модель отказалась выполнять задачу")
            calls = reply.tool_calls
            if not calls:
                messages.append({"role": "user", "content": f"Continue, and call {final['name']} when finished."})
                continue
            results = []
            for c in calls:
                problem = check_call(c, tools)
                if problem:
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": problem,
                                    "is_error": True})
                    continue
                if c["name"] == final["name"]:
                    return c["input"]
                if log:
                    log(f"🔧 {c['name'].split('__', 1)[-1]}")
                text, is_error = await toolbox.call(c["name"], c["input"])
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": text,
                                "is_error": is_error})
            messages.append({"role": "user", "content": results})
        raise PublishError("Задача не завершилась за отведённое число шагов")
    except llm.ProviderError as e:
        raise PublishError(llm.api_error_text(e))
    finally:
        await toolbox.close()


async def publish_test(project: dict, test: dict, log=None) -> dict:
    """Create or update the test case. Returns {status, key, url, summary}; on success the
    key is stored in test["external"][<system>] (the caller saves the test)."""
    cfg = project["pipeline"]["publish"]
    conn = connection(project)
    system, key = system_of(conn), _key(conn)
    fields, secrets = mcp_hub.fields_of(project["id"], conn)
    ext = (test.get("external") or {}).get(key) or {}
    if system == "testit":
        try:
            res = await testit.publish_test(fields, secrets, test, project["name"])
        except testit.TestItError as e:
            raise PublishError(str(e))
        test.setdefault("external", {})[key] = res.pop("external") | {"key": res["key"]}
        return res
    existing = ext.get("key", "")
    source = ext.get("case_id") or ""
    task = (
        "Publish this automated UI test as a test case.\n"
        + (f"Project key: {fields['project_key']}\n" if fields.get("project_key") else "")
        + (f"Project id: {fields['project_id']}\n" if fields.get("project_id") else "")
        + (f"Folder: {cfg['folder']}\n" if cfg.get("folder") else "")
        + (f"The test case already exists with key {existing}: update it and overwrite its steps.\n"
           if existing else "The test has not been published before.\n")
        + (f"It automates the manual test case {source}: link the automated test to it (or mark that case as "
           "automated) if the system supports it.\n" if source and not existing else "")
        + "\nTest (JSON):\n" + json.dumps(_test_payload(test), ensure_ascii=False, indent=2))
    res = await _agent(project, conn, cfg, _skills(cfg.get("skills", []), ["zephyr-publish"], system, "skills"),
                       task, log)
    if res.get("status") == "ok" and res.get("key"):
        test.setdefault("external", {})[key] = ext | {"key": res["key"], "url": res.get("url", ""), "at": time.time()}
    return res


async def report_run(project: dict, test: dict, report: dict, log=None) -> dict:
    """Record a run of an already published test as a test execution."""
    cfg = project["pipeline"]["publish"]
    conn = connection(project)
    system, key = system_of(conn), _key(conn)
    fields, secrets = mcp_hub.fields_of(project["id"], conn)
    ext = (test.get("external") or {}).get(key) or {}
    if not ext.get("key"):
        raise PublishError(f"Тест ещё не опубликован в {title_of(project)}")
    if system == "testit":
        try:
            return await testit.report_run(fields, secrets, test, report)
        except testit.TestItError as e:
            raise PublishError(str(e))
    results = [{"n": i + 1, "description": r["description"], "status": r["status"], "error": r["error"],
                "self_healed": r.get("healed", False)} for i, r in enumerate(report["results"])]
    run = {"test_case_key": ext["key"], "passed": report["passed"], "steps_total": len(test["steps"]),
           "status": report.get("status", ""), "flaky": report.get("flaky", False),
           "browser": report.get("browser", ""), "results": results, "healed": report.get("healed", 0),
           "analysis": report.get("analysis"),
           "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(report.get("started", time.time())))}
    task = ("Record this automated run of the test case as a test execution.\n"
            + (f"Project key: {fields['project_key']}\n" if fields.get("project_key") else "")
            + (f"Project id: {fields['project_id']}\n" if fields.get("project_id") else "")
            + "\nRun (JSON):\n" + json.dumps(run, ensure_ascii=False, indent=2))
    return await _agent(project, conn, cfg, _skills(cfg.get("run_skills", []), ["zephyr-report-run"], system,
                                                    "run_skills"), task, log)


# ---------- manual test cases -> scenarios ----------

class _Step(BaseModel):
    action: str
    expected: str = ""


def _scenario(system: str, c: dict) -> dict:
    steps = [_Step(**s) if isinstance(s, dict) else s for s in c.get("steps") or []]
    lines = [f"{i}. {s.action}" + (f" — ожидается: {s.expected}" if s.expected else "") for i, s in enumerate(steps, 1)]
    return {"title": c.get("name") or f"Кейс {c.get('id')}", "type": "positive",
            "priority": c.get("priority") if c.get("priority") in ("high", "medium", "low") else "medium",
            "preconditions": c.get("preconditions") or "", "instructions": "\n".join(lines),
            "expected_result": next((s.expected for s in reversed(steps) if s.expected), ""), "gherkin": "",
            "source_case": {"system": system, "id": str(c.get("id")), "name": c.get("name", "")}}


def parse_ids(text: str) -> list[str]:
    return [x for x in re.split(r"[\s,;]+", text or "") if x]


async def import_cases(project: dict, conn: dict, ids: list[str], log=None) -> list[dict]:
    """Manual test cases of the system -> pipeline scenarios (each keeps "source_case")."""
    system = system_of(conn)
    fields, secrets = mcp_hub.fields_of(project["id"], conn)
    if system == "testit":
        try:
            return await testit.import_cases(fields, secrets, ids or None)
        except testit.TestItError as e:
            raise PublishError(str(e))
    if not ids:
        raise PublishError("Укажите номера кейсов для импорта")
    cfg = project["pipeline"]["publish"]
    task = (f"Read these manual test cases and return them with the cases tool: {', '.join(ids)}.\n"
            + (f"Project id: {fields['project_id']}\n" if fields.get("project_id") else "")
            + (f"Project key: {fields['project_key']}\n" if fields.get("project_key") else ""))
    res = await _agent(project, conn, cfg, SYSTEMS[system]["import_skills"], task, log, system=IMPORT_SYSTEM,
                       mode="read", final=CASES_TOOL)
    return [_scenario(system, c) for c in res.get("cases") or []]
