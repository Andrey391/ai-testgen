"""Suite results for CI: JUnit XML and Allure results.

JUnit: one <testcase> per test. A failure carries the failed step, the error and
Claude's analysis; a flaky test (failed, then passed on re-run) passes with the
property flaky=true; a failed test in quarantine is <skipped>, so it does not
fail the build but stays visible.

Allure: <dir>/<uuid>-result.json per test with steps and the screenshot of the
failed step as an attachment (allure generate <dir>).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from . import runs

VERDICT = {"product_bug": "дефект продукта", "test_issue": "проблема теста", "environment": "окружение",
           "flaky": "нестабильный тест", "unknown": "не ясно"}


def _duration(run: dict | None) -> float:
    if not run or not run.get("finished"):
        return 0.0
    return round(run["finished"] - run["started"], 3)


def _steps_log(run: dict) -> str:
    lines = []
    for a in run.get("attempts") or []:
        if len(run["attempts"]) > 1:
            lines.append(f"Попытка {a['attempt']}: {'passed' if a['passed'] else 'failed'}")
        for i, r in enumerate(a["results"], 1):
            lines.append(f"  {i}. [{r['status']}] {r['description']}" + (" (self-healed)" if r.get("healed") else "")
                         + (f" — {r['error']}" if r.get("error") else ""))
    for e in (run.get("events") or [])[:20]:
        lines.append(f"  [{e['type']}] {e['text'][:200]}")
    return "\n".join(lines)


def junit(suite: dict, run_by_id: dict[str, dict] | None = None) -> str:
    run_by_id = run_by_id or {}
    items = suite["items"]
    runs_of = {i["test_id"]: run_by_id.get(i["run_id"]) or (runs.get(i["run_id"]) if i["run_id"] else None)
               for i in items}
    failures = sum(i["status"] == "failed" and not i["quarantined"] for i in items)
    errors = sum(i["status"] == "error" and not i["quarantined"] for i in items)
    skipped = sum(i["status"] in ("failed", "error") and i["quarantined"] for i in items)
    total_time = sum(_duration(r) for r in runs_of.values())
    root = ET.Element("testsuites", name="AI Test Generator", tests=str(len(items)), failures=str(failures),
                      errors=str(errors), skipped=str(skipped), time=f"{total_time:.3f}")
    ts = ET.SubElement(root, "testsuite", name=suite["project"], tests=str(len(items)), failures=str(failures),
                       errors=str(errors), skipped=str(skipped), time=f"{total_time:.3f}")
    for i in items:
        run = runs_of.get(i["test_id"])
        tc = ET.SubElement(ts, "testcase", classname=suite["project"], name=i["name"],
                           time=f"{_duration(run):.3f}")
        props = [("test_id", i["test_id"]), ("run_id", i["run_id"] or "")] + [("tag", t) for t in i["tags"]]
        if i["status"] == "flaky":
            props.append(("flaky", "true"))
        if run and run.get("healed"):
            props.append(("self_healed", str(run["healed"])))
        pe = ET.SubElement(tc, "properties")
        for k, v in props:
            ET.SubElement(pe, "property", name=k, value=str(v))
        message = f"{i['failed_step']}: {i['error']}" if i["failed_step"] else i["error"]
        analysis = (run or {}).get("analysis") or {}
        detail = "\n".join(filter(None, [
            f"Анализ: {VERDICT.get(analysis.get('verdict'), analysis.get('verdict'))} — {analysis.get('summary')}"
            if analysis else "", analysis.get("suggestion", ""), _steps_log(run) if run else ""]))
        if i["status"] in ("failed", "error") and i["quarantined"]:
            ET.SubElement(tc, "skipped", message=f"В карантине, упал: {message}"[:1000])
        elif i["status"] == "failed":
            el = ET.SubElement(tc, "failure", message=message[:1000], type=analysis.get("verdict") or "failure")
            el.text = detail
        elif i["status"] == "error":
            el = ET.SubElement(tc, "error", message=message[:1000] or "error", type="error")
            el.text = detail
        elif run:
            ET.SubElement(tc, "system-out").text = _steps_log(run)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def allure(suite: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in suite["items"]:
        run = runs.get(i["run_id"]) if i["run_id"] else None
        uid = str(uuid.uuid4())
        status = {"passed": "passed", "flaky": "passed", "failed": "failed"}.get(i["status"], "broken")
        if i["quarantined"] and status != "passed":
            status = "skipped"
        start = int(((run or {}).get("started") or 0) * 1000)
        stop = int(((run or {}).get("finished") or 0) * 1000)
        steps, attachments = [], []
        for r in (run or {}).get("results") or []:
            steps.append({"name": r["description"], "status": "passed" if r["status"] == "passed" else "failed",
                          "stage": "finished", "statusDetails": {"message": r.get("error", "")}})
            if r["status"] == "failed" and r.get("shot") and run:
                f = runs.file(run, r["shot"])
                if f:
                    name = f"{uuid.uuid4()}-attachment.jpg"
                    shutil.copy(f, out_dir / name)
                    attachments.append({"name": "screenshot", "source": name, "type": "image/jpeg"})
        analysis = (run or {}).get("analysis") or {}
        result = {"uuid": uid, "historyId": hashlib.md5(i["test_id"].encode()).hexdigest(),
                  "name": i["name"], "fullName": f"{suite['project']}.{i['name']}", "status": status,
                  "stage": "finished", "start": start, "stop": stop, "steps": steps, "attachments": attachments,
                  "statusDetails": {"message": f"{i['failed_step']}: {i['error']}" if i["error"] else "",
                                    "trace": analysis.get("summary", ""), "flaky": i["status"] == "flaky"},
                  "labels": [{"name": "suite", "value": suite["project"]}, {"name": "framework", "value": "testgen"}]
                  + [{"name": "tag", "value": t} for t in i["tags"]]}
        (out_dir / f"{uid}-result.json").write_text(json.dumps(result, ensure_ascii=False), "utf-8")
