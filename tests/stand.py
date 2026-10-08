"""The test stand: a tiny web application for the studio's own tests.

Static pages from tests/site/<variant>/ (falling back to v1), so switching
`variant` to "v2" changes the layout under the same URLs - the case for fallback
locators and self-healing. A small JSON API keeps state in memory:

    POST /api/login      {"username", "password"} -> {"ok": bool, "token"}   (demo / s3cret-pass!); sets the "auth" cookie
    GET  /api/me         {"user"} with a valid "auth" cookie or "Authorization: Bearer <token>", else 401
                         (account.html: login-once; API tests)
    POST /api/otp        {"code"} -> {"ok"}: the TOTP code of TOTP_SECRET (login-2fa.html)
    POST /api/send-code  {"email"}: "sends" a letter with a code to the fake Mailpit below
    POST /api/check-code {"email", "code"} -> {"ok"}
    GET  /mailpit/api/v1/search?query=to:"x", /mailpit/api/v1/message/<id>   a fake Mailpit
    GET/POST /api/orders, DELETE /api/orders/<id>   orders (before/after data preparation)
    GET  /download/report.csv   a file download
    GET  /api/items      -> [{"name"}]              (`default_items` for a new visitor)
    POST /api/items      {"name"} -> all items of this visitor
    GET  /api/flaky      500 on the first call after reset, then 200 (flaky tests)
    GET  /__variant/v2   switch the layout (for clicking through by hand)

Items live per visitor (a cookie), so every test run in a fresh browser starts
from the same list, as with a per-user account in a real application.
"""
from __future__ import annotations

import datetime
import json
import random
import re
import sys
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SITE = Path(__file__).resolve().parent / "site"
USERNAME, PASSWORD = "demo", "s3cret-pass!"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _totp_ok(code: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import time

    from testgen.testdata import totp
    return any(totp(TOTP_SECRET, time.time() + d) == str(code).strip() for d in (-30, 0, 30))


class Stand:
    def __init__(self, port: int = 0):
        self.variant = "v1"
        self.reset()
        stand = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _visitor(self) -> list:
                cookie = self.headers.get("Cookie") or ""
                sid = next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith("sid=")), "")
                if sid not in stand.visitors:
                    sid = uuid.uuid4().hex
                    stand.visitors[sid] = [dict(i) for i in stand.default_items]
                self._sid = sid
                return stand.visitors[sid]

            def _json(self, data, status=200, cookies=()):
                body = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                if getattr(self, "_sid", ""):
                    self.send_header("Set-Cookie", f"sid={self._sid}; Path=/")
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _cookie(self, name: str) -> str:
                cookie = self.headers.get("Cookie") or ""
                return next((c.split("=", 1)[1] for c in cookie.split("; ") if c.startswith(name + "=")), "")

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                path = self.path.split("?")[0]
                stand.requests.append(("GET", path))
                if path == "/api/items":
                    return self._json(self._visitor())
                if path == "/api/flaky":
                    stand.flaky_calls += 1
                    return self._json({"error": "unavailable"}, 500) if stand.flaky_calls == 1 else self._json({"ok": True})
                if path.startswith("/__variant/"):            # manual demos: switch the layout
                    stand.variant = path.rsplit("/", 1)[1] if (SITE / path.rsplit("/", 1)[1]).is_dir() else "v1"
                    return self._json({"variant": stand.variant})
                if path == "/api/me":
                    token = self._cookie("auth") or (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    return self._json({"user": USERNAME}) if token in stand.tokens else self._json({"error": "auth"}, 401)
                if path == "/api/orders":
                    return self._json(stand.orders)
                if path == "/download/report.csv":
                    body = "id;name\n1;Отчёт\n2;Итоги\n".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="report-2026.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                if path == "/mailpit/api/v1/search":
                    query = parse_qs(urlparse(self.path).query).get("query", [""])[0]
                    m = re.search(r'to:"([^"]+)"', query)
                    found = [x for x in reversed(stand.mails) if not m or x["To"][0]["Address"] == m.group(1)]
                    return self._json({"messages": [{k: v for k, v in x.items() if k != "Text"} for x in found]})
                if path.startswith("/mailpit/api/v1/message/"):
                    mid = path.rsplit("/", 1)[1]
                    mail = next((x for x in stand.mails if x["ID"] == mid), None)
                    return self._json(mail | {"HTML": ""} if mail else {"error": "not found"}, 200 if mail else 404)
                if path in ("/logout", "/delete-account"):
                    stand.dangerous.append(path)
                return super().do_GET()

            def do_DELETE(self):
                path = self.path.split("?")[0]
                stand.requests.append(("DELETE", path))
                m = re.fullmatch(r"/api/orders/(\d+)", path)
                if m and any(o["id"] == int(m.group(1)) for o in stand.orders):
                    stand.orders = [o for o in stand.orders if o["id"] != int(m.group(1))]
                    return self._json({"ok": True})
                return self._json({"error": "not found"}, 404)

            def do_POST(self):
                path = self.path.split("?")[0]
                stand.requests.append(("POST", path))
                data = self._body()
                if path == "/api/login":
                    ok = data.get("username") == USERNAME and data.get("password") == PASSWORD
                    stand.logins += ok
                    token = uuid.uuid4().hex
                    if ok:
                        stand.tokens.add(token)
                    return self._json({"ok": ok} | ({"token": token} if ok else {}), cookies=[f"auth={token}; Path=/; HttpOnly"] if ok else [])
                if path == "/api/otp":
                    return self._json({"ok": _totp_ok(data.get("code", ""))})
                if path == "/api/send-code":
                    code = f"{random.randint(0, 999999):06d}"
                    stand.codes[data.get("email", "")] = code
                    stand.mails.append({"ID": uuid.uuid4().hex[:12], "Subject": "Код подтверждения",
                                        "Created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                        "To": [{"Address": data.get("email", "")}],
                                        "Text": f"Здравствуйте! Ваш код подтверждения: {code}. Никому его не сообщайте."})
                    return self._json({"ok": True})
                if path == "/api/check-code":
                    return self._json({"ok": bool(data.get("code")) and stand.codes.get(data.get("email", "")) == data["code"]})
                if path == "/api/orders":
                    stand.next_order += 1
                    order = {"id": stand.next_order, "title": str(data.get("title", ""))}
                    stand.orders.append(order)
                    return self._json(order, 201)
                if path == "/api/items":
                    items = self._visitor()
                    items.append({"name": str(data.get("name", ""))})
                    return self._json(items)
                self.send_error(404)

            def translate_path(self, path):
                name = path.split("?")[0].lstrip("/") or "index.html"
                for variant in (stand.variant, "v1"):
                    f = SITE / variant / name
                    if f.is_file():
                        return str(f)
                return str(SITE / "v1" / "__missing__")

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass     # the browser closing a connection early is not an error here

        self.server = Server(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self, variant: str = "v1") -> None:
        self.variant = variant
        self.default_items = [{"name": "Хлеб"}, {"name": "Сыр"}]
        self.visitors: dict[str, list] = {}
        self.flaky_calls = 0
        self.requests: list[tuple[str, str]] = []
        self.dangerous: list[str] = []
        self.tokens: set[str] = set()       # valid "auth" cookies (expire_sessions() logs everyone out)
        self.logins = 0
        self.codes: dict[str, str] = {}
        self.mails: list[dict] = []
        self.orders: list[dict] = []
        self.next_order = 0

    def expire_sessions(self) -> None:
        self.tokens.clear()

    def close(self) -> None:
        self.server.shutdown()


if __name__ == "__main__":
    # A stand to click through by hand: python tests/stand.py (PORT, default 8780; VARIANT=v2 for the new layout)
    import os

    stand = Stand(int(os.environ.get("PORT", "8780")))
    stand.reset(os.environ.get("VARIANT", "v1"))
    print(f"Test stand: {stand.url}  (login {USERNAME} / {PASSWORD}, layout {stand.variant})", flush=True)
    threading.Event().wait()
