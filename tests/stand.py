"""The test stand: a tiny web application for the studio's own tests.

Static pages from tests/site/<variant>/ (falling back to v1), so switching
`variant` to "v2" changes the layout under the same URLs - the case for fallback
locators and self-healing. A small JSON API keeps state in memory:

    POST /api/login      {"username", "password"} -> {"ok": bool}   (demo / s3cret-pass!)
    GET  /api/items      -> [{"name"}]              (`default_items` for a new visitor)
    POST /api/items      {"name"} -> all items of this visitor
    GET  /api/flaky      500 on the first call after reset, then 200 (flaky tests)
    GET  /__variant/v2   switch the layout (for clicking through by hand)

Items live per visitor (a cookie), so every test run in a fresh browser starts
from the same list, as with a per-user account in a real application.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent / "site"
USERNAME, PASSWORD = "demo", "s3cret-pass!"


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

            def _json(self, data, status=200):
                body = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                if getattr(self, "_sid", ""):
                    self.send_header("Set-Cookie", f"sid={self._sid}; Path=/")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
                if path in ("/logout", "/delete-account"):
                    stand.dangerous.append(path)
                return super().do_GET()

            def do_POST(self):
                path = self.path.split("?")[0]
                stand.requests.append(("POST", path))
                data = self._body()
                if path == "/api/login":
                    return self._json({"ok": data.get("username") == USERNAME and data.get("password") == PASSWORD})
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

    def close(self) -> None:
        self.server.shutdown()


if __name__ == "__main__":
    # A stand to click through by hand: python tests/stand.py (PORT, default 8780; VARIANT=v2 for the new layout)
    import os

    stand = Stand(int(os.environ.get("PORT", "8780")))
    stand.reset(os.environ.get("VARIANT", "v1"))
    print(f"Test stand: {stand.url}  (login {USERNAME} / {PASSWORD}, layout {stand.variant})", flush=True)
    threading.Event().wait()
