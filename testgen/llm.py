"""Shared LLM client settings, prompt caching and token accounting.

Prompt caching. Requests render as tools -> system -> messages. `system()` puts a
cache breakpoint on the system prompt, so the tools, the rules and the skills
(the static prefix of every request of a stage) are read from the cache after the
first request. The authoring agent adds top-level automatic caching (`auto_cache()`)
for its growing conversation. Context editing (clear_tool_uses) rewrites old tool
results, which invalidates the conversation cache from the first cleared block on,
but never the tools + system breakpoint - that is why it is explicit.
TESTGEN_PROMPT_CACHE=off switches caching off, to measure the difference.

Token accounting. `track(resp)` adds a response's usage to a `Usage`: the one
given, or the one opened with `usage_scope()` around a piece of work (a test
run, a pipeline job). `Usage.cost()` estimates dollars when TESTGEN_PRICES gives
the price of every model used.

Nothing here names a model or a price: the model comes from TESTGEN_MODEL (or a
project stage's settings), prices from TESTGEN_PRICES.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from pathlib import Path

import anthropic

# No built-in default: the model is chosen by whoever deploys the studio.
MODEL = os.environ.get("TESTGEN_MODEL", "").strip()
# Browser driving is a latency-sensitive loop of many small decisions;
# "medium" keeps each step fast. Raise to "high" for tricky apps.
EFFORT = os.environ.get("TESTGEN_EFFORT", "medium")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROMPT_CACHE = os.environ.get("TESTGEN_PROMPT_CACHE", "on").lower() not in ("off", "0", "false", "no")

# API protocol versions. The API only accepts these features under a dated
# identifier; they are kept together here and nowhere else.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
CLEAR_TOOL_USES = "clear_tool_uses_20250919"

MODEL_HINT = ("Не задана модель: укажите TESTGEN_MODEL (или модель этапа в «Проект → "
              "Процесс генерации»)")


class ModelNotConfigured(Exception):
    """Neither TESTGEN_MODEL nor the stage's settings name a model."""

    def __init__(self):
        super().__init__(MODEL_HINT)


def _load_prices() -> dict[str, tuple[float, float]]:
    """TESTGEN_PRICES: JSON {"<model>": [input, output]} in $ per million tokens,
    inline or a path to a .json file. Empty -> costs are not estimated."""
    raw = os.environ.get("TESTGEN_PRICES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw if raw.startswith("{") else Path(raw).read_text("utf-8"))
        return {str(k): (float(v[0]), float(v[1])) for k, v in data.items()}
    except (OSError, ValueError, TypeError, IndexError, AttributeError) as e:
        raise SystemExit(f"TESTGEN_PRICES: ожидается JSON {{\"модель\": [вход, выход]}} или путь к нему ({e})")


# $ per million tokens: input, output. Cache writes cost 1.25x input, reads 0.1x.
PRICES = _load_prices()

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def common_params(stage: dict | None = None) -> dict:
    """Parameters shared by every request.

    `stage` is a pipeline stage config from the project: its "model" / "effort"
    override the global defaults when set.

    fallbacks="default": if the safety classifiers decline a request, the
    API re-runs it on the provider's fallback model instead of failing.
    """
    stage = stage or {}
    effort = stage.get("effort") if stage.get("effort") in EFFORTS else EFFORT
    model = (stage.get("model") or "").strip() or MODEL
    if not model:
        raise ModelNotConfigured()
    return {
        "model": model,
        "output_config": {"effort": effort},
        "fallbacks": "default",
    }


def system(text: str) -> str | list[dict]:
    """A system prompt with a cache breakpoint (caches the tools before it as well)."""
    if not PROMPT_CACHE:
        return text
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def auto_cache() -> dict:
    """Top-level automatic caching for a growing conversation (spread into the request)."""
    return {"cache_control": {"type": "ephemeral"}} if PROMPT_CACHE else {}


class Usage:
    FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

    def __init__(self):
        self.requests = 0
        self.by_model: dict[str, dict[str, int]] = {}

    def add(self, model: str, usage) -> None:
        if usage is None:
            return
        self.requests += 1
        m = self.by_model.setdefault(model or MODEL, dict.fromkeys(self.FIELDS, 0))
        for f in self.FIELDS:
            m[f] += getattr(usage, f, None) or 0

    def merge(self, other: "Usage") -> None:
        self.requests += other.requests
        for model, counts in other.by_model.items():
            m = self.by_model.setdefault(model, dict.fromkeys(self.FIELDS, 0))
            for f in self.FIELDS:
                m[f] += counts[f]

    def totals(self) -> dict[str, int]:
        return {f: sum(m[f] for m in self.by_model.values()) for f in self.FIELDS}

    def cost(self) -> float | None:
        """Estimated $, or None if a model's price is not in TESTGEN_PRICES."""
        total = 0.0
        for model, m in self.by_model.items():
            price = next((p for k, p in PRICES.items() if model == k or model.startswith(k + "-")), None)
            if not price:
                return None
            inp, out = price
            total += (m["input_tokens"] * inp + m["cache_creation_input_tokens"] * inp * 1.25
                      + m["cache_read_input_tokens"] * inp * 0.1 + m["output_tokens"] * out) / 1e6
        return round(total, 4)

    def as_dict(self) -> dict:
        t = self.totals()
        prompt = t["input_tokens"] + t["cache_creation_input_tokens"] + t["cache_read_input_tokens"]
        return t | {"requests": self.requests, "cost_usd": self.cost(), "cache": PROMPT_CACHE,
                    "cache_hit": round(t["cache_read_input_tokens"] / prompt, 3) if prompt else 0.0}


_scopes: contextvars.ContextVar[tuple[Usage, ...]] = contextvars.ContextVar("usage", default=())


@contextlib.contextmanager
def usage_scope():
    """Collect the usage of every request made inside (including tasks started inside).
    Scopes nest: a run inside a pipeline job counts for both."""
    u = Usage()
    token = _scopes.set(_scopes.get() + (u,))
    try:
        yield u
    finally:
        _scopes.reset(token)


def track(resp, usage: Usage | None = None) -> None:
    for u in {id(x): x for x in (usage, *_scopes.get()) if x is not None}.values():
        u.add(getattr(resp, "model", "") or "", getattr(resp, "usage", None))


def is_api_error(e: Exception) -> bool:
    """The LLM is unavailable: an API error, no credentials or no model configured."""
    return (isinstance(e, (anthropic.APIError, ModelNotConfigured))
            or (isinstance(e, TypeError) and "authentication" in str(e)))


def api_error_text(e: Exception) -> str:
    if isinstance(e, ModelNotConfigured):
        return MODEL_HINT + "."
    if isinstance(e, anthropic.AuthenticationError):
        return "API ИИ: неверный или отсутствующий ключ (задайте ANTHROPIC_API_KEY)."
    if isinstance(e, anthropic.RateLimitError):
        return "API ИИ: превышен лимит запросов, повторите чуть позже."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "API ИИ: ошибка сети."
    if isinstance(e, TypeError) and "authentication" in str(e):
        # The SDK raises this before sending anything when no credentials are set.
        return "API ИИ: неверный или отсутствующий ключ (задайте ANTHROPIC_API_KEY)."
    return f"{type(e).__name__}: {e}"
