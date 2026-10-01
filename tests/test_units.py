"""Pure-Python units: pipeline settings, test data placeholders, masking, tags, flakiness."""
from __future__ import annotations

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
    assert p["authoring"]["skills"] == ["ui-test-authoring"]   # untouched stage keeps defaults


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
    resp = SimpleNamespace(model="test-model", usage=SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=9000))
    own = llm.Usage()
    with llm.usage_scope() as job:
        with llm.usage_scope() as run:
            llm.track(resp, own)
        llm.track(resp)
    assert (own.requests, run.requests, job.requests) == (1, 1, 2)
    d = run.as_dict()
    assert d["cache_hit"] == 0.9 and d["cost_usd"] == round((1000 * 5 + 9000 * 0.5 + 100 * 25) / 1e6, 4)


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
