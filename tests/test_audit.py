"""Audit log (stage 5.4): records of API changes, export to a SIEM (the hash chain: tests/test_scale.py)."""
from __future__ import annotations

import json
import socket
import uuid

from fastapi.testclient import TestClient

from testgen import audit, auth, projects, storage
from testgen.steps import new_step


def test_copies_to_a_file_and_syslog(tmp_path, monkeypatch):
    copy = tmp_path / "siem" / "audit.jsonl"
    monkeypatch.setenv("TESTGEN_AUDIT_FILE", str(copy))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    monkeypatch.setenv("TESTGEN_AUDIT_SYSLOG", f"127.0.0.1:{sock.getsockname()[1]}")
    try:
        rec = audit.record("run.start", user="bob", project_id="p2", via="cli")
        msg = sock.recv(65536).decode()
    finally:
        sock.close()
    assert json.loads(copy.read_text("utf-8"))["hash"] == rec["hash"]
    assert msg.startswith("<3") and "testgen-audit:" in msg and rec["hash"] in msg      # facility auth (4)


def test_api_changes_are_recorded(monkeypatch):
    import server
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ADMINS", {"root"})
    for u in ("alice", "vic", "root"):
        auth.set_password(u, "password-123")
    p = projects.create("Журнал " + uuid.uuid4().hex[:6], owner="alice")
    projects.set_access(p["id"], "members", {"alice": "owner", "vic": "viewer"})
    step = new_step("click", "Кнопка", locator=[{"kind": "css", "value": "#a"}])
    t = storage.save({"project_id": p["id"], "name": "Т", "url": "http://x", "scenario": "", "steps": [step],
                      "heal_proposals": [{"id": "hp1", "step_id": step["id"], "old": step["locator"],
                                          "new": [{"kind": "css", "value": "#b"}]}]})

    def client(user):
        return TestClient(server.app, base_url="http://127.0.0.1:8765", cookies={auth.COOKIE: auth.make_token(user)})
    alice, vic, root = client("alice"), client("vic"), client("root")
    anon = TestClient(server.app, base_url="http://127.0.0.1:8765")

    assert anon.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    assert alice.put(f"/api/tests/{t['id']}", json={"name": "Т2"}).status_code == 200
    assert alice.post(f"/api/tests/{t['id']}/proposals/hp1/accept").status_code == 200
    assert vic.put(f"/api/tests/{t['id']}", json={"name": "x"}).status_code == 403
    alice.put(f"/api/projects/{p['id']}/access", json={"members": {"alice": "owner", "vic": "editor"}})
    alice.put(f"/api/projects/{p['id']}/credentials", json={"username": "u", "password": "Very-Secret-777"})
    alice.get(f"/api/projects/{p['id']}")                     # reading is not recorded

    log = {r["action"]: r for r in reversed(alice.get(f"/api/projects/{p['id']}/audit").json())
           if r["user"] == "alice"}
    assert log["test.update"]["user"] == "alice" and log["test.update"]["target"] == {"tid": t["id"]}
    assert log["heal.accept"]["status"] == 200 and log["heal.accept"]["project_id"] == p["id"]
    assert log["project.access"]["details"]["members"] == {"vic": ["viewer", "editor"]}
    assert "project.credentials" in log and not any(r["action"].startswith("GET") for r in log.values())
    denied = [r for r in audit.read(project_id=p["id"]) if r["user"] == "vic"]
    assert denied[0]["status"] == 403
    failed = next(r for r in audit.read(action="auth.login"))
    assert failed["user"] == "alice" and failed["status"] == 401
    month = audit.read(limit=1)[0]["at"][:7]
    assert "Very-Secret-777" not in audit.export(month) and "heal.accept" in audit.export(month)

    # Who may read it: owners their project, admins everything.
    assert vic.get(f"/api/projects/{p['id']}/audit").status_code == 403
    assert vic.get("/api/audit").status_code == 403
    assert root.get("/api/audit/verify").json()["ok"]
    assert "heal.accept" in root.get(f"/api/audit/export?month={month}").text
