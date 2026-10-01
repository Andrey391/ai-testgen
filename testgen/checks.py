"""Heavier assertions: accessibility (axe-core) and visual regression.

assert_accessible runs axe-core in the page and fails on WCAG 2.x A/AA violations
of at least the project's impact threshold (run.a11y_impact). axe-core is not
shipped with the studio and its source is not hard-coded: TESTGEN_AXE_JS is a
path to axe.min.js, TESTGEN_AXE_URL an address it is downloaded from once (kept
in data/cache/). Without either the check fails with a hint.

assert_screenshot compares the page (or one element) with a baseline image. The
first run of a saved test stores the baseline in
data/projects/<id>/baselines/<test>/<step>.png; later runs compare pixel by pixel
in a scratch browser page (canvas), with dynamic zones masked by the step's CSS
selectors. The actual image and a diff image are kept with the run, so a person
can look at them and accept the new look as the baseline.

A failed check raises CheckFailed with `details` for the run report.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path

from . import fs, projects
from .paths import OFFLINE

WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
IMPACTS = ["minor", "moderate", "serious", "critical"]
DEFAULT_VISUAL_THRESHOLD = 1.0   # percent of differing pixels

_axe: str | None = None


class CheckFailed(AssertionError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


# ---------- accessibility ----------

async def axe_source() -> str:
    global _axe
    if _axe:
        return _axe
    custom = os.environ.get("TESTGEN_AXE_JS")
    if custom:
        _axe = Path(custom).read_text("utf-8")
        return _axe
    url = os.environ.get("TESTGEN_AXE_URL", "").strip()
    if not url:
        raise RuntimeError("Проверка доступности не настроена: укажите путь к axe.min.js в TESTGEN_AXE_JS "
                           "или адрес для загрузки в TESTGEN_AXE_URL")
    cache = projects.DATA / "cache" / f"axe-{hashlib.sha256(url.encode()).hexdigest()[:12]}.min.js"
    if cache.exists():
        _axe = cache.read_text("utf-8")
        return _axe
    if OFFLINE:
        raise RuntimeError("Режим без интернета (TESTGEN_OFFLINE): укажите путь к axe.min.js в TESTGEN_AXE_JS "
                           "(в Docker-образе студии он уже есть)")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Не удалось загрузить axe-core из TESTGEN_AXE_URL: {e}") from None
    if r.status_code != 200 or "axe" not in r.text[:2000]:
        raise RuntimeError(f"TESTGEN_AXE_URL не вернул axe-core (HTTP {r.status_code})")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(r.text, "utf-8")
    _axe = r.text
    return _axe


_AXE_RUN = """async (tags) => {
  const r = await axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']});
  return r.violations.map(v => ({id: v.id, impact: v.impact || 'minor', help: v.help, url: v.helpUrl,
    nodes: v.nodes.length, targets: v.nodes.slice(0, 3).map(n => n.target.join(' '))}));
}"""


def impact_at_least(impact: str, threshold: str) -> bool:
    t = threshold if threshold in IMPACTS else "serious"
    return IMPACTS.index(impact if impact in IMPACTS else "minor") >= IMPACTS.index(t)


async def accessibility(bs, step: dict) -> dict:
    page = bs.page
    if not await page.evaluate("() => typeof window.axe === 'object'"):
        # evaluate() is not subject to the page's Content-Security-Policy, unlike a script tag.
        await page.evaluate(await axe_source())
    violations = await page.evaluate(_AXE_RUN, WCAG_TAGS)
    threshold = bs.options.get("a11y_impact") or "serious"
    failing = [v for v in violations if impact_at_least(v["impact"], threshold)]
    details = {"a11y": {"threshold": threshold, "violations": violations}}
    if failing:
        top = "; ".join(f"{v['id']} ({v['impact']}, элементов: {v['nodes']})" for v in failing[:5])
        raise CheckFailed(f"Нарушения доступности WCAG уровня {threshold} и выше: {len(failing)} — {top}", details)
    return details


# ---------- visual regression ----------

_COMPARE = """async ([a, b]) => {
  const load = src => new Promise((ok, fail) => {
    const i = new Image(); i.onload = () => ok(i); i.onerror = fail; i.src = 'data:image/png;base64,' + src; });
  const [ia, ib] = await Promise.all([load(a), load(b)]);
  if (ia.width !== ib.width || ia.height !== ib.height)
    return {ratio: 1, size: [ia.width, ia.height, ib.width, ib.height], diff: ''};
  const w = ia.width, h = ia.height;
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  const x = c.getContext('2d', {willReadFrequently: true});
  x.drawImage(ia, 0, 0); const da = x.getImageData(0, 0, w, h).data;
  x.clearRect(0, 0, w, h); x.drawImage(ib, 0, 0); const db = x.getImageData(0, 0, w, h).data;
  const out = x.createImageData(w, h), o = out.data;
  let n = 0;
  for (let p = 0; p < da.length; p += 4) {
    const d = Math.abs(da[p] - db[p]) + Math.abs(da[p + 1] - db[p + 1]) + Math.abs(da[p + 2] - db[p + 2]);
    if (d > 48) { n++; o[p] = 230; o[p + 1] = 20; o[p + 2] = 60; o[p + 3] = 255; }
    else { const g = 225 + (db[p] * .3 + db[p + 1] * .59 + db[p + 2] * .11) / 255 * 30;
           o[p] = o[p + 1] = o[p + 2] = g; o[p + 3] = 255; }
  }
  x.putImageData(out, 0, 0);
  return {ratio: n / (w * h), size: [w, h, w, h], diff: c.toDataURL('image/png').split(',')[1]};
}"""


def baseline_file(pid: str, tid: str, step_id: str) -> Path:
    safe = lambda s: re.sub(r"[^\w-]+", "_", s)   # noqa: E731
    return projects.path(pid) / "baselines" / safe(tid) / f"{safe(step_id)}.png"


async def compare(bs, expected: bytes, actual: bytes) -> tuple[float, bytes]:
    """-> (share of differing pixels, diff image PNG)."""
    page = await bs.scratch_page()
    try:
        r = await page.evaluate(_COMPARE, [base64.b64encode(expected).decode(), base64.b64encode(actual).decode()])
    finally:
        await page.context.close()
    return r["ratio"], base64.b64decode(r["diff"]) if r["diff"] else b""


async def screenshot(bs, step: dict, loc=None) -> dict:
    page = bs.page
    masks = [page.locator(css) for css in step.get("masks") or [] if css.strip()]
    png = await (loc or page).screenshot(type="png", animations="disabled", caret="hide", mask=masks,
                                         timeout=15000)
    o = bs.options
    if not o.get("test_id"):
        return {"visual": {"recorded": True}}      # authoring: the baseline comes with the first run
    base = baseline_file(o["project_id"], o["test_id"], step["id"])
    run_dir: Path | None = o.get("run_dir")
    prefix = f"{o.get('attempt_prefix', '')}visual-{step['id']}"
    if run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"{prefix}-actual.png").write_bytes(png)
    if not fs.is_file(base):
        fs.write_bytes(base, png)
        return {"visual": {"baseline_created": True, "actual": f"{prefix}-actual.png" if run_dir else ""}}
    ratio, diff = await compare(bs, fs.read_bytes(base), png)
    threshold = float(o.get("visual_threshold") or DEFAULT_VISUAL_THRESHOLD)
    info = {"ratio": round(ratio, 5), "threshold": threshold,
            "actual": f"{prefix}-actual.png" if run_dir else "", "diff": ""}
    if run_dir and diff:
        (run_dir / f"{prefix}-diff.png").write_bytes(diff)
        info["diff"] = f"{prefix}-diff.png"
    if ratio * 100 > threshold:
        raise CheckFailed(f"Визуальное расхождение с эталоном: {ratio:.2%} пикселей (порог {threshold}%)",
                          {"visual": info})
    return {"visual": info}
