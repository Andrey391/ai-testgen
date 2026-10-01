"""Model connection of a project, prompt caching and token accounting.

Model connection. The studio is not tied to any model: each project chooses its
own in "Проект → Модель" (project.json "llm": model, effort, API address, prices;
the API key in secrets/projects/<id>/llm.json, or ANTHROPIC_API_KEY on the server
when the project has none). Pipeline stages may override the model and effort.
Every request goes through `model(project_id, stage)`: its `client` and `params`
make the request, `track()` counts the response. Without a model the project's
LLM features fail with `NotConfigured`.

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
run, a pipeline job). `Usage.cost()` estimates dollars from the prices entered
in the project settings.
"""
from __future__ import annotations

import contextlib
import contextvars
import os

import anthropic

EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROMPT_CACHE = os.environ.get("TESTGEN_PROMPT_CACHE", "on").lower() not in ("off", "0", "false", "no")

# API protocol versions. The API only accepts these features under a dated
# identifier; they are kept together here and nowhere else.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
CLEAR_TOOL_USES = "clear_tool_uses_20250919"


# Capabilities the studio relies on: structured output (scenarios, healing, analysis),
# screenshots, context editing (the authoring agent).
NEEDED = {"structured_outputs": "структурированный ответ", "image_input": "изображения",
          "context_management": "context editing"}


class NotConfigured(Exception):
    """The project has no model chosen."""


def make_client(api_key: str, base_url: str) -> anthropic.AsyncAnthropic:
    """Empty values fall back to the SDK's environment (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL)."""
    return anthropic.AsyncAnthropic(api_key=api_key or None, base_url=base_url or None)


_clients: dict[tuple[str, str], anthropic.AsyncAnthropic] = {}


def client_for(api_key: str, base_url: str) -> anthropic.AsyncAnthropic:
    key = (api_key, base_url)
    if key not in _clients:
        _clients[key] = make_client(api_key, base_url)
    return _clients[key]


def model_info(m) -> dict:
    """A Models API entry -> {id, name, efforts, missing}. `efforts`: supported effort
    levels, None if the API did not say; `missing`: capabilities from NEEDED it lacks."""
    d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
    caps = d.get("capabilities") or {}
    eff = caps.get("effort")
    efforts = None
    if isinstance(eff, dict):
        efforts = [e for e in EFFORTS if eff.get("supported") and (eff.get(e) or {}).get("supported")]
    missing = [k for k in NEEDED if isinstance(caps.get(k), dict) and caps[k].get("supported") is False]
    return {"id": d.get("id", ""), "name": d.get("display_name") or d.get("id", ""),
            "efforts": efforts, "missing": missing}


class Model:
    """A project's model connection resolved for one request (or one stage)."""

    def __init__(self, conf: dict, stage: dict | None = None):
        stage = stage or {}
        self.name = (stage.get("model") or "").strip() or conf.get("model", "")
        if not self.name:
            raise NotConfigured("Модель не настроена: выберите её в «Проект → Модель».")
        self.effort = stage.get("effort") if stage.get("effort") in EFFORTS else conf.get("effort", "")
        known = next((m for m in conf.get("models") or [] if m["id"] == self.name), None)
        if known and known.get("efforts") is not None and self.effort not in known["efforts"]:
            self.effort = ""          # the model does not take this effort level: its default
        self.api_key = conf.get("api_key", "")
        self.base_url = conf.get("base_url", "")
        self.prices = conf.get("prices") or {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        return client_for(self.api_key, self.base_url)

    @property
    def params(self) -> dict:
        """Parameters shared by every request.

        fallbacks="default": if the safety classifiers decline a request, the
        API re-runs it on the provider's fallback model instead of failing.
        """
        p = {"model": self.name, "fallbacks": "default"}
        if self.effort:
            p["output_config"] = {"effort": self.effort}
        return p

    def track(self, resp, usage: "Usage | None" = None) -> None:
        track(resp, usage, self.prices)


def model(project_id: str, stage: dict | None = None) -> Model:
    """The model for a request of the project; `stage` is a pipeline stage config,
    its "model" / "effort" override the project's."""
    from . import projects
    return Model(projects.llm_settings(project_id), stage)


async def check(conf: dict) -> list[dict]:
    """The models the connection offers (model_info dicts). A gateway without the
    Models API is checked with a one-word request to the chosen model instead."""
    c = client_for(conf.get("api_key", ""), conf.get("base_url", ""))
    try:
        return [model_info(m) async for m in c.models.list(limit=100)]
    except anthropic.NotFoundError:
        if not conf.get("model"):
            raise
    await c.messages.create(model=conf["model"], max_tokens=16,
                            messages=[{"role": "user", "content": "ping"}])
    return [{"id": conf["model"], "name": conf["model"], "efforts": None, "missing": []}]


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
        self.prices: dict[str, list[float]] = {}   # model -> $ per million tokens: input, output

    def add(self, model: str, usage, prices: dict | None = None) -> None:
        if usage is None:
            return
        self.requests += 1
        self.prices.update(prices or {})
        m = self.by_model.setdefault(model or "?", dict.fromkeys(self.FIELDS, 0))
        for f in self.FIELDS:
            m[f] += getattr(usage, f, None) or 0

    def merge(self, other: "Usage") -> None:
        self.requests += other.requests
        self.prices.update(other.prices)
        for model, counts in other.by_model.items():
            m = self.by_model.setdefault(model, dict.fromkeys(self.FIELDS, 0))
            for f in self.FIELDS:
                m[f] += counts[f]

    def totals(self) -> dict[str, int]:
        return {f: sum(m[f] for m in self.by_model.values()) for f in self.FIELDS}

    def cost(self) -> float | None:
        """Estimated $ (cache writes cost 1.25x input, reads 0.1x), or None if a model's
        price is not set in the project."""
        total = 0.0
        for model, m in self.by_model.items():
            price = next((p for k, p in self.prices.items() if model == k or model.startswith(k + "-")), None)
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


def track(resp, usage: Usage | None = None, prices: dict | None = None) -> None:
    for u in {id(x): x for x in (usage, *_scopes.get()) if x is not None}.values():
        u.add(getattr(resp, "model", "") or "", getattr(resp, "usage", None), prices)


def is_api_error(e: Exception) -> bool:
    return isinstance(e, (anthropic.APIError, NotConfigured)) or (isinstance(e, TypeError)
                                                                  and "authentication" in str(e))


NO_KEY = "Claude API: неверный или не заданный API-ключ (укажите его в «Проект → Модель»)."


def api_error_text(e: Exception) -> str:
    if isinstance(e, NotConfigured):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return NO_KEY
    if isinstance(e, anthropic.RateLimitError):
        return "API ИИ: превышен лимит запросов, повторите чуть позже."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "API ИИ: ошибка сети."
    if isinstance(e, TypeError) and "authentication" in str(e):
        # The SDK raises this before sending anything when no credentials are set.
        return NO_KEY
    return f"{type(e).__name__}: {e}"
