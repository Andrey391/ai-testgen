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

from .base import ProviderError, Reply, Request, trim_history, usage_dict

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


NO_KEY = "API ИИ: неверный или не заданный API-ключ (укажите его в «Проект → Модель»)."


def error_text(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError) or (isinstance(e, TypeError) and "authentication" in str(e)):
        # The SDK raises the TypeError before sending anything when no credentials are set.
        return NO_KEY
    if isinstance(e, anthropic.RateLimitError):
        return "API ИИ: превышен лимит запросов, повторите чуть позже."
    if isinstance(e, anthropic.APIStatusError):
        return f"Ошибка API ИИ {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "API ИИ: ошибка сети."
    return f"{type(e).__name__}: {e}"


def is_error(e: Exception) -> bool:
    return isinstance(e, anthropic.APIError) or (isinstance(e, TypeError) and "authentication" in str(e))


class AnthropicProvider:
    def __init__(self, client, features: set[str]):
        self._client = client
        self.features = set(features)

    def client(self):
        return self._client

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
        messages = req.messages
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
            if "context_editing" in self.features:
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

    async def chat(self, req: Request) -> Reply:
        p, betas = self._params(req)
        try:
            resp = await self.client().beta.messages.create(**p, **({"betas": betas} if betas else {}))
        except Exception as e:
            if is_error(e):
                raise ProviderError(error_text(e), status=getattr(e, "status_code", None)) from e
            raise
        return self._reply(resp)

    @property
    def structured(self) -> bool:
        return "structured" in self.features

    async def parse(self, req: Request, schema) -> Reply:
        """Server-side structured output; the caller falls back to the prompt when it is off."""
        p, betas = self._params(req)
        try:
            resp = await self.client().beta.messages.parse(**p, output_format=schema,
                                                           **({"betas": betas} if betas else {}))
        except Exception as e:
            if is_error(e):
                raise ProviderError(error_text(e), status=getattr(e, "status_code", None)) from e
            raise
        return self._reply(resp)
