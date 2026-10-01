"""What every LLM provider speaks: requests and replies in one neutral format.

The neutral message format is Anthropic's Messages format with plain dicts, so the
agent's conversation needs no conversion for Claude:

    {"role": "user" | "assistant", "content": str | [block, ...]}
    block: {"type": "text", "text"}
           {"type": "image", "source": {"type": "base64", "media_type", "data"}}
           {"type": "tool_use", "id", "name", "input", "_meta"?}
           {"type": "tool_result", "tool_use_id", "content": str | [text/image blocks], "is_error"?}

A provider translates it to its own API (OpenAI-compatible Chat Completions,
GigaChat) and back. Provider-specific data a later request needs (GigaChat's
functions_state_id) rides in a block's "_meta" and is dropped for other providers.
Tools are Anthropic-style dicts ({name, description, input_schema}).

Functions the other APIs lack are replaced here: old screenshots are cut out of the
history (`trim_history`) instead of server-side context editing; structured output is
a JSON schema in the prompt, checked with pydantic and asked again once on error.
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


def drop_images(messages: list[dict], note: str = "[image omitted: this model does not see images]") -> list[dict]:
    out = copy.deepcopy(messages)
    for m in out:
        if isinstance(m["content"], list):
            m["content"] = [_no_image(b, note) for b in m["content"]]
    return out


def _no_image(b: dict, note: str) -> dict:
    if b.get("type") == "image":
        return {"type": "text", "text": note}
    if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
        return b | {"content": [_no_image(x, note) for x in b["content"]]}
    return b


def blocks(content) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def result_parts(content) -> tuple[str, list[dict]]:
    """A tool_result's content -> (text, image blocks)."""
    text, images = [], []
    for b in blocks(content):
        if b.get("type") == "image":
            images.append(b)
        elif b.get("type") == "text":
            text.append(b.get("text", ""))
    return "\n".join(text), images


def strip_meta(messages: list[dict]) -> list[dict]:
    """Messages without provider-specific extras (for Anthropic, which rejects unknown keys)."""
    out = []
    for m in messages:
        if isinstance(m["content"], list):
            m = m | {"content": [{k: v for k, v in b.items() if k != "_meta"} for b in m["content"]]}
        out.append(m)
    return out


# ---------- tools ----------

def plain_schema(schema: dict) -> dict:
    """A JSON schema without the keywords weaker function-calling APIs reject."""
    def clean(s):
        if isinstance(s, dict):
            return {k: clean(v) for k, v in s.items() if k not in ("strict", "additionalProperties", "$schema")}
        if isinstance(s, list):
            return [clean(x) for x in s]
        return s
    return clean(schema or {"type": "object", "properties": {}})


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


def loads_args(raw) -> dict:
    """Function arguments as a dict; unparsable JSON -> {"__raw__": text} (repaired by the agent)."""
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"__raw__": str(raw)}
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", str(raw or ""), re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                if isinstance(v, dict):
                    return v
            except ValueError:
                pass
        return {"__raw__": str(raw)}


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
