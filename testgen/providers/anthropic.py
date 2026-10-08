"""Requests to the model through the Anthropic Messages API - every feature the studio was built on.

Features (llm.Model.features) a model may lack - the studio then does the same on its side:

    cache            prompt caching (a breakpoint on tools + system, automatic caching of the conversation)
    context_editing  old screenshots are cleared server-side (clear_tool_uses); off: the studio cuts them
    effort           output_config.effort
    fallbacks        fallbacks="default": a declined request is re-run on the provider's fallback model
    structured       messages.parse with a pydantic output format; off: JSON schema in the prompt
"""
from __future__ import annotations

import anthropic
import pydantic
from anthropic.lib._parse._response import parse_beta_response
from anthropic.types.beta import BetaMessage

from .base import ProviderError, Reply, Request, fill_empty, trim_history, usage_dict

FEATURES = ("cache", "context_editing", "effort", "fallbacks", "structured")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"
CLEAR_TOOL_USES = "clear_tool_uses_20250919"
EPHEMERAL = {"type": "ephemeral"}
STOP = {"tool_use": "tool", "end_turn": "end", "stop_sequence": "end", "pause_turn": "end",
        "max_tokens": "max_tokens", "refusal": "refusal", "model_context_window_exceeded": "max_tokens"}


def _dump(block) -> dict:
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True, mode="json")
    return {k: v for k, v in vars(block).items() if v is not None}


def _lenient(data: dict) -> dict:
    """A gateway or a server of its own may answer with blocks the strict BetaMessage rejects,
    e.g. a thinking block with "signature": null: they are fixed before validation."""
    content = []
    for b in data.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "thinking":
            b = {**b, "thinking": b.get("thinking") or "", "signature": b.get("signature") or ""}
        content.append(b)
    return {**data, "content": content}


NO_KEY ="API ИИ: неверный или не заданный API-ключ (укажите его в «Проект → Модель»)."
NO_CREDITS = ("API ИИ: на балансе аккаунта, к которому относится API-ключ проекта, закончились кредиты. "
              "Пополните баланс в консоли Anthropic (Plans & Billing) или укажите ключ другого аккаунта "
              "в «Проект → Модель».")


def _no_credits(e: Exception) -> bool:
    """The API answers 400 invalid_request_error "credit balance is too low" (or a billing_error)."""
    body = getattr(e, "body", None)
    err = body.get("error", body) if isinstance(body, dict) else {}
    kind = err.get("type", "") if isinstance(err, dict) else ""
    return kind == "billing_error" or "credit balance" in str(getattr(e, "message", "") or e).lower()


def error_text(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError) or (isinstance(e, TypeError) and "authentication" in str(e)):
        # The SDK raises the TypeError before sending anything when no credentials are set.
        return NO_KEY
    if isinstance(e, anthropic.APIStatusError) and _no_credits(e):
        return NO_CREDITS
    if isinstance(e, anthropic.RateLimitError):
        return "API ИИ: превышен лимит запросов, повторите чуть позже."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "API ИИ: ошибка сети."
    return f"{type(e).__name__}: {e}"


def is_error(e: Exception) -> bool:
    return isinstance(e, anthropic.APIError) or (isinstance(e, TypeError) and "authentication" in str(e))


def _context_editing_failed(e: Exception) -> bool:
    """A gateway (e.g. LiteLLM) took context_management but failed on it - its polyfill does not
    know image blocks - instead of answering that the feature is unsupported."""
    if not isinstance(e, anthropic.APIStatusError):
        return False
    text = f"{getattr(e, 'message', '')} {getattr(e, 'body', '')}".lower()
    return any(k in text for k in ("context_management", "context-management", CLEAR_TOOL_USES))


# Connections (API address, model) where context editing failed: the studio cuts old screenshots itself.
NO_CONTEXT_EDITING: set[tuple[str, str]] = set()


class AnthropicProvider:
    def __init__(self, client, features: set[str]):
        self._client = client
        self.features = set(features)

    def client(self):
        return self._client

    def _key(self, model: str) -> tuple[str, str]:
        return str(getattr(self._client, "base_url", "")), model

    def _params(self, req: Request) -> tuple[dict, list[str]]:
        cache = req.cache and "cache" in self.features
        if cache:
            system = [{"type": "text", "text": req.system, "cache_control": EPHEMERAL}]
            if req.context:
                # A breakpoint of its own: the same context (requirements) in every request of a job.
                system = [{"type": "text", "text": req.system},
                          {"type": "text", "text": req.context, "cache_control": EPHEMERAL}]
        else:
            system = req.system + (f"\n\n{req.context}" if req.context else "")
        messages = fill_empty(req.messages)
        betas: list[str] = []
        p = {"model": req.model, "max_tokens": req.max_tokens, "system": system}
        if "effort" in self.features and req.effort:
            p["output_config"] = {"effort": req.effort}
        if "fallbacks" in self.features:
            p["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if req.tools:
            p["tools"] = req.tools
            p["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": req.one_tool}
        if req.keep_images:
            if "context_editing" in self.features and self._key(req.model) not in NO_CONTEXT_EDITING:
                betas.append(CONTEXT_BETA)
                # Old screenshots/snapshots are useless once the page moved on.
                p["context_management"] = {"edits": [{
                    "type": CLEAR_TOOL_USES,
                    "trigger": {"type": "input_tokens", "value": 40000},
                    "keep": {"type": "tool_uses", "value": req.keep_images},
                    "clear_at_least": {"type": "input_tokens", "value": 8000},
                }]}
            else:
                messages = trim_history(messages, req.keep_images)
        if cache and req.cache_all:
            p["cache_control"] = EPHEMERAL
        p["messages"] = messages
        return p, betas

    def _reply(self, resp) -> Reply:
        u = getattr(resp, "usage", None)
        return Reply(content=[_dump(b) for b in resp.content or []],
                     stop=STOP.get(getattr(resp, "stop_reason", "") or "", "end"),
                     model=getattr(resp, "model", "") or "",
                     usage=usage_dict(getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0),
                                      getattr(u, "cache_creation_input_tokens", 0),
                                      getattr(u, "cache_read_input_tokens", 0)),
                     parsed=getattr(resp, "parsed_output", None))

    async def _send(self, req: Request, call) -> Reply:
        p, betas = self._params(req)
        try:
            try:
                resp = await call(p, betas)
            except anthropic.APIStatusError as e:
                if "context_management" not in p or not _context_editing_failed(e):
                    raise
                NO_CONTEXT_EDITING.add(self._key(req.model))
                p, betas = self._params(req)          # now without context editing
                resp = await call(p, betas)
        except Exception as e:
            if is_error(e):
                raise ProviderError(error_text(e), status=getattr(e, "status_code", None)) from e
            raise
        return self._reply(resp)

    async def chat(self, req: Request) -> Reply:
        return await self._send(req, lambda p, betas: self.client().beta.messages.create(
            **p, **({"betas": betas} if betas else {})))

    @property
    def structured(self) -> bool:
        return "structured" in self.features

    async def parse(self, req: Request, schema) -> Reply:
        """Server-side structured output; the caller falls back to the prompt when it is off.
        The SDK validates the answer itself and raises on one cut off by max_tokens, losing the
        stop reason and the usage: the raw response is read and validated here instead."""
        async def call(p, betas):
            kw = {**p, "output_format": schema, **({"betas": betas} if betas else {})}
            api = self.client().beta.messages
            raw_api = getattr(api, "with_raw_response", None)
            if raw_api is None:
                return await api.parse(**kw)
            raw = await raw_api.parse(**kw)
            msg = BetaMessage.model_validate(_lenient(raw.http_response.json()))
            try:
                return parse_beta_response(response=msg, output_format=schema)
            except pydantic.ValidationError:
                return msg          # no parsed_output: the caller sees the stop reason and the text
        return await self._send(req, call)
