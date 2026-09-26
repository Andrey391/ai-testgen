"""Shared Claude client settings."""
from __future__ import annotations

import os

import anthropic

MODEL = os.environ.get("TESTGEN_MODEL", "claude-opus-5")
# Browser driving is a latency-sensitive loop of many small decisions;
# "medium" keeps each step fast. Raise to "high" for tricky apps.
EFFORT = os.environ.get("TESTGEN_EFFORT", "medium")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_BETA = "context-management-2025-06-27"

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

    fallbacks="default": if Claude's safety classifiers decline a request, the
    API re-runs it on Anthropic's recommended fallback model instead of failing.
    """
    stage = stage or {}
    effort = stage.get("effort") if stage.get("effort") in EFFORTS else EFFORT
    return {
        "model": (stage.get("model") or "").strip() or MODEL,
        "output_config": {"effort": effort},
        "fallbacks": "default",
    }


def api_error_text(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "Claude API: invalid or missing credentials (set ANTHROPIC_API_KEY)."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude API: rate limited, try again in a moment."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude API: network error."
    if isinstance(e, TypeError) and "authentication" in str(e):
        # The SDK raises this before sending anything when no credentials are set.
        return "Claude API: invalid or missing credentials (set ANTHROPIC_API_KEY)."
    return f"{type(e).__name__}: {e}"
