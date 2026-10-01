"""The benchmark runner on the local stand with a scripted model: generation, three clean runs,
the injected defect, the v2 layout with self-healing, mutations, the report and the Auto-Pilot gate."""
from __future__ import annotations

import json

from fakes import Resp, last_user_text, ref_for
from helpers import arun
from test_agent_invariants import _agent
from testgen import bench, providers
from testgen.runner import HealChoice


def test_bench_scenario_metrics_and_report(stand, fake_llm, tmp_path):
    site = next(s for s in bench.load_sites() if s["id"] == "stand-list")
    sc = next(x for x in site["scenarios"] if x["id"] == "add-item")
    author = _agent([
        ("fill", {"text": "Молоко", "press_enter": False, "description": "Ввести «Молоко»"}, "Новый пункт"),
        ("click", {"description": "Нажать «Добавить»"}, "Добавить"),
        ("assert_element_text", {"text": "Всего: 3", "description": "В списке 3 пункта"}, "Всего: 3"),
        ("finish", {"status": "passed", "summary": "ok"}, None)])

    def script(kind, kw):
        if kind == "parse" and kw.get("output_format") is HealChoice:
            return Resp(parsed=HealChoice(ref=ref_for(last_user_text(kw).split("Elements:")[1], "Добавить пункт"),
                                          reason="та же кнопка"))
        return author(kind, kw)
    fake_llm.script = script
    project = bench.bench_project("anthropic", "claude-opus-5")
    r = arun(bench.run_scenario(project, site, sc, stand, log=lambda *_: None))
    assert r["authored"] and r["first_try"] and r["clean_passes"] == 3, r
    assert r["catches_defect"] is True               # the "Добавить" click is swallowed: the test fails
    assert r["survives_v2"] is True and r["healed_v2"] >= 1
    assert r["mutation_score"] == 1.0

    result = {"provider": "anthropic", "model": "claude-opus-5", "at": "now", "scenarios": [r],
              "summary": bench.summarize([r])}
    assert result["summary"]["first_try"] == 1.0 and result["summary"]["cost_usd"] > 0
    page = bench.html_report(result)
    assert "Рабочий тест с первой генерации" in page and "stand-list/add-item" in page

    other = tmp_path / "manual.json"
    other.write_text(json.dumps({"approach": "вручную", "summary": {"first_try": 0.9, "minutes": 35}}), "utf-8")
    mine = tmp_path / "studio.json"
    mine.write_text(json.dumps(result), "utf-8")
    cmp = bench.compare([mine, other])
    assert [a["approach"] for a in cmp["approaches"]] == ["anthropic/claude-opus-5", "вручную"]
    assert "вручную" in bench.compare_html(cmp)


def test_bench_result_opens_autopilot(fake_http):
    from fakes import use_providers
    use_providers(fake_http)
    providers.record_bench("fake-giga", "GigaChat-2-Max", {"success": 0.82, "at": 0, "scenarios": 40})
    assert providers.profile({"provider": "fake-giga"})["bench"]["success"] == 0.82
