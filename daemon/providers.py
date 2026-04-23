"""
brainloop.providers
~~~~~~~~~~~~~~~~~~~
Minimal LLM provider adapters for the analyzer.

Two shapes are supported:

  1. OpenAICompat — any endpoint speaking /v1/chat/completions with
     response_format: json_schema. Covers OpenAI, Gemini's OpenAI-compat
     endpoint, Ollama, etc.

  2. Anthropic — native /v1/messages, uses a single tool for structured
     output because Claude doesn't support response_format.

Both return the same 3-tuple: (payload, tokens_in, tokens_out).

Only stdlib http is used — we don't want to pull the `openai` or `anthropic`
packages into the daemon's venv just for one request per 30 minutes.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("brainloop.providers")

_TIMEOUT_SECS = 120
_MAX_OUTPUT_TOKENS = 16384   # Gemini 2.5 thinks before answering; budget generously.


class LLMError(Exception):
    """Raised when an endpoint rejects the request or returns unusable output."""


def call(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    schema: dict,
) -> tuple[dict, int, int]:
    """Dispatch to the right adapter. Returns (payload, tokens_in, tokens_out).

    Raises LLMError on any non-retryable failure (bad key, bad schema, etc.).
    """
    provider = provider.lower().strip()
    if provider == "anthropic":
        return _call_anthropic(base_url, api_key, model, system, user, schema)
    # openai, gemini, ollama, local, or anything else — all OpenAI-compat.
    return _call_openai_compat(base_url, api_key, model, system, user, schema)


# ── OpenAI-compatible /v1/chat/completions ────────────────────────────────────

def _call_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    schema: dict,
) -> tuple[dict, int, int]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema["name"], "schema": schema["schema"], "strict": True},
        },
    }
    # Gemini 2.5 reasoning models silently consume the output budget on
    # internal thinking before producing the JSON. Ask them to spend as little
    # thinking as they can justify. Harmless on non-Gemini endpoints that
    # ignore the field.
    if "googleapis.com" in base_url or "gemini" in model.lower():
        body["reasoning_effort"] = "low"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = _post_json(url, headers, body)

    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in response: {data}")
    content = choices[0].get("message", {}).get("content")
    if not content:
        raise LLMError(f"empty content: {data}")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as e:
        finish = choices[0].get("finish_reason")
        raise LLMError(
            f"invalid JSON in content (finish_reason={finish}, content_len={len(content)}): {e}; "
            f"content[head]={content[:400]} ... content[tail]={content[-400:]}"
        ) from e

    usage = data.get("usage") or {}
    return payload, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


# ── Anthropic /v1/messages ────────────────────────────────────────────────────

def _call_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    schema: dict,
) -> tuple[dict, int, int]:
    url = base_url.rstrip("/") + "/v1/messages"
    # Claude doesn't support response_format. Use a single "emit_summary" tool
    # and force its use — the model must call it with the structured payload.
    tool = {
        "name": "emit_summary",
        "description": "Emit the day summary as a strictly-typed JSON object.",
        "input_schema": schema["schema"],
    }
    body = {
        "model": model,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "emit_summary"},
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data = _post_json(url, headers, body)

    blocks = data.get("content") or []
    payload = None
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == "emit_summary":
            payload = block.get("input")
            break
    if payload is None:
        raise LLMError(f"no tool_use block in response: {data}")

    usage = data.get("usage") or {}
    return payload, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


# ── Chat (tool-use aware, free-text output) ──────────────────────────────────
#
# Shape of the `messages` argument is OpenAI-flavored and we translate to
# Anthropic on the way in / out. Callers get a normalised response:
#
#   {"type": "text",       "content": "..."}                    — final answer
#   {"type": "tool_calls", "calls":   [{id, name, arguments}..]} — model wants
#                                                                  a tool run
#
# The caller runs the tool(s) and loops with updated messages; we don't
# iterate here.

def call_chat(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[dict, int, int]:
    provider = (provider or "").lower().strip()
    if provider == "anthropic":
        return _chat_anthropic(base_url, api_key, model, system, messages, tools)
    return _chat_openai_compat(base_url, api_key, model, system, messages, tools)


def _chat_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[dict, int, int]:
    url = base_url.rstrip("/") + "/chat/completions"
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": 0.3,
        "max_tokens": _MAX_OUTPUT_TOKENS,
    }
    if tools:
        body["tools"] = [
            {"type": "function", "function": t} for t in tools
        ]
        body["tool_choice"] = "auto"
    if "googleapis.com" in base_url or "gemini" in model.lower():
        body["reasoning_effort"] = "low"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = _post_json(url, headers, body)
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"no choices in chat response: {data}")
    msg = choices[0].get("message") or {}
    tool_calls = msg.get("tool_calls") or []
    usage = data.get("usage") or {}
    tin  = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    if tool_calls:
        normalised = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except Exception:
                args = {"_parse_error": args_raw}
            normalised.append({
                "id":   tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": args,
            })
        return ({"type": "tool_calls", "calls": normalised, "raw_assistant": msg}, tin, tout)
    return ({"type": "text", "content": msg.get("content") or ""}, tin, tout)


def _chat_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> tuple[dict, int, int]:
    url = base_url.rstrip("/") + "/v1/messages"
    # Anthropic expects messages without a 'system' role; we passed system
    # separately. Translate OpenAI-flavored tool messages to Anthropic blocks
    # on the caller's behalf — see _to_anthropic_messages.
    anthro_messages = _to_anthropic_messages(messages)
    body: dict = {
        "model": model,
        "max_tokens": _MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": anthro_messages,
    }
    if tools:
        body["tools"] = [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]}
            for t in tools
        ]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data = _post_json(url, headers, body)
    blocks = data.get("content") or []
    usage = data.get("usage") or {}
    tin  = int(usage.get("input_tokens") or 0)
    tout = int(usage.get("output_tokens") or 0)

    tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
    if tool_uses:
        calls = [{
            "id":   b.get("id") or "",
            "name": b.get("name") or "",
            "arguments": b.get("input") or {},
        } for b in tool_uses]
        return ({"type": "tool_calls", "calls": calls, "raw_assistant": blocks}, tin, tout)

    text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return ({"type": "text", "content": "".join(text_parts).strip()}, tin, tout)


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Translate OpenAI-flavoured chat messages to Anthropic blocks.

    Assistant messages with tool_calls become content with tool_use blocks.
    Tool results (role='tool') become user-role content with tool_result blocks.
    Plain user/assistant/text messages pass through.
    """
    out: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results():
        nonlocal pending_tool_results
        if pending_tool_results:
            out.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in messages:
        role = m.get("role")
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") or "",
            })
            continue
        # Anything else — flush any pending tool results first.
        flush_tool_results()
        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                except Exception:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id":   tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "input": args,
                })
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role or "user", "content": m.get("content") or ""})
    flush_tool_results()
    return out


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _post_json(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise LLMError(f"HTTP {e.code} from {url}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"network error reaching {url}: {e.reason}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"non-JSON response from {url}: {raw[:300]}") from e
