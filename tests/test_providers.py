"""Language model providers (llm.py, providers/): the agent on GigaChat and an OpenAI-compatible
server, the password invariant for every provider, repair of invalid answers, structured output
without server support, the fallback chain, the Anthropic proxy mode and budgets."""
from __future__ import annotations

import json

import pytest

from fakes import Resp, dump, text, tool
from helpers import arun
from stand import PASSWORD, USERNAME
from test_agent_invariants import _agent, _run
from testgen import exporters, llm, projects, providers
from testgen.agent import StudioSession
from testgen.providers import base
from testgen.providers.openai_compat import text_tool_call, to_messages
from testgen.runner import FailureAnalysis

LOGIN_PLAN = [
    ("fill", {"text": "{{username}}", "press_enter": False, "description": "Ввести логин"}, "Логин"),
    ("fill", {"text": "{{password}}", "press_enter": False, "description": "Ввести пароль"}, "Пароль"),
    ("click", {"description": "Нажать «Войти»"}, "Войти"),
    ("assert_element_text", {"text": "Всего: 2", "description": "В списке два пункта"}, "Всего: 2"),
    ("finish", {"status": "passed", "summary": "Вошли, список открыт"}, None),
]


def _on(project: dict, provider: str, **authoring) -> dict:
    p = projects.get(project["id"])
    p["pipeline"]["authoring"].update(provider=provider, **authoring)
    return projects.update(p["id"], {"pipeline": p["pipeline"]})


@pytest.mark.parametrize("provider", ["fake-openai", "fake-giga"])
def test_agent_on_other_providers_and_the_password_stays_home(stand, project, fake_http, provider):
    from fakes import use_providers
    use_providers(fake_http, bench={"success": 0.9})
    fake_http.script = _agent(LOGIN_PLAN)
    p = _on(project, provider)
    s = StudioSession(p, "Вход", f"{stand.url}/login.html", "Войти и увидеть список",
                      credentials={"username": USERNAME, "password": PASSWORD})
    t, _ = arun(_run(s, s.save))
    assert s.status == "done" and s.finish_status == "passed", s.chat
    assert [x["action"] for x in t["steps"]] == ["navigate", "fill", "fill", "click", "assert_element_text"]
    assert t["steps"][2]["value"] == "{{password}}"
    everywhere = "\n".join(fake_http.bodies) + dump(s.messages) + json.dumps(t, ensure_ascii=False) \
        + exporters.to_playwright(t)
    assert PASSWORD not in everywhere
    assert USERNAME in "\n".join(fake_http.bodies)            # the login itself may be shown
    assert t["authoring_usage"]["cost_rub"] > 0 and t["authoring_stats"]["model"] == f"{provider}/" + \
        providers.resolve(p["pipeline"]["authoring"])[1]
    if provider == "fake-giga":
        assert fake_http.uploads >= 1                          # screenshots go through /files
        assert fake_http.states                                # functions_state_id is sent back
        raw = fake_http.calls[-1][1]["raw"]
        assert sum(1 for m in raw["messages"] if m.get("attachments")) <= 1    # one image per chat
    else:
        raw = fake_http.calls[1][1]["raw"]
        assert raw["tools"][0]["type"] == "function" and raw["parallel_tool_calls"] is False
        assert any(m["role"] == "tool" for m in raw["messages"])


def test_autopilot_needs_benchmark_success_for_other_models(project, fake_http):
    from fakes import use_providers
    use_providers(fake_http, bench={"success": 0.4})
    s = StudioSession(_on(project, "fake-openai"), "x", project["base_url"], "x")
    s.set_autopilot(True)
    assert not s.autopilot and "бенчмарке" in s.chat[-1]["text"] and "40%" in s.chat[-1]["text"]
    s2 = StudioSession(_on(project, "fake-openai", autopilot_min_success=30), "x", project["base_url"], "x")
    assert s2.autopilot_allowed()[0]
    # Weaker models get the compact prompt with example turns and text mode when configured.
    s3 = StudioSession(_on(project, "fake-openai", screenshots="on_request"), "x", project["base_url"], "x")
    assert "ONE tool call per turn" in s3.system and "Примеры ходов" in s3.system
    assert any(t["name"] == "look" for t in s3.tools)


def test_invalid_answers_are_repaired_then_the_agent_stops(stand, project, fake_http):
    from fakes import latest_page, ref_for, use_providers
    use_providers(fake_http, bench={"success": 1.0})
    answers = iter([
        tool("click", ref="e999", description="Нет такого"),               # not on the page
        tool("clik", ref="e1", description="опечатка"),                    # unknown tool
        "valid",
        tool("click", ref="e998", description="снова"), tool("click", ref="e997", description="и снова"),
        tool("click", ref="e996", description="и опять"),
    ])

    def script(kind, kw):
        a = next(answers)
        if a == "valid":
            return tool("click", ref=ref_for(latest_page(kw), "Войти"), description="Нажать «Войти»")
        return a
    fake_http.script = script
    s = StudioSession(_on(project, "fake-openai"), "Вход", f"{stand.url}/login.html", "Нажать войти")
    status = arun(_run(s, lambda: s.status))
    assert [x["description"] for x in s.steps][1:] == ["Нажать «Войти»"]     # invalid calls never ran
    assert status == "idle" and "некорректный шаг" in s.chat[-1]["text"]
    errors = [m["content"] for _, kw in fake_http.calls for m in kw["messages"] if "Invalid call" in m["content"]]
    assert any("e999" in e for e in errors) and any("Unknown tool 'clik'" in e for e in errors)


def test_structured_output_by_prompt_with_one_retry(fake_http):
    from fakes import use_providers
    use_providers(fake_http)
    answers = iter([text("Думаю, это дефект."), Resp(parsed=FailureAnalysis(verdict="product_bug", summary="s",
                                                                           suggestion="x"))])
    fake_http.script = lambda kind, kw: next(answers)
    reply = arun(llm.parse({"provider": "fake-openai"}, system="triage", messages=[{"role": "user", "content": "?"}],
                           schema=FailureAnalysis))
    assert reply.parsed.verdict == "product_bug"
    assert "JSON schema" in fake_http.calls[0][1]["raw"]["messages"][0]["content"]
    assert "not valid" in fake_http.calls[1][1]["messages"][-1]["content"]

    # GigaChat: a forced call of the "answer" function.
    fake_http.script = lambda kind, kw: Resp(parsed=FailureAnalysis(verdict="flaky", summary="s", suggestion=""))
    reply = arun(llm.parse({"provider": "fake-giga"}, system="triage", messages=[{"role": "user", "content": "?"}],
                           schema=FailureAnalysis))
    assert reply.parsed.verdict == "flaky" and fake_http.calls[-1][0] == "parse"


def test_fallback_chain(fake_http):
    from fakes import use_providers
    use_providers(fake_http)
    s = providers.settings()
    s["fallbacks"] = ["fake-giga"]
    providers.save_settings(s)
    fake_http.fail_first = 1
    fake_http.script = lambda kind, kw: text("ответ")
    reply = arun(llm.chat({"provider": "fake-openai"}, system="s", messages=[{"role": "user", "content": "hi"}]))
    assert reply.provider == "fake-giga" and reply.text == "ответ"


def test_anthropic_proxy_mode_switches_beta_features_off(monkeypatch, fake_llm):
    monkeypatch.setenv("TESTGEN_LLM_BASE_URL", "http://127.0.0.1:8090")
    providers._instances.clear()
    assert providers.settings()["providers"][0]["features"] == []
    msgs = [{"role": "user", "content": [{"type": "text", "text": "URL: a\n" + "x" * 500},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                      "data": "AAAA"}}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": "latest"}]}]
    arun(llm.chat({}, system="rules", messages=msgs, tools=[], keep_images=1, cache_all=True))
    kw = fake_llm.calls[-1][1]
    assert isinstance(kw["system"], str) and "context_management" not in kw and "betas" not in kw
    assert "fallbacks" not in kw and "output_config" not in kw and "cache_control" not in kw
    assert kw["messages"][0]["content"][1] == {"type": "text", "text": base.REMOVED_IMAGE}
    monkeypatch.setenv("TESTGEN_LLM_FEATURES", "cache,effort")
    assert set(providers.settings()["providers"][0]["features"]) == {"cache", "effort"}
    providers._instances.clear()


def test_session_job_and_month_budgets(fake_http, project):
    from fakes import use_providers
    use_providers(fake_http)
    fake_http.script = lambda kind, kw: text("ok")
    stage = {"provider": "fake-openai"}
    msgs = [{"role": "user", "content": "hi"}]
    warned = []
    u = llm.Usage(0.2, "RUB", "сессии")          # a call costs 0.115 ₽ (1000 in, 50 out at 100/300 ₽ per M)
    u.on_warn = warned.append
    arun(llm.chat(stage, system="s", messages=msgs, usage=u))
    assert not warned
    arun(llm.chat(stage, system="s", messages=msgs, usage=u))
    assert len(warned) == 1 and "80%" in warned[0]
    with pytest.raises(llm.BudgetExceeded):
        arun(llm.chat(stage, system="s", messages=msgs, usage=u))

    p = projects.get(project["id"])
    p["pipeline"]["budget"].update(month=0.2, currency="RUB")
    projects.update(p["id"], {"pipeline": p["pipeline"]})
    for _ in range(2):
        arun(llm.chat(stage, system="s", messages=msgs, project_id=p["id"], stage_name="authoring"))
    with pytest.raises(llm.BudgetExceeded, match="месячный"):
        arun(llm.chat(stage, system="s", messages=msgs, project_id=p["id"], stage_name="authoring"))
    report = llm.ledger_report(p["id"])
    assert report["rows"][0]["stage"] == "authoring" and report["rows"][0]["requests"] == 2
    assert report["total_rub"] == pytest.approx(0.23, abs=0.001) and report["total_usd"] > 0


def test_history_trimming_and_wire_formats():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
    msgs = [{"role": "user", "content": [{"type": "text", "text": "start"}, img]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "click", "input": {"ref": "e1"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": [{"type": "text", "text": "done"}, img]}]}]
    trimmed = base.trim_history(msgs, 1)
    assert trimmed[0]["content"][1]["text"] == base.REMOVED_IMAGE and trimmed[2] == msgs[2]
    wire = to_messages("sys", msgs)
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool", "user"]
    assert wire[3]["content"] == "done" and wire[4]["content"][1]["type"] == "image_url"
    assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {"ref": "e1"}

    tools = [{"name": "click", "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}},
                                                "required": ["ref"]}}]
    call = text_tool_call('Кликаю <tool_call>{"name": "click", "arguments": {"ref": "e5"}}</tool_call>', tools)
    assert call["name"] == "click" and call["input"] == {"ref": "e5"}
    assert base.check_call({"name": "click", "input": {}}, tools).startswith("Missing")
    assert base.check_call({"name": "click", "input": base.loads_args("{bad json")}, tools).startswith("The tool")
    assert base.check_call({"name": "click", "input": {"ref": "e1"}}, tools) == ""
    assert base.plain_schema({"type": "object", "additionalProperties": False, "strict": True}) == {"type": "object"}
