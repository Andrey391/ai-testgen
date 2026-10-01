"""assert_screenshot (baselines, diff, the LLM's verdict) and assert_accessible (axe-core)."""
from __future__ import annotations

import pytest

from fakes import Resp
from helpers import arun
from testgen import checks, fs, runner
from testgen.runner import VisualVerdict
from testgen.steps import new_step


def _visual_test(stand, project):
    shot = new_step("assert_screenshot", "Страница входа выглядит как эталон")
    shot["masks"] = ["#msg"]
    return {"id": "visual1", "project_id": project["id"], "name": "Вид входа",
            "steps": [new_step("navigate", "Открыть", f"{stand.url}/login.html"), shot]}


def test_visual_baseline_compare_and_verdict(stand, project, fake_llm, tmp_path):
    t = _visual_test(stand, project)
    step_id = t["steps"][1]["id"]
    base = checks.baseline_file(project["id"], t["id"], step_id)

    r1 = arun(runner.run_test(t, run_dir=tmp_path / "r1"))
    assert r1["passed"] and r1["results"][1]["details"]["visual"]["baseline_created"] and fs.is_file(base)

    r2 = arun(runner.run_test(t, run_dir=tmp_path / "r2"))
    v = r2["results"][1]["details"]["visual"]
    assert r2["passed"] and v["ratio"] < 0.001

    fake_llm.script = lambda kind, kw: Resp(parsed=VisualVerdict(verdict="expected_change", summary="Новый баннер"))
    stand.reset("v2")
    r3 = arun(runner.run_test(t, cfg={"analyze_failures": True, "trace": "off"}, run_dir=tmp_path / "r3"))
    assert not r3["passed"]
    v = r3["results"][1]["details"]["visual"]
    assert v["ratio"] * 100 > v["threshold"] and (tmp_path / "r3" / v["diff"]).exists()
    assert v["verdict"]["verdict"] == "expected_change"
    sent = fake_llm.calls[-1][1]["messages"][0]["content"]
    assert sum(1 for b in sent if b["type"] == "image") == 3           # baseline, actual, diff


@pytest.fixture
def axe():
    try:
        arun(checks.axe_source())
    except Exception as e:           # offline machine without TESTGEN_AXE_JS
        pytest.skip(f"axe-core is not available: {e}")


def test_accessibility(stand, axe):
    bad = [new_step("navigate", "Открыть", f"{stand.url}/errors.html"), new_step("assert_accessible", "WCAG")]
    rep = arun(runner.run_test({"id": "a1", "project_id": "", "name": "a11y", "steps": bad}))
    assert not rep["passed"] and "image-alt" in rep["results"][1]["error"]
    ids = {v["id"] for v in rep["results"][1]["details"]["a11y"]["violations"]}
    assert {"image-alt", "label"} <= ids

    good = [new_step("navigate", "Открыть", f"{stand.url}/form.html"), new_step("assert_accessible", "WCAG")]
    rep = arun(runner.run_test({"id": "a2", "project_id": "", "name": "a11y", "steps": good},
                               cfg={"a11y_impact": "critical"}))
    assert rep["passed"], rep["results"][-1]
