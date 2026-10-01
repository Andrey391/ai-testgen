"""Export saved tests: Playwright (pytest) code, Gherkin, a whole project as a
pytest bundle, and API tests from the traffic recorded while authoring.

Exported UI tests keep the studio's robustness: an element step uses up to
MAX_ALTERNATIVES of its locator candidates joined with `.or_()`, so a changed
test id or text does not break the test while another candidate still matches.

Everything that varies between machines comes from fixtures, defined in the file
itself (single test) or in conftest.py (bundle):
    app_url              TESTGEN_BASE_URL, default: the recorded application URL
    credentials          TESTGEN_USERNAME / TESTGEN_PASSWORD, never the real values
    testdata             {{unique}}, {{faker.email}}... generated like in the studio (testdata.py)
    console_errors       for "no console errors" checks
    check_accessibility  axe-core, for accessibility checks
"""
from __future__ import annotations

import inspect
import json
import re
from urllib.parse import parse_qsl, urlparse

from . import checks, testdata
from .testdata import CREDENTIALS, PLACEHOLDER

MAX_ALTERNATIVES = 4


def _py(s) -> str:
    return repr(s)


def _slug(s: str) -> str:
    return re.sub(r"\W+", "_", s.lower()).strip("_") or "generated"


def _val(s: str) -> str:
    """Step value as a Python expression; credentials and test data come from fixtures."""
    parts = []
    for i, chunk in enumerate(PLACEHOLDER.split(s or "")):
        if i % 2:
            parts.append(f"credentials[{chunk!r}]" if chunk in CREDENTIALS else f"testdata[{chunk!r}]")
        elif chunk:
            parts.append(_py(chunk))
    return " + ".join(parts) or "''"


def _one(c: dict) -> str:
    k = c["kind"]
    if k == "testid":
        return f"page.get_by_test_id({_py(c['value'])})"
    if k == "role":
        return f"page.get_by_role({_py(c['role'])}, name={_py(c['name'])}, exact=True)"
    if k == "label":
        return f"page.get_by_label({_py(c['value'])}, exact=True)"
    if k == "placeholder":
        return f"page.get_by_placeholder({_py(c['value'])}, exact=True)"
    if k == "text":
        return f"page.get_by_text({_py(c['value'])}, exact=True)"
    return f"page.locator({_py(c['value'])})"


def _locator_expr(locator: list[dict], alternatives: bool = True) -> str:
    """The step's locator; with `alternatives` the other candidates are fallbacks via .or_()."""
    if not locator:
        return "page.locator('body')"
    exprs = [_one(c) for c in locator[:MAX_ALTERNATIVES if alternatives else 1]]
    if len(exprs) == 1:
        return exprs[0]
    return "(" + exprs[0] + "".join(f"\n               .or_({e})" for e in exprs[1:]) + ")"


def _origin(url: str) -> str:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}" if u.netloc else ""


# ---------- fixtures ----------

def _fixture_app_url(default: str) -> str:
    return f'''@pytest.fixture
def app_url() -> str:
    """The application under test; TESTGEN_BASE_URL points the tests at another stand."""
    return os.environ.get("TESTGEN_BASE_URL", {default!r}).rstrip("/")
'''


FIXTURE_CREDENTIALS = '''class _Credentials(dict):
    def __missing__(self, key):
        name = f"TESTGEN_{key.upper()}"
        if not os.environ.get(name):
            pytest.fail(f"Set the {name} environment variable (login for the application)")
        return os.environ[name]


@pytest.fixture
def credentials() -> dict:
    """Login for the application: TESTGEN_USERNAME / TESTGEN_PASSWORD."""
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

FIXTURE_IMPORTS = {"testdata": ["datetime", "os", "random", "time"], "check_accessibility": ["os", "urllib.request"],
                   "credentials": ["os"], "app_url": ["os"], "console_errors": []}


def _fixtures_code(names: set[str], app_url: str) -> str:
    parts = []
    if "app_url" in names:
        parts.append(_fixture_app_url(app_url))
    if "credentials" in names:
        parts.append(FIXTURE_CREDENTIALS)
    if "testdata" in names:
        parts.append(_fixture_testdata())
    if "console_errors" in names:
        parts.append(FIXTURE_CONSOLE)
    if "check_accessibility" in names:
        parts.append(FIXTURE_A11Y)
    return "\n\n".join(parts)


# ---------- UI tests ----------

def _body(test: dict, app_url: str, a11y_impact: str = "serious") -> tuple[list[str], set[str]]:
    """Lines of the test function body and the fixtures it needs."""
    out, needs = [], set()
    for s in test["steps"]:
        a, v = s["action"], s.get("value", "")
        for key in PLACEHOLDER.findall(v or ""):
            needs.add("credentials" if key in CREDENTIALS else "testdata")
        out.append(f"    # {s['description']}")
        element = a not in ("navigate", "press_key", "scroll", "wait", "assert_text_present", "assert_url_contains",
                            "assert_no_console_errors", "assert_accessible", "mock_route") \
            and not (a == "assert_screenshot")
        if element:
            alternatives = a != "assert_count"   # .or_() would add up the counts of the alternatives
            out.append(f"    element = {_locator_expr(s.get('locator', []), alternatives)}")
        if a == "navigate":
            if app_url and v.startswith(app_url):
                needs.add("app_url")
                out.append(f"    page.goto(app_url + {_py(v[len(app_url):])})")
            else:
                out.append(f"    page.goto({_val(v)})")
        elif a == "click":
            out.append("    element.first.click()")
        elif a == "fill":
            out.append(f"    element.first.fill({_val(v)})")
            if s.get("press_enter"):
                out.append("    element.first.press('Enter')")
        elif a == "select_option":
            out.append(f"    element.first.select_option(label={_val(v)})")
        elif a == "hover":
            out.append("    element.first.hover()")
        elif a == "press_key":
            out.append(f"    page.keyboard.press({_py(v)})")
        elif a == "scroll":
            out.append(f"    page.mouse.wheel(0, {-700 if v == 'up' else 700})")
        elif a == "wait":
            out.append(f"    page.wait_for_timeout({int(float(v or 1) * 1000)})")
        elif a == "assert_visible":
            out.append("    expect(element.first).to_be_visible()")
        elif a == "assert_text_present":
            out.append(f"    expect(page.get_by_text({_val(v)}).first).to_be_visible()")
        elif a == "assert_url_contains":
            out.append(f"    expect(page).to_have_url(re.compile(re.escape({_val(v)})))")
        elif a == "assert_value":
            out.append(f"    expect(element.first).to_have_value({_val(v)})")
        elif a == "assert_checked":
            out.append(f"    expect(element.first).to_be_checked(checked={_flag(v)})")
        elif a == "assert_enabled":
            out.append(f"    expect(element.first).to_be_enabled(enabled={_flag(v)})")
        elif a == "assert_count":
            out.append(f"    expect(element).to_have_count({int(v or 0)})")
        elif a == "assert_element_text":
            out.append(f"    expect(element.first).to_contain_text({_val(v)})")
        elif a == "assert_no_console_errors":
            needs.add("console_errors")
            out.append("    new_errors, checked = console_errors[checked:], len(console_errors)")
            out.append('    assert not new_errors, f"Console errors: {new_errors}"')
        elif a == "assert_accessible":
            needs.add("check_accessibility")
            out.append(f"    check_accessibility({a11y_impact!r})")
        elif a == "assert_screenshot":
            out.append("    # Visual check against a baseline image: runs in AI Test Generator only.")
        elif a == "mock_route":
            spec = json.loads(v or "{}")
            fulfill = (f"route.fulfill(status={int(spec.get('status') or 200)}, "
                       f"content_type={_py(spec.get('content_type') or 'application/json')}, "
                       f"body={_py(spec.get('body') or '')})")
            method = (spec.get("method") or "").upper()
            handler = f"{fulfill} if route.request.method == {method!r} else route.fallback()" if method else fulfill
            out.append(f"    page.route({_py(spec.get('url', '**'))}, lambda route: {handler})")
    if "console_errors" in needs:
        out.insert(0, "    checked = 0")
    return out, needs


def _flag(v: str) -> bool:
    return str(v).strip().lower() not in ("false", "0", "no", "off", "нет")


def _test_function(test: dict, app_url: str, a11y_impact: str) -> tuple[str, set[str]]:
    body, needs = _body(test, app_url, a11y_impact)
    args = ", ".join(["page: Page"] + sorted(needs))
    head = [f"def test_{_slug(test['name'])}({args}) -> None:",
            f'    """{test["name"]}"""' if '"""' not in test["name"] else ""]
    return "\n".join([h for h in head if h] + (body or ["    pass"])) + "\n", needs


def _header(title: str, scenario: str = "") -> list[str]:
    return ['"""Generated by AI Test Generator.', title] + ([f"Scenario: {scenario}"] if scenario else []) + [
        "", "Run: pip install pytest pytest-playwright faker && pytest <this file>",
        "Environment: TESTGEN_BASE_URL (application), TESTGEN_USERNAME / TESTGEN_PASSWORD (login).", '"""']


def _imports(needs: set[str], code: str) -> list[str]:
    mods = {"os"} if re.search(r"\bos\.", code) else set()
    if re.search(r"\bre\.", code):
        mods.add("re")
    for n in needs:
        mods.update(FIXTURE_IMPORTS.get(n, []))
    lines = [f"import {m}" for m in sorted(mods)]
    return lines + ["", "import pytest", "from playwright.sync_api import Page, expect"]


def to_playwright(test: dict, a11y_impact: str = "serious") -> str:
    """A self-contained pytest-playwright file for one test."""
    app_url = _origin(test.get("url", ""))
    fn, needs = _test_function(test, app_url, a11y_impact)
    fixtures = _fixtures_code(needs, app_url)
    code = fixtures + fn
    scenario = (test.get("scenario") or "").replace('"""', "'''")
    out = _header(f"Test: {test['name']}", scenario) + _imports(needs, code) + ["", ""]
    if fixtures:
        out += [fixtures, ""]
    out.append(fn)
    return "\n".join(out)


def conftest(app_url: str) -> str:
    names = {"app_url", "credentials", "testdata", "console_errors", "check_accessibility"}
    code = _fixtures_code(names, app_url)
    return "\n".join(['"""Fixtures shared by the exported tests (AI Test Generator)."""']
                     + [ln for ln in _imports(names, code) if "playwright" not in ln] + ["", "", code])


def bundle(project: dict, tests: list[dict], a11y_impact: str = "serious") -> dict[str, str]:
    """The project's tests as a pytest project: path -> text."""
    app_url = _origin(project.get("base_url", "")) or _origin(next((t.get("url", "") for t in tests), ""))
    files = {"conftest.py": conftest(app_url),
             "pytest.ini": "[pytest]\ntestpaths = tests\n",
             "requirements.txt": "pytest\npytest-playwright\nfaker\nhttpx\n",
             "README.md": _readme(project, tests, app_url)}
    used: set[str] = set()
    for t in tests:
        name = _slug(t["name"])
        while name in used:
            name += "_"
        used.add(name)
        fn, needs = _test_function(t | {"name": name}, app_url, a11y_impact)
        scenario = (t.get("scenario") or "").replace('"""', "'''")
        files[f"tests/test_{name}.py"] = "\n".join(
            _header(f"Test: {t['name']}", scenario) + _imports(needs, fn) + ["", "", fn])
        files[f"features/{name}.feature"] = to_gherkin(t | {"project": project.get("name", "")})
    return files


def _readme(project: dict, tests: list[dict], app_url: str) -> str:
    return f"""# {project.get('name', 'Tests')}: exported UI tests

Generated by AI Test Generator: {len(tests)} tests (pytest + Playwright) and their Gherkin features.

```
pip install -r requirements.txt
playwright install chromium
TESTGEN_BASE_URL={app_url or 'https://your-stand'} TESTGEN_USERNAME=... TESTGEN_PASSWORD=... pytest
```

- `TESTGEN_BASE_URL` - the application under test (default: {app_url or 'the recorded URL'});
- `TESTGEN_USERNAME` / `TESTGEN_PASSWORD` - its login, only for tests that log in;
- `TESTGEN_FAKER_LOCALE` - locale of generated test data (default en_US).

Element locators have fallbacks (`.or_()`), but the studio's AI self-healing and visual checks
work only in studio runs (`python -m testgen.run`).
"""


def to_gherkin(test: dict) -> str:
    lines = [f"Feature: {test.get('project', 'Web application')}", "",
             f"  Scenario: {test['name']}"]
    if test.get("scenario"):
        lines.insert(1, f"  {test['scenario']}")
    prev = None
    for i, s in enumerate(x for x in test["steps"] if x["action"] != "mock_route"):
        kind = "Given" if i == 0 else "Then" if s["action"].startswith("assert") else "When"
        lines.append(f"    {'And' if kind == prev else kind} {s['description']}")
        prev = kind
    return "\n".join(lines) + "\n"


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
                    parts.append(f"env('TESTGEN_{chunk.upper()}')" if chunk in CREDENTIALS
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
