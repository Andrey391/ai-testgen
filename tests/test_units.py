"""Pure-Python units: pipeline settings, test data placeholders, masking, tags, flakiness."""
from __future__ import annotations

import json
import uuid

import pytest

from testgen import projects, runs, storage, testdata, traffic
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


def test_own_gateway_gets_the_key_as_bearer_too():
    from testgen import llm
    headers = llm.make_client("sk-test", "https://gw.example").auth_headers
    assert headers["X-Api-Key"] == "sk-test" and headers["Authorization"] == "Bearer sk-test"


def test_model_comes_from_the_project_settings():
    from types import SimpleNamespace

    from testgen import llm, projects
    p = projects.create(f"Модель {uuid.uuid4().hex[:6]}")
    with pytest.raises(llm.NotConfigured):
        llm.model(p["id"])
    projects.update_llm(p["id"], {"model": "model-a", "effort": "high", "base_url": "https://gw.example/"},
                        api_key="sk-test")
    assert projects.llm_key(p["id"]) == "sk-test"
    assert "sk-test" not in json.dumps(projects.get(p["id"]))      # the key stays in secrets/
    m = llm.model(p["id"])
    assert m.params == {"model": "model-a", "fallbacks": "default", "output_config": {"effort": "high"}}
    assert (m.api_key, m.base_url) == ("sk-test", "https://gw.example")
    # A stage overrides the model and effort.
    assert llm.model(p["id"], {"model": "model-b", "effort": "low"}).params["model"] == "model-b"
    # Effort the model does not take (from the last connection check) is left to the model.
    info = llm.model_info(SimpleNamespace(to_dict=lambda: {"id": "model-a", "display_name": "A", "capabilities": {
        "effort": {"supported": False}, "structured_outputs": {"supported": True},
        "image_input": {"supported": True}, "context_management": {"supported": False}}}))
    assert info["efforts"] == [] and info["missing"] == ["context_management"]
    pr = projects.get(p["id"])
    pr["llm"]["models"] = [info]
    projects.save(pr)
    assert "output_config" not in llm.model(p["id"]).params
    # A new key or address makes the check stale.
    projects.update_llm(p["id"], {"base_url": ""})
    assert projects.get(p["id"])["llm"]["models"] == []


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
    import anthropic
    import httpx

    from testgen.providers.anthropic import NO_CREDITS, error_text

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def err(status, kind, message):
        body = {"type": "error", "error": {"type": kind, "message": message}}
        return anthropic.APIStatusError(message, response=httpx.Response(status, request=req), body=body)

    low = err(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API.")
    assert error_text(low) == NO_CREDITS
    assert error_text(err(402, "billing_error", "Payment required")) == NO_CREDITS
    assert error_text(err(400, "invalid_request_error", "messages: field required")).startswith("Ошибка API ИИ 400")


def test_context_editing_failure_falls_back_to_trimming():
    """A gateway (LiteLLM) whose context_management polyfill fails on images: the request is
    repeated with the old screenshots cut by the studio, and later ones skip context editing."""
    import asyncio
    import types

    import anthropic
    import httpx

    from testgen.providers import anthropic as prov
    from testgen.providers.base import Request

    req = httpx.Request("POST", "http://gateway.local/v1/messages")
    body = {"type": "error", "error": {"type": "api_error", "message":
            "context_management polyfill failed: Invalid content item type: image."}}
    calls = []

    async def create(**kw):
        calls.append(kw)
        if "context_management" in kw:
            raise anthropic.APIStatusError(body["error"]["message"],
                                           response=httpx.Response(500, request=req), body=body)
        return types.SimpleNamespace(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
                                     model=kw["model"], usage=None)

    client = types.SimpleNamespace(base_url="http://gateway.local/" + uuid.uuid4().hex,
                                   beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create)))
    p = prov.AnthropicProvider(client, set(prov.FEATURES))
    shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "step"}, shot]} for _ in range(4)]
    r = Request(model="deepseek-coder", system="s", messages=messages, keep_images=1)

    assert asyncio.run(p.chat(r)).text == "ok"
    assert len(calls) == 2 and "context_management" not in calls[1]
    assert json.dumps(calls[1]["messages"]).count('"image"') == 1     # old screenshots cut out
    asyncio.run(p.chat(r))
    assert len(calls) == 3 and "context_management" not in calls[2]   # no failing first try any more


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