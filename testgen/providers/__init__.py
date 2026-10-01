"""LLM providers: Claude, GigaChat, OpenAI-compatible servers (Yandex AI Studio, vLLM,
Ollama). The studio talks to all of them through one interface (llm.chat / llm.parse).

Settings live in data/llm.json (no secrets): the providers, the default one, the
fallback chain, prices and the ruble rate. Keys live in secrets/llm/<provider>.json
or in the environment variable a provider names (api_key_env). The "anthropic"
provider always exists and follows the environment as before: ANTHROPIC_API_KEY,
TESTGEN_MODEL, TESTGEN_LLM_BASE_URL (an Anthropic-compatible proxy such as gpt2giga),
TESTGEN_LLM_FEATURES. TESTGEN_LLM_PROVIDER picks the default provider.

A pipeline stage chooses "provider" and "model" (empty = defaults), so the author can
run on a local vision model while failure analysis runs on GigaChat.

Prices are per million tokens in the model's currency (USD or RUB). A model's
"bench" holds its last benchmark result (testgen.bench): Auto-Pilot for a model other
than Claude is allowed only when its success rate reaches the project's threshold.
"""
from __future__ import annotations

import copy
import json
import os
import re

from .. import fs, vault
from ..paths import DATA, OFFLINE
from .anthropic import FEATURES as ANTHROPIC_FEATURES
from .anthropic import AnthropicProvider
from .base import ProviderError, Reply, Request  # noqa: F401  (re-exported)
from .gigachat import GigaChatProvider
from .openai_compat import OpenAIProvider

FILE = DATA / "llm.json"
SECRETS_KIND = "llm"
KINDS = ("anthropic", "openai", "gigachat")
CURRENCIES = ("USD", "RUB")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# Test hook: an httpx transport for every HTTP-based provider (tests/fakes.py, FakeHttpLLM).
TRANSPORT = None

# $ per million tokens: input, output. Anthropic cache writes cost 1.25x input, reads 0.1x.
CLAUDE_PRICES = {"claude-opus-5": (5, 25), "claude-opus-5-5": (4, 20), "claude-fable-5-1": (10, 50),
                 "claude-fable-5": (10, 50), "claude-sonnet-5": (2, 10), "claude-sonnet-5-5": (2, 10),
                 "claude-opus-4-8": (5, 25), "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25),
                 "claude-sonnet-4-6": (3, 15), "claude-haiku-4-5": (1, 5)}

# Starting points for the "add a provider" form (Модели ИИ).
TEMPLATES = {
    "gigachat": {"kind": "gigachat", "title": "GigaChat", "base_url": "https://gigachat.devices.sberbank.ru/api/v1",
                 "auth_url": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth", "scope": "GIGACHAT_API_PERS",
                 "model": "GigaChat-2-Max", "api_key_env": "GIGACHAT_CREDENTIALS", "vision": True,
                 "prompt": "compact", "screenshots": "on_request",
                 "models": [{"name": "GigaChat-2-Max", "input": 1950, "output": 1950, "currency": "RUB", "vision": True},
                            {"name": "GigaChat-2-Pro", "input": 1500, "output": 1500, "currency": "RUB", "vision": True},
                            {"name": "GigaChat-2", "input": 200, "output": 200, "currency": "RUB", "vision": False}]},
    "yandex": {"kind": "openai", "title": "Yandex AI Studio", "base_url": "https://llm.api.cloud.yandex.net/v1",
               "model": "gpt://<folder_id>/yandexgpt/latest", "api_key_env": "YANDEX_API_KEY", "auth_scheme": "Api-Key",
               "vision": False, "prompt": "compact", "screenshots": "never",
               "models": [{"name": "gpt://<folder_id>/yandexgpt/latest", "input": 1200, "output": 1200,
                           "currency": "RUB", "vision": False}]},
    "vllm": {"kind": "openai", "title": "Своя модель (vLLM)", "base_url": "http://vllm:8000/v1",
             "model": "Qwen/Qwen2.5-VL-32B-Instruct", "api_key_env": "", "vision": True, "prompt": "compact",
             "screenshots": "on_request", "features": ["json_schema", "parallel_tool_calls"],
             "models": [{"name": "Qwen/Qwen2.5-VL-32B-Instruct", "input": 0, "output": 0, "currency": "RUB",
                         "vision": True}]},
    "ollama": {"kind": "openai", "title": "Ollama", "base_url": "http://localhost:11434/v1", "model": "qwen2.5vl:7b",
               "api_key_env": "", "vision": True, "prompt": "compact", "screenshots": "on_request",
               "features": ["json_schema"],
               "models": [{"name": "qwen2.5vl:7b", "input": 0, "output": 0, "currency": "RUB", "vision": True}]},
    "openai": {"kind": "openai", "title": "OpenAI-совместимый сервер", "base_url": "", "model": "",
               "api_key_env": "", "vision": True, "prompt": "compact", "screenshots": "on_request", "models": []},
}


def _anthropic_default() -> dict:
    base_url = os.environ.get("TESTGEN_LLM_BASE_URL", "").strip()
    given = os.environ.get("TESTGEN_LLM_FEATURES")
    if given is not None:
        features = list(ANTHROPIC_FEATURES) if given.strip() == "all" else \
            [f.strip() for f in given.split(",") if f.strip() in ANTHROPIC_FEATURES]
    else:
        # A proxy (gpt2giga...) speaks the Messages API, not its beta features.
        features = [] if base_url else list(ANTHROPIC_FEATURES)
    if os.environ.get("TESTGEN_PROMPT_CACHE", "on").lower() in ("off", "0", "false", "no") and "cache" in features:
        features.remove("cache")
    return {"id": "anthropic", "kind": "anthropic", "title": "Claude (Anthropic)", "base_url": base_url,
            "model": os.environ.get("TESTGEN_MODEL", "claude-opus-5"), "api_key_env": "ANTHROPIC_API_KEY",
            "features": features, "vision": True, "prompt": "full", "screenshots": "always",
            "models": [{"name": k, "input": v[0], "output": v[1], "currency": "USD", "vision": True}
                       for k, v in CLAUDE_PRICES.items()]}


DEFAULT = {"default": "anthropic", "fallbacks": [], "usd_rub": 80.0, "providers": []}


def _local_from_env() -> dict | None:
    """A model on your own server given by the environment (docker-compose.yml): provider "local".
    TESTGEN_LOCAL_LLM_URL (OpenAI-compatible, e.g. http://vllm:8000/v1), TESTGEN_LOCAL_LLM_MODEL,
    TESTGEN_LOCAL_LLM_VISION=off for a text-only model, TESTGEN_LOCAL_LLM_KEY if the server wants one."""
    url = os.environ.get("TESTGEN_LOCAL_LLM_URL", "").strip()
    if not url:
        return None
    model = os.environ.get("TESTGEN_LOCAL_LLM_MODEL", "Qwen/Qwen2.5-VL-7B-Instruct").strip()
    vision = os.environ.get("TESTGEN_LOCAL_LLM_VISION", "on").lower() not in ("off", "0", "false", "no")
    return {"id": "local", "kind": "openai", "title": "Своя модель", "base_url": url, "model": model,
            "api_key_env": "TESTGEN_LOCAL_LLM_KEY", "vision": vision, "prompt": "compact",
            "screenshots": "on_request" if vision else "never", "features": ["json_schema", "parallel_tool_calls"],
            "models": [{"name": model, "input": 0.0, "output": 0.0, "currency": "RUB", "vision": vision}]}


# ---------- settings ----------

def settings() -> dict:
    """The effective settings: the stored ones over the defaults, "anthropic" always present."""
    with fs.reading(FILE):
        stored = fs.read_json(FILE, {})
    s = copy.deepcopy(DEFAULT) | {k: v for k, v in stored.items() if k in DEFAULT}
    env_default = _anthropic_default()
    providers = [p for p in s["providers"] if isinstance(p, dict) and _ID.fullmatch(str(p.get("id", "")))]
    anth = next((p for p in providers if p["id"] == "anthropic"), None)
    if anth is None:
        providers.insert(0, env_default)
    else:
        # The environment decides where Claude is and which features the endpoint has.
        for key in ("base_url", "features") if os.environ.get("TESTGEN_LLM_BASE_URL") else ():
            anth[key] = env_default[key]
        if os.environ.get("TESTGEN_LLM_FEATURES") is not None:
            anth["features"] = env_default["features"]
        anth.setdefault("models", env_default["models"])
    local = _local_from_env()
    if local and not any(p["id"] == "local" for p in providers):
        providers.append(local)
    s["providers"] = providers
    env_pick = os.environ.get("TESTGEN_LLM_PROVIDER", "").strip()
    if env_pick:
        s["default"] = env_pick
    if not any(p["id"] == s["default"] for p in providers):
        s["default"] = "anthropic"
    return s


def save_settings(s: dict) -> dict:
    clean = {"default": str(s.get("default") or "anthropic"),
             "fallbacks": [str(x) for x in s.get("fallbacks") or [] if str(x).strip()],
             "usd_rub": float(s.get("usd_rub") or 80.0),
             "providers": [normalize(p) for p in s.get("providers") or []]}
    with fs.lock(FILE):
        fs.write_json(FILE, clean)
    _instances.clear()
    return settings()


def normalize(p: dict) -> dict:
    kind = p.get("kind") if p.get("kind") in KINDS else "openai"
    pid = str(p.get("id") or "").strip().lower()
    if not _ID.fullmatch(pid):
        raise ValueError("Идентификатор провайдера: латиница в нижнем регистре, цифры и дефис")
    out = {"id": pid, "kind": kind, "title": str(p.get("title") or pid)[:80],
           "base_url": str(p.get("base_url") or "").strip(), "model": str(p.get("model") or "").strip(),
           "api_key_env": str(p.get("api_key_env") or "").strip(),
           "vision": bool(p.get("vision", True)),
           "prompt": p.get("prompt") if p.get("prompt") in ("full", "compact") else "compact",
           "screenshots": p.get("screenshots") if p.get("screenshots") in ("always", "on_request", "never")
           else "on_request",
           "features": [str(f) for f in p.get("features") or []],
           "models": []}
    for key in ("auth_url", "scope", "auth_scheme"):
        if p.get(key):
            out[key] = str(p[key]).strip()
    if p.get("verify") is not None and p.get("verify") != "":
        v = p["verify"]
        out["verify"] = False if str(v).lower() in ("false", "0", "no", "off") else str(v)
    if isinstance(p.get("headers"), dict):
        out["headers"] = {str(k): str(v) for k, v in p["headers"].items()}
    if p.get("temperature") not in (None, ""):
        out["temperature"] = float(p["temperature"])
    for m in p.get("models") or []:
        if not isinstance(m, dict) or not str(m.get("name") or "").strip():
            continue
        out["models"].append({"name": str(m["name"]).strip(), "input": float(m.get("input") or 0),
                              "output": float(m.get("output") or 0),
                              "currency": m.get("currency") if m.get("currency") in CURRENCIES else "USD",
                              "vision": bool(m.get("vision", out["vision"])),
                              **({"bench": m["bench"]} if isinstance(m.get("bench"), dict) else {})})
    return out


def provider_cfg(pid: str) -> dict | None:
    return next((p for p in settings()["providers"] if p["id"] == pid), None)


def model_cfg(pid: str, model: str) -> dict:
    p = provider_cfg(pid) or {}
    return next((m for m in p.get("models") or [] if m["name"] == model), {})


def key_of(p: dict) -> str:
    stored = (vault.load(SECRETS_KIND, p["id"]) or {}).get("api_key", "")
    return stored or (os.environ.get(p["api_key_env"], "") if p.get("api_key_env") else "")


def set_key(pid: str, key: str) -> None:
    if key:
        vault.save(SECRETS_KIND, pid, {"api_key": key})
    else:
        vault.delete(SECRETS_KIND, pid)
    _instances.clear()


def public(p: dict) -> dict:
    return p | {"key_set": bool(key_of(p)), "key_stored": bool((vault.load(SECRETS_KIND, p["id"]) or {}).get("api_key"))}


# ---------- instances ----------

_instances: dict[str, object] = {}


def get(pid: str, client_factory=None):
    """A provider instance by id (cached: GigaChat keeps its access token and uploaded images)."""
    p = provider_cfg(pid)
    if not p:
        raise ProviderError(f"Провайдер модели «{pid}» не настроен", retryable=True)
    sig = json.dumps(p, sort_keys=True) + str(id(TRANSPORT))
    cached = _instances.get(pid)
    if cached is not None and getattr(cached, "_sig", None) == sig:
        return cached
    if p["kind"] == "anthropic":
        if OFFLINE and not p.get("base_url"):
            raise ProviderError("Режим без интернета (TESTGEN_OFFLINE): Claude недоступен, выберите свою модель",
                                retryable=True)
        inst = AnthropicProvider(p, client_factory)
    elif p["kind"] == "gigachat":
        inst = GigaChatProvider(p, key_of(p), transport=TRANSPORT)
    else:
        inst = OpenAIProvider(p, key_of(p), transport=TRANSPORT)
    inst._sig = sig
    _instances[pid] = inst
    return inst


def resolve(stage: dict | None) -> tuple[str, str]:
    """(provider id, model) of a pipeline stage: its own or the defaults."""
    stage = stage or {}
    s = settings()
    pid = (stage.get("provider") or "").strip() or s["default"]
    p = next((x for x in s["providers"] if x["id"] == pid), None) or next(
        x for x in s["providers"] if x["id"] == s["default"])
    return p["id"], (stage.get("model") or "").strip() or p["model"]


def chain(stage: dict | None) -> list[tuple[str, str]]:
    """The stage's provider and model, then the fallback chain ("provider" or "provider:model")."""
    first = resolve(stage)
    out = [first]
    for item in settings()["fallbacks"]:
        pid, _, model = item.partition(":")
        p = provider_cfg(pid.strip())
        if p and (p["id"], model.strip() or p["model"]) not in out:
            out.append((p["id"], model.strip() or p["model"]))
    return out


def price(pid: str, model: str) -> tuple[float, float, str] | None:
    """(input, output per million tokens, currency) or None if unknown."""
    p = provider_cfg(pid or "anthropic")
    for m in (p or {}).get("models") or []:
        if model == m["name"] or model.startswith(m["name"] + "-"):
            return m["input"], m["output"], m["currency"]
    if (p or {}).get("kind") == "anthropic" or not pid:
        for k, (i, o) in CLAUDE_PRICES.items():
            if model == k or model.startswith(k + "-"):
                return i, o, "USD"
    return None


def usd_rub() -> float:
    try:
        return float(os.environ.get("TESTGEN_USD_RUB") or settings()["usd_rub"])
    except ValueError:
        return 80.0


def profile(stage: dict | None) -> dict:
    """How the authoring agent should work with this stage's model: prompt size, screenshots,
    whether Auto-Pilot is allowed (benchmark success) and whether it sees images."""
    pid, model = resolve(stage)
    p = provider_cfg(pid) or {}
    m = model_cfg(pid, model)
    return {"provider": pid, "model": model, "kind": p.get("kind", "anthropic"),
            "title": p.get("title", pid), "prompt": p.get("prompt", "full"),
            "screenshots": p.get("screenshots", "always"),
            "vision": bool(m.get("vision", p.get("vision", True))),
            "bench": m.get("bench")}


def record_bench(pid: str, model: str, result: dict) -> None:
    """Store a benchmark summary with the model (testgen.bench)."""
    s = settings()
    p = next((x for x in s["providers"] if x["id"] == pid), None)
    if not p:
        return
    m = next((x for x in p["models"] if x["name"] == model), None)
    if not m:
        m = {"name": model, "input": 0, "output": 0, "currency": "USD", "vision": p.get("vision", True)}
        p["models"].append(m)
    m["bench"] = result
    save_settings(s)
