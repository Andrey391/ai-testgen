"""Earlier scenarios and tests are reused or refined instead of duplicated.

When new scenarios are designed, the model sees what the project already has: its tests and the
scenarios of earlier analyses that have no test yet (candidates()). A planned scenario that checks the
same thing as one of them ("same"), or that one of them would check after a refinement ("refine"),
gets a `match`. A person decides what to do with it (DECISIONS): create a new test anyway, reuse the
earlier one (its test is linked to the new scenario, nothing is generated) or refine it (the agent
replays the earlier test and extends it to the new scenario; the test keeps its id). The pipeline
waits for the decisions before authoring (Job._await_reuse); the Requirements tab asks in the row
of the scenario.

match = {"kind": "test" | "scenario", "id", "analysis_id" (kind scenario), "title",
         "relation": "same" | "refine", "decision": "" | "new" | "reuse" | "refine"}
"""
from __future__ import annotations

import re

MAX = 300            # earlier tests and scenarios shown to the model
RELATIONS = ("same", "refine")
DECISIONS = ("new", "reuse", "refine")
DECISION_TITLES = {"new": "создать новый", "reuse": "переиспользовать", "refine": "доработать"}

INSTRUCTION = (
    "The project already has these tests (T…) and scenarios without a test (S…). For every scenario you plan, "
    "compare it with them: when one of them already checks the same thing, set `existing` to its ref and "
    "`relation` to \"same\"; when one of them checks part of it and extending it would check this scenario "
    "(refining the earlier one is better than a new test), set `relation` to \"refine\"; otherwise leave both "
    "empty. Plan the scenario anyway: a person decides whether to reuse, refine or create a new one.")


def candidates(pid: str, exclude_analysis: str = "") -> list[dict]:
    """The project's tests (newest first) and the scenarios of earlier analyses without a test."""
    from . import analyses, fs, storage
    out = []
    tests = sorted(storage.all_tests(pid), key=lambda t: t.get("updated") or 0, reverse=True)
    for t in tests:
        if t.get("role") == "module":
            continue
        out.append({"ref": f"T{len(out) + 1}", "kind": "test", "id": t["id"], "title": t["name"],
                    "text": " ".join(str(t.get("scenario") or "").split())[:200]})
    for f, _, _ in fs.documents(analyses._dir(pid)):
        if f.stem == exclude_analysis:
            continue
        a = fs.read_json(f) or {}
        for s in a.get("scenarios") or []:
            if s.get("test_ids") or s.get("pending") or not s.get("title"):
                continue
            out.append({"ref": f"S{len(out) + 1}", "kind": "scenario", "id": s["id"], "analysis_id": f.stem,
                        "title": s["title"], "text": " ".join(str(s.get("instructions") or "").split())[:200]})
    return out[:MAX]


def listing(cands: list[dict]) -> str:
    if not cands:
        return ""
    return (INSTRUCTION + "\n" + "\n".join(f"{c['ref']} — {c['title']}" + (f" — {c['text']}" if c["text"] else "")
                                         for c in cands))


def resolve(cands: list[dict], ref: str, relation: str) -> dict | None:
    """The match of a planned scenario (`existing` ref, `relation`), or None."""
    ref = (ref or "").strip().upper()
    if relation not in RELATIONS or not re.fullmatch(r"[TS]\d+", ref):
        return None
    c = next((c for c in cands if c["ref"] == ref), None)
    if not c:
        return None
    return {"kind": c["kind"], "id": c["id"], "analysis_id": c.get("analysis_id", ""), "title": c["title"],
            "relation": relation, "decision": ""}


def clean(match) -> dict | None:
    if not isinstance(match, dict) or match.get("kind") not in ("test", "scenario") or not match.get("id"):
        return None
    return {"kind": match["kind"], "id": str(match["id"])[:40], "analysis_id": str(match.get("analysis_id") or "")[:40],
            "title": str(match.get("title") or "")[:300],
            "relation": match.get("relation") if match.get("relation") in RELATIONS else "same",
            "decision": match.get("decision") if match.get("decision") in DECISIONS else ""}


def pending(match) -> bool:
    """A match nobody decided on yet."""
    return bool(match) and not match.get("decision")


def test_of(pid: str, match) -> dict | None:
    """The earlier test the match points to, if it is still there (in this project)."""
    from . import storage
    if not match or match.get("kind") != "test":
        return None
    t = storage.load(match["id"])
    return t if t and t.get("project_id") == pid else None


REFINE_TASK = (
    "This saved test checks an earlier scenario. The steps above were replayed. Refine the test so it also "
    "checks the new scenario below: keep what it already checks, add or change only the steps and assertions "
    "the new scenario needs, then finish.\nNew scenario:\n{scenario}")
