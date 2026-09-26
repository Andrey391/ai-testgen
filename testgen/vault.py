"""Secrets kept on the server, outside data/ so they never end up in tests,
exports or the test list: studio users, Atlassian tokens, logins for the
applications under test.

Files are plain JSON under secrets/<kind>/<key>.json. Protect the folder with
file-system permissions; do not commit it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets"


def _path(kind: str, key: str) -> Path:
    return SECRETS / kind / f"{re.sub(r'[^\w@.-]+', '_', key.strip()) or 'default'}.json"


def load(kind: str, key: str) -> dict | None:
    p = _path(kind, key)
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def save(kind: str, key: str, data: dict) -> None:
    p = _path(kind, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def delete(kind: str, key: str) -> bool:
    p = _path(kind, key)
    if p.exists():
        p.unlink()
        return True
    return False
