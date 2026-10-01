"""Playwright wrapper: page snapshots for the LLM, actions, robust locators.

Every interactive element on the page gets a short ref (e1, e2, ...) that the
model uses to pick a target. When a step is recorded, the ref is turned into a
list of stable locator candidates (test id, role+name, label, placeholder, css)
so the saved test does not depend on refs and can be replayed / exported.

The snapshot covers the whole page: every frame (payment forms, editors, old portals
are often iframes) and open shadow roots of web components. A candidate of an element
inside a frame carries "frame": the selectors of the iframes from the top page down,
resolved with frame_locator(). Every element gets a ref; the model's listing shows the
MAX_LISTED nearest to the visible area, `find_text()` finds the rest by text.

The session also collects what the page reports while it runs: console errors,
uncaught exceptions, failed requests and 4xx/5xx responses (`events`, used by
assert_no_console_errors and by failure analysis), and, when asked, the XHR/fetch
traffic of the scenario (`traffic`, for API tests and mocks; see traffic.py).
Dialogs (alert/confirm/prompt) are handled as a handle_dialog step armed them, or
dismissed and reported; downloads are kept for assert_download.
"""
from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Frame, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .testdata import CREDENTIALS, PLACEHOLDER, DataValues, totp

VIEWPORT = {"width": 1280, "height": 800}
MAX_EVENTS = 300
MAX_TRAFFIC = 500
MAX_LISTED = 150         # interactive elements listed to the model (the rest: find_text)
MAX_CONTENT = 40         # content elements listed (list items, headings, messages)
EVAL_TIMEOUT = 30        # seconds: a page script that never returns must not hang the studio
ENGINES = ("chromium", "firefox", "webkit")

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
      if (ty === 'file') return 'button';
      return 'textbox';
    }
    return '';
  };
  // Labels, ids and aria references live in the element's own tree (the document or a shadow root).
  const scope = el => el.getRootNode && el.getRootNode().querySelector ? el.getRootNode() : document;
  const labelText = el => {
    if (el.id) {
      const l = scope(el).querySelector(`label[for="${CSS.escape(el.id)}"]`);
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
      const root = scope(el);
      const t = lb.split(/\s+/).map(id => root.getElementById ? root.getElementById(id) : root.querySelector('#' + CSS.escape(id)))
        .filter(Boolean).map(e => e.innerText).join(' ');
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
  // A CSS path; inside a shadow root it is prefixed by the host's path. Playwright's CSS engine
  // pierces open shadow roots, so "host-path inner-path" finds the element again.
  const cssPath = el => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      if (cur.id && !/\d{3,}/.test(cur.id)) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      let part = cur.tagName.toLowerCase();
      const siblings = cur.parentNode && cur.parentNode.children ? [...cur.parentNode.children] : [];
      const same = siblings.filter(c => c.tagName === cur.tagName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(cur) + 1})`;
      parts.unshift(part);
      cur = cur.parentElement;     // null at the top of a shadow root
    }
    const root = el.getRootNode && el.getRootNode();
    const inner = parts.join(' > ');
    return root && root.host ? cssPath(root.host) + ' ' + inner : inner;
  };
  // Every element matching `sel`, inside open shadow roots too.
  const deepAll = (root, sel) => {
    const out = [...root.querySelectorAll(sel)];
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) out.push(...deepAll(el.shadowRoot, sel));
    return out;
  };
  const deepPoint = (x, y) => {
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    return hit;
  };
"""

SNAPSHOT_JS = r"""
({maxItems, contentItems, point, point2, start}) => {
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=combobox], [role=textbox], [role=searchbox], [role=switch], ' +
    '[contenteditable=""], [contenteditable=true], [onclick], [draggable=true]';
""" + _HELPERS_JS + r"""
  deepAll(document, '[data-tg-ref]').forEach(e => e.removeAttribute('data-tg-ref'));
  const els = deepAll(document, SEL).filter(visible);
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
  // Then content elements that assertions target: list items and rows (assert_count),
  // messages, headings and test-id blocks (assert_element_text). They are listed after the
  // interactive ones and never crowd them out.
  const CONTENT = 'li, tr, h1, h2, h3, output, [role=alert], [role=status], [role=listitem], [role=row], ' +
    TESTID_ATTRS.map(a => `[${a}]`).join(', ');
  const content = deepAll(document, CONTENT)
    .filter(el => !list.includes(el) && !el.matches(SEL) && visible(el) && clean(el.innerText))
    .map(el => ({el, r: el.getBoundingClientRect()}))
    .map(s => ({...s, inView: s.r.bottom > 0 && s.r.top < vh}))
    .sort((a, b) => (b.inView - a.inView) || (Math.abs(a.r.top) - Math.abs(b.r.top)))
    .slice(0, contentItems).map(s => s.el);
  const contentSet = new Set(content);
  list.push(...content);
  // Element picker: make sure the element under the cursor is described too,
  // even when it is not interactive (e.g. a message we want to assert on).
  const pickAt = p => {
    const hit = p ? deepPoint(p[0], p[1]) : null;
    if (!hit) return null;
    const el = (hit.closest && hit.closest(SEL)) || hit;
    if (!list.includes(el)) list.unshift(el);
    return el;
  };
  const picked = pickAt(point), picked2 = pickAt(point2);   // point2: the drop target of drag & drop
  const items = [];
  let n = start || 0, pickedRef = null, pickedRef2 = null;
  for (const el of list) {
    const r = el.getBoundingClientRect();
    const inView = r.bottom > 0 && r.top < vh;
    const ref = 'e' + (++n);
    if (el === picked) pickedRef = ref;
    if (el === picked2) pickedRef2 = ref;
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
      value: ['input', 'textarea'].includes(t) && el.type !== 'password' && el.type !== 'file' ? (el.value || '').slice(0, 60) : '',
      checked: (el.type === 'checkbox' || el.type === 'radio') ? el.checked : null,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      options: t === 'select' ? [...el.options].slice(0, 15).map(o => clean(o.text)) : [],
      css: cssPath(el),
      shadow: !!(el.getRootNode && el.getRootNode().host),
      content: contentSet.has(el),
      inView,
    });
  }
  const text = clean(document.body ? document.body.innerText : '').slice(0, 4000);
  return { url: location.href, title: document.title, elements: items, text, picked: pickedRef, picked2: pickedRef2 };
}
"""

# How to find an iframe element again from its parent frame.
FRAME_SELECTOR_JS = r"""el => {
  const q = (a, v) => `${el.tagName.toLowerCase()}[${a}="${CSS.escape(v)}"]`;
  if (el.id && !/\d{3,}/.test(el.id)) return '#' + CSS.escape(el.id);
  if (el.getAttribute('name')) return q('name', el.getAttribute('name'));
  if (el.getAttribute('data-testid')) return q('data-testid', el.getAttribute('data-testid'));
  if (el.getAttribute('title')) return q('title', el.getAttribute('title'));
  const src = el.getAttribute('src') || '';
  const path = src.split('?')[0].split('/').filter(Boolean).pop();
  if (path) return `${el.tagName.toLowerCase()}[src*="${CSS.escape(path)}"]`;
  const same = [...document.querySelectorAll(el.tagName)];
  return `${el.tagName.toLowerCase()} >> nth=${same.indexOf(el)}`;
}"""

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


def listed(elements: list[dict], interactive: int = MAX_LISTED, content: int = MAX_CONTENT) -> list[dict]:
    """The elements shown to a model: the nearest interactive ones, then content elements."""
    return [e for e in elements if not e.get("content")][:interactive] + \
        [e for e in elements if e.get("content")][:content]


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
    if e.get("frame"):
        bits.append(f"(in frame {' > '.join(e['frame'])})")
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
    if e.get("id") and not re.search(r"\d{3,}|^:r", e["id"]) and not e.get("shadow"):
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
        if e.get("frame"):
            cand = cand | {"frame": list(e["frame"])}
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
    return [x | {"frame": list(e["frame"])} if e.get("frame") else x for x in c]


def expand(credentials: dict, value: str, data: DataValues | None = None, variables: dict | None = None,
           params: dict | None = None) -> str:
    """Replace {{username}} / {{password}} / {{totp}} with real values, test data placeholders
    ({{unique}}, {{faker.email}}...) with generated ones, {{vars.x}} and {{params.x}} with the
    run's values - only when a step executes."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key == "totp":
            if not credentials.get("totp_secret"):
                raise ValueError("No TOTP secret set for this application: add it to the login settings")
            return totp(credentials["totp_secret"])
        if key in CREDENTIALS:
            if not credentials.get(key):
                raise ValueError(f"No {key} set for this application: add login credentials")
            return credentials[key]
        if key.startswith("vars."):
            name = key[5:]
            if variables is None or name not in variables:
                raise ValueError(f"Variable {{{{{key}}}}} has no value in this run (it is set by a 'before' "
                                 "request or read_email step)")
            return str(variables[name])
        if key.startswith("params."):
            name = key[7:]
            if params is None or name not in params:
                raise ValueError(f"Module parameter {{{{{key}}}}} is not given")
            return str(params[name])
        return (data if data is not None else DataValues())[key]
    return PLACEHOLDER.sub(sub, value or "")


def css_escape(s: str) -> str:
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", s)


def frame_root(page: Page, frame: list[str] | None):
    """The page, or the frame an element lives in (iframe selectors from the top page down)."""
    root = page
    for sel in frame or []:
        root = root.frame_locator(sel)
    return root


def resolve(page: Page, cand: dict):
    root = frame_root(page, cand.get("frame"))
    k = cand["kind"]
    if k == "testid":
        return root.get_by_test_id(cand["value"])
    if k == "role":
        return root.get_by_role(cand["role"], name=cand["name"], exact=True)
    if k == "label":
        return root.get_by_label(cand["value"], exact=True)
    if k == "placeholder":
        return root.get_by_placeholder(cand["value"], exact=True)
    if k == "text":
        return root.get_by_text(cand["value"], exact=True)
    return root.locator(cand["value"])


DEVICES: dict[str, dict] = {}      # Playwright's device profiles, filled by the first launch


def context_options(device: str = "", locale: str = "", timezone: str = "",
                    storage_state: dict | None = None, engine: str = "chromium") -> dict:
    """new_context() arguments: a device profile (iPhone, Pixel...), locale, time zone, a saved login."""
    opts: dict = {"viewport": VIEWPORT}
    if device and device != "desktop":
        if device not in DEVICES:
            raise ValueError(f"Неизвестное устройство «{device}» (например: iPhone 13, Pixel 7, iPad Mini)")
        opts = dict(DEVICES[device])
        opts.pop("default_browser_type", None)
        if engine == "firefox":
            opts.pop("is_mobile", None)       # Firefox has no mobile emulation: size, touch and user agent only
    opts["locale"] = locale or "en-US"
    if timezone:
        opts["timezone_id"] = timezone
    if storage_state:
        opts["storage_state"] = storage_state
    opts["accept_downloads"] = True
    return opts


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
        self.vars: dict[str, str] = {}      # {{vars.x}}: saved by before-requests and read_email
        self.params: list[dict] = []        # {{params.x}}: a stack, one entry per module being run
        self.events: list[dict] = []        # console errors, page errors, failed and 4xx/5xx requests
        self.traffic: list[dict] | None = None   # XHR/fetch exchanges, when recording
        self.step_index = -1                # the step being executed, for events and traffic
        self.options: dict = {}             # project, test, run dir and thresholds for checks.py
        self.follow_new_tabs = True         # a link opening a tab: go there (tests with switch_tab: no)
        self.new_tabs = 0                   # tabs opened since the last check (the agent records switch_tab)
        self.downloads: list[dict] = []     # files the page downloaded: assert_download
        self._downloads_checked = 0
        self.dialog_plan: dict | None = None    # armed by handle_dialog: what to do with the next dialog
        self.dialog_error = ""              # an armed dialog that did not look as expected
        self.dialogs: list[dict] = []       # dialogs seen: type, message, what was done
        self._errors_checked = 0            # assert_no_console_errors looks at errors after this
        self._frame_paths: dict[Frame, list[str]] = {}
        self._pending: set[asyncio.Task] = set()

    @classmethod
    async def launch(cls, headless: bool = True, browser: Browser | None = None, record_traffic: bool = False,
                     engine: str = "chromium", device: str = "", locale: str = "", timezone: str = "",
                     storage_state: dict | None = None) -> "BrowserSession":
        """A fresh context and page. With `browser` the context is created in that
        shared browser, which close() then leaves running. `engine`: chromium | firefox |
        webkit; `device`: a Playwright device name ("iPhone 13", "Pixel 7")."""
        pw = None
        if browser is None:
            pw = await async_playwright().start()
            DEVICES.update(pw.devices)
            try:
                browser = await getattr(pw, engine if engine in ENGINES else "chromium").launch(headless=headless)
            except Exception:
                await pw.stop()
                raise
        elif device and not DEVICES:
            tmp = await async_playwright().start()
            DEVICES.update(tmp.devices)
            await tmp.stop()
        s = cls(pw, browser)
        s.context = await browser.new_context(**context_options(device, locale, timezone, storage_state,
                                                                browser.browser_type.name))
        s.page = await s.context.new_page()
        s._watch_page(s.page)
        s.context.on("page", s._on_new_page)
        s.context.on("console", s._on_console)
        s.context.on("weberror", s._on_page_error)
        s.context.on("response", s._on_response)
        s.context.on("requestfailed", s._on_request_failed)
        if record_traffic:
            s.traffic = []
            s.context.on("requestfinished", s._on_request_finished)
        return s

    def _watch_page(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)

    def _on_new_page(self, page: Page) -> None:
        self._watch_page(page)
        self.new_tabs += 1
        # Links that open a new tab: follow them, like a user would (unless the test switches itself).
        if self.follow_new_tabs:
            self.page = page

    async def close(self) -> None:
        for t in list(self._pending):
            t.cancel()
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

    def secrets(self) -> list[str]:
        return [v for k, v in self.credentials.items() if k in ("password", "totp_secret") and v]

    def mask(self, text: str) -> str:
        for s in self.secrets():
            if text:
                text = text.replace(s, "***")
        return text

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

    def _on_dialog(self, dialog) -> None:
        """alert / confirm / prompt: do what handle_dialog armed, else dismiss and report."""
        plan, self.dialog_plan = self.dialog_plan, None
        record = {"type": dialog.type, "message": self.mask(dialog.message), "step": self.step_index}
        if plan:
            expect = plan.get("expect") or ""
            if expect and expect not in dialog.message:
                self.dialog_error = f"Диалог «{self.mask(dialog.message)[:200]}» не содержит «{expect}»"
            action = "accept" if plan.get("action", "accept") == "accept" else "dismiss"
            task = asyncio.ensure_future(dialog.accept(self.expand(plan.get("prompt_text") or ""))
                                         if action == "accept" and dialog.type == "prompt"
                                         else dialog.accept() if action == "accept" else dialog.dismiss())
        else:
            action = "dismissed"
            task = asyncio.ensure_future(dialog.dismiss())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        self.dialogs.append(record | {"action": action})
        self._event("dialog", f"{dialog.type}: {dialog.message}")

    def _on_download(self, download) -> None:
        async def keep():
            try:
                path = await download.path()
                size = Path(path).stat().st_size if path else 0
            except Exception:
                path, size = None, -1
            self.downloads.append({"name": download.suggested_filename, "path": str(path or ""), "size": size,
                                   "url": download.url[:300], "step": self.step_index})
        task = asyncio.ensure_future(keep())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def console_errors(self) -> list[dict]:
        """Console errors and uncaught exceptions since the previous check. "Failed to load
        resource" messages are left out: failed requests are reported as http events."""
        new = self.events[self._errors_checked:]
        self._errors_checked = len(self.events)
        return [e for e in new if e["type"] == "pageerror"
                or (e["type"] == "console" and not e["text"].startswith("Failed to load resource"))]

    async def new_downloads(self, wait: float = 10) -> list[dict]:
        """Downloads since the previous check, waiting up to `wait` seconds for the first one."""
        deadline = time.monotonic() + wait
        while len(self.downloads) <= self._downloads_checked and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        new = self.downloads[self._downloads_checked:]
        self._downloads_checked = len(self.downloads)
        return new

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

    async def switch_tab(self, which: str, wait: float = 5) -> None:
        """switch_tab: "last" | "first" | a number (1 = the first tab) | text in the tab's URL.
        A tab opened by the previous step may appear a moment later: wait for it."""
        which = (which or "last").strip()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            pages = [p for p in self.context.pages if not p.is_closed()]
            if which == "first" or (which == "last" and (len(pages) > 1 and (pages[-1] is not self.page
                                                                            or self.new_tabs))) \
                    or (which.isdigit() and len(pages) >= int(which)) \
                    or (not which.isdigit() and which not in ("last", "first") and any(which in p.url for p in pages)):
                break
            await asyncio.sleep(0.2)
        pages = [p for p in self.context.pages if not p.is_closed()]
        if not pages:
            raise ValueError("Нет открытых вкладок")
        if which == "last":
            page = pages[-1]
        elif which == "first":
            page = pages[0]
        elif which.isdigit():
            i = int(which) - 1
            if not 0 <= i < len(pages):
                raise ValueError(f"Вкладки {which} нет: открыто {len(pages)}")
            page = pages[i]
        else:
            page = next((p for p in pages if which in p.url), None)
            if page is None:
                raise ValueError(f"Нет вкладки с адресом, содержащим «{which}»")
        self.page = page
        self.new_tabs = 0
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

    # ---------- the page ----------

    async def settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    async def _frame_path(self, frame: Frame) -> list[str] | None:
        """Selectors of the iframes from the top page down to `frame` (None: cannot be addressed)."""
        if frame == self.page.main_frame:
            return []
        if frame in self._frame_paths:
            return self._frame_paths[frame]
        parent = frame.parent_frame
        if parent is None:
            return None
        parent_path = await self._frame_path(parent)
        if parent_path is None:
            return None
        try:
            el = await frame.frame_element()
            sel = await el.evaluate(FRAME_SELECTOR_JS)
        except Exception:
            return None
        path = parent_path + [sel]
        self._frame_paths[frame] = path
        return path

    async def _evaluate(self, frame: Frame, script: str, arg):
        return await asyncio.wait_for(frame.evaluate(script, arg), EVAL_TIMEOUT)

    async def snapshot(self, max_items: int = 1500, point: tuple[float, float] | None = None,
                       content_items: int = 300, point2: tuple[float, float] | None = None) -> dict:
        """Every frame of the page: elements with refs (unique across frames), the top page's
        text, the element under `point` (Element Picker, with coordinates of the top page) and
        under `point2` (the drop target of drag & drop)."""
        for attempt in range(4):
            await self.settle()
            try:
                snap = await self._snapshot_frames(max_items, point, content_items, point2)
                break
            except asyncio.TimeoutError:
                raise RuntimeError(f"Страница не ответила на снимок за {EVAL_TIMEOUT} с (скрипт страницы "
                                   "занял поток или открыт модальный диалог)") from None
            except PlaywrightError as e:
                # A navigation started by the page itself (a redirect after a request) replaced
                # the document under us: wait for the new one and look again.
                if attempt == 3 or ("context was destroyed" not in str(e) and "navigat" not in str(e)
                                    and "detached" not in str(e)):
                    raise
                await asyncio.sleep(0.3)
        self.elements = {e["ref"]: e for e in snap["elements"]}
        return snap

    async def _snapshot_frames(self, max_items: int, point, content_items: int, point2=None) -> dict:
        main = await self._evaluate(self.page.main_frame, SNAPSHOT_JS, {
            "maxItems": max_items, "contentItems": content_items, "point": list(point) if point else None,
            "point2": list(point2) if point2 else None, "start": 0})
        elements = main["elements"]
        picked, picked2 = main["picked"], main.get("picked2")
        texts = []
        for frame in self.page.frames[1:]:
            if frame.is_detached() or frame.url in ("", "about:blank"):
                continue
            path = await self._frame_path(frame)
            if path is None:
                continue
            box = None
            if point or point2:
                try:
                    box = await (await frame.frame_element()).bounding_box()
                except Exception:
                    box = None

            def inside(p):
                if not p or not box or not (box["x"] <= p[0] <= box["x"] + box["width"]
                                            and box["y"] <= p[1] <= box["y"] + box["height"]):
                    return None
                return [p[0] - box["x"], p[1] - box["y"]]
            try:
                sub = await self._evaluate(frame, SNAPSHOT_JS, {"maxItems": max_items, "contentItems": content_items,
                                                                "point": inside(point), "point2": inside(point2),
                                                                "start": len(elements)})
            except (PlaywrightError, asyncio.TimeoutError):
                continue
            for e in sub["elements"]:
                e["frame"] = path
            elements += sub["elements"]
            picked = sub["picked"] or picked            # the deepest frame under the cursor wins
            picked2 = sub.get("picked2") or picked2
            if sub["text"]:
                texts.append(f"[frame {' > '.join(path)}] {sub['text'][:600]}")
        return {"url": main["url"], "title": main["title"], "elements": elements,
                "text": " ".join([main["text"]] + texts), "picked": picked, "picked2": picked2}

    async def screenshot_b64(self) -> str:
        png = await self.page.screenshot(type="jpeg", quality=60, timeout=15000)
        return base64.b64encode(png).decode()

    def expand(self, value: str) -> str:
        return expand(self.credentials, value, self.testdata, self.vars, self.params[-1] if self.params else {})

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    async def describe(self) -> str:
        """The current page for the model: URL, elements with refs, visible text. Only the
        elements nearest to the visible area are listed; find_text() searches all of them."""
        snap = await self.snapshot()
        shown = listed(snap["elements"])
        hidden = len(snap["elements"]) - len(shown)
        lines = "\n".join(describe_element(e) for e in shown)
        more = (f"\n({hidden} more elements are not listed: call find_elements with a text to find them)"
                if hidden > 0 else "")
        tabs = [p for p in self.context.pages if not p.is_closed()]
        tabs_note = f"Tabs: {len(tabs)} open, this is tab {tabs.index(self.page) + 1}\n" if len(tabs) > 1 else ""
        return (f"URL: {snap['url']}\nTitle: {snap['title']}\n{tabs_note}\n"
                f"Elements (interactive ones first, then content for assertions):\n{lines or '(none)'}{more}\n\n"
                f"Visible page text (truncated):\n{snap['text'][:2500]}")

    def find_text(self, text: str, limit: int = 20) -> list[dict]:
        """Elements of the latest snapshot whose name, label, placeholder, value or test id contain `text`."""
        needle = (text or "").strip().lower()
        if not needle:
            return []
        keys = ("name", "label", "placeholder", "value", "testid", "href", "id")
        return [e for e in self.elements.values() if any(needle in str(e.get(k) or "").lower() for k in keys)][:limit]

    async def execute(self, step: dict) -> None:
        """Run a step on the live page; element steps carry a `ref` from the latest snapshot,
        which is turned into stable locators BEFORE acting (a click may navigate away).

        An assertion recorded without an expected value (Element Picker) takes the
        element's current state: its value, text, checked / enabled state, or the
        number of elements in its group."""
        from .steps import ELEMENT_ACTIONS, OPTIONAL_ELEMENT, perform
        loc = None
        ref = step.pop("ref", "")
        target_ref = step.pop("target_ref", "")
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
            if a == "drag_to":
                if not target_ref:
                    raise ValueError("drag_to needs the ref of the target element")
                self.by_ref(target_ref)
                step["target"] = await self.unique_candidates(self.elements[target_ref])
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
        return frame_root(self.page, self.elements[ref].get("frame")).locator(f'[data-tg-ref="{ref}"]').first

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

    async def pick_at(self, x: float, y: float, target: tuple[float, float] | None = None):
        """Element under viewport point (x, y), with fresh refs for the whole page; with `target`
        -> (element, element under the target point) for drag & drop."""
        snap = await self.snapshot(point=(x, y), point2=target)
        e = self.elements.get(snap.get("picked") or "")
        if target is None:
            return e
        return e, self.elements.get(snap.get("picked2") or "")

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
