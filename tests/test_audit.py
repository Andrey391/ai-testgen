"""Audit log (stage 5.4): the hash chain, records of API changes, export to a SIEM."""
from __future__ import annotations

import json
import socket
import uuid

import pytest
from fastapi.testclient import TestClient

from testgen import audit, auth, fs, projects, storage
from testgen.steps import new_step


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DATA", tmp_path)
    return tmp_path / "audit"


@pytest.mark.skipif(fs.remote(), reason="the log is in the database: tests/test_scale.py")
def test_chain_detects_edits_and_removals(log_dir):
    for i in range(5):
        audit.record("test.update", user="alice", project_id="p1", target={"tid": f"t{i}"})
    assert audit.verify() == {"ok": True, "records": 5, "error": ""}
    assert [r["target"]["tid"] for r in audit.read(limit=2)] == ["t4", "t3"]
    f = next(log_dir.glob("*.jsonl"))
    lines = f.read_text("utf-8").splitlines()

    f.write_text("\n".join(lines[:2] + lines[3:]) + "\n", "utf-8")            # a record removed
    assert "цепочка прервана" in audit.verify()["error"]
    edited = json.loads(lines[2])
    edited["user"] = "mallory"
    f.write_text("\n".join(lines[:2] + [json.dumps(edited, ensure_ascii=False)] + lines[3:]) + "\n", "utf-8")
    assert "изменена" in audit.verify()["error"]
    f.write_text("\n".join(lines) + "\n", "utf-8")
    assert audit.verify()["ok"]


def test_copies_to_a_file_and_syslog(log_dir, tmp_path, monkeypatch):
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


def test_api_changes_are_recorded(log_dir, monkeypatch):
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
