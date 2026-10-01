"""The one door to language models: every request of the studio goes through
`chat()` (a conversation, optionally with tools) or `parse()` (an answer in a pydantic
structure). The provider and model come from the pipeline stage (providers.resolve);
when a provider fails, the next one of the fallback chain is tried.

Prompt caching (Claude, providers/anthropic.py). Requests render as tools -> system ->
messages. The system prompt carries a cache breakpoint, so the tools, the rules and the
skills (the static prefix of every request of a stage) are read from the cache after the
first request. The authoring agent adds automatic caching of its growing conversation
(cache_all). Context editing (clear_tool_uses) rewrites old tool results, which
invalidates the conversation cache from the first cleared block on, but never the
tools + system breakpoint - that is why it is explicit. TESTGEN_PROMPT_CACHE=off
switches caching off, to measure the difference. Other providers cut old screenshots
out themselves (keep_images).

Token accounting. Every answer is added to a `Usage`: the one given, those opened
with `usage_scope()` around a piece of work (a test run, a pipeline job) and, with a
project, the project's monthly ledger (data/projects/<id>/usage/<YYYY-MM>.json, by
stage and model). Costs come from the providers' prices, in dollars and rubles.

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

from . import fs, providers
from .paths import DATA
from .providers.base import ProviderError, Reply, Request, schema_instruction, validate

MODEL = os.environ.get("TESTGEN_MODEL", "claude-opus-5")
# Browser driving is a latency-sensitive loop of many small decisions;
# "medium" keeps each step fast. Raise to "high" for tricky apps.
EFFORT = os.environ.get("TESTGEN_EFFORT", "medium")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROMPT_CACHE = os.environ.get("TESTGEN_PROMPT_CACHE", "on").lower() not in ("off", "0", "false", "no")
WARN_AT = 0.8
FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


class BudgetExceeded(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, retryable=False)


# ---------- the Anthropic client (tests replace _client) ----------

_client: anthropic.AsyncAnthropic | None = None
_clients: dict[tuple, anthropic.AsyncAnthropic] = {}


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        base = os.environ.get("TESTGEN_LLM_BASE_URL", "").strip()
        _client = anthropic.AsyncAnthropic(**({"base_url": base} if base else {}))
    return _client


def anthropic_client(cfg: dict) -> anthropic.AsyncAnthropic:
    """The client for an Anthropic provider: the global one (environment key and address) for the
    built-in provider as configured by the environment, else one per (address, key)."""
    base = cfg.get("base_url", "")
    if cfg.get("id") == "anthropic" and not _own_key(cfg) and base == os.environ.get("TESTGEN_LLM_BASE_URL", "").strip():
        return client()
    key = providers.key_of(cfg)
    k = (base, key)
    if k not in _clients:
        _clients[k] = anthropic.AsyncAnthropic(api_key=key or None, **({"base_url": base} if base else {}))
    return _clients[k]


def _own_key(cfg: dict) -> bool:
    from . import vault
    return bool((vault.load(providers.SECRETS_KIND, cfg["id"]) or {}).get("api_key"))


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
        self.provider_of: dict[str, str] = {}
        self.limit, self.currency, self.name = float(limit or 0), currency, name
        self.on_warn: Callable[[str], None] | None = None
        self.warned = False

    def add(self, model: str, usage, provider: str = "") -> None:
        if usage is None:
            return
        self.requests += 1
        model = model or MODEL
        m = self.by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
        self.provider_of.setdefault(model, provider or "anthropic")
        for f in FIELDS:
            m[f] += _num(usage, f)

    def merge(self, other: "Usage") -> None:
        self.requests += other.requests
        for model, counts in other.by_model.items():
            m = self.by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
            self.provider_of.setdefault(model, other.provider_of.get(model, "anthropic"))
            for f in FIELDS:
                m[f] += counts[f]

    def totals(self) -> dict[str, int]:
        return {f: sum(m[f] for m in self.by_model.values()) for f in FIELDS}

    def costs(self) -> dict[str, float] | None:
        """Cost by currency, or None if a model's price is unknown."""
        out: dict[str, float] = {}
        for model, m in self.by_model.items():
            c = cost_of(self.provider_of.get(model, "anthropic"), model, m)
            if c is None:
                return None
            out[c[1]] = out.get(c[1], 0.0) + c[0]
        return {k: round(v, 4) for k, v in out.items()}

    def cost(self, currency: str = "USD") -> float | None:
        costs = self.costs()
        return None if costs is None else convert(costs, currency)

    def spent(self, currency: str) -> float:
        """Spent in `currency`, models with an unknown price counting as free."""
        out: dict[str, float] = {}
        for model, m in self.by_model.items():
            c = cost_of(self.provider_of.get(model, "anthropic"), model, m)
            if c:
                out[c[1]] = out.get(c[1], 0.0) + c[0]
        return convert(out, currency)

    def as_dict(self) -> dict:
        t = self.totals()
        prompt = t["input_tokens"] + t["cache_creation_input_tokens"] + t["cache_read_input_tokens"]
        costs = self.costs()
        return t | {"requests": self.requests, "cost_usd": None if costs is None else convert(costs, "USD"),
                    "cost_rub": None if costs is None else convert(costs, "RUB"), "costs": costs,
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


def cost_of(provider: str, model: str, m: dict) -> tuple[float, str] | None:
    p = providers.price(provider, model)
    if not p:
        return None
    inp, out, currency = p
    anthropic_cache = (providers.provider_cfg(provider) or {"kind": "anthropic"}).get("kind") == "anthropic"
    write = 1.25 if anthropic_cache else 1.0
    read = 0.1 if anthropic_cache else 1.0
    total = (m["input_tokens"] * inp + m["cache_creation_input_tokens"] * inp * write
             + m["cache_read_input_tokens"] * inp * read + m["output_tokens"] * out) / 1e6
    return total, currency


def convert(costs: dict[str, float], currency: str) -> float:
    rate = providers.usd_rub()
    total = 0.0
    for cur, v in costs.items():
        if cur == currency:
            total += v
        elif cur == "USD" and currency == "RUB":
            total += v * rate
        elif cur == "RUB" and currency == "USD":
            total += v / rate
    return round(total, 4)


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


def track(resp, usage: Usage | None = None, project_id: str = "", stage: str = "") -> None:
    """Add an answer (a providers Reply, or an SDK response) to the usages and the ledger."""
    provider = getattr(resp, "provider", "") or "anthropic"
    model = getattr(resp, "model", "") or ""
    u_raw = getattr(resp, "usage", None)
    for u in {id(x): x for x in (usage, *_scopes.get()) if x is not None}.values():
        u.add(model, u_raw, provider)
        u.after()
    if project_id and u_raw is not None:
        ledger_add(project_id, stage or "other", provider, model or MODEL, u_raw)
    from . import monitoring
    monitoring.LLM.labels(stage or "other", provider, model or MODEL).inc()


# ---------- project ledger and monthly budget ----------

def _ledger_file(pid: str, month: str = ""):
    month = month or datetime.date.today().strftime("%Y-%m")
    return DATA / "projects" / pid / "usage" / f"{month}.json"


def ledger(pid: str, month: str = "") -> dict:
    try:
        return fs.read_json(_ledger_file(pid, month)) or {"stages": {}, "requests": 0}
    except ValueError:
        return {"stages": {}, "requests": 0}


def ledger_add(pid: str, stage: str, provider: str, model: str, usage) -> None:
    if not re.fullmatch(r"[a-z0-9]{4,32}", pid or ""):
        return
    f = _ledger_file(pid)
    with fs.lock(f):             # workers and instances spend on the same project
        d = ledger(pid)
        d["requests"] = d.get("requests", 0) + 1
        key = f"{provider}/{model}"
        m = d["stages"].setdefault(stage, {}).setdefault(key, dict.fromkeys(FIELDS, 0) | {"requests": 0})
        for fld in FIELDS:
            m[fld] += _num(usage, fld)
        m["requests"] += 1
        fs.write_json(f, d, indent=1)


def ledger_usage(pid: str, month: str = "") -> Usage:
    u = Usage()
    for stage in ledger(pid, month)["stages"].values():
        for key, m in stage.items():
            provider, _, model = key.partition("/")
            counts = {f: m[f] for f in FIELDS}
            u.by_model.setdefault(model, dict.fromkeys(FIELDS, 0))
            u.provider_of.setdefault(model, provider)
            for f in FIELDS:
                u.by_model[model][f] += counts[f]
            u.requests += m.get("requests", 0)
    return u


def ledger_report(pid: str, month: str = "") -> dict:
    """The month's spending by stage and model, for the project settings."""
    rows = []
    for stage, models in ledger(pid, month)["stages"].items():
        for key, m in models.items():
            provider, _, model = key.partition("/")
            c = cost_of(provider, model, m)
            rows.append({"stage": stage, "provider": provider, "model": model, "requests": m.get("requests", 0),
                         "tokens": sum(m[f] for f in FIELDS), "cost": round(c[0], 4) if c else None,
                         "currency": c[1] if c else ""})
    total = ledger_usage(pid, month)
    return {"month": month or datetime.date.today().strftime("%Y-%m"), "rows": rows,
            "total_usd": total.spent("USD"), "total_rub": total.spent("RUB"), "requests": total.requests}


def _project_budget(pid: str) -> dict:
    from . import projects      # projects imports this module
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

def _request(stage: dict | None, model: str, **kw) -> Request:
    stage = stage or {}
    effort = stage.get("effort") if stage.get("effort") in EFFORTS else EFFORT
    return Request(model=model, effort=effort, stage=stage, **kw)


async def chat(stage: dict | None, *, system: str, messages: list[dict], tools: list[dict] | None = None,
               max_tokens: int = 16000, one_tool: bool = True, cache: bool = True, cache_all: bool = False,
               context: str = "", keep_images: int = 0, usage: Usage | None = None, project_id: str = "",
               stage_name: str = "") -> Reply:
    """One answer of the stage's model (then of the fallback chain, if it fails)."""
    _check(usage, project_id)
    last: ProviderError | None = None
    for pid, model in providers.chain(stage):
        req = _request(stage, model, system=system, messages=messages, tools=tools or [], max_tokens=max_tokens,
                       one_tool=one_tool, cache=cache and PROMPT_CACHE, cache_all=cache_all, context=context,
                       keep_images=keep_images)
        try:
            reply = await providers.get(pid, anthropic_client).chat(req)
        except ProviderError as e:
            last = e
            if not e.retryable:
                raise
            continue
        track(reply, usage, project_id, stage_name)
        return reply
    raise last or ProviderError("Нет доступной модели")


async def parse(stage: dict | None, *, system: str, messages: list[dict], schema, max_tokens: int = 4000,
                context: str = "", usage: Usage | None = None, project_id: str = "", stage_name: str = "") -> Reply:
    """An answer as `schema` (reply.parsed; None if the model declined). Providers without
    server-side structured output get the schema in the prompt; an answer that does not
    validate is sent back with the error once."""
    _check(usage, project_id)
    last: ProviderError | None = None
    for pid, model in providers.chain(stage):
        prov = providers.get(pid, anthropic_client)
        req = _request(stage, model, system=system, messages=messages, max_tokens=max_tokens,
                       cache=PROMPT_CACHE, context=context)
        try:
            if getattr(prov, "structured", False):
                reply = await prov.parse(req, schema)
                track(reply, usage, project_id, stage_name)
                if reply.parsed is None and reply.stop != "refusal":
                    reply.parsed, _ = validate(schema, reply.text)
                return reply
            req.system = system + schema_instruction(schema)
            reply = await prov.chat(req)
            track(reply, usage, project_id, stage_name)
            parsed, error = validate(schema, reply.text)
            if parsed is None and reply.stop not in ("refusal", "max_tokens"):
                req.messages = messages + [{"role": "assistant", "content": reply.text or "(empty)"},
                                           {"role": "user", "content": f"The answer is not valid: {error}\n"
                                                                       "Answer again with the JSON object only."}]
                reply = await prov.chat(req)
                track(reply, usage, project_id, stage_name)
                parsed, _ = validate(schema, reply.text)
            reply.parsed = parsed
            return reply
        except ProviderError as e:
            last = e
            if not e.retryable:
                raise
    raise last or ProviderError("Нет доступной модели")


def is_api_error(e: Exception) -> bool:
    return isinstance(e, ProviderError) or isinstance(e, anthropic.APIError) or (
        isinstance(e, TypeError) and "authentication" in str(e))


def api_error_text(e: Exception) -> str:
    if isinstance(e, ProviderError):
        return str(e)
    from .providers.anthropic import error_text
    return error_text(e)
