"""Pure-Python units: pipeline settings, test data placeholders, masking, tags, flakiness."""
from __future__ import annotations

import json
import uuid

import pytest

from testgen import defects, notify, projects, runs, storage, testdata, traffic
from testgen.browser import expand, group_candidates


def test_normalize_pipeline_merges_defaults_and_drops_unknown():
    p = projects.normalize_pipeline({"run": {"keep_runs": "5", "bogus": 1, "heal_mode": "yolo",
                                             "trace": "always", "visual_threshold": "2.5", "parallel": 99},
                                     "verify": {"enabled": 1, "mutants": 50},
                                     "explore": {"max_pages": 1000, "max_depth": 0},
                                     "nope": {"x": 1}})
    assert "nope" not in p and "bogus" not in p["run"]
    assert p["run"]["keep_runs"] == 5
    assert p["run"]["heal_mode"] == "review"          # unknown value -> default
    assert p["run"]["trace"] == "always"
    assert p["run"]["visual_threshold"] == 2.5
    assert p["run"]["parallel"] == 8                  # clamped
    assert p["verify"]["enabled"] is True and p["verify"]["mutants"] == 10
    assert p["explore"]["max_pages"] == 100 and p["explore"]["max_depth"] == 1
    assert p["authoring"]["skills"] == projects.DEFAULT_PIPELINE["authoring"]["skills"]   # untouched stage: defaults


def test_normalize_pipeline_defaults_are_complete():
    p = projects.normalize_pipeline(None)
    assert set(p) == set(projects.DEFAULT_PIPELINE)
    assert p["run"]["heal_mode"] == "review" and p["run"]["retry_failed"] is True
    assert p["verify"]["enabled"] is False


def test_testdata_is_stable_within_a_run_and_new_between_runs():
    run1, run2 = testdata.DataValues(), testdata.DataValues()
    a = expand({}, "{{faker.email}} / {{faker.email}} / {{unique}}", run1)
    email, again, unique = a.split(" / ")
    assert email == again and email.endswith("@example.com") and unique in email
    assert expand({}, "{{faker.email}}", run2) != email
    assert len(expand({}, "{{today}}", run1)) == 10
    assert expand({}, "{{faker.first_name}}", run1)


def test_testdata_unknown_placeholder_and_missing_credentials():
    with pytest.raises(ValueError):
        expand({}, "{{faker.credit_card}}", testdata.DataValues())
    with pytest.raises(ValueError):
        expand({}, "{{password}}")
    assert expand({"username": "u", "password": "p"}, "{{username}}:{{password}}") == "u:p"


def test_group_candidates_strip_the_position():
    e = {"testid": "item", "testid_attr": "data-testid", "css": "#items > li:nth-of-type(2)"}
    assert group_candidates(e) == [{"kind": "testid", "value": "item"}, {"kind": "css", "value": "#items > li"}]
    assert group_candidates({"css": "#single"}) == []


def test_tags_are_normalized():
    assert storage.normalize_tags(["Smoke", "smoke", "regression", "bad tag!"]) == ["smoke", "regression"]
    assert storage.normalize_tags("smoke, api") == ["smoke", "api"]


def test_flip_rate():
    stable = [{"outcomes": [True]}] * 5
    assert runs.flip_rate(stable) == 0
    alternating = [{"outcomes": [True]}, {"outcomes": [False]}, {"outcomes": [True]}, {"outcomes": [False]}]
    assert runs.flip_rate(alternating) == 1
    flaky_run = [{"outcomes": [True]}, {"outcomes": [False, True]}, {"outcomes": [True]}]
    assert runs.flip_rate(flaky_run) == pytest.approx(2 / 3, abs=0.01)
    assert runs.flip_rate([{"outcomes": [True]}]) is None


def test_traffic_masking():
    creds = {"username": "demo", "password": "s3cret-pass!"}
    e = traffic.mask_entry({
        "method": "POST", "url": "https://app.test/api/login?token=abc&page=2",
        "request_headers": {"content-type": "application/json", "authorization": "Bearer xyz", "Cookie": "sid=1"},
        "post_data": '{"username": "demo", "password": "s3cret-pass!", "api_key": "k-123", "note": "hi"}',
        "status": 200, "response_headers": {"set-cookie": "sid=2", "content-type": "application/json"},
        "mime": "application/json", "body": '{"access_token": "t-9", "user": {"name": "Demo"}}'}, creds)
    assert "s3cret-pass!" not in str(e) and "xyz" not in str(e) and "k-123" not in str(e) and "t-9" not in str(e)
    assert '"password": "{{password}}"' in e["post_data"] and '"username": "{{username}}"' in e["post_data"]
    assert e["request_headers"]["authorization"] == "***" and e["response_headers"]["set-cookie"] == "***"
    assert "token=%2A%2A%2A" in e["url"] or "token=***" in e["url"]
    assert '"note": "hi"' in e["post_data"]
    form = traffic.mask_text("login=demo&password=s3cret-pass%21&x=1", "application/x-www-form-urlencoded", creds)
    assert form == "login={{username}}&password={{password}}&x=1"


def test_usage_scopes_nest():
    from types import SimpleNamespace

    from testgen import llm
    resp = SimpleNamespace(model="some-model", usage=SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=9000))
    prices = {"some-model": [5, 25]}
    own = llm.Usage()
    with llm.usage_scope() as job:
        with llm.usage_scope() as run:
            llm.track(resp, own, prices)
        llm.track(resp)
    assert (own.requests, run.requests, job.requests) == (1, 1, 2)
    d = run.as_dict()
    assert d["cache_hit"] == 0.9 and d["cost_usd"] == round((1000 * 5 + 9000 * 0.5 + 100 * 25) / 1e6, 4)
    assert job.as_dict()["cost_usd"] == round(2 * (1000 * 5 + 9000 * 0.5 + 100 * 25) / 1e6, 4)
    other = llm.Usage()
    llm.track(resp, other)
    assert other.as_dict()["cost_usd"] is None          # no price entered for the model


def test_client_sends_the_key_as_bearer():
    from testgen import llm
    client = llm.make_client("sk-test", "https://gw.example/v1/")
    assert client._http.headers["Authorization"] == "Bearer sk-test" and client.base_url == "https://gw.example/v1"
    with pytest.raises(llm.NotConfigured):          # no address by default: the project names its model's server
        llm.make_client("sk-test", "")


def test_model_comes_from_the_project_settings():
    from testgen import llm, projects
    p = projects.create(f"Модель {uuid.uuid4().hex[:6]}")
    with pytest.raises(llm.NotConfigured):
        llm.model(p["id"])
    projects.update_llm(p["id"], {"model": "model-a", "effort": "high", "base_url": "https://gw.example/"},
                        api_key="sk-test")
    assert projects.llm_key(p["id"]) == "sk-test"
    assert "sk-test" not in json.dumps(projects.get(p["id"]))      # the key stays in secrets/
    m = llm.model(p["id"])
    assert (m.name, m.effort) == ("model-a", "high")
    assert (m.api_key, m.base_url) == ("sk-test", "https://gw.example")
    # A stage overrides the model and effort.
    assert llm.model(p["id"], {"model": "model-b", "effort": "low"}).name == "model-b"
    # Effort the model does not take (from the last connection check) is left to the model.
    info = llm.model_info({"id": "model-a", "display_name": "A", "capabilities": {
        "effort": {"supported": False}, "structured_outputs": {"supported": False},
        "image_input": {"supported": True}}})
    assert info["efforts"] == [] and info["missing"] == ["structured_outputs"]
    assert llm.model_info({"id": "qwen", "object": "model"}) == {"id": "qwen", "name": "qwen", "efforts": None,
                                                                  "missing": []}
    pr = projects.get(p["id"])
    pr["llm"]["models"] = [info]
    projects.save(pr)
    m = llm.model(p["id"])
    assert m.effort == "" and "structured" not in m.features
    # A new key or address makes the check stale.
    projects.update_llm(p["id"], {"base_url": ""})
    assert projects.get(p["id"])["llm"]["models"] == []


def test_project_remembers_the_first_step_after_the_wizard():
    p = projects.create(f"Старт {uuid.uuid4().hex[:6]}")
    assert "start" not in p
    assert projects.update(p["id"], {"start": "studio"})["start"] == "studio"
    assert projects.update(p["id"], {"name": p["name"]})["start"] == "studio"      # kept without the field
    assert projects.update(p["id"], {"start": "elsewhere"})["start"] == ""


def test_har_roundtrip():
    entries = [{"method": "GET", "url": "https://app.test/api/items?q=1", "request_headers": {}, "post_data": "",
                "status": 200, "response_headers": {"content-type": "application/json"}, "mime": "application/json",
                "body": "[]", "at": 0, "step": 2},
               {"method": "GET", "url": "https://cdn.other.io/x.json", "request_headers": {}, "post_data": "",
                "status": 200, "response_headers": {}, "mime": "application/json", "body": "{}", "at": 0, "step": 3}]
    back = traffic.from_har(traffic.to_har(entries, "https://app.test/login"))
    assert [b["step"] for b in back] == [2, 3]
    assert [b["third_party"] for b in back] == [False, True]
    assert traffic.mock_spec(back[0])["url"] == "https://app.test/api/items*"


def test_api_error_text_no_credits():
    from testgen.providers.openai_compat import NO_CREDITS, NO_KEY, ApiError, error_text

    def err(status, code, message):
        return ApiError(status, message, {"error": {"code": code, "message": message}})

    assert error_text(err(429, "insufficient_quota", "You exceeded your current quota.")) == NO_CREDITS
    assert error_text(err(402, "payment_required", "Payment required")) == NO_CREDITS
    assert error_text(err(401, "invalid_api_key", "Incorrect API key")) == NO_KEY
    assert error_text(err(429, "rate_limit", "Slow down")).startswith("API ИИ: превышен лимит")
    assert error_text(err(400, "invalid_request", "messages: field required")).startswith("Ошибка API ИИ 400")


def test_conversation_goes_as_chat_completions():
    """The studio's neutral conversation - tool calls, tool results with screenshots - becomes Chat Completions,
    and an answer with tool calls comes back as tool_use blocks."""
    from testgen.providers.openai_compat import messages_of, reply_of, tools_of

    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    out = messages_of("rules", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "Click."},
                                          {"type": "tool_use", "id": "c1", "name": "click", "input": {"ref": "e1"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": [
            {"type": "text", "text": "OK"}, shot]}, {"type": "text", "text": "URL: /cart"}]},
    ])
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "click", "arguments": '{"ref": "e1"}'}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "OK"}
    assert out[4]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert out[4]["content"][-1] == {"type": "text", "text": "URL: /cart"}
    assert tools_of([{"name": "click", "input_schema": {"type": "object"}}])[0]["function"]["parameters"] \
        == {"type": "object"}

    reply = reply_of({"model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None, "reasoning_content": "hm", "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "fill", "arguments": '{"ref": "e2"}'}},
            {"id": "c3", "type": "function", "function": {"name": "click", "arguments": "{broken"}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}}})
    assert reply.stop == "tool" and [c["input"] for c in reply.tool_calls] == [{"ref": "e2"}, {"__raw__": "{broken"}]
    assert reply.content[0] == {"type": "thinking", "thinking": "hm"}
    assert reply.usage["input_tokens"] == 40 and reply.usage["cache_read_input_tokens"] == 60


class _Gateway:
    """A fake gateway: `refuse(payload)` - the error message for a request it refuses, or None."""

    def __init__(self, refuse, answer="{}"):
        self.base_url = "http://gateway.local/" + uuid.uuid4().hex
        self.refuse, self.answer, self.calls = refuse, answer, []

    async def complete(self, payload, *, stream=False, timeout=0, on_delta=None):
        from testgen.providers.openai_compat import ApiError
        self.calls.append(payload)
        message = self.refuse(payload)
        if message:
            raise ApiError(400, message, {"error": {"message": message, "code": "400"}})
        return {"model": payload["model"], "choices": [{"message": {"content": self.answer}, "finish_reason": "stop"}]}


def test_old_screenshots_are_cut_by_the_studio():
    import asyncio

    from testgen.providers import openai_compat as prov
    from testgen.providers.base import Request

    gw = _Gateway(lambda p: None, "ok")
    p = prov.ChatProvider(gw, set(prov.FEATURES))
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    messages = []
    for _ in range(4):
        messages += [{"role": "user", "content": [{"type": "text", "text": "step"}, shot]},
                     {"role": "assistant", "content": "next"}]
    r = Request(model="m", system="s", messages=messages[:-1], keep_images=1)
    assert asyncio.run(p.chat(r)).text == "ok"
    assert json.dumps(gw.calls[0]["messages"]).count('"type": "image_url"') == 1


def test_tool_choice_refused_by_gateway_is_dropped():
    """LiteLLM over a Responses API: "tool_choice type 'auto' is not supported by this gateway phase" -
    the request is repeated with the tools but without tool_choice, and later ones skip it."""
    import asyncio

    from testgen.providers import openai_compat as prov
    from testgen.providers.base import Request

    gw = _Gateway(lambda p: "tool_choice type 'auto' is not supported by this gateway phase." if "tool_choice" in p
                  else None, "ok")
    p = prov.ChatProvider(gw, set(prov.FEATURES))
    tool = {"name": "click", "description": "c", "input_schema": {"type": "object", "properties": {}}}
    r = Request(model="deepseek-coder", system="s", messages=[{"role": "user", "content": "go"}], tools=[tool])

    assert asyncio.run(p.chat(r)).text == "ok"
    assert len(gw.calls) == 2 and "tool_choice" not in gw.calls[1]
    assert gw.calls[1]["tools"] == prov.tools_of([tool])
    asyncio.run(p.chat(r))
    assert len(gw.calls) == 3 and "tool_choice" not in gw.calls[2]     # no failing first try any more


def test_json_schema_refused_by_gateway_goes_to_prompt():
    """"response_format json_schema is not supported" - structured output is off for the connection and the
    caller is told to put the schema in the prompt (FeatureRefused), not shown the gateway's error."""
    import asyncio

    import pydantic

    from testgen.providers import openai_compat as prov
    from testgen.providers.base import Request

    gw = _Gateway(lambda p: "response_format type 'json_schema' is not supported, only 'text' and 'json_object' "
                            "are allowed." if "response_format" in p else None)

    class Answer(pydantic.BaseModel):
        ok: bool = True

    p = prov.ChatProvider(gw, set(prov.FEATURES))
    r = Request(model="deepseek-coder", system="s", messages=[{"role": "user", "content": "go"}])
    assert p.structured("deepseek-coder")
    with pytest.raises(prov.FeatureRefused):
        asyncio.run(p.parse(r, Answer))
    assert len(gw.calls) == 1 and not p.structured("deepseek-coder")
    assert asyncio.run(p.chat(r)).text == "{}"                         # the schema-in-prompt request goes through


def test_unnamed_refusal_drops_least_needed_part_first():
    """A refusal that names no part ("Unsupported parameter") drops the optional parts one by one, the least needed
    first, and remembers only what had to go for the request to go through."""
    import asyncio

    from testgen.providers import openai_compat as prov
    from testgen.providers.base import Request

    gw = _Gateway(lambda p: "Unsupported parameter in request" if "tool_choice" in p else None)
    p = prov.ChatProvider(gw, set(prov.FEATURES))
    tool = {"name": "click", "input_schema": {"type": "object"}}
    r = Request(model="m", effort="high", system="s", messages=[{"role": "user", "content": "go"}], tools=[tool])
    assert asyncio.run(p.chat(r)).text == "{}"
    assert "reasoning_effort" in gw.calls[0] and "reasoning_effort" not in gw.calls[1]
    assert "parallel_tool_calls" not in gw.calls[2] and "tool_choice" not in gw.calls[3]
    assert prov.REFUSED[p._key("m")] == {"effort", "parallel", "tool_choice"}
    asyncio.run(p.chat(r))
    assert len(gw.calls) == 5                                          # no failing tries any more


def test_other_errors_drop_nothing():
    """A 400 that is not a refusal of a part is not retried; a refusal that stays after every part was dropped
    turns nothing off for the connection."""
    import asyncio

    from testgen.providers import openai_compat as prov
    from testgen.providers.base import ProviderError, Request

    gw = _Gateway(lambda p: "messages: field required")
    p = prov.ChatProvider(gw, set(prov.FEATURES))
    r = Request(model="m", effort="high", system="s", messages=[{"role": "user", "content": "go"}])
    with pytest.raises(ProviderError):
        asyncio.run(p.chat(r))
    assert len(gw.calls) == 1

    gw = _Gateway(lambda p: "images are not supported by this model")
    p = prov.ChatProvider(gw, set(prov.FEATURES))
    with pytest.raises(ProviderError):
        asyncio.run(p.chat(r))
    assert len(gw.calls) > 1 and not prov.REFUSED.get(p._key("m"))


def test_empty_assistant_turn_gets_placeholder():
    """A model that answered with nothing: its turn must not go back to the API empty
    (LiteLLM: "assistant must provide content, reasoning_content or tool_calls")."""
    from testgen.providers.base import EMPTY_REPLY, fill_empty

    call = {"type": "tool_use", "id": "t1", "name": "click", "input": {}}
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": []},
                {"role": "user", "content": "go on"},
                {"role": "assistant", "content": [{"type": "text", "text": ""}, call]},
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
    out = fill_empty(messages)

    assert out[1]["content"] == [{"type": "text", "text": EMPTY_REPLY}]
    assert out[3]["content"] == [call]
    assert out[4]["content"] == EMPTY_REPLY
    assert out[5] is messages[5] and messages[1]["content"] == []      # the history itself is not changed


def test_fix_task_names_failed_step_and_rest():
    """«Разобрать с агентом»: the task names the failed step, its error, the analysis and the steps after it."""
    from testgen.agent import FIX_TASK, fix_task

    steps = [{"action": "navigate", "description": "Open", "value": "https://x"},
             {"action": "click", "description": "Click Save", "value": ""},
             {"action": "assert_text_present", "description": "Saved", "value": "Saved"}]
    text = fix_task(steps, 1, {"error": "locator not found"},
                    {"summary": "the button was renamed", "suggestion": "use Submit"})

    assert text.startswith(FIX_TASK)
    assert "Failed step: 2. [click] Click Save" in text and "locator not found" in text
    assert "the button was renamed" in text and "use Submit" in text
    assert "3. [assert_text_present] Saved | value: Saved" in text and "1. [navigate]" not in text
    assert "last step" in fix_task(steps, 2, {"error": "no text"})

def test_explorer_scope_is_start_page_and_children():
    """Planner explores only the given page and its child pages."""
    from testgen.explorer import _in_scope

    start = "https://app.test/catalog"
    assert _in_scope("https://app.test/catalog", start)
    assert _in_scope("https://app.test/catalog/", start)
    assert _in_scope("https://app.test/catalog/42?tab=info", start)
    assert not _in_scope("https://app.test/catalog-old", start)
    assert not _in_scope("https://app.test/admin", start)
    assert not _in_scope("https://app.test/", start)
    assert not _in_scope("https://other.test/catalog/1", start)
    assert _in_scope("https://app.test/anything", "https://app.test/")             # from the root: the whole site
    assert _in_scope("https://app.test/app/list.html", "https://app.test/app/index.html")
    assert not _in_scope("https://app.test/list.html", "https://app.test/app/index.html")

def test_explorer_logs_in_with_the_login_test_and_says_when_it_stopped_at_login(project, save_test):
    """Planner replays the project's login test when explore.login_test is empty; a map of the login page alone
    says the application is behind a login."""
    from testgen import explorer

    assert explorer.login_test(project) is None
    login = save_test("Вход", [], role="login")
    save_test("Каталог", [])
    assert explorer.login_test(projects.get(project["id"]))["id"] == login["id"]
    p = projects.get(project["id"])
    p["pipeline"]["explore"]["login_test"] = "Каталог"
    assert explorer.login_test(p)["name"] == "Каталог"            # the one named in the settings wins

    form = {"forms": [{"fields": [{"type": "text"}, {"type": "password"}]}]}
    assert explorer.login_wall({"pages": [{"url": "/login"} | form]})
    assert not explorer.login_wall({"pages": [{"url": "/login"}]})
    assert not explorer.login_wall({"pages": [{"url": f"/{i}"} | form for i in range(3)]})   # got past it
    assert not explorer.login_wall({"pages": []})

def test_record_and_play_acts_as_a_user_would():
    """Record & Play: the action follows from the element clicked; a field and a list ask for the value."""
    from testgen.agent import VALUE_ACTIONS, auto_action
    assert auto_action({"tag": "button", "role": "button"}) == "click"
    assert auto_action({"tag": "a", "role": "link"}) == "click"
    assert auto_action({"tag": "input", "type": "checkbox", "role": "checkbox"}) == "click"
    assert auto_action({"tag": "input", "type": "submit"}) == "click"
    assert auto_action({"tag": "input", "type": ""}) == "fill"
    assert auto_action({"tag": "input", "type": "email", "role": "textbox"}) == "fill"
    assert auto_action({"tag": "textarea"}) == "fill"
    assert auto_action({"tag": "div", "role": "textbox"}) == "fill"
    assert auto_action({"tag": "select", "role": "combobox"}) == "select_option"
    assert auto_action({"tag": "input", "type": "file"}) == "upload_file"
    assert {"fill", "select_option", "upload_file"} == set(VALUE_ACTIONS)


def test_notification_links_lead_to_the_screens(monkeypatch):
    """Links of notifications and defects are addresses of the frontend's router: the project, then the screen."""
    p = {"id": "p1", "name": "Shop", "notify": {"events": {"suite": "always", "proposals": True}}}
    suite = {"id": "s1", "passed": False, "tags": ["smoke"], "summary": {"failed": 12, "total": 12},
             "items": [{"name": f"Тест {n}", "status": "failed", "quarantined": False, "run_id": f"r{n}",
                        "failed_step": "", "error": "упал"} for n in range(12)]}
    monkeypatch.delenv("TESTGEN_PUBLIC_URL", raising=False)
    assert "href" not in notify.message(p, "suite", suite=suite)[1]           # no address of the studio - no links
    assert notify.public_view(p)["links"] is False

    monkeypatch.setenv("TESTGEN_PUBLIC_URL", "https://tg.test/")
    text = notify.message(p, "suite", suite=suite)[1]
    assert text.endswith('<a href="https://tg.test/#/p/p1/suites/s1">Открыть в студии</a>')
    runs_linked = [f"https://tg.test/#/p/p1/runs/r{n}" for n in range(12) if f"/runs/r{n}\"" in text]
    assert len(runs_linked) == 10                                             # the failed runs, up to 10
    assert "#suite=" not in text and "#test=" not in text
    assert 'href="https://tg.test/#/p/p1/tests/t1"' in notify.message(p, "proposals", test={"id": "t1", "name": "T"},
                                                                       run={"proposals": 1})[1]
    assert 'href="https://tg.test/#/p/p1/tests"' in notify.message(p, "streak", n=3)[1]
    assert notify.public_view(p)["links"] is True
    report = defects.draft({"name": "T", "steps": []}, {"id": "r1", "project_id": "p1", "results": []})
    assert "https://tg.test/#/p/p1/runs/r1" in report["text"]
