"""Value metrics of a project for the QA lead's dashboard (stage 4.4): what the manager is shown.

    automation   tests, and how many automate a manual case of the test management system (of the
                 cases imported into the pipeline)
    stability    share of flaky tests (they flip or passed only on re-run), tests in quarantine
    quality      share of killed mutants over the tests checked with mutations
    regression   duration of the last suite runs (the main effect of automation for most companies)
    saved hours  automatic runs in the period x the time of a manual run of a case (run.manual_minutes)
    spending     language models this month and the average cost of generating a test
"""
from __future__ import annotations

import statistics
import time

from . import llm, pipeline, projects, runs, storage, suite


def project_metrics(pid: str, days: int = 30) -> dict:
    p = projects.get(pid)
    cfg = p["pipeline"]["run"]
    since = time.time() - days * 86400
    tests = [t for t in storage.all_tests(pid) if t.get("role") != "module"]
    n = len(tests)

    linked = sum(1 for t in tests if any((e or {}).get("case_id") or (e or {}).get("work_item_id")
                                         for e in (t.get("external") or {}).values()))
    imported = 0
    for j in pipeline.list_jobs(pid, limit=200):
        full = pipeline.get_job(j["id"]) or {}
        if full.get("cases"):
            imported += len(full.get("scenarios") or [])

    flaky, quarantined, run_count, passed_runs, per_day = 0, 0, 0, 0, {}
    threshold = cfg["flaky_threshold"] / 100
    for t in tests:
        hist = runs.history(pid, t["id"], limit=0)
        rate = runs.flip_rate(hist[-runs.FLAKY_WINDOW:])
        if (rate is not None and rate >= threshold) or (t.get("last_run") or {}).get("flaky"):
            flaky += 1
        if (t.get("quarantine") or {}).get("on"):
            quarantined += 1
        for h in hist:
            if (h.get("started") or 0) >= since:
                run_count += 1
                passed_runs += h.get("status") in ("passed", "flaky")
                day = time.strftime("%Y-%m-%d", time.localtime(h["started"]))
                per_day[day] = per_day.get(day, 0) + 1

    scores = [t["verify"]["score"] for t in tests if (t.get("verify") or {}).get("score") is not None]
    suites = [s for s in suite.list_suites(pid, limit=20) if s.get("finished") and s.get("started")]
    durations = [{"at": s["started"], "minutes": round((s["finished"] - s["started"]) / 60, 1),
                  "passed": s.get("passed"), "total": (s.get("summary") or {}).get("total")} for s in reversed(suites)]
    costs = [(t.get("authoring_usage") or {}).get("cost_usd") for t in tests]
    costs = [c for c in costs if c]
    month = llm.ledger_report(pid)
    minutes = cfg.get("manual_minutes") or 5
    return {
        "days": days, "tests": n, "by_status": _by_status(tests),
        "automation": {"linked_cases": linked, "imported_cases": imported,
                       "share": round(linked / imported, 3) if imported else None},
        "stability": {"flaky": flaky, "flaky_share": round(flaky / n, 3) if n else None, "quarantined": quarantined,
                      "pass_rate": round(passed_runs / run_count, 3) if run_count else None},
        "quality": {"verified": len(scores), "mutation_score": round(statistics.mean(scores), 3) if scores else None},
        "regression": {"suites": durations[-10:], "last_minutes": durations[-1]["minutes"] if durations else None},
        "saved": {"runs": run_count, "manual_minutes": minutes, "hours": round(run_count * minutes / 60, 1),
                  "per_day": [{"day": d, "runs": c} for d, c in sorted(per_day.items())]},
        "spending": {"month": month["month"], "usd": month["total_usd"], "rub": month["total_rub"],
                     "test_generation_usd": round(statistics.mean(costs), 3) if costs else None},
    }


def _by_status(tests: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in tests:
        s = t.get("status") or "ready"
        out[s] = out.get(s, 0) + 1
    return out
