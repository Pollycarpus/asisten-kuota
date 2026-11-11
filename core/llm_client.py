"""
File: core/llm_client.py
Purpose: One small, provider-agnostic LLM client: chat(messages, tools) ->
         {message, tool_calls, usage}. Providers: azure_openai, openai_compatible
         (Gemini free tier, local models) and mock. Switch via .env only.
D2 component: Configurable LLM (§2.11, AD-1)
Depends on: requests, core/config.py, core/trace.py, core/mock_llm.py
"""

import json
import time

import requests

from core import mock_llm
from core.config import SETTINGS, rule
from core.trace import estimate_tokens


class LlmError(Exception):
    """
    Raised when the LLM provider fails (network, HTTP error, unusable response).

    Why: the agent catches it and answers with a safe fallback message.
    Args:
        message (str): description.
    Returns:
        n/a
    Raises / Errors:
        n/a
    Side effects:
        None
    """


class LlmRateLimited(LlmError):
    """
    Raised when the provider still returns HTTP 429 after one backoff.

    Why: §2.11 — 429: back off once, then fallback / handoff offer.
    Args:
        message (str): description.
    Returns:
        n/a
    Raises / Errors:
        n/a
    Side effects:
        None
    """


def build_request(messages: list, tools: list) -> tuple:
    """
    Build URL, headers and JSON body for the configured provider.

    Why: Azure and OpenAI-compatible endpoints differ only in URL and auth header (AD-1, §2.11).
    Args:
        messages (list): Chat Completions messages.
        tools (list): tool JSON schemas.
    Returns:
        tuple: (url, headers, body)
    Raises / Errors:
        LlmError: unknown provider or missing base URL.
    Side effects:
        None
    """
    llm = SETTINGS["llm"]
    if not llm["base_url"]:
        raise LlmError("LLM_BASE_URL is empty")
    body = {"messages": messages, "tools": tools, "tool_choice": "auto", "temperature": llm["temperature"]}
    if llm["reasoning_effort"]:
        body["reasoning_effort"] = llm["reasoning_effort"]   # Gemini 3: keep thinking short and cheap
    if llm["provider"] == "azure_openai":
        url = f"{llm['base_url']}/openai/deployments/{llm['model']}/chat/completions?api-version={llm['api_version']}"
        headers = {"api-key": llm["api_key"], "Content-Type": "application/json"}
    elif llm["provider"] == "openai_compatible":
        url = f"{llm['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"}
        body["model"] = llm["model"]
    else:
        raise LlmError(f"Unknown LLM_PROVIDER: {llm['provider']}")
    return url, headers, body


def post_with_rate_limit_retry(url: str, headers: dict, body: dict) -> dict:
    """
    POST to the provider; on HTTP 429 wait once and retry once.

    Why: §2.11 — free tiers rate-limit; back off once, then give up gracefully.
    Args:
        url (str), headers (dict), body (dict): the request.
    Returns:
        dict: parsed JSON response.
    Raises / Errors:
        LlmRateLimited: still 429 after one retry.
        LlmError: network error or other HTTP error.
    Side effects:
        HTTP calls to the LLM provider; may sleep.
    """
    timeout = SETTINGS["llm"]["timeout_s"]
    for attempt in (1, 2):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as error:
            raise LlmError(f"LLM request failed: {type(error).__name__}") from error
        if response.status_code == 429 and attempt == 1:
            time.sleep(rule("agent", "llm_rate_limit_backoff_s"))   # back off once
            continue
        if response.status_code == 429:
            raise LlmRateLimited("LLM rate limited (429) after one retry")
        if response.status_code >= 400:
            raise LlmError(f"LLM HTTP {response.status_code}: {response.text[:200]}")
        return response.json()
    raise LlmError("unreachable")


def parse_tool_calls(message: dict) -> list:
    """
    Convert provider tool calls into [{id, name, arguments(dict)}].

    Why: one normalized shape for the agent, whatever the provider.
    Args:
        message (dict): assistant message from the provider.
    Returns:
        list: normalized tool calls.
    Raises / Errors:
        ValueError: if a tool call's arguments are not valid JSON.
    Side effects:
        None
    """
    calls = []
    for call in message.get("tool_calls") or []:
        raw_arguments = call["function"].get("arguments") or "{}"
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        calls.append({"id": call.get("id") or f"call_{len(calls)}", "name": call["function"]["name"],
                      "arguments": arguments})
    return calls


def read_usage(response_json: dict, messages: list, message: dict) -> dict:
    """
    Read token usage, or estimate it when the provider sends none.

    Why: §2.11 — missing usage => estimate tokens and mark them as estimated.
    Args:
        response_json (dict): full provider response.
        messages (list): the prompt messages (for estimation).
        message (dict): the assistant message (for estimation).
    Returns:
        dict: {prompt_tokens, completion_tokens, estimated}
    Raises / Errors:
        None
    Side effects:
        None
    """
    usage = response_json.get("usage") or {}
    if usage.get("prompt_tokens") is not None:
        return {"prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage.get("completion_tokens") or 0, "estimated": False}
    return {"prompt_tokens": estimate_tokens(json.dumps(messages)),
            "completion_tokens": estimate_tokens(json.dumps(message)), "estimated": True}


def chat(messages: list, tools: list) -> dict:
    """
    Send one chat turn to the configured LLM and return a normalized result.

    Why: §2.11 single entry point; guardrails never depend on which model answers.
    Args:
        messages (list): Chat Completions messages (system + masked history).
        tools (list): tool JSON schemas.
    Returns:
        dict: {"message": assistant message as returned (kept verbatim for Gemini thought
               signatures), "tool_calls": [...], "usage": {...}}
    Raises / Errors:
        LlmRateLimited: 429 after one retry.
        LlmError: provider failure or malformed tool-call JSON after one repair retry.
    Side effects:
        HTTP calls to the provider (none for mock).
    """
    if SETTINGS["llm"]["provider"] == "mock":
        return mock_llm.respond(messages, tools)
    url, headers, body = build_request(messages, tools)
    for attempt in (1, 2):                      # attempt 2 = one repair retry for malformed tool JSON
        response_json = post_with_rate_limit_retry(url, headers, body)
        try:
            message = response_json["choices"][0]["message"]
            tool_calls = parse_tool_calls(message)
        except (KeyError, IndexError, ValueError) as error:
            if attempt == 2:
                raise LlmError(f"Unusable LLM response: {type(error).__name__}") from error
            continue
        message.setdefault("role", "assistant")
        return {"message": message, "tool_calls": tool_calls, "usage": read_usage(response_json, messages, message)}
    raise LlmError("unreachable")
