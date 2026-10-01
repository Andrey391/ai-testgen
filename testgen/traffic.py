"""XHR/fetch traffic recorded while a test is authored (built-in engine).

It feeds two features:
- API tests: exporters.to_api_tests turns the scenario's requests into pytest + httpx;
- mocks: a recorded response becomes a `mock_route` step, so an unstable external
  service can be replaced by what it answered during recording.

Secrets never get into the recording: values of auth headers and cookies, and of
JSON / form fields that look secret (password, token, api key...) are replaced
by "***"; the login of the app under test is replaced by the {{username}} /
{{password}} placeholders, so exported API tests read it from TESTGEN_* variables
like the UI tests do. Stored as HAR 1.2 in data/projects/<id>/traffic/<test>.har.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse

from . import fs, projects

SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
                  "x-auth-token", "x-csrf-token", "x-xsrf-token"}
SECRET_KEY = re.compile(r"pass(word|wd)?$|secret|token|api[_-]?key|authorization|session|csrf|otp", re.I)
TEXT_TYPES = ("json", "text", "xml", "javascript", "x-www-form-urlencoded", "graphql")
MAX_BODY = 100_000
MASK = "***"


async def capture(request, credentials: dict) -> dict | None:
    """One finished request with its response, masked."""
    resp = await request.response()
    if resp is None:
        return None
    rheaders = await resp.all_headers()
    ctype = rheaders.get("content-type", "")
    body = ""
    if any(t in ctype for t in TEXT_TYPES):
        try:
            body = (await resp.body())[:MAX_BODY].decode("utf-8", "replace")
        except Exception:
            body = ""
    entry = {"method": request.method, "url": request.url, "request_headers": await request.all_headers(),
             "post_data": request.post_data or "", "status": resp.status, "response_headers": rheaders,
             "mime": ctype.split(";")[0].strip(), "body": body, "at": time.time()}
    return mask_entry(entry, credentials)


# ---------- masking ----------

def _mask_value(key: str, value, credentials: dict):
    if isinstance(value, dict):
        return {k: _mask_value(k, v, credentials) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_value(key, v, credentials) for v in value]
    if not isinstance(value, str):
        return value
    if credentials.get("password") and value == credentials["password"]:
        return "{{password}}"
    if credentials.get("username") and value == credentials["username"]:
        return "{{username}}"
    if value in ("{{password}}", "{{username}}"):
        return value
    return MASK if SECRET_KEY.search(key or "") and value else value


def mask_text(text: str, mime: str, credentials: dict) -> str:
    if not text:
        return text
    pw = credentials.get("password")
    if pw:
        text = text.replace(pw, "{{password}}").replace(quote_plus(pw), "{{password}}")
    if "json" in mime or text.lstrip()[:1] in ("{", "["):
        try:
            return json.dumps(_mask_value("", json.loads(text), credentials), ensure_ascii=False)
        except ValueError:
            pass
    if "x-www-form-urlencoded" in mime or re.fullmatch(r"[\w.%+-]+=[^&]*(&[\w.%+-]+=[^&]*)*", text):
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            masked = [(k, _mask_value(k, v, credentials)) for k, v in pairs]
            return "&".join(f"{quote_plus(k)}={v if v.startswith('{{') or v == MASK else quote_plus(v)}"
                            for k, v in masked)
    return text


def mask_url(url: str, credentials: dict) -> str:
    u = urlparse(url)
    if not u.query:
        return url
    q = [(k, _mask_value(k, v, credentials)) for k, v in parse_qsl(u.query, keep_blank_values=True)]
    return u._replace(query=urlencode(q, safe="{}*")).geturl()


def mask_entry(entry: dict, credentials: dict) -> dict:
    def headers(h: dict) -> dict:
        return {k: (MASK if k.lower() in SECRET_HEADERS or SECRET_KEY.search(k) else v) for k, v in h.items()}
    req_mime = entry["request_headers"].get("content-type", "")
    return entry | {
        "url": mask_url(entry["url"], credentials),
        "request_headers": headers(entry["request_headers"]),
        "response_headers": headers(entry["response_headers"]),
        "post_data": mask_text(entry["post_data"], req_mime, credentials),
        "body": mask_text(entry["body"], entry["mime"], credentials),
    }


# ---------- storage (HAR 1.2) ----------

def _file(pid: str, tid: str):
    return projects.path(pid) / "traffic" / f"{re.sub(r'[^\w-]+', '_', tid)}.har"


def third_party(url: str, app_url: str) -> bool:
    """Another site than the application (compared by the last two labels of the host)."""
    def base(u: str) -> str:
        return ".".join((urlparse(u).hostname or "").split(".")[-2:])
    return bool(app_url) and base(url) != base(app_url)


def to_har(entries: list[dict], app_url: str = "") -> dict:
    def nv(d: dict) -> list[dict]:
        return [{"name": k, "value": v} for k, v in d.items()]
    out = []
    for e in entries:
        u = urlparse(e["url"])
        out.append({
            "startedDateTime": datetime.fromtimestamp(e.get("at", 0), timezone.utc).isoformat(),
            "time": 0,
            "request": {"method": e["method"], "url": e["url"], "httpVersion": "HTTP/1.1",
                        "headers": nv(e["request_headers"]), "cookies": [],
                        "queryString": [{"name": k, "value": v} for k, v in parse_qsl(u.query)],
                        "postData": {"mimeType": e["request_headers"].get("content-type", ""),
                                     "text": e["post_data"]} if e["post_data"] else None,
                        "headersSize": -1, "bodySize": len(e["post_data"] or "")},
            "response": {"status": e["status"], "statusText": "", "httpVersion": "HTTP/1.1",
                         "headers": nv(e["response_headers"]), "cookies": [],
                         "content": {"size": len(e["body"]), "mimeType": e["mime"], "text": e["body"]},
                         "redirectURL": "", "headersSize": -1, "bodySize": -1},
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            "_step": e.get("step", -1), "_thirdParty": third_party(e["url"], app_url),
        })
        if out[-1]["request"]["postData"] is None:
            del out[-1]["request"]["postData"]
    return {"log": {"version": "1.2", "creator": {"name": "AI Test Generator", "version": "1"},
                    "pages": [], "entries": out}}


def from_har(har: dict) -> list[dict]:
    out = []
    for h in (har.get("log") or {}).get("entries", []):
        req, resp = h["request"], h["response"]
        out.append({"method": req["method"], "url": req["url"],
                    "request_headers": {x["name"]: x["value"] for x in req.get("headers", [])},
                    "post_data": (req.get("postData") or {}).get("text", ""),
                    "status": resp["status"],
                    "response_headers": {x["name"]: x["value"] for x in resp.get("headers", [])},
                    "mime": resp.get("content", {}).get("mimeType", ""),
                    "body": resp.get("content", {}).get("text", ""),
                    "step": h.get("_step", -1), "third_party": h.get("_thirdParty", False)})
    return out


def save(pid: str, tid: str, entries: list[dict], app_url: str = "") -> None:
    f = _file(pid, tid)
    if not entries:
        fs.unlink(f)
        return
    fs.write_json(f, to_har(entries, app_url), indent=1)


def load(pid: str, tid: str) -> list[dict]:
    har = fs.read_json(_file(pid, tid))
    return from_har(har) if har else []


def exists(pid: str, tid: str) -> bool:
    return fs.is_file(_file(pid, tid))


def load_har(pid: str, tid: str) -> dict | None:
    return fs.read_json(_file(pid, tid))


def delete(pid: str, tid: str) -> None:
    fs.unlink(_file(pid, tid))


# ---------- mocks ----------

def mock_spec(entry: dict) -> dict:
    """The mock_route value for a recorded exchange: same URL (any query), method, answer."""
    return {"url": entry["url"].split("?")[0] + ("*" if "?" in entry["url"] else ""),
            "method": entry["method"], "status": entry["status"],
            "content_type": entry["mime"] or "application/json", "body": entry["body"]}
