"""Run a project's saved tests from the command line (CI).

    python -m testgen.run --project "Shop" --tag smoke --junit report.xml
    python -m testgen.run --project Shop --test "Login" --test 3f9a1c2b7e --headed
    python -m testgen.run --project Shop --list

Tests, settings and history come from the data folder (TESTGEN_DATA_DIR, by
default ./data), the application login from the project / test settings or
TESTGEN_USERNAME / TESTGEN_PASSWORD. Self-healing and failure analysis use the
project's model (Project -> Model in the studio) and its API key, or ANTHROPIC_API_KEY
when the secrets folder is not there; without a model a broken locator simply fails
the step.

Exit code: 0 - all tests passed (flaky ones and failures in quarantine do not
count), 1 - failures, 2 - bad arguments or setup.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import projects, reports, storage, suite

MARK = {"passed": "PASS ", "flaky": "FLAKY", "failed": "FAIL ", "error": "ERROR"}


def find_project(ref: str) -> dict | None:
    p = projects.get(ref) if ref else None
    if p:
        return p
    for item in projects.list_projects():
        if item["name"].strip().lower() == (ref or "").strip().lower():
            return projects.get(item["id"])
    return None


def pick_tests(project: dict, tags: list[str], refs: list[str]) -> list[dict]:
    tests = storage.all_tests(project["id"])
    if refs:
        wanted = {r.strip().lower() for r in refs}
        return [t for t in tests if t["id"] in refs or t["name"].strip().lower() in wanted]
    return storage.select(project["id"], tags=tags)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m testgen.run", description="Run saved tests of a project.")
    ap.add_argument("--project", required=True, help="project name or id")
    ap.add_argument("--tag", action="append", default=[], help="run tests with this tag (repeatable)")
    ap.add_argument("--test", action="append", default=[], help="test name or id (repeatable)")
    ap.add_argument("--junit", help="write a JUnit XML report here")
    ap.add_argument("--allure", help="write Allure results into this folder")
    ap.add_argument("--parallel", type=int, help="tests at once (default: project setting)")
    ap.add_argument("--headed", action="store_true", help="show the browser windows")
    ap.add_argument("--list", action="store_true", help="only list the tests that would run")
    args = ap.parse_args(argv)

    project = find_project(args.project)
    if not project:
        print(f"Project not found: {args.project}", file=sys.stderr)
        return 2
    tests = pick_tests(project, storage.normalize_tags(args.tag), args.test)
    if not tests:
        print("No tests to run" + (f" with tags {', '.join(args.tag)}" if args.tag else ""), file=sys.stderr)
        return 2
    if args.list:
        for t in tests:
            q = " [quarantine]" if (t.get("quarantine") or {}).get("on") else ""
            print(f"{t['id']}  {t['name']}  {' '.join('#' + x for x in t.get('tags') or [])}{q}")
        return 0

    print(f"{project['name']}: {len(tests)} tests")
    s = suite.new(project, tests, tags=storage.normalize_tags(args.tag), trigger="cli")

    def report(item: dict) -> None:
        q = " (quarantine)" if item["quarantined"] else ""
        line = f"{MARK.get(item['status'], item['status'])} {item['name']}{q}  {item['duration']}s"
        if item["status"] in ("failed", "error"):
            line += f"\n      {item['failed_step']}: {item['error']}".rstrip(": ")
        print(line, flush=True)

    s = asyncio.run(suite.run(project, s, tests, headless=not args.headed, parallel=args.parallel,
                              on_item=report))
    c = s["summary"]
    print(f"\npassed {c['passed']}, flaky {c['flaky']}, failed {c['failed']}, errors {c['error']}"
          + (f" (in quarantine: {c['quarantined_failed']})" if c["quarantined_failed"] else "")
          + (f"; Claude API ≈ ${s['usage']['cost_usd']}" if s["usage"].get("requests") and s["usage"].get("cost_usd") else ""))
    if args.junit:
        Path(args.junit).parent.mkdir(parents=True, exist_ok=True)
        Path(args.junit).write_text(reports.junit(s), "utf-8")
        print(f"JUnit: {args.junit}")
    if args.allure:
        reports.allure(s, Path(args.allure))
        print(f"Allure: {args.allure}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
