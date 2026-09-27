"""Playwright wrapper: page snapshots for the LLM, actions, robust locators.

Every interactive element on the page gets a short ref (e1, e2, ...) that the
model uses to pick a target. When a step is recorded, the ref is turned into a
list of stable locator candidates (test id, role+name, label, placeholder, css)
so the saved test does not depend on refs and can be replayed / exported.

The session also collects what the page reports while it runs: console errors,
uncaught exceptions, failed requests and 4xx/5xx responses (`events`, used by
assert_no_console_errors and by failure analysis), and, when asked, the XHR/fetch
traffic of the scenario (`traffic`, for API tests and mocks; see traffic.py).
"""
from __future__ import annotations

import asyncio
import base64
import re
import time

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .testdata import CREDENTIALS, PLACEHOLDER, DataValues

VIEWPORT = {"width": 1280, "height": 800}
MAX_EVENTS = 300
MAX_TRAFFIC = 500

# Helpers shared by the page snapshot and the single-element probe (Playwright MCP engine).
_HELPERS_JS = r"""
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity) > 0.05;
  };
  const implicitRole = el => {
    const t = el.tagName.toLowerCase();
    const role = el.getAttribute('role');
    if (role) return role;
    if (t === 'a') return 'link';
    if (t === 'button' || t === 'summary') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'search') return 'searchbox';
      if (ty === 'range') return 'slider';
      return 'textbox';
    }
    return '';
  };
  const labelText = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const p = el.closest('label');
    return p ? clean(p.innerText) : '';
  };
  const accName = el => {
    const al = el.getAttribute('aria-label');
    if (al) return clean(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean)
        .map(e => e.innerText).join(' ');
      if (clean(t)) return clean(t);
    }
    const t = el.tagName.toLowerCase();
    if (['input', 'select', 'textarea'].includes(t)) {
      const lt = labelText(el);
      if (lt) return lt;
      const ty = (el.getAttribute('type') || '').toLowerCase();
      if (['button', 'submit', 'reset'].includes(ty)) return clean(el.value);
      return clean(el.getAttribute('placeholder') || el.getAttribute('title') || '');
    }
    const txt = clean(el.innerText);
    if (txt) return txt.slice(0, 100);
    const img = el.querySelector('img[alt]');
    if (img) return clean(img.getAttribute('alt'));
    return clean(el.getAttribute('title') || '');
  };
  const cssPath = el => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      if (cur.id && !/\d{3,}/.test(cur.id)) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = [...parent.children].filter(c => c.tagName === cur.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  };
"""

SNAPSHOT_JS = r"""
({maxItems, contentItems, point}) => {
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=combobox], [role=textbox], [role=searchbox], [role=switch], ' +
    '[contenteditable=""], [contenteditable=true], [onclick]';
  document.querySelectorAll('[data-tg-ref]').forEach(e => e.removeAttribute('data-tg-ref'));
""" + _HELPERS_JS + r"""
  const els = [...document.querySelectorAll(SEL)].filter(visible);
  const vh = window.innerHeight;
  const scored = els.map(el => {
    const r = el.getBoundingClientRect();
    const inView = r.bottom > 0 && r.top < vh;
    return { el, r, inView };
  });
  // Elements in the viewport first, then by distance from it.
  scored.sort((a, b) => (b.inView - a.inView) || (Math.abs(a.r.top) - Math.abs(b.r.top)));

  const TESTID_ATTRS = ['data-testid', 'data-test', 'data-qa', 'data-test-id', 'data-cy'];
  const list = scored.slice(0, maxItems).map(s => s.el);
  // Then a few content elements that assertions target: list items and rows (assert_count),
  // messages, headings and test-id blocks (assert_element_text). They are listed after the
  // interactive ones and never crowd them out.
  const CONTENT = 'li, tr, h1, h2, h3, output, [role=alert], [role=status], [role=listitem], [role=row], ' +
    TESTID_ATTRS.map(a => `[${a}]`).join(', ');
  const content = [...document.querySelectorAll(CONTENT)]
    .filter(el => !list.includes(el) && !el.matches(SEL) && visible(el) && clean(el.innerText))
    .map(el => ({el, r: el.getBoundingClientRect()}))
    .map(s => ({...s, inView: s.r.bottom > 0 && s.r.top < vh}))
    .sort((a, b) => (b.inView - a.inView) || (Math.abs(a.r.top) - Math.abs(b.r.top)))
    .slice(0, contentItems).map(s => s.el);
  list.push(...content);
  // Element picker: make sure the element under the cursor is described too,
  // even when it is not interactive (e.g. a message we want to assert on).
  let picked = null;
  if (point) {
    const hit = document.elementFromPoint(point[0], point[1]);
    if (hit) {
      picked = hit.closest(SEL) || hit;
      if (!list.includes(picked)) list.unshift(picked);
    }
  }
  const items = [];
  let n = 0, pickedRef = null;
  for (const el of list) {
    const r = el.getBoundingClientRect();
    const inView = r.bottom > 0 && r.top < vh;
    const ref = 'e' + (++n);
    if (el === picked) pickedRef = ref;
    el.setAttribute('data-tg-ref', ref);
    const t = el.tagName.toLowerCase();
    items.push({
      ref, tag: t, role: implicitRole(el), name: accName(el),
      type: el.getAttribute('type') || '',
      placeholder: el.getAttribute('placeholder') || '',
      label: ['input', 'select', 'textarea'].includes(t) ? labelText(el) : '',
      testid_attr: TESTID_ATTRS.find(a => el.getAttribute(a)) || '',
      testid: (TESTID_ATTRS.map(a => el.getAttribute(a)).find(Boolean)) || '',
      id: el.id || '',
      href: t === 'a' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      value: ['input', 'textarea'].includes(t) && el.type !== 'password' ? (el.value || '').slice(0, 60) : '',
      checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      options: t === 'select' ? [...el.options].slice(0, 15).map(o => clean(o.text)) : [],
      css: cssPath(el),
      inView,
    });
  }
  const text = clean(document.body ? document.body.innerText : '').slice(0, 4000);
  return { url: location.href, title: document.title, elements: items, text, picked: pickedRef };
}
"""

# Same fields as a snapshot item, for one element (used via browser_evaluate).
ELEMENT_INFO_JS = r"""(el) => {
  const TESTID_ATTRS = ['data-testid', 'data-test', 'data-qa', 'data-test-id', 'data-cy'];
""" + _HELPERS_JS + r"""
  const t = el.tagName.toLowerCase();
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return {
    tag: t, role: implicitRole(el), name: accName(el), type: el.getAttribute('type') || '',
    placeholder: el.getAttribute('placeholder') || '',
    label: ['input', 'select', 'textarea'].includes(t) ? labelText(el) : '',
    testid_attr: TESTID_ATTRS.find(a => el.getAttribute(a)) || '',
    testid: (TESTID_ATTRS.map(a => el.getAttribute(a)).find(Boolean)) || '',
    id: el.id || '', css: cssPath(el),
    visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
  };
}"""


def describe_element(e: dict) -> str:
    """One compact line per element for the model."""
    bits = [f"[{e['ref']}]", e["role"] or e["tag"]]
    if e["name"]:
        bits.append(f'"{e["name"][:80]}"')
    for key in ("placeholder", "type", "href", "value"):
        if e.get(key) and not (key == "type" and e["tag"] != "input"):
            bits.append(f"{key}={e[key]!r}")
    if e.get("options"):
        bits.append(f"options={e['options']}")
    if e.get("checked") is not None:
        bits.append("checked" if e["checked"] else "unchecked")
    if e.get("disabled"):
        bits.append("disabled")
    if not e.get("inView"):
        bits.append("(off-screen)")
    return " ".join(bits)


def locator_candidates(e: dict) -> list[dict]:
    """Stable ways to find this element again, most robust first."""
    c: list[dict] = []
    if e.get("testid"):
        attr = e.get("testid_attr") or "data-testid"
        if attr == "data-testid":
            c.append({"kind": "testid", "value": e["testid"]})
        else:
            c.append({"kind": "css", "value": f'[{attr}="{e["testid"]}"]'})
    if e.get("id") and not re.search(r"\d{3,}|^:r", e["id"]):
        c.append({"kind": "css", "value": "#" + css_escape(e["id"])})
    if e.get("role") and e.get("name") and len(e["name"]) <= 80:
        c.append({"kind": "role", "role": e["role"], "name": e["name"]})
    if e.get("label"):
        c.append({"kind": "label", "value": e["label"]})
    if e.get("placeholder"):
        c.append({"kind": "placeholder", "value": e["placeholder"]})
    if e.get("name") and e.get("tag") not in ("input", "select", "textarea") and len(e["name"]) <= 60:
        c.append({"kind": "text", "value": e["name"]})
    if e.get("css"):
        c.append({"kind": "css", "value": e["css"]})
    unique = []
    for cand in c:
        if cand not in unique:
            unique.append(cand)
    return unique




def group_candidates(e: dict) -> list[dict]:
    """Locators for the group an element belongs to (list items, table rows, cards):
    used by assert_count, so unlike locator_candidates they may match many elements."""
    c: list[dict] = []
    if e.get("testid"):
        attr = e.get("testid_attr") or "data-testid"
        c.append({"kind": "testid", "value": e["testid"]} if attr == "data-testid"
                 else {"kind": "css", "value": f'[{attr}="{e["testid"]}"]'})
    css = e.get("css") or ""
    if re.search(r":nth-of-type\(\d+\)$", css):
        c.append({"kind": "css", "value": re.sub(r":nth-of-type\(\d+\)$", "", css)})
    return c


def expand(credentials: dict, value: str, data: DataValues | None = None) -> str:
    """Replace {{username}} / {{password}} with real values and test data placeholders
    ({{unique}}, {{faker.email}}...) with generated ones - only when a step executes."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in CREDENTIALS:
            if not credentials.get(key):
                raise ValueError(f"No {key} set for this application: add login credentials")
            return credentials[key]
        return (data if data is not None else DataValues())[key]
    return PLACEHOLDER.sub(sub, value or "")


def css_escape(s: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", s)


def resolve(page: Page, cand: dict):
    k = cand["kind"]
    if k == "testid":
        return page.get_by_test_id(cand["value"])
    if k == "role":
        return page.get_by_role(cand["role"], name=cand["name"], exact=True)
    if k == "label":
        return page.get_by_label(cand["value"], exact=True)
    if k == "placeholder":
        return page.get_by_placeholder(cand["value"], exact=True)
    if k == "text":
        return page.get_by_text(cand["value"], exact=True)
    return page.locator(cand["value"])


class BrowserSession:
    """One browser page driven by the agent, the recorder or the replayer.

    The authoring agent talks to a browser engine through `describe()`,
    `screenshot_b64()`, `execute()`, `url` and `close()`; mcp_browser.McpBrowser
    implements the same interface on top of Playwright MCP.
    """

    engine = "builtin"

    def __init__(self, pw: Playwright | None, browser: Browser):
        self._pw = pw                       # None: a shared browser (suite run), not ours to close
        self._browser = browser
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.elements: dict[str, dict] = {}
        # Login for the app under test. Steps hold {{username}} / {{password}}
        # placeholders; real values are substituted only when a step executes.
        self.credentials: dict[str, str] = {}
        self.testdata = DataValues()        # {{unique}}, {{faker.email}}...: one value per run
        self.events: list[dict] = []        # console errors, page errors, failed and 4xx/5xx requests
        self.traffic: list[dict] | None = None   # XHR/fetch exchanges, when recording
        self.step_index = -1                # the step being executed, for events and traffic
        self.options: dict = {}             # project, test, run dir and thresholds for checks.py
        self._errors_checked = 0            # assert_no_console_errors looks at errors after this

    @classmethod
    async def launch(cls, headless: bool = True, browser: Browser | None = None,
                     record_traffic: bool = False) -> "BrowserSession":
        """A fresh context and page. With `browser` the context is created in that
        shared browser, which close() then leaves running."""
        pw = None
        if browser is None:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=headless)
        s = cls(pw, browser)
        s.context = await browser.new_context(viewport=VIEWPORT, locale="en-US")
        s.page = await s.context.new_page()
        s.context.on("page", s._on_new_page)
        s.context.on("console", s._on_console)
        s.context.on("weberror", s._on_page_error)
        s.context.on("response", s._on_response)
        s.context.on("requestfailed", s._on_request_failed)
        if record_traffic:
            s.traffic = []
            s.context.on("requestfinished", s._on_request_finished)
        return s

    def _on_new_page(self, page: Page) -> None:
        # Links that open a new tab: follow them, like a user would.
        self.page = page

    async def close(self) -> None:
        if self._pw is None:
            await self.context.close()
            return
        try:
            await self._browser.close()
        finally:
            await self._pw.stop()

    async def scratch_page(self) -> Page:
        """A blank page in a separate context of the same browser, for work that must
        not touch the page under test (image comparison)."""
        return await self._browser.new_page()

    # ---------- what the page reports ----------

    def mask(self, text: str) -> str:
        pw = self.credentials.get("password")
        return text.replace(pw, "***") if pw and text else text

    def _event(self, kind: str, text: str, **extra) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append({"type": kind, "text": self.mask(text)[:500], "step": self.step_index,
                                "at": time.time()}
                               | {k: self.mask(v) if isinstance(v, str) else v for k, v in extra.items()})

    def _on_console(self, msg) -> None:
        if msg.type == "error":
            self._event("console", msg.text, url=(msg.location or {}).get("url", ""))

    def _on_page_error(self, error) -> None:
        self._event("pageerror", str(error.error))

    def _on_response(self, response) -> None:
        if response.status >= 400:
            self._event("http", f"{response.status} {response.request.method} {response.url}",
                        status=response.status, url=response.url[:300])

    def _on_request_failed(self, request) -> None:
        self._event("network", f"{request.method} {request.url[:300]}: {request.failure or 'failed'}",
                    url=request.url[:300])

    async def _on_request_finished(self, request) -> None:
        if request.resource_type not in ("xhr", "fetch") or len(self.traffic) >= MAX_TRAFFIC:
            return
        from .traffic import capture
        step = self.step_index
        try:
            entry = await capture(request, self.credentials)
        except Exception:
            return
        if entry:
            self.traffic.append(entry | {"step": step})

    def console_errors(self) -> list[dict]:
        """Console errors and uncaught exceptions since the previous check. "Failed to load
        resource" messages are left out: failed requests are reported as http events."""
        new = self.events[self._errors_checked:]
        self._errors_checked = len(self.events)
        return [e for e in new if e["type"] == "pageerror"
                or (e["type"] == "console" and not e["text"].startswith("Failed to load resource"))]

    async def mock(self, spec: dict) -> None:
        """mock_route: answer requests matching spec["url"] (a glob) with a recorded response."""
        method = (spec.get("method") or "").upper()

        async def handler(route):
            if method and route.request.method != method:
                await route.fallback()
                return
            await route.fulfill(status=int(spec.get("status") or 200), body=spec.get("body") or "",
                                content_type=spec.get("content_type") or "application/json")

        await self.context.route(spec["url"], handler)

    # ---------- the page ----------

    async def settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    async def snapshot(self, max_items: int = 150, point: tuple[float, float] | None = None,
                       content_items: int = 40) -> dict:
        for attempt in range(4):
            await self.settle()
            try:
                snap = await self.page.evaluate(SNAPSHOT_JS, {"maxItems": max_items, "contentItems": content_items,
                                                              "point": list(point) if point else None})
                break
            except PlaywrightError as e:
                # A navigation started by the page itself (a redirect after a request) replaced
                # the document under us: wait for the new one and look again.
                if attempt == 3 or "context was destroyed" not in str(e) and "navigat" not in str(e):
                    raise
                await asyncio.sleep(0.3)
        self.elements = {e["ref"]: e for e in snap["elements"]}
        return snap

    async def screenshot_b64(self) -> str:
        png = await self.page.screenshot(type="jpeg", quality=60)
        return base64.b64encode(png).decode()

    def expand(self, value: str) -> str:
        return expand(self.credentials, value, self.testdata)

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    async def describe(self) -> str:
        """The current page for the model: URL, elements with refs, visible text."""
        snap = await self.snapshot()
        lines = "\n".join(describe_element(e) for e in snap["elements"])
        return (f"URL: {snap['url']}\nTitle: {snap['title']}\n\n"
                f"Elements (interactive ones first, then content for assertions):\n{lines or '(none)'}\n\n"
                f"Visible page text (truncated):\n{snap['text'][:2500]}")

    async def execute(self, step: dict) -> None:
        """Run a step on the live page; element steps carry a `ref` from the latest snapshot,
        which is turned into stable locators BEFORE acting (a click may navigate away).

        An assertion recorded without an expected value (Element Picker) takes the
        element's current state: its value, text, checked / enabled state, or the
        number of elements in its group."""
        from .steps import ELEMENT_ACTIONS, OPTIONAL_ELEMENT, perform
        loc = None
        ref = step.pop("ref", "")
        a = step["action"]
        if a in ELEMENT_ACTIONS or (a in OPTIONAL_ELEMENT and ref):
            if not ref:
                raise ValueError("This action needs an element ref")
            loc = self.by_ref(ref)
            e = self.elements[ref]
            if a == "assert_count":
                step["locator"] = [c for c in group_candidates(e) if await resolve(self.page, c).count() > 0]
                if not step["locator"]:
                    raise ValueError("This element is not part of a list or group of similar elements")
            else:
                step["locator"] = await self.unique_candidates(e)
            if not step.get("value"):
                step["value"] = await self._current_state(a, loc, e, step)
        await perform(self, step, loc)

    async def _current_state(self, action: str, loc, e: dict, step: dict) -> str:
        if action == "assert_value":
            return await loc.input_value(timeout=5000)
        if action == "assert_checked":
            return str(await loc.is_checked(timeout=5000)).lower()
        if action == "assert_enabled":
            return str(await loc.is_enabled(timeout=5000)).lower()
        if action == "assert_element_text":
            return e.get("name") or (await loc.inner_text(timeout=5000)).strip()[:100]
        if action == "assert_count":
            return str(await resolve(self.page, step["locator"][0]).count())
        return ""

    def by_ref(self, ref: str):
        if ref not in self.elements:
            raise ValueError(f"Unknown element ref '{ref}'. Use a ref from the latest page snapshot.")
        return self.page.locator(f'[data-tg-ref="{ref}"]').first

    async def unique_candidates(self, e: dict) -> list[dict]:
        """Keep candidates that match exactly one element on the current page."""
        good = []
        for cand in locator_candidates(e):
            try:
                if await resolve(self.page, cand).count() == 1:
                    good.append(cand)
            except Exception:
                continue
        return good or locator_candidates(e)[-1:]

    async def pick_at(self, x: float, y: float) -> dict | None:
        """Element under viewport point (x, y), with fresh refs for the whole page."""
        snap = await self.snapshot(point=(x, y))
        return self.elements.get(snap.get("picked") or "")

    async def find(self, locator: list[dict], wait: float = 0):
        """Resolve a saved locator: first candidate that matches -> Locator, else None.
        With `wait` (seconds) keep looking while the page is still changing, e.g. right
        after a click that navigates: healing is only for elements that really are gone."""
        deadline = time.monotonic() + wait
        while True:
            for cand in locator or []:
                try:
                    loc = resolve(self.page, cand)
                    n = await loc.count()
                    if n >= 1:
                        return loc.first, cand
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return None, None
            await asyncio.sleep(0.25)

    async def find_all(self, locator: list[dict]):
        """A group locator (assert_count): the first candidate that matches anything,
        else the first candidate, so that "0 elements" can still be asserted."""
        for cand in locator or []:
            try:
                loc = resolve(self.page, cand)
                if await loc.count() >= 1:
                    return loc
            except Exception:
                continue
        if not locator:
            raise ValueError("No locator for this step")
        return resolve(self.page, locator[0])
