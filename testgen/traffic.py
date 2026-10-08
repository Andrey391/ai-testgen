"""XHR/fetch traffic recorded while a test is authored (built-in engine).

It feeds three features:
- API tests: exporters.to_api_tests turns the scenario's requests into pytest + httpx;
- the API catalog (`catalog`): endpoints the application calls, for API scenarios and the agent
  writing an API test (with the endpoints the Planner saw while exploring);
- mocks: a recorded response becomes a `mock_route` step, so an unstable external
  service can be replaced by what it answered during recording.

Secrets never get into the recording: values of auth headers and cookies, and of
JSON / form fields that look secret (password, token, api key...) are replaced
by "***"; the login of the app under test is replaced by the {{username}} /
{{password}} placeholders, so exported API tests read it from TESTGEN_* variables
like the UI tests do. Stored as HAR 1.2 in data/projects/<id>/traffic/<test>.har.
"""
from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse

from . import fs, projects
from .testdata import secret_pairs

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
    for secret, placeholder in secret_pairs(credentials):
        if value == secret:
            return placeholder
    if credentials.get("username") and value == credentials["username"]:
        return "{{username}}"
    if value in ("{{password}}", "{{username}}") or re.fullmatch(r"\{\{auth\.\w+\}\}", value):
        return value
    return MASK if SECRET_KEY.search(key or "") and value else value


def mask_text(text: str, mime: str, credentials: dict) -> str:
    if not text:
        return text
    for secret, placeholder in secret_pairs(credentials):
        text = text.replace(secret, placeholder).replace(quote_plus(secret), placeholder)
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


# ---------- the application's API: endpoints seen in the recorded traffic ----------

MAX_ENDPOINTS = 150
_ID = re.compile(r"\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f-]{27}|[A-Za-z0-9_-]{24,}", re.I)


def _template(path: str) -> str:
    """/api/orders/42/items -> /api/orders/{id}/items: one endpoint for every object."""
    return "/".join("{id}" if seg and _ID.fullmatch(seg) else seg for seg in (path or "/").split("/")) or "/"


def _keys(text: str) -> list[str]:
    """Top-level fields of a JSON body (of its first item for a list)."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return []
    if isinstance(data, list):
        data = data[0] if data else None
        prefix = "[]."
    else:
        prefix = ""
    return [prefix + k for k in list(data)[:20]] if isinstance(data, dict) else []


def endpoints(entries: list[dict], app_url: str = "") -> list[dict]:
    """Requests grouped by method and path template: what an API test of the application can call.
    Requests to other sites are left out; values are not kept, only names of fields and statuses."""
    out: dict[tuple, dict] = {}
    for e in entries:
        if e.get("third_party") or third_party(e["url"], app_url):
            continue
        u = urlparse(e["url"])
        ep = out.setdefault((e["method"], _template(u.path)), {
            "method": e["method"], "path": _template(u.path), "count": 0,
            "statuses": [], "query": [], "request": [], "response": []})
        ep["count"] += 1
        for field, values in (("statuses", [e["status"]]), ("query", [k for k, _ in parse_qsl(u.query)]),
                              ("request", _keys(e.get("post_data") or "")), ("response", _keys(e.get("body") or ""))):
            ep[field] += [v for v in values if v not in ep[field]]
    return sorted(out.values(), key=lambda x: (x["path"], x["method"]))[:MAX_ENDPOINTS]


def merge(*lists: list[dict]) -> list[dict]:
    out: dict[tuple, dict] = {}
    for ep in (x for lst in lists for x in lst or []):
        cur = out.get((ep["method"], ep["path"]))
        if not cur:
            out[(ep["method"], ep["path"])] = copy.deepcopy(ep)
            continue
        cur["count"] += ep.get("count", 0)
        for field in ("statuses", "query", "request", "response"):
            cur[field] += [v for v in ep.get(field) or [] if v not in cur[field]]
    return sorted(out.values(), key=lambda x: (x["path"], x["method"]))[:MAX_ENDPOINTS]


def recorded(pid: str) -> list[dict]:
    """The application's API as the project has seen it: requests recorded while its tests were
    authored and while the Planner explored the site."""
    from . import explorer
    p = projects.get(pid) or {}
    entries = []
    for f in fs.glob(projects.path(pid) / "traffic", "*.har"):
        har = fs.read_json(f)
        if har:
            entries += from_har(har)
    return merge(endpoints(entries, p.get("base_url", "")), (explorer.latest(pid) or {}).get("api") or [])


# People edit the catalog: their endpoints (new ones, or a recorded one corrected) win over the recorded
# ones with the same method and path; a removed recorded endpoint stays removed when it is seen again.
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def _edits_file(pid: str):
    return projects.path(pid) / "api-endpoints.json"


def _edits(pid: str) -> dict:
    d = fs.read_json(_edits_file(pid)) or {}
    return {"manual": d.get("manual") or [], "removed": d.get("removed") or []}


def _key(ep: dict) -> str:
    return f"{ep['method']} {ep['path']}"


def clean_endpoint(ep: dict) -> dict:
    """An endpoint typed by a person: a method, a path from "/", names of fields, statuses, a note."""
    def names(v) -> list[str]:
        items = v if isinstance(v, list) else str(v or "").replace("\n", ",").split(",")
        return list(dict.fromkeys(str(x).strip()[:100] for x in items if str(x).strip()))[:50]
    method = str(ep.get("method") or "").strip().upper()
    if method not in METHODS:
        raise ValueError(f"Метод должен быть одним из: {', '.join(METHODS)}")
    path = urlparse(str(ep.get("path") or "").strip()).path if "://" in str(ep.get("path") or "") \
        else str(ep.get("path") or "").strip().split("?")[0]
    if not path.startswith("/") or len(path) > 300 or re.search(r"\s", path):
        raise ValueError("Путь должен начинаться с «/» и быть без пробелов, например /api/orders/{id}")
    statuses = []
    for s in names(ep.get("statuses")):
        if not s.isdigit() or not 100 <= int(s) <= 599:
            raise ValueError(f"Статус ответа «{s}» — не код HTTP (100–599)")
        statuses.append(int(s))
    return {"method": method, "path": path, "count": 0, "statuses": statuses, "query": names(ep.get("query")),
            "request": names(ep.get("request")), "response": names(ep.get("response")),
            "note": str(ep.get("note") or "").strip()[:500], "source": "manual"}


def catalog(pid: str) -> list[dict]:
    """The API catalog: the recorded endpoints with the edits of people applied."""
    edits = _edits(pid)
    manual = {_key(ep): ep for ep in edits["manual"]}
    removed = set(edits["removed"])
    out = []
    for ep in recorded(pid):
        if _key(ep) in removed:
            continue
        own = manual.pop(_key(ep), None)
        out.append(ep | {"source": "recorded"} if own is None else own | {"source": "edited", "count": ep["count"]})
    out += manual.values()
    return sorted(out, key=lambda x: (x["path"], x["method"]))


def save_endpoint(pid: str, ep: dict, old: str = "") -> dict:
    """Add an endpoint, or replace `old` ("METHOD /path") with it."""
    ep = clean_endpoint(ep)
    with fs.lock(_edits_file(pid)):
        edits = _edits(pid)
        if _key(ep) != old and any(_key(x) == _key(ep) for x in catalog(pid)):
            raise ValueError(f"Эндпоинт {_key(ep)} уже есть в каталоге")
        recorded_keys = {_key(x) for x in recorded(pid)}
        edits["manual"] = [x for x in edits["manual"] if _key(x) not in (old, _key(ep))] + [ep]
        if old and old != _key(ep) and old in recorded_keys:
            edits["removed"].append(old)          # a recorded endpoint renamed: the old one is gone
        edits["removed"] = [k for k in dict.fromkeys(edits["removed"]) if k != _key(ep)]
        fs.write_json(_edits_file(pid), edits, indent=1)
    return ep


def delete_endpoint(pid: str, key: str) -> bool:
    with fs.lock(_edits_file(pid)):
        edits = _edits(pid)
        manual = [x for x in edits["manual"] if _key(x) != key]
        found = len(manual) != len(edits["manual"])
        edits["manual"] = manual
        if key in {_key(x) for x in recorded(pid)}:
            edits["removed"] = list(dict.fromkeys(edits["removed"] + [key]))
            found = True
        if found:
            fs.write_json(_edits_file(pid), edits, indent=1)
    return found


def catalog_text(eps: list[dict], limit: int = 80) -> str:
    """The endpoints for a model: one line each."""
    lines = []
    for ep in eps[:limit]:
        line = f"{ep['method']} {ep['path']}"
        if ep["query"]:
            line += f" ?{'&'.join(ep['query'][:10])}"
        line += f" -> {', '.join(str(s) for s in ep['statuses'])}"
        if ep["request"]:
            line += f"; request fields: {', '.join(ep['request'])}"
        if ep["response"]:
            line += f"; response fields: {', '.join(ep['response'])}"
        if ep.get("note"):
            line += f" — {ep['note']}"
        lines.append(line)
    if len(eps) > limit:
        lines.append(f"(and {len(eps) - limit} more)")
    return "\n".join(lines)


# ---------- mocks ----------

def mock_spec(entry: dict) -> dict:
    """The mock_route value for a recorded exchange: same URL (any query), method, answer."""
    return {"url": entry["url"].split("?")[0] + ("*" if "?" in entry["url"] else ""),
            "method": entry["method"], "status": entry["status"],
            "content_type": entry["mime"] or "application/json", "body": entry["body"]}
