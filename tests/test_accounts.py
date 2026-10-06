"""Accounts of the application under test: any number per project, each with extra login
parameters ({{auth.name}}); secret ones stay home like the password."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import auth, exporters, projects, storage, testdata, traffic, vault
from testgen.agent import StudioSession
from testgen.browser import expand
from testgen.steps import new_step

OTP = "731905"


def _project() -> dict:
    return projects.create("Учётки " + uuid.uuid4().hex[:6], base_url="http://127.0.0.1:1")


def test_old_single_login_becomes_the_default_account():
    p = _project()
    vault.save(projects.secrets_kind(p["id"]), "app", {"username": "old", "password": "old-pw"})
    [acc] = projects.accounts_view(p["id"])
    assert acc["default"] and acc["username"] == "old" and acc["has_password"]
    assert projects.app_credentials(p["id"]) == {"username": "old", "password": "old-pw"}
    # The wizard's login changes that account, it does not add another one.
    projects.set_app_credentials(p["id"], "old2")
    assert [a["username"] for a in projects.accounts_view(p["id"])] == ["old2"]
    assert projects.app_credentials(p["id"])["password"] == "old-pw"


def test_many_accounts_with_parameters_and_a_default():
    p = _project()
    admin = projects.save_account(p["id"], {"name": "Админ", "username": "admin", "password": "a-pw"})
    buyer = projects.save_account(p["id"], {"name": "Покупатель", "username": "buyer", "password": "b-pw",
                                            "params": [{"name": "otp", "value": OTP, "secret": True},
                                                       {"name": "tenant", "value": "shop-1"}]})
    for i in range(3):
        projects.save_account(p["id"], {"username": f"user{i}"})
    assert len(projects.accounts_view(p["id"])) == 5
    assert projects.app_credentials(p["id"])["username"] == "admin"       # the first one is the default
    c = projects.account_credentials(p["id"], buyer["id"])
    assert testdata.auth_params(c) == {"otp": OTP, "tenant": "shop-1"}

    # The API never returns a secret parameter; sent back empty it keeps its value.
    view = next(a for a in projects.accounts_view(p["id"]) if a["id"] == buyer["id"])
    assert {x["name"]: x["value"] for x in view["params"]} == {"otp": "", "tenant": "shop-1"}
    projects.save_account(p["id"], {"params": view["params"], "default": True}, buyer["id"])
    assert testdata.auth_params(projects.app_credentials(p["id"]))["otp"] == OTP
    assert projects.accounts_view(p["id"], full=False)[0].keys() == {"id", "name", "default"}

    with pytest.raises(ValueError):
        projects.save_account(p["id"], {"params": [{"name": "1bad", "value": "x"}]}, admin["id"])
    with pytest.raises(ValueError):
        projects.save_account(p["id"], {"params": [{"name": "a", "value": "1"}, {"name": "a", "value": "2"}]},
                              admin["id"])

    # A test runs with its account; a deleted account falls back to the default one.
    t = {"id": "t1", "project_id": p["id"], "account": admin["id"]}
    assert storage.credentials(t)["username"] == "admin"
    assert projects.delete_account(p["id"], admin["id"])
    assert storage.credentials(t)["username"] == "buyer"


def test_auth_placeholder_is_substituted_and_secret_parameters_are_masked():
    c = {"username": "u", "password": "pw-123456",
         "params": [{"name": "otp", "value": OTP, "secret": True}, {"name": "tenant", "value": "shop-1"}]}
    assert expand(c, "{{auth.otp}}/{{auth.tenant}}") == f"{OTP}/shop-1"
    with pytest.raises(ValueError, match="pin"):
        expand(c, "{{auth.pin}}")
    assert testdata.keys("{{auth.otp}} {{unique}}") == ["unique"]
    assert set(testdata.secret_values(c)) == {"pw-123456", OTP}

    # Steps: the agent's session puts the placeholder back.
    s = StudioSession.__new__(StudioSession)
    s.credentials = c
    step = new_step("fill", f"Ввести код {OTP}", OTP)
    s._mask(step)
    assert step["value"] == "{{auth.otp}}" and OTP not in step["description"]

    # Recorded traffic.
    entry = traffic.mask_entry({"method": "POST", "url": f"http://x/login?code={OTP}", "request_headers":
                                {"content-type": "application/json"}, "post_data": f'{{"code": "{OTP}"}}',
                                "status": 200, "response_headers": {}, "mime": "application/json", "body": "",
                                "at": 0}, c)
    assert OTP not in entry["url"] + entry["post_data"] and "{{auth.otp}}" in entry["post_data"]


def test_export_reads_login_parameters_from_the_environment():
    steps = [new_step("fill", "Код", "{{auth.otp}}")]
    steps[0]["locators"] = [{"kind": "label", "value": "Код"}]
    code = exporters.to_playwright({"id": "t", "name": "OTP", "url": "http://x", "steps": steps})
    assert "credentials['auth.otp']" in code and OTP not in code
    ns: dict = {}
    exec(exporters.FIXTURE_CREDENTIALS.split("\n\n\n@pytest")[0],
         {"os": __import__("os"), "re": __import__("re"), "pytest": pytest, "totp": testdata.totp}, ns)
    import os
    os.environ["TESTGEN_AUTH_OTP"] = OTP
    try:
        assert ns["_Credentials"]()["auth.otp"] == OTP
    finally:
        del os.environ["TESTGEN_AUTH_OTP"]
    assert testdata.env_name("auth.otp") == "TESTGEN_AUTH_OTP"


def test_accounts_api_owner_edits_viewer_sees_names_only(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ADMINS", {"root"})
    for u in ("ann", "val"):
        auth.set_password(u, "password-123")
    p = projects.create("API " + uuid.uuid4().hex[:6], owner="ann")
    projects.set_access(p["id"], "members", {"ann": "owner", "val": "viewer"})

    def client(user):
        return TestClient(server.app, base_url="http://127.0.0.1:8765",
                          cookies={auth.COOKIE: auth.make_token(user)})

    ann, val = client("ann"), client("val")
    r = ann.post(f"/api/projects/{p['id']}/accounts", json={
        "name": "Менеджер", "username": "mgr", "password": "m-pw-1",
        "params": [{"name": "otp", "value": OTP, "secret": True}]})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert OTP not in r.text and "m-pw-1" not in r.text
    assert ann.post(f"/api/projects/{p['id']}/accounts", json={"params": [{"name": "with space"}]}).status_code == 400
    assert val.post(f"/api/projects/{p['id']}/accounts", json={"name": "x"}).status_code == 403

    seen = val.get(f"/api/projects/{p['id']}").json()["accounts"]
    assert seen == [{"id": aid, "name": "Менеджер", "default": True}]

    t = storage.save({"project_id": p["id"], "name": "T", "url": "http://x", "scenario": "", "steps": []})
    second = ann.post(f"/api/projects/{p['id']}/accounts", json={"name": "Второй", "username": "two"}).json()["id"]
    assert ann.put(f"/api/tests/{t['id']}/credentials", json={"account": second}).status_code == 200
    assert storage.credentials(storage.load(t["id"]))["username"] == "two"
    assert ann.put(f"/api/tests/{t['id']}/credentials", json={"account": "nope"}).status_code == 400
    assert ann.delete(f"/api/projects/{p['id']}/accounts/{second}").status_code == 200
    assert storage.credentials(storage.load(t["id"]))["username"] == "mgr"
