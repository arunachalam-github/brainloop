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
