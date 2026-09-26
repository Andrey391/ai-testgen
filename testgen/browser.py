"""Playwright wrapper: page snapshots for the LLM, actions, robust locators.

Every interactive element on the page gets a short ref (e1, e2, ...) that the
model uses to pick a target. When a step is recorded, the ref is turned into a
list of stable locator candidates (test id, role+name, label, placeholder, css)
so the saved test does not depend on refs and can be replayed / exported.
"""
from __future__ import annotations

import base64
import re
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

VIEWPORT = {"width": 1280, "height": 800}
PLACEHOLDER = re.compile(r"\{\{(username|password)\}\}")

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
({maxItems, point}) => {
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


def expand(credentials: dict, value: str) -> str:
    """Replace {{username}} / {{password}} with real values (only when a step executes)."""
    def sub(m: re.Match) -> str:
        if not credentials.get(m.group(1)):
            raise ValueError(f"No {m.group(1)} set for this application: add login credentials")
        return credentials[m.group(1)]
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

    def __init__(self, pw: Playwright, browser: Browser):
        self._pw = pw
        self._browser = browser
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.elements: dict[str, dict] = {}
        # Login for the app under test. Steps hold {{username}} / {{password}}
        # placeholders; real values are substituted only when a step executes.
        self.credentials: dict[str, str] = {}

    @classmethod
    async def launch(cls, headless: bool = True) -> "BrowserSession":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        s = cls(pw, browser)
        s.context = await browser.new_context(viewport=VIEWPORT, locale="en-US")
        s.page = await s.context.new_page()
        s.context.on("page", s._on_new_page)
        return s

    def _on_new_page(self, page: Page) -> None:
        # Links that open a new tab: follow them, like a user would.
        self.page = page

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._pw.stop()

    async def settle(self) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    async def snapshot(self, max_items: int = 150, point: tuple[float, float] | None = None) -> dict:
        await self.settle()
        snap = await self.page.evaluate(SNAPSHOT_JS, {"maxItems": max_items,
                                                      "point": list(point) if point else None})
        self.elements = {e["ref"]: e for e in snap["elements"]}
        return snap

    async def screenshot_b64(self) -> str:
        png = await self.page.screenshot(type="jpeg", quality=60)
        return base64.b64encode(png).decode()

    def expand(self, value: str) -> str:
        return expand(self.credentials, value)

    @property
    def url(self) -> str:
        return self.page.url if self.page else ""

    async def describe(self) -> str:
        """The current page for the model: URL, interactive elements with refs, visible text."""
        snap = await self.snapshot()
        lines = "\n".join(describe_element(e) for e in snap["elements"])
        return (f"URL: {snap['url']}\nTitle: {snap['title']}\n\n"
                f"Interactive elements:\n{lines or '(none)'}\n\n"
                f"Visible page text (truncated):\n{snap['text'][:2500]}")

    async def execute(self, step: dict) -> None:
        """Run a step on the live page; element steps carry a `ref` from the latest snapshot,
        which is turned into stable locators BEFORE acting (a click may navigate away)."""
        from .steps import ELEMENT_ACTIONS, perform
        loc = None
        ref = step.pop("ref", "")
        if step["action"] in ELEMENT_ACTIONS:
            if not ref:
                raise ValueError("This action needs an element ref")
            loc = self.by_ref(ref)
            step["locator"] = await self.unique_candidates(self.elements[ref])
        await perform(self, step, loc)

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

    async def find(self, locator: list[dict]):
        """Resolve a saved locator: first candidate that matches -> Locator, else None."""
        for cand in locator or []:
            try:
                loc = resolve(self.page, cand)
                n = await loc.count()
                if n >= 1:
                    return loc.first, cand
            except Exception:
                continue
        return None, None
