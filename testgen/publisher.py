"""Publishing to a test management system (Zephyr Scale) through its MCP server.

Community Zephyr MCP servers differ in tool names and arguments, so instead of
hard-coding one API the LLM gets the connection's tools (everything except
delete/remove tools) plus the "publish" skills, and drives them itself. It ends
with the `done` tool, which reports the key of the created/updated object.

- publish_test: create or update the test case with its steps.
- report_run:   record a run result as a test execution.

The test's Zephyr key is kept in test["external"]["zephyr"], so the next publish
updates the same test case instead of creating a duplicate. Test steps hold
only {{username}} / {{password}} placeholders, never real credentials.
"""
from __future__ import annotations

import json
import time

import anthropic

from . import exporters, llm, mcp_hub, skills

MAX_TURNS = 30

SYSTEM = """You publish automated UI test cases to the team's test management system using the tools provided (they come from its MCP server). Work carefully and with as few calls as possible:
- Read before you write when you need identifiers (project, folder, statuses, existing test case).
- Never delete anything. Never change objects other than the ones this task is about.
- Test data may contain the placeholders {{username}} and {{password}}: keep them as is, they are not secrets, and never ask for real credentials.
- If a tool call fails, read the error and fix the arguments; after repeated failures call done with status "failed" and explain.
- When finished, call done with the key (and URL, if known) of the object you created or updated."""

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


class PublishError(Exception):
    """A message that can be shown to the user as is."""


def _connection(project: dict) -> dict:
    cfg = project["pipeline"]["publish"]
    conn = (mcp_hub.find(project, cid=cfg.get("connection", "")) if cfg.get("connection")
            else mcp_hub.find(project, "zephyr"))
    if not conn:
        raise PublishError("В проекте нет включённого подключения Zephyr (Настройки проекта → Подключения)")
    return conn


def _test_payload(test: dict) -> dict:
    return {
        "name": test["name"], "scenario": test.get("scenario", ""), "url": test.get("url", ""),
        "priority": test.get("priority", ""), "type": test.get("scenario_type", ""),
        "source": test.get("source", ""),
        "steps": [{"n": i + 1, "action": s["action"], "description": s["description"],
                   "value": s.get("value", "")} for i, s in enumerate(test["steps"])],
        "gherkin": exporters.to_gherkin(test),
    }


async def _agent(project: dict, conn: dict, stage_cfg: dict, skill_names: list[str], task: str,
                 log=None) -> dict:
    toolbox = await mcp_hub.Toolbox(project["id"], [conn], mode="write").start()
    try:
        if toolbox.errors:
            raise PublishError(toolbox.errors[0])
        if not toolbox.tools:
            raise PublishError(f"У подключения «{conn['name']}» нет подходящих инструментов")
        system = SYSTEM + skills.prompt(project["id"], skill_names)
        messages = [{"role": "user", "content": task}]
        model = llm.model(project["id"], stage_cfg)
        for _ in range(MAX_TURNS):
            resp = await model.client.beta.messages.create(
                **model.params,
                max_tokens=16000,
                system=system,
                tools=toolbox.tools + [DONE_TOOL],
                messages=messages,
                cache_control={"type": "ephemeral"},
                betas=[llm.FALLBACK_BETA],
            )
            model.track(resp)
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                raise PublishError("Модель отказалась выполнять публикацию")
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Continue, and call done when finished."})
                continue
            results = []
            for c in calls:
                inp = c.input if isinstance(c.input, dict) else json.loads(c.input)
                if c.name == "done":
                    return inp
                if log:
                    log(f"🔧 {c.name.split('__', 1)[-1]}")
                text, is_error = await toolbox.call(c.name, inp)
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": text,
                                "is_error": is_error})
            messages.append({"role": "user", "content": results})
        raise PublishError("Публикация не завершилась за отведённое число шагов")
    except (anthropic.APIError, llm.NotConfigured) as e:
        raise PublishError(llm.api_error_text(e))
    finally:
        await toolbox.close()


async def publish_test(project: dict, test: dict, log=None) -> dict:
    """Create or update the test case. Returns {status, key, url, summary}; on success the
    key is stored in test["external"]["zephyr"] (the caller saves the test)."""
    cfg = project["pipeline"]["publish"]
    conn = _connection(project)
    fields = conn.get("fields") or {}
    existing = (test.get("external") or {}).get("zephyr", {}).get("key", "")
    task = (
        "Publish this automated UI test as a test case.\n"
        + (f"Project key: {fields['project_key']}\n" if fields.get("project_key") else "")
        + (f"Folder: {cfg['folder']}\n" if cfg.get("folder") else "")
        + (f"The test case already exists with key {existing}: update it and overwrite its steps.\n"
           if existing else "The test has not been published before.\n")
        + "\nTest (JSON):\n" + json.dumps(_test_payload(test), ensure_ascii=False, indent=2))
    res = await _agent(project, conn, cfg, cfg.get("skills", []), task, log)
    if res.get("status") == "ok" and res.get("key"):
        test.setdefault("external", {})["zephyr"] = {"key": res["key"], "url": res.get("url", ""),
                                                     "at": time.time()}
    return res


async def report_run(project: dict, test: dict, report: dict, log=None) -> dict:
    """Record a run of an already published test as a test execution."""
    cfg = project["pipeline"]["publish"]
    conn = _connection(project)
    key = (test.get("external") or {}).get("zephyr", {}).get("key", "")
    if not key:
        raise PublishError("Тест ещё не опубликован в Zephyr")
    fields = conn.get("fields") or {}
    results = [{"n": i + 1, "description": r["description"], "status": r["status"], "error": r["error"],
                "self_healed": r.get("healed", False)} for i, r in enumerate(report["results"])]
    run = {"test_case_key": key, "passed": report["passed"], "steps_total": len(test["steps"]),
           "status": report.get("status", ""), "flaky": report.get("flaky", False),
           "results": results, "healed": report.get("healed", 0), "analysis": report.get("analysis"),
           "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(report.get("started", time.time())))}
    task = ("Record this automated run of the test case as a test execution.\n"
            + (f"Project key: {fields['project_key']}\n" if fields.get("project_key") else "")
            + "\nRun (JSON):\n" + json.dumps(run, ensure_ascii=False, indent=2))
    return await _agent(project, conn, cfg, cfg.get("run_skills", []), task, log)
