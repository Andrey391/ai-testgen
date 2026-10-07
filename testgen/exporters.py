"""Export saved tests: Playwright (pytest) code, Gherkin, a whole project as a
pytest bundle, and API tests from the traffic recorded while authoring.

Exported UI tests keep the studio's robustness: an element step uses up to
MAX_ALTERNATIVES of its locator candidates joined with `.or_()`, so a changed
test id or text does not break the test while another candidate still matches.
Elements inside iframes are reached with frame_locator().

Everything that varies between machines comes from fixtures, defined in the file
itself (single test) or in conftest.py (bundle):
    app_url              TESTGEN_BASE_URL, default: the recorded application URL
    credentials          TESTGEN_USERNAME / TESTGEN_PASSWORD / TESTGEN_TOTP_SECRET and TESTGEN_AUTH_<NAME>
                         for login parameters ({{auth.name}}), never the real values
    testdata             {{unique}}, {{faker.email}}... generated like in the studio (testdata.py)
    console_errors       for "no console errors" checks
    check_accessibility  axe-core, for accessibility checks
    data                 the test's "before" requests (their saved values) and "after" cleanup
With a login test in the project, the bundle's conftest.py logs in once per session
(browser_context_args with storage_state); modules become helper functions; test
files (upload_file) go to tests/fixtures/.
"""
from __future__ import annotations

import inspect
import json
import re
from typing import Callable
from urllib.parse import parse_qsl, urlparse

from . import checks, steps as steps_mod, testdata
from .testdata import PLACEHOLDER, env_name, is_credential

MAX_ALTERNATIVES = 4
NO_ELEMENT = ("navigate", "press_key", "scroll", "wait", "assert_text_present", "assert_url_contains",
              "assert_no_console_errors", "assert_accessible", "mock_route", "switch_tab", "handle_dialog",
              "assert_download", "read_email", "use_module", "api_request", "assert_screenshot")


def _py(s) -> str:
    return repr(s)


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", s.lower()).strip("_") or "generated"


def _val(s: str) -> str:
    """Step value as a Python expression; credentials, test data, run variables and module
    parameters come from fixtures and arguments."""
    parts = []
    for i, chunk in enumerate(PLACEHOLDER.split(s or "")):
        if i % 2:
            if is_credential(chunk):
                parts.append(f"credentials[{chunk!r}]")
            elif chunk.startswith("vars."):
                parts.append(f"str(data[{chunk[5:]!r}])")
            elif chunk.startswith("params."):
                parts.append(f"str(params[{chunk[7:]!r}])")
            else:
                parts.append(f"testdata[{chunk!r}]")
        elif chunk:
            parts.append(_py(chunk))
    return " + ".join(parts) or "''"


def _value_obj(v) -> str:
    """A JSON value (request body) as Python code with placeholders resolved."""
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k!r}: {_value_obj(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_value_obj(x) for x in v) + "]"
    if isinstance(v, str):
        return _val(v)
    return repr(v)


def _needs_of(value: str, needs: set[str]) -> None:
    for key in PLACEHOLDER.findall(value or ""):
        if is_credential(key):
            needs.add("credentials")
        elif key.startswith("vars."):
            needs.add("data")
        elif not key.startswith("params."):
            needs.add("testdata")


def _one(c: dict, root: str = "page") -> str:
    k = c["kind"]
    if k == "testid":
        return f"{root}.get_by_test_id({_py(c['value'])})"
    if k == "role":
        return f"{root}.get_by_role({_py(c['role'])}, name={_py(c['name'])}, exact=True)"
    if k == "label":
        return f"{root}.get_by_label({_py(c['value'])}, exact=True)"
    if k == "placeholder":
        return f"{root}.get_by_placeholder({_py(c['value'])}, exact=True)"
    if k == "text":
        return f"{root}.get_by_text({_py(c['value'])}, exact=True)"
    return f"{root}.locator({_py(c['value'])})"


def _frame_root(frame: list[str] | None) -> str:
    return "page" + "".join(f".frame_locator({_py(sel)})" for sel in frame or [])


def _locator_expr(locator: list[dict], alternatives: bool = True) -> str:
    """The step's locator; with `alternatives` the other candidates are fallbacks via .or_().
    Inside a frame the fallbacks are chained on the page and entered through the frame once."""
    if not locator:
        return "page.locator('body')"
    frame = locator[0].get("frame") or []
    cands = [c for c in locator if (c.get("frame") or []) == frame][:MAX_ALTERNATIVES if alternatives else 1]
    if len(cands) == 1:
        return _one(cands[0], _frame_root(frame))
    chain = _one(cands[0]) + "".join(f"\n               .or_({_one(c)})" for c in cands[1:])
    if not frame:
        return f"({chain})"
    return f"{_frame_root(frame)}.locator(\n               {chain})"


def _origin(url: str) -> str:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


def _url_expr(v: str, app_url: str, needs: set[str]) -> str:
    if app_url and v.startswith(app_url):
        needs.add("app_url")
        rest = v[len(app_url):]
        return "app_url" + (f" + {_val(rest)}" if rest else "")
    if v.startswith("/"):
        needs.add("app_url")
        return f"app_url + {_val(v)}"
    return _val(v)


# ---------- helpers embedded into the exported code ----------

def _fixture_app_url(default: str) -> str:
    return f'''@pytest.fixture(scope="session")
def app_url() -> str:
    """The application under test; TESTGEN_BASE_URL points the tests at another stand."""
    return os.environ.get("TESTGEN_BASE_URL", {default!r}).rstrip("/")
'''


FIXTURE_CREDENTIALS = '''class _Credentials(dict):
    def __missing__(self, key):
        if key == "totp":
            return totp(self["totp_secret"])
        name = "TESTGEN_" + re.sub(r"\\W", "_", key).upper()
        if not os.environ.get(name):
            pytest.fail(f"Set the {name} environment variable (login for the application)")
        return os.environ[name]


@pytest.fixture(scope="session")
def credentials() -> dict:
    """Login for the application: TESTGEN_USERNAME / TESTGEN_PASSWORD (TESTGEN_TOTP_SECRET for 2FA,
    TESTGEN_AUTH_<NAME> for a login parameter {{auth.name}})."""
    return _Credentials()
'''


def _fixture_testdata() -> str:
    source = "\n\n".join(inspect.getsource(obj).strip() for obj in (testdata.DataValues, testdata.generate))
    return f'''FIELDS = {sorted(testdata.FIELDS)!r}


{source}


@pytest.fixture
def testdata() -> DataValues:
    """Unique data for {{{{unique}}}}, {{{{faker.email}}}}...: new on every run, the same within one."""
    return DataValues()
'''


FIXTURE_CONSOLE = '''@pytest.fixture
def console_errors(page) -> list:
    """Console errors and uncaught exceptions of the page ("Failed to load resource" left out)."""
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error"
            and not m.text.startswith("Failed to load resource") else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return errors
'''

FIXTURE_A11Y = f'''IMPACTS = {checks.IMPACTS!r}
_axe_source = []


@pytest.fixture
def check_accessibility(page):
    """WCAG 2.1 A/AA check with axe-core: fails on violations of `threshold` impact or higher."""
    def check(threshold: str = "serious") -> None:
        if not page.evaluate("() => typeof window.axe === 'object'"):
            if not _axe_source:
                if os.environ.get("TESTGEN_AXE_JS"):
                    _axe_source.append(open(os.environ["TESTGEN_AXE_JS"], encoding="utf-8").read())
                elif os.environ.get("TESTGEN_AXE_URL"):
                    _axe_source.append(urllib.request.urlopen(os.environ["TESTGEN_AXE_URL"], timeout=30).read().decode("utf-8"))
                else:
                    pytest.fail("axe-core is not configured: set TESTGEN_AXE_JS (path to axe.min.js) or TESTGEN_AXE_URL")
            page.evaluate(_axe_source[0])
        violations = page.evaluate("""async () => (await axe.run(document, {{runOnly: {{type: 'tag',
            values: {checks.WCAG_TAGS!r}}}}})).violations.map(v => ({{id: v.id, impact: v.impact || 'minor',
            nodes: v.nodes.length}}))""")
        failing = [v for v in violations if IMPACTS.index(v["impact"]) >= IMPACTS.index(threshold)]
        assert not failing, f"WCAG violations: {{failing}}"
    return check
'''

HELPER_SWITCH_TAB = '''def _switch_tab(page, which: str = "last"):
    """The tab to go on with: "last", "first", a number (1 = first) or text of its URL."""
    page.wait_for_timeout(300)
    pages = [p for p in page.context.pages if not p.is_closed()]
    for _ in range(50):          # a tab opened by the previous step may appear a moment later
        if which != "last" or pages[-1] is not page:
            break
        page.wait_for_timeout(100)
        pages = [p for p in page.context.pages if not p.is_closed()]
    if which == "last":
        target = pages[-1]
    elif which == "first":
        target = pages[0]
    elif which.isdigit():
        target = pages[int(which) - 1]
    else:
        target = next(p for p in pages if which in p.url)
    target.bring_to_front()
    return target
'''

HELPER_DIALOG = '''def _answer_dialog(dialog, action: str, prompt_text: str, seen: list) -> None:
    seen.append(dialog.message)
    if action == "accept":
        dialog.accept(prompt_text) if dialog.type == "prompt" else dialog.accept()
    else:
        dialog.dismiss()
'''

HELPER_API = '''def _api(page, method: str, url: str, expect=None, **kwargs):
    """A request through the page's context (its cookies): the test's data preparation."""
    r = page.request.fetch(url, method=method, **kwargs)
    ok = r.status == expect if expect else 200 <= r.status < 300
    assert ok, f"{method} {url} -> {r.status}"
    return r
'''

HELPER_EMAIL = '''def _read_email(to: str = "", subject: str = "", pattern: str = r"\\b(\\d{4,8})\\b", timeout: float = 60) -> str:
    """A code from the newest matching letter of the test mailbox: Mailpit (TESTGEN_MAILPIT_URL) or IMAP
    (TESTGEN_IMAP_HOST, TESTGEN_IMAP_USER, TESTGEN_IMAP_PASSWORD)."""
    import email as email_lib, html, imaplib, time as time_lib, urllib.parse, urllib.request
    deadline = time_lib.monotonic() + timeout
    while True:
        texts = []
        if os.environ.get("TESTGEN_MAILPIT_URL"):
            base = os.environ["TESTGEN_MAILPIT_URL"].rstrip("/")
            query = " ".join(filter(None, [f'to:"{to}"' if to else "", f'subject:"{subject}"' if subject else ""]))
            found = json.load(urllib.request.urlopen(f"{base}/api/v1/search?" + urllib.parse.urlencode({"query": query or "*"})))
            for m in (found.get("messages") or [])[:5]:
                body = json.load(urllib.request.urlopen(f"{base}/api/v1/message/{m['ID']}"))
                texts.append((body.get("Text") or "") + html.unescape(re.sub(r"<[^>]+>", " ", body.get("HTML") or "")))
        else:
            box = imaplib.IMAP4_SSL(os.environ["TESTGEN_IMAP_HOST"])
            box.login(os.environ["TESTGEN_IMAP_USER"], os.environ["TESTGEN_IMAP_PASSWORD"])
            box.select("INBOX", readonly=True)
            ids = box.search(None, *(["TO", f'"{to}"'] if to else ["ALL"]))[1][0].split()[-5:]
            for i in reversed(ids):
                msg = email_lib.message_from_bytes(box.fetch(i, "(BODY.PEEK[])")[1][0][1])
                if subject and subject.lower() not in str(msg.get("Subject", "")).lower():
                    continue
                for part in msg.walk():
                    if part.get_content_type() in ("text/plain", "text/html"):
                        texts.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
            box.logout()
        for text in texts:
            m = re.search(pattern, text)
            if m:
                return m.group(1) if m.groups() else m.group(0)
        assert time_lib.monotonic() < deadline, f"No e-mail with a code for {to or subject}"
        time_lib.sleep(2)
'''

FIXTURE_IMPORTS = {"testdata": ["datetime", "os", "random", "time"], "check_accessibility": ["os", "urllib.request"],
                   "credentials": ["os", "base64", "hashlib", "hmac", "re", "struct", "time"], "app_url": ["os"],
                   "console_errors": [], "data": ["json"], "read_email": ["json", "os", "re"],
                   "files": ["pathlib"]}


class _Ctx:
    """What the exported code needs besides the test body: fixtures, helpers, modules, files."""

    def __init__(self, app_url: str, a11y_impact: str, lookup: Callable[[str], dict | None] | None = None,
                 testit: bool = False):
        self.app_url, self.a11y = app_url, a11y_impact
        self.lookup = lookup or (lambda _id: None)
        self.testit = testit            # testit-adapter-pytest decorators and steps: results go to Test IT
        self.helpers: set[str] = set()
        self.modules: dict[str, tuple[str, str]] = {}     # module test id -> (function name, code)
        self.files: set[str] = set()


def _fixtures_code(names: set[str], app_url: str) -> str:
    parts = []
    if "app_url" in names:
        parts.append(_fixture_app_url(app_url))
    if "credentials" in names:
        parts.append(inspect.getsource(testdata.totp).strip() + "\n")
        parts.append(FIXTURE_CREDENTIALS)
    if "testdata" in names:
        parts.append(_fixture_testdata())
    if "console_errors" in names:
        parts.append(FIXTURE_CONSOLE)
    if "check_accessibility" in names:
        parts.append(FIXTURE_A11Y)
    return "\n\n".join(parts)


def _helpers_code(ctx: _Ctx) -> str:
    parts = []
    if "switch_tab" in ctx.helpers:
        parts.append(HELPER_SWITCH_TAB)
    if "dialog" in ctx.helpers:
        parts.append(HELPER_DIALOG)
    if "api" in ctx.helpers:
        parts.append(HELPER_API)
        parts.append(inspect.getsource(steps_mod.json_path).replace("def json_path", "def _json_path") + "\n")
    if "email" in ctx.helpers:
        parts.append(HELPER_EMAIL)
    if "files" in ctx.helpers:
        parts.append('FILES = pathlib.Path(__file__).parent / "fixtures"     # files for upload steps\n')
    return "\n\n".join(parts)


# ---------- UI tests ----------

def _body(steps: list[dict], ctx: _Ctx, needs: set[str], indent: str = "    ", module: bool = False) -> list[str]:
    """Lines of a test (or module) body; `needs` collects the fixtures it uses."""
    out: list[str] = []
    pending_dialog_expect = ""
    app_url = ctx.app_url
    for i, s in enumerate(steps):
        a, v = s["action"], s.get("value", "")
        if a not in ("handle_dialog", "assert_download", "api_request", "read_email", "use_module", "mock_route"):
            _needs_of(v, needs)
        lines: list[str] = [f"# {s['description']}"]
        if a not in NO_ELEMENT:
            alternatives = a != "assert_count"   # .or_() would add up the counts of the alternatives
            lines.append(f"element = {_locator_expr(s.get('locator', []), alternatives)}")
        if a == "navigate":
            lines.append(f"page.goto({_url_expr(v, app_url, needs)})")
        elif a == "click":
            lines.append("element.first.click()")
        elif a == "double_click":
            lines.append("element.first.dblclick()")
        elif a == "fill":
            lines.append(f"element.first.fill({_val(v)})")
            if s.get("press_enter"):
                lines.append("element.first.press('Enter')")
        elif a == "select_option":
            lines.append(f"element.first.select_option(label={_val(v)})")
        elif a == "hover":
            lines.append("element.first.hover()")
        elif a == "upload_file":
            ctx.helpers.add("files")
            ctx.files.add(v)
            needs.add("files")
            lines.append(f"element.first.set_input_files(FILES / {_py(v)})")
        elif a == "drag_to":
            lines.append(f"element.first.drag_to({_locator_expr(s.get('target') or [])}.first)")
        elif a == "press_key":
            lines.append(f"page.keyboard.press({_py(v)})")
        elif a == "scroll":
            lines.append(f"page.mouse.wheel(0, {-700 if v == 'up' else 700})")
        elif a == "wait":
            lines.append(f"page.wait_for_timeout({int(float(v or 1) * 1000)})")
        elif a == "switch_tab":
            ctx.helpers.add("switch_tab")
            lines.append(f"page = _switch_tab(page, {_py(v or 'last')})")
        elif a == "handle_dialog":
            d = json.loads(v or "{}")
            ctx.helpers.add("dialog")
            needs.add("dialogs")
            _needs_of(d.get("prompt_text") or "", needs)
            lines.append(f"page.once('dialog', lambda dialog: _answer_dialog(dialog, {_py(d.get('action', 'accept'))}, "
                         f"{_val(d.get('prompt_text') or '')}, dialogs))")
            pending_dialog_expect = d.get("expect") or ""
            out += [indent + ln if ln else "" for ln in lines]
            continue
        elif a == "assert_visible":
            lines.append("expect(element.first).to_be_visible()")
        elif a == "assert_text_present":
            lines.append(f"expect(page.get_by_text({_val(v)}).first).to_be_visible()")
        elif a == "assert_url_contains":
            lines.append(f"expect(page).to_have_url(re.compile(re.escape({_val(v)})))")
            needs.add("re")
        elif a == "assert_value":
            lines.append(f"expect(element.first).to_have_value({_val(v)})")
        elif a == "assert_checked":
            lines.append(f"expect(element.first).to_be_checked(checked={_flag(v)})")
        elif a == "assert_enabled":
            lines.append(f"expect(element.first).to_be_enabled(enabled={_flag(v)})")
        elif a == "assert_count":
            lines.append(f"expect(element).to_have_count({int(v or 0)})")
        elif a == "assert_element_text":
            lines.append(f"expect(element.first).to_contain_text({_val(v)})")
        elif a == "assert_no_console_errors":
            needs.add("console_errors")
            lines.append("new_errors, checked = console_errors[checked:], len(console_errors)")
            lines.append('assert not new_errors, f"Console errors: {new_errors}"')
        elif a == "assert_accessible":
            needs.add("check_accessibility")
            lines.append(f"check_accessibility({ctx.a11y!r})")
        elif a == "assert_screenshot":
            lines.append("# Visual check against a baseline image: runs in AI Test Generator only.")
        elif a == "assert_download":
            d = json.loads(v or "{}")
            lines.append("download = download_info.value")
            lines.append(f"assert fnmatch.fnmatch(download.suggested_filename.lower(), {_py((d.get('name') or '*').lower())}), "
                         "download.suggested_filename")
            if d.get("min_bytes"):
                lines.append(f"assert os.path.getsize(download.path()) >= {int(d['min_bytes'])}")
            needs.add("fnmatch")
        elif a == "read_email":
            d = json.loads(v or "{}")
            ctx.helpers.add("email")
            needs.add("data")
            _needs_of((d.get("to") or "") + (d.get("subject") or ""), needs)
            lines.append(f"data[{_py(d.get('save') or 'code')}] = _read_email({_val(d.get('to') or '')}, "
                         f"{_val(d.get('subject') or '')}"
                         + (f", {_py(d['pattern'])}" if d.get("pattern") else "") + ")")
        elif a == "use_module":
            d = json.loads(v or "{}")
            fn = _module_function(d.get("module", ""), ctx, needs)
            args = ", ".join(f"{k}={_val(str(x))}" for k, x in (d.get("params") or {}).items())
            for x in (d.get("params") or {}).values():
                _needs_of(str(x), needs)
            needs.update({"app_url", "credentials", "testdata", "data"})
            lines.append(f"page = {fn}(page, app_url, credentials, testdata, data{', ' + args if args else ''})"
                         if fn else "# (the module was deleted)")
        elif a == "api_request":
            # A step of an API (backend) test: the request, its expected status and response fields.
            ctx.helpers.add("api")
            expr, spec = _api_call(s, ctx, needs)
            checks_, saved = spec.get("expect") or {}, spec.get("save") or {}
            lines.append(f"response = {expr}" if checks_ or saved else expr)
            for path, want in checks_.items():
                # the same comparison as the studio's run: strings as they are, other values as JSON
                lines.append(f"field = _json_path(response.json(), {_py(path)})")
                lines.append(f"assert (field if isinstance(field, str) else json.dumps(field, ensure_ascii=False)) == "
                             f"{_val(str(want))}, f{_py('Response field ' + path + ': {field!r}')}")
            if saved:
                needs.add("data")
                lines += [f"data[{_py(k)}] = _json_path(response.json(), {_py(p)})" for k, p in saved.items()]
        elif a == "mock_route":
            spec = json.loads(v or "{}")
            fulfill = (f"route.fulfill(status={int(spec.get('status') or 200)}, "
                       f"content_type={_py(spec.get('content_type') or 'application/json')}, "
                       f"body={_py(spec.get('body') or '')})")
            method = (spec.get("method") or "").upper()
            handler = f"{fulfill} if route.request.method == {method!r} else route.fallback()" if method else fulfill
            lines.append(f"page.route({_py(spec.get('url', '**'))}, lambda route: {handler})")
        if i + 1 < len(steps) and steps[i + 1]["action"] == "assert_download":
            # The download is caught around the step that starts it.
            lines = [lines[0], "with page.expect_download() as download_info:"] + ["    " + ln for ln in lines[1:]]
        if pending_dialog_expect and a != "handle_dialog":
            lines.append(f"assert dialogs and {_py(pending_dialog_expect)} in dialogs[-1], f\"Dialog: {{dialogs}}\"")
            pending_dialog_expect = ""
        if ctx.testit and not module and len(lines) > 1:
            lines = [lines[0], f"with testit.step({_py(s['description'][:250])}):"] + ["    " + ln for ln in lines[1:]]
        out += [indent + ln for ln in lines]
    return out


def _flag(v: str) -> bool:
    return str(v).strip().lower() not in ("false", "0", "no", "off", "нет")


def _module_function(module_id: str, ctx: _Ctx, needs: set[str]) -> str:
    """A module (a test used as a step) becomes a helper function; its name is returned."""
    if module_id in ctx.modules:
        return ctx.modules[module_id][0]
    m = ctx.lookup(module_id)
    if not m:
        return ""
    name = f"module_{_slug(m['name'])}"
    ctx.modules[module_id] = (name, "")       # recursion guard
    inner: set[str] = set()
    body = _body(m["steps"], ctx, inner, module=True)
    prelude = ["    checked = 0"] if "console_errors" in inner else []
    prelude += ["    dialogs = []"] if "dialogs" in inner else []
    code = "\n".join([f"def {name}(page, app_url, credentials, testdata, data, **params):",
                      f'    """Модуль «{m["name"]}»."""'] + prelude + body + ["    return page"]) + "\n"
    ctx.modules[module_id] = (name, code)
    needs.update(inner - {"console_errors", "check_accessibility", "dialogs"})
    return name


def _api_call(step: dict, ctx: _Ctx, needs: set[str]) -> tuple[str, dict]:
    """An api_request step -> (the _api(...) call, its spec); the fixtures it uses go to `needs`."""
    s = json.loads(step.get("value") or "{}")
    for text in (s.get("url") or "", json.dumps(s.get("body") or "", ensure_ascii=False),
                 json.dumps(s.get("headers") or {}, ensure_ascii=False)):
        _needs_of(text, needs)
    needs.add("app_url")
    url = _url_expr(s.get("url") or "/", ctx.app_url, needs)
    if not url.startswith("app_url"):
        url = f"app_url + {url}" if not (s.get("url") or "").startswith("http") else url
    kw = []
    if s.get("headers"):
        kw.append(f"headers={_value_obj(s['headers'])}")
    if s.get("body") not in (None, ""):
        kw.append(f"data={_value_obj(s['body'])}")
    if s.get("expect_status"):
        kw.append(f"expect={int(s['expect_status'])}")
    return f"_api(page, {_py((s.get('method') or 'GET').upper())}, {url}{', ' + ', '.join(kw) if kw else ''})", s


def _data_fixture(test: dict, ctx: _Ctx, needs: set[str]) -> str:
    """The test's before / after requests as a pytest fixture with yield: after always runs."""
    before, after = test.get("before") or [], test.get("after") or []
    if not before and not after:
        return ""
    ctx.helpers.add("api")
    inner: set[str] = {"app_url"}

    def call(step: dict) -> tuple[str, dict]:
        return _api_call(step, ctx, inner)

    calls = [call(step) for step in before + after]      # collects the fixtures the requests use
    args = ", ".join(["page", "app_url"] + sorted(inner & {"credentials", "testdata"}))
    lines = ["@pytest.fixture", f"def data({args}):",
             '    """Подготовка данных (before) и очистка (after): очистка выполняется всегда."""', "    data = {}"]
    del calls
    for step in before:
        expr, s = call(step)
        lines.append(f"    # {step.get('description', '')}")
        if s.get("save"):
            lines.append(f"    r = {expr}")
            lines += [f"    data[{_py(k)}] = _json_path(r.json(), {_py(p)})" for k, p in s["save"].items()]
        else:
            lines.append(f"    {expr}")
    lines.append("    yield data")
    if after:
        lines.append("    errors = []")
        for step in after:
            expr, _ = call(step)
            lines += [f"    # {step.get('description', '')}", "    try:", f"        {expr}",
                      "    except Exception as e:", "        errors.append(e)"]
        lines.append('    assert not errors, f"Cleanup failed: {errors}"')
    needs.update(inner | {"data_fixture"})
    return "\n".join(lines) + "\n"


def _test_function(test: dict, ctx: _Ctx) -> tuple[str, set[str], str]:
    """(the test function, fixtures it needs, its data fixture)."""
    needs: set[str] = set()
    data_fixture = _data_fixture(test, ctx, needs)
    body = _body(test["steps"], ctx, needs)
    prelude = []
    if "console_errors" in needs:
        prelude.append("    checked = 0")
    if "dialogs" in needs:
        prelude.append("    dialogs = []")
    if "data" in needs and "data_fixture" not in needs:
        prelude.append("    data = {}")
    fixtures = sorted(needs & {"app_url", "credentials", "testdata", "console_errors", "check_accessibility"}
                      | ({"data"} if "data_fixture" in needs else set()))
    args = ", ".join(["page: Page"] + fixtures)
    head = [f"def test_{_slug(test['name'])}({args}) -> None:",
            f'    """{test["name"]}"""' if '"""' not in test["name"] else ""]
    if ctx.testit:
        # testit-adapter-pytest: the autotest is matched by externalId, linked to its manual case.
        work_item = ((test.get("external") or {}).get("testit") or {}).get("work_item_id")
        head = ([f"@testit.externalId({_py(test.get('id') or _slug(test['name']))})",
                 f"@testit.displayName({_py(test.get('display_name') or test['name'])})",
                 f"@testit.title({_py(test.get('display_name') or test['name'])})"]
                + ([f"@testit.workItemIds({_py(str(work_item))})"] if work_item else [])
                + ([f"@testit.labels({', '.join(_py(t) for t in test.get('tags') or [])})"] if test.get("tags") else [])
                + head)
        needs.add("testit")
    return "\n".join([h for h in head if h] + prelude + (body or ["    pass"])) + "\n", needs, data_fixture


def _header(title: str, scenario: str = "", files: set[str] | None = None) -> list[str]:
    lines = ['"""Generated by AI Test Generator.', title] + ([f"Scenario: {scenario}"] if scenario else []) + [
        "", "Run: pip install pytest pytest-playwright faker && pytest <this file>",
        "Environment: TESTGEN_BASE_URL (application), TESTGEN_USERNAME / TESTGEN_PASSWORD (login)."]
    if files:
        lines.append("Files for upload steps go to fixtures/ next to this file: " + ", ".join(sorted(files)) + ".")
    return lines + ['"""']


def _imports(needs: set[str], code: str) -> list[str]:
    mods = {"os"} if re.search(r"\bos\.", code) else set()
    for m in ("re", "json", "fnmatch"):
        if re.search(rf"\b{m}\.", code):
            mods.add(m)
    for n in needs:
        mods.update(FIXTURE_IMPORTS.get(n, []))
    if "_read_email" in code:
        mods.update(FIXTURE_IMPORTS["read_email"])
    if "FILES = " in code:
        mods.add("pathlib")
    if "_json_path" in code:
        mods.add("re")
    lines = [f"import {m}" for m in sorted(mods)]
    return lines + ["", "import pytest"] + (["import testit"] if "testit." in code else []) + \
        ["from playwright.sync_api import Page, expect"]


def _context_args(run_cfg: dict | None) -> dict:
    run_cfg = run_cfg or {}
    out = {}
    if run_cfg.get("locale"):
        out["locale"] = run_cfg["locale"]
    if run_cfg.get("timezone"):
        out["timezone_id"] = run_cfg["timezone"]
    return out


def _login_block(login: dict | None, ctx: _Ctx, run_cfg: dict | None) -> tuple[str, set[str]]:
    """Log in once per session: the login test's steps, then storage_state for every test's context."""
    extra = _context_args(run_cfg)
    if not login:
        if not extra:
            return "", set()
        return ('@pytest.fixture(scope="session")\ndef browser_context_args(browser_context_args):\n'
                f"    return {{**browser_context_args, **{extra!r}}}\n"), set()
    needs: set[str] = set()
    body = _body(login["steps"], ctx, needs)
    fn = ["def _login(page, app_url, credentials, testdata, data):",
          f'    """Вход: шаги теста «{login["name"]}»."""'] + \
        (["    checked = 0"] if "console_errors" in needs else []) + \
        (["    dialogs = []"] if "dialogs" in needs else []) + body
    fixture = f'''

@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, browser, app_url, credentials, tmp_path_factory):
    """Log in once per session (the login test), then every test starts logged in."""
    args = {{**browser_context_args, **{extra!r}}}
    context = browser.new_context(**args)
    _login(context.new_page(), app_url, credentials, DataValues(), {{}})
    path = tmp_path_factory.mktemp("login") / "state.json"
    context.storage_state(path=str(path))
    context.close()
    return {{**args, "storage_state": str(path)}}
'''
    return "\n".join(fn) + "\n" + fixture, needs | {"app_url", "credentials", "testdata"}


def _login_override(run_cfg: dict | None) -> str:
    return ('@pytest.fixture\ndef browser_context_args():\n    """The login test itself starts logged out."""\n'
            f"    return {_context_args(run_cfg)!r}\n")


def to_playwright(test: dict, a11y_impact: str = "serious", lookup: Callable[[str], dict | None] | None = None,
                  login: dict | None = None, run_cfg: dict | None = None, testit: bool = False) -> str:
    """A self-contained pytest-playwright file for one test. `lookup(test_id)` finds modules;
    `login`: the project's login test (log in once); `run_cfg`: locale and time zone;
    `testit`: decorators of testit-adapter-pytest (pytest --testit sends results to Test IT)."""
    app_url = _origin(test.get("url", ""))
    ctx = _Ctx(app_url, a11y_impact, lookup, testit)
    is_login = login is not None and login.get("id") == test.get("id")
    login_code, login_needs = _login_block(None if is_login else login, ctx, run_cfg)
    fn, needs, data_fixture = _test_function(test, ctx)
    all_needs = needs | login_needs
    modules = "\n\n".join(code for _, code in ctx.modules.values())
    fixtures = _fixtures_code(all_needs, app_url)
    helpers = _helpers_code(ctx)
    blocks = [b for b in (fixtures, helpers, modules, login_code, _login_override(run_cfg) if is_login else "",
                          data_fixture) if b]
    code = "\n\n".join(blocks + [fn])
    scenario = (test.get("scenario") or "").replace('"""', "'''")
    out = _header(f"Test: {test['name']}", scenario, ctx.files) + _imports(all_needs, code) + ["", ""]
    out.append("\n\n".join(blocks) + ("\n\n" if blocks else "") + fn)
    return "\n".join(out)


def conftest(app_url: str, login_code: str = "", needs: set[str] | None = None) -> str:
    names = {"app_url", "credentials", "testdata", "console_errors", "check_accessibility"} | (needs or set())
    code = _fixtures_code(names, app_url) + ("\n\n" + login_code if login_code else "")
    imports = [ln for ln in _imports(names, code) if "playwright" not in ln]
    if "expect(" in code:
        imports.append("from playwright.sync_api import expect")
    return "\n".join(['"""Fixtures shared by the exported tests (AI Test Generator)."""'] + imports + ["", "", code])


def bundle(project: dict, tests: list[dict], a11y_impact: str = "serious",
           lookup: Callable[[str], dict | None] | None = None, login: dict | None = None,
           files: Callable[[str], bytes | None] | None = None, testit: bool = False) -> dict[str, str | bytes]:
    """The project's tests as a pytest project: path -> text (bytes for test files). `testit`: with
    testit-adapter-pytest decorators and its connection_config.ini (no token inside)."""
    app_url = _origin(project.get("base_url", "")) or _origin(next((t.get("url", "") for t in tests), ""))
    run_cfg = (project.get("pipeline") or {}).get("run") or {}
    login = login if run_cfg.get("login_once", True) else None
    ctx = _Ctx(app_url, a11y_impact, lookup)
    login_code, login_needs = _login_block(login, ctx, run_cfg)
    shared_helpers = ""
    out: dict[str, str | bytes] = {"pytest.ini": "[pytest]\ntestpaths = tests\n",
                                   "requirements.txt": "pytest\npytest-playwright\nfaker\nhttpx\n"
                                   + ("testit-adapter-pytest\n" if testit else ""),
                                   "README.md": _readme(project, tests, app_url, login, testit)}
    if testit:
        conn = next((c for c in project.get("connections", []) if c.get("preset") == "testit"), None)
        f = (conn or {}).get("fields") or {}
        out["connection_config.ini"] = ("[testit]\n# The token is not stored here: set TMS_PRIVATE_TOKEN in CI.\n"
                                        f"url = {f.get('site', 'https://testit.example.com')}\n"
                                        f"projectId = {f.get('project_id', '<project uuid>')}\n"
                                        f"configurationId = {f.get('configuration_id', '<configuration uuid>')}\n"
                                        "adapterMode = 2\n")
    used: set[str] = set()
    for t in tests:
        name = _slug(t["name"])
        while name in used:
            name += "_"
        used.add(name)
        tctx = _Ctx(app_url, a11y_impact, lookup, testit)
        fn, needs, data_fixture = _test_function(t | {"name": name, "display_name": t["name"]}, tctx)
        modules = "\n\n".join(code for _, code in tctx.modules.values())
        helpers = _helpers_code(tctx)
        ctx.files |= tctx.files
        is_login = login is not None and login.get("id") == t.get("id")
        blocks = [b for b in (helpers, modules, _login_override(run_cfg) if is_login else "", data_fixture) if b]
        code = "\n\n".join(blocks + [fn])
        scenario = (t.get("scenario") or "").replace('"""', "'''")
        out[f"tests/test_{name}.py"] = "\n".join(
            _header(f"Test: {t['name']}", scenario) + _imports(needs, code) + ["", ""]
            + ["\n\n".join(blocks) + ("\n\n" if blocks else "") + fn])
        out[f"features/{name}.feature"] = to_gherkin(t | {"project": project.get("name", "")}, project.get("language", ""))
    shared_helpers = _helpers_code(ctx) if login else ""
    login_all = "\n\n".join(b for b in (shared_helpers, "\n\n".join(c for _, c in ctx.modules.values()), login_code) if b)
    out["conftest.py"] = conftest(app_url, login_all, login_needs)
    for f in sorted(ctx.files):
        data = files(f) if files else None
        if data is not None:
            out[f"tests/fixtures/{f}"] = data
    return out


def _readme(project: dict, tests: list[dict], app_url: str, login: dict | None = None, testit: bool = False) -> str:
    extra = ("\nResults to Test IT (testit-adapter-pytest): `TMS_PRIVATE_TOKEN=... pytest --testit` "
             "(url, project and configuration are in connection_config.ini).\n") if testit else ""
    return _readme_text(project, tests, app_url, login) + extra


def _readme_text(project: dict, tests: list[dict], app_url: str, login: dict | None = None) -> str:
    return f"""# {project.get('name', 'Tests')}: exported UI tests

Generated by AI Test Generator: {len(tests)} tests (pytest + Playwright) and their Gherkin features.

```
pip install -r requirements.txt
playwright install chromium
TESTGEN_BASE_URL={app_url or 'https://your-stand'} TESTGEN_USERNAME=... TESTGEN_PASSWORD=... pytest
```

- `TESTGEN_BASE_URL` - the application under test (default: {app_url or 'the recorded URL'});
- `TESTGEN_USERNAME` / `TESTGEN_PASSWORD` - its login, only for tests that log in;
- `TESTGEN_TOTP_SECRET` - the 2FA secret, for tests that type a one-time code;
- `TESTGEN_AUTH_<NAME>` - a login parameter of the account (`{{{{auth.otp}}}}` -> `TESTGEN_AUTH_OTP`);
- `TESTGEN_MAILPIT_URL` or `TESTGEN_IMAP_HOST` / `TESTGEN_IMAP_USER` / `TESTGEN_IMAP_PASSWORD` - the test mailbox;
- `TESTGEN_FAKER_LOCALE` - locale of generated test data (default en_US).
{f"{chr(10)}The session logs in once with the steps of «{login['name']}» (conftest.py), every test starts logged in.{chr(10)}" if login else ""}
Other browsers and devices: `pytest --browser firefox --browser webkit`, `pytest --device "iPhone 13"`.
Element locators have fallbacks (`.or_()`), but the studio's AI self-healing and visual checks
work only in studio runs (`python -m testgen.run`).
"""


GHERKIN = {"en": {"feature": "Feature", "scenario": "Scenario", "Given": "Given", "When": "When", "Then": "Then",
                  "And": "And"},
           "ru": {"feature": "Функция", "scenario": "Сценарий", "Given": "Дано", "When": "Когда", "Then": "Тогда",
                  "And": "И"}}


def to_gherkin(test: dict, language: str = "") -> str:
    """Gherkin; `language` "ru" writes Russian keywords (# language: ru), as Cucumber supports."""
    lang = language or test.get("language") or ""
    k = GHERKIN.get(lang, GHERKIN["en"])
    lines = [f"{k['feature']}: {test.get('project', 'Web application')}", "",
             f"  {k['scenario']}: {test['name']}"]
    if test.get("scenario"):
        lines.insert(1, f"  {test['scenario']}")
    prev = None
    for i, s in enumerate(x for x in (test.get("before") or []) + test["steps"] if x["action"] != "mock_route"):
        kind = "Given" if i == 0 or s["action"] == "api_request" else \
            "Then" if s["action"].startswith("assert") else "When"
        lines.append(f"    {k['And'] if kind == prev else k[kind]} {s['description']}")
        prev = kind
    return ("# language: ru\n" if lang == "ru" else "") + "\n".join(lines) + "\n"


# ---------- API tests from recorded traffic ----------

def _api_value(key: str, value, secrets: set[str]) -> str:
    """A JSON value of a recorded request body as Python code."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {_api_value(k, v, secrets)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_api_value(key, v, secrets) for v in value) + "]"
    if isinstance(value, str):
        if value == "***":
            name = "TESTGEN_API_" + re.sub(r"\W+", "_", key).upper().strip("_")
            secrets.add(name)
            return f"env({name!r})"
        if PLACEHOLDER.search(value):
            parts = []
            for i, chunk in enumerate(PLACEHOLDER.split(value)):
                if i % 2:
                    parts.append(f"env({env_name(chunk)!r})" if is_credential(chunk)
                                 else f"testdata[{chunk!r}]")
                elif chunk:
                    parts.append(repr(chunk))
            return " + ".join(parts)
    return repr(value)


def _keys(d: dict) -> str:
    return "{" + ", ".join(repr(k) for k in sorted(d)[:15]) + "}"


def _shape_asserts(body: str) -> list[str]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    out = ["    data = r.json()"]
    if isinstance(data, dict):
        out.append(f"    assert {_keys(data)} <= set(data)" if data else "    assert isinstance(data, dict)")
    elif isinstance(data, list):
        out.append("    assert isinstance(data, list)")
        if data and isinstance(data[0], dict) and data[0]:
            out.append(f"    assert not data or {_keys(data[0])} <= set(data[0])")
    return out


def to_api_tests(test: dict, entries: list[dict]) -> str:
    """pytest + httpx tests replaying the scenario's requests: status code and JSON shape."""
    app = _origin(test.get("url", ""))
    own = [e for e in entries if not e.get("third_party") and e["method"] != "DELETE"]
    skipped = [e for e in entries if e.get("third_party") or e["method"] == "DELETE"]
    seen, chosen = set(), []
    for e in own:
        key = (e["method"], urlparse(e["url"]).path)
        if key not in seen:
            seen.add(key)
            chosen.append(e)
    step_names = {i: s["description"] for i, s in enumerate(test["steps"])}
    secrets: set[str] = set()
    funcs, uses_data = [], False
    for n, e in enumerate(chosen, 1):
        u = urlparse(e["url"])
        base = f"{u.scheme}://{u.netloc}"
        path = u.path or "/"
        target = repr(path) if base == app else repr(e["url"].split("?")[0])
        args = [target]
        params = parse_qsl(u.query, keep_blank_values=True)
        if params:
            args.append("params={" + ", ".join(f"{k!r}: {_api_value(k, v, secrets)}" for k, v in params) + "}")
        ctype = e["request_headers"].get("content-type", "")
        if e["post_data"]:
            try:
                args.append(f"json={_api_value('', json.loads(e['post_data']), secrets)}")
            except ValueError:
                if "x-www-form-urlencoded" in ctype:
                    pairs = parse_qsl(e["post_data"], keep_blank_values=True)
                    args.append("data={" + ", ".join(f"{k!r}: {_api_value(k, v, secrets)}" for k, v in pairs) + "}")
                else:
                    args.append(f"content={e['post_data']!r}, headers={{'content-type': {ctype!r}}}")
        code = "\n".join(args)
        uses_data |= "testdata[" in code
        step = step_names.get(e.get("step", -1), "")
        name = f"test_{n:02d}_{e['method'].lower()}_{_slug(path)}"[:80]
        funcs.append("\n".join(
            [f"def {name}(api{', testdata' if 'testdata[' in code else ''}):",
             f'    """{e["method"]} {path}' + (f" — step: {step}" if step else "").replace('"""', "'''") + '"""',
             f"    r = api.request({e['method']!r}, {', '.join(args)})",
             f"    assert r.status_code == {e['status']}, r.text[:500]"]
            + (_shape_asserts(e["body"]) if 200 <= e["status"] < 300 and "json" in (e["mime"] or "") else [])))
    head = ['"""API tests generated by AI Test Generator from the requests of the UI test',
            f"«{test['name']}» recorded in the studio: each request of the scenario is replayed and its",
            "status code and the shape of its JSON answer are checked.",
            "",
            "Run: pip install pytest httpx faker && pytest <this file>",
            "Environment: TESTGEN_API_URL (default: the recorded address), TESTGEN_API_TOKEN (Authorization",
            "header, if the API needs one), TESTGEN_USERNAME / TESTGEN_PASSWORD (login in request bodies)"
            + (", " + ", ".join(sorted(secrets)) + " (masked secret values)" if secrets else "") + ".",
            "Cookie-based sessions are not replayed: log in inside a test or pass a token."]
    if skipped:
        head += ["", "Not included (other sites or DELETE requests):"] + [
            f"    {e['method']} {e['url'].split('?')[0]}" for e in skipped[:30]]
    head.append('"""')
    fixtures = [f'BASE = os.environ.get("TESTGEN_API_URL", {app!r}).rstrip("/")', "", "",
                "def env(name: str) -> str:",
                "    if not os.environ.get(name):",
                '        pytest.fail(f"Set the {name} environment variable")',
                "    return os.environ[name]", "", "",
                '@pytest.fixture(scope="module")',
                "def api():",
                '    headers = {"Accept": "application/json"}',
                '    if os.environ.get("TESTGEN_API_TOKEN"):',
                '        headers["Authorization"] = os.environ["TESTGEN_API_TOKEN"]',
                "    with httpx.Client(base_url=BASE, headers=headers, timeout=30, follow_redirects=True) as client:",
                "        yield client"]
    imports = ["import os", "", "import httpx", "import pytest"]
    extra = ""
    if uses_data:
        imports = ["import datetime", "import os", "import random", "import time", "", "import httpx", "import pytest"]
        extra = "\n\n" + _fixture_testdata()
    body = "\n\n\n".join(funcs) if funcs else "# No requests of this site were recorded."
    return "\n".join(head + imports + ["", ""] + fixtures) + extra + "\n\n\n" + body + "\n"
