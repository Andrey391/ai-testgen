"""Requests and replies in one neutral format: the Anthropic Messages format with plain dicts,
so the agent's conversation goes to the API as is:

    {"role": "user" | "assistant", "content": str | [block, ...]}
    block: {"type": "text", "text"}
           {"type": "image", "source": {"type": "base64", "media_type", "data"}}
           {"type": "tool_use", "id", "name", "input"}
           {"type": "tool_result", "tool_use_id", "content": str | [text/image blocks], "is_error"?}

What a model may lack is replaced here: old screenshots are cut out of the history
(	rim_history) instead of server-side context editing; structured output is a JSON
schema in the prompt, checked with pydantic and asked again once on error.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

REMOVED_IMAGE = "[screenshot removed: an older page state]"
REMOVED_STATE = "[older page state removed]"


class ProviderError(Exception):
    """A provider failed (network, auth, quota, bad answer): the message is shown to the user.
    `retryable`: another provider of the fallback chain may succeed."""

    def __init__(self, message: str, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class Request:
    model: str
    system: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)
    max_tokens: int = 16000
    effort: str = ""
    one_tool: bool = True            # at most one tool call per answer
    cache: bool = True               # cache the static prefix: tools + system (Anthropic)
    cache_all: bool = False          # also cache the growing conversation (the authoring agent)
    context: str = ""                # static text after the system prompt, cached separately
    keep_images: int = 0             # >0: keep only the last N screenshots / page states
    stage: dict = field(default_factory=dict)


@dataclass
class Reply:
    content: list[dict]              # neutral assistant blocks: goes into the history as is
    stop: str = "end"                # end | tool | max_tokens | refusal
    model: str = ""
    provider: str = ""
    usage: dict = field(default_factory=dict)
    parsed: Any = None

    @property
    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text" and b.get("text"))

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def usage_dict(input_tokens=0, output_tokens=0, cache_write=0, cache_read=0) -> dict:
    return {"input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0),
            "cache_creation_input_tokens": int(cache_write or 0), "cache_read_input_tokens": int(cache_read or 0)}


# ---------- history ----------

def _is_state(text: str) -> bool:
    return text.startswith("URL: ") or "Elements (" in text or "\nElements:" in text


def trim_history(messages: list[dict], keep: int) -> list[dict]:
    """A copy of the conversation where only the last `keep` user turns keep their screenshots
    and page snapshots: the rest become short placeholders (what context editing does on the
    Anthropic side). The latest turn is always kept whole."""
    if keep <= 0:
        return messages
    out = copy.deepcopy(messages)
    user_turns = [i for i, m in enumerate(out) if m["role"] == "user" and isinstance(m["content"], list)]
    for i in user_turns[:-keep]:
        out[i]["content"] = [_trim_block(b) for b in out[i]["content"]]
    return out


def _trim_block(b: dict) -> dict:
    t = b.get("type")
    if t == "image":
        return {"type": "text", "text": REMOVED_IMAGE}
    if t == "text" and _is_state(b.get("text", "")) and len(b["text"]) > 400:
        return {"type": "text", "text": b["text"].split("\n", 1)[0] + "\n" + REMOVED_STATE}
    if t == "tool_result" and isinstance(b.get("content"), list):
        return b | {"content": [_trim_block(x) for x in b["content"]]}
    return b


# ---------- tools ----------

def check_call(call: dict, tools: list[dict]) -> str:
    """"" if the tool call fits the declared tools, else what is wrong (sent back to the model)."""
    tool = next((t for t in tools if t["name"] == call.get("name")), None)
    if tool is None:
        return (f"Unknown tool {call.get('name')!r}. Use one of: " + ", ".join(t["name"] for t in tools))
    inp = call.get("input")
    if not isinstance(inp, dict) or "__raw__" in inp:
        return "The tool arguments are not a valid JSON object. Call the tool again with correct JSON arguments."
    schema = tool.get("input_schema") or {}
    missing = [k for k in schema.get("required", []) if k not in inp]
    if missing:
        return f"Missing required arguments of {tool['name']}: {', '.join(missing)}."
    for k, spec in (schema.get("properties") or {}).items():
        if k in inp and spec.get("enum") and inp[k] not in spec["enum"]:
            return f"Argument {k} of {tool['name']} must be one of {spec['enum']}."
    return ""


# ---------- structured output without server support ----------

def schema_instruction(schema: type[BaseModel]) -> str:
    return ("\n\nAnswer with ONE JSON object only, no text before or after it, that validates against this "
            "JSON schema:\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False))


def extract_json(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def validate(schema: type[BaseModel], text_or_obj) -> tuple[Any, str]:
    """-> (model instance or None, error text)."""
    try:
        if isinstance(text_or_obj, dict):
            return schema.model_validate(text_or_obj), ""
        return schema.model_validate_json(extract_json(text_or_obj)), ""
    except (ValidationError, ValueError) as e:
        return None, str(e)[:1500]
