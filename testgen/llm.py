"""The one door to the model: every request of the studio goes through `chat()` (a
conversation, optionally with tools) or `parse()` (an answer in a pydantic structure).

Model connection. The studio is not tied to any model: each project chooses its own
in "Проект → Модель" (project.json "llm": model, effort, API address, prices; the API
key in secrets/projects/<id>/llm.json, or ANTHROPIC_API_KEY on the server when the
project has none). Pipeline stages may override the model and effort. `model(project_id,
stage)` resolves them for a request; without a model the project's LLM features fail
with `NotConfigured`. `check()` asks the connection which models it offers.

Prompt caching (providers/anthropic.py). Requests render as tools -> system ->
messages. The system prompt carries a cache breakpoint, so the tools, the rules and the
skills (the static prefix of every request of a stage) are read from the cache after the
first request. The authoring agent adds automatic caching of its growing conversation
(cache_all). Context editing (clear_tool_uses) rewrites old tool results, which
invalidates the conversation cache from the first cleared block on, but never the
tools + system breakpoint - that is why it is explicit. TESTGEN_PROMPT_CACHE=off
switches caching off, to measure the difference.

Token accounting. Every answer is added to a `Usage`: the one given, those opened
with `usage_scope()` around a piece of work (a test run, a pipeline job) and, with a
project, the project's monthly ledger (data/projects/<id>/usage/<YYYY-MM>.json by
stage and model, or rows of the `usage` table with a shared database: repo/usage.py). Costs come from the prices entered in the project settings ($ per
million tokens), in rubles at TESTGEN_USD_RUB.

Budgets. A Usage can carry a limit (session, pipeline job) and the project a monthly
one (pipeline "budget"): a request that would start over a limit raises
BudgetExceeded; crossing 80% calls the Usage's `on_warn` once.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime
import os
import re
from typing import Callable

import anthropic

from .paths import OFFLINE
from .repo import usage as usage_repo
from .providers.anthropic import (CLEAR_TOOL_USES, CONTEXT_BETA, FALLBACK_BETA, FEATURES,  # noqa: F401
                                  AnthropicProvider, error_text, is_error)
from .providers.base import ProviderError, Reply, Request, schema_instruction, validate

EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROMPT_CACHE = os.environ.get("TESTGEN_PROMPT_CACHE", "on").lower() not in ("off", "0", "false", "no")
WARN_AT = 0.8
FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

# Capabilities the studio relies on: structured output (scenarios, healing, analysis),
# screenshots, context editing (the authoring agent).
NEEDED = {"structured_outputs": "структурированный ответ", "image_input": "изображения",
          "context_management": "context editing"}


class NotConfigured(ProviderError):
    """The project has no model chosen."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class BudgetExceeded(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


def usd_rub() -> float:
    try:
        return float(os.environ.get("TESTGEN_USD_RUB") or 80)
    except ValueError:
        return 80.0


# ---------- the model connection (tests replace make_client) ----------

def make_client(api_key: str, base_url: str) -> anthropic.AsyncAnthropic:
    """Empty values fall back to the SDK's environment (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL).
    A gateway of the project's own (base_url) gets the key both as x-api-key and as
    "Authorization: Bearer": many gateways with the Messages API accept only the latter."""
    return anthropic.AsyncAnthropic(api_key=api_key or None, base_url=base_url or None,
                                    auth_token=(api_key or None) if base_url else None)


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
        self.missing = set((known or {}).get("missing") or [])
        self.api_key = conf.get("api_key", "")
        self.base_url = conf.get("base_url", "")
        self.prices = conf.get("prices") or {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        return client_for(self.api_key, self.base_url)

    @property
    def features(self) -> set[str]:
        """What the requests may use: everything the last check did not find missing."""
        f = set(FEATURES)
        if "context_management" in self.missing:
            f.discard("context_editing")      # old screenshots are cut out by the studio instead
        if "structured_outputs" in self.missing:
            f.discard("structured")           # the JSON schema goes into the prompt instead
        return f

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

    def provider(self) -> AnthropicProvider:
        if OFFLINE and not self.base_url:
            raise NotConfigured("Режим без интернета (TESTGEN_OFFLINE): укажите в «Проект → Модель» адрес API "
                                "во внутренней сети")
        return AnthropicProvider(self.client, self.features)


def model(project_id: str, stage: dict | None = None) -> Model:
    """The model for a request of the project; `stage` is a pipeline stage config,
    its "model" / "effort" override the project's."""
    from . import projects      # projects imports this module
    return Model(projects.llm_settings(project_id), stage)


async def check(conf: dict) -> list[dict]:
    """The models the connection offers (model_info dicts). With a model chosen it also
    gets a one-word request: gateways often list models without checking the key, so
    only a request to the model shows the key and the model really work."""
    c = client_for(conf.get("api_key", ""), conf.get("base_url", ""))
    try:
        models = [model_info(m) async for m in c.models.list(limit=100)]
    except anthropic.NotFoundError:      # a gateway without the Models API
        if not conf.get("model"):
            raise
        models = [{"id": conf["model"], "name": conf["model"], "efforts": None, "missing": []}]
    if conf.get("model"):
        await c.messages.create(model=conf["model"], max_tokens=16,
                                messages=[{"role": "user", "content": "ping"}])
    return models


# ---------- usage ----------

def _num(usage, f: str) -> int:
    if isinstance(usage, dict):
        return int(usage.get(f) or 0)
    return int(getattr(usage, f, None) or 0)


class Usage:
    FIELDS = FIELDS

    def __init__(self, limit: float = 0, currency: str = "USD", name: str = ""):
        self.requests = 0
        self.by_model: dict[str, dict[str, int]] = {}
        self.prices: dict[str, list[float]] = {}   # model -> $ per million tokens: input, output
        self.limit, self.currency, self.name = float(limit or 0), currency, name
        self.on_warn: Callable[[str], None] | None = None
        self.warned = False

    def add(self, model: str, usage, prices: dict | None = None) -> None:
        if usage is None:
            return
        self.requests += 1
        self.prices.update(prices or {})
        m = self.by_model.setdefault(model or "?", dict.fromkeys(FIELDS, 0))
        for f in FIELDS:
            m[f] += _num(usage, f)

    def merge(self, other: "Usage") -> None:
        self.requests += other.requests
        self.prices.update(other.prices)
        for model, counts in other.by_model.items():
            m = self.by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
            for f in FIELDS:
                m[f] += counts[f]

    def totals(self) -> dict[str, int]:
        return {f: sum(m[f] for m in self.by_model.values()) for f in FIELDS}

    def cost(self, currency: str = "USD") -> float | None:
        """Estimated cost, or None if a model's price is not set in the project."""
        total = 0.0
        for model, m in self.by_model.items():
            c = cost_of(self.prices, model, m)
            if c is None:
                return None
            total += c
        return convert(total, currency)

    def spent(self, currency: str) -> float:
        """Spent in `currency`, models with an unknown price counting as free."""
        return convert(sum(cost_of(self.prices, model, m) or 0.0 for model, m in self.by_model.items()), currency)

    def as_dict(self) -> dict:
        t = self.totals()
        prompt = t["input_tokens"] + t["cache_creation_input_tokens"] + t["cache_read_input_tokens"]
        return t | {"requests": self.requests, "cost_usd": self.cost("USD"), "cost_rub": self.cost("RUB"),
                    "cache": PROMPT_CACHE, "models": sorted(self.by_model),
                    "cache_hit": round(t["cache_read_input_tokens"] / prompt, 3) if prompt else 0.0}

    # budget
    def check(self) -> None:
        if self.limit and self.spent(self.currency) >= self.limit:
            raise BudgetExceeded(f"Исчерпан лимит расхода{' ' + self.name if self.name else ''}: "
                                 f"{self.spent(self.currency):.2f} из {self.limit:g} {self.currency}")

    def after(self) -> None:
        if self.limit and not self.warned and self.spent(self.currency) >= WARN_AT * self.limit:
            self.warned = True
            if self.on_warn:
                self.on_warn(f"Израсходовано {self.spent(self.currency):.2f} из {self.limit:g} {self.currency} "
                             f"лимита{' ' + self.name if self.name else ''} (80%)")


def cost_of(prices: dict, model: str, m: dict) -> float | None:
    """$ for the counts of a model (cache writes cost 1.25x input, reads 0.1x), None without a price."""
    price = next((p for k, p in prices.items() if model == k or model.startswith(k + "-")), None)
    if not price:
        return None
    inp, out = price
    return (m["input_tokens"] * inp + m["cache_creation_input_tokens"] * inp * 1.25
            + m["cache_read_input_tokens"] * inp * 0.1 + m["output_tokens"] * out) / 1e6


def convert(usd: float, currency: str) -> float:
    return round(usd * usd_rub() if currency == "RUB" else usd, 4)


_scopes: contextvars.ContextVar[tuple[Usage, ...]] = contextvars.ContextVar("usage", default=())


@contextlib.contextmanager
def usage_scope(limit: float = 0, currency: str = "USD", name: str = "", on_warn=None):
    """Collect the usage of every request made inside (including tasks started inside).
    Scopes nest: a run inside a pipeline job counts for both. With `limit` requests stop
    when the scope has spent it (BudgetExceeded)."""
    u = Usage(limit, currency, name)
    u.on_warn = on_warn
    token = _scopes.set(_scopes.get() + (u,))
    try:
        yield u
    finally:
        _scopes.reset(token)


def track(resp, usage: Usage | None = None, prices: dict | None = None, project_id: str = "",
          stage: str = "") -> None:
    """Add an answer (a Reply, or an SDK response) to the usages and the project's ledger."""
    model = getattr(resp, "model", "") or ""
    u_raw = getattr(resp, "usage", None)
    for u in {id(x): x for x in (usage, *_scopes.get()) if x is not None}.values():
        u.add(model, u_raw, prices)
        u.after()
    if project_id and u_raw is not None:
        ledger_add(project_id, stage or "other", model or "?", u_raw)
    from . import monitoring
    monitoring.LLM.labels(stage or "other", model or "?").inc()


# ---------- project ledger and monthly budget ----------

def _month(month: str = "") -> str:
    return month or datetime.date.today().strftime("%Y-%m")


def ledger(pid: str, month: str = "") -> dict:
    return usage_repo.backend().ledger(pid, _month(month))


def ledger_add(pid: str, stage: str, model: str, usage) -> None:
    if not re.fullmatch(r"[a-z0-9]{4,32}", pid or ""):
        return
    usage_repo.backend().add(pid, _month(), stage, model, {f: _num(usage, f) for f in FIELDS})


def _prices(pid: str) -> dict:
    from . import projects
    p = projects.get(pid) if pid else None
    return ((p or {}).get("llm") or {}).get("prices") or {}


def ledger_usage(pid: str, month: str = "") -> Usage:
    u = Usage()
    u.prices.update(_prices(pid))
    for stage in ledger(pid, month)["stages"].values():
        for model, m in stage.items():
            counts = u.by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
            for f in FIELDS:
                counts[f] += m[f]
            u.requests += m.get("requests", 0)
    return u


def ledger_report(pid: str, month: str = "") -> dict:
    """The month's spending by stage and model, for the project settings."""
    prices = _prices(pid)
    rows = []
    for stage, models in ledger(pid, month)["stages"].items():
        for model, m in models.items():
            c = cost_of(prices, model, m)
            rows.append({"stage": stage, "model": model, "requests": m.get("requests", 0),
                         "tokens": sum(m[f] for f in FIELDS), "cost": round(c, 4) if c is not None else None,
                         "currency": "USD" if c is not None else ""})
    total = ledger_usage(pid, month)
    return {"month": month or datetime.date.today().strftime("%Y-%m"), "rows": rows,
            "total_usd": total.spent("USD"), "total_rub": total.spent("RUB"), "requests": total.requests}


def _project_budget(pid: str) -> dict:
    from . import projects
    p = projects.get(pid) if pid else None
    return (p or {}).get("pipeline", {}).get("budget") or {}


_month_warned: set[tuple[str, str]] = set()
MONTH_WARN_HOOKS: list[Callable[[str, str], None]] = []    # (project id, text): notifications


def _check_month(pid: str) -> None:
    b = _project_budget(pid)
    limit, cur = float(b.get("month") or 0), b.get("currency") or "USD"
    if not limit:
        return
    spent = ledger_usage(pid).spent(cur)
    if spent >= limit:
        raise BudgetExceeded(f"Исчерпан месячный лимит расхода проекта: {spent:.2f} из {limit:g} {cur}")
    key = (pid, datetime.date.today().strftime("%Y-%m"))
    if spent >= WARN_AT * limit and key not in _month_warned:
        _month_warned.add(key)
        for hook in MONTH_WARN_HOOKS:
            try:
                hook(pid, f"Проект израсходовал {spent:.2f} из {limit:g} {cur} месячного лимита (80%)")
            except Exception:
                pass


def _check(usage: Usage | None, project_id: str) -> None:
    for u in {id(x): x for x in (usage, *_scopes.get()) if x is not None}.values():
        u.check()
    if project_id:
        _check_month(project_id)


# ---------- requests ----------

async def chat(stage: dict | None, *, system: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int = 16000, one_tool: bool = True, cache: bool = True, cache_all: bool = False,
               context: str = "", keep_images: int = 0, usage: Usage | None = None, project_id: str = "",
               stage_name: str = "") -> Reply:
    """One answer of the project's model (the stage may override it)."""
    m = model(project_id, stage)
    _check(usage, project_id)
    req = Request(model=m.name, effort=m.effort, stage=stage or {}, system=system, messages=messages,
                  tools=tools or [], max_tokens=max_tokens, one_tool=one_tool, cache=cache and PROMPT_CACHE,
                  cache_all=cache_all, context=context, keep_images=keep_images)
    reply = await m.provider().chat(req)
    track(reply, usage, m.prices, project_id, stage_name)
    return reply


async def parse(stage: dict | None, *, system: str, messages: list[dict], schema, max_tokens: int = 4000,
                context: str = "", usage: Usage | None = None, project_id: str = "", stage_name: str = "") -> Reply:
    """An answer as `schema` (reply.parsed; None if the model declined). A model without
    server-side structured output gets the schema in the prompt; an answer that does not
    validate is sent back with the error once."""
    m = model(project_id, stage)
    _check(usage, project_id)
    prov = m.provider()
    req = Request(model=m.name, effort=m.effort, stage=stage or {}, system=system, messages=messages,
                  max_tokens=max_tokens, cache=PROMPT_CACHE, context=context)
    if prov.structured:
        reply = await prov.parse(req, schema)
        track(reply, usage, m.prices, project_id, stage_name)
        if reply.parsed is None and reply.stop != "refusal":
            reply.parsed, _ = validate(schema, reply.text)
        return reply
    req.system = system + schema_instruction(schema)
    reply = await prov.chat(req)
    track(reply, usage, m.prices, project_id, stage_name)
    parsed, error = validate(schema, reply.text)
    if parsed is None and reply.stop not in ("refusal", "max_tokens"):
        req.messages = messages + [{"role": "assistant", "content": reply.text or "(empty)"},
                                   {"role": "user", "content": f"The answer is not valid: {error}\n"
                                                               "Answer again with the JSON object only."}]
        reply = await prov.chat(req)
        track(reply, usage, m.prices, project_id, stage_name)
        parsed, _ = validate(schema, reply.text)
    reply.parsed = parsed
    return reply


def is_api_error(e: Exception) -> bool:
    return isinstance(e, ProviderError) or is_error(e)


def api_error_text(e: Exception) -> str:
    if isinstance(e, ProviderError):
        return str(e)
    return error_text(e)
