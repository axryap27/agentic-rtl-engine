"""Shared LLM client and call helper.

Uses an OpenAI-compatible API.  The proxy at LLM_BASE_URL routes to the
underlying provider (Anthropic / Google / etc.) based on the model name.

Credentials are loaded from a gitignored .env file via python-dotenv.

Environment variables
---------------------
    LLM_BASE_URL  – OpenAI-compatible base URL (e.g. the proxy or
                    https://api.anthropic.com/v1)
    LLM_API_KEY   – Bearer key for the proxy / provider
    LLM_MODEL     – Default model identifier (e.g. anthropic/claude-sonnet-latest)

Usage
-----
    from pipeline.llm import call_claude

    text = call_claude(
        system="You are a hardware design expert ...",
        user="Generate a TLA+ spec for ...",
    )

The function name `call_claude` is kept for backwards compatibility with the
existing pipeline nodes — under the hood it uses the OpenAI SDK and works
with any model the proxy exposes (override with the `model=` kwarg).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI

# Load .env from the project root (one level above this file).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-latest")

_client: Optional[OpenAI] = None


def get_client() -> OpenAI:
    """Return (or lazily create) the shared OpenAI-compatible client."""
    global _client
    if _client is None:
        api_key = os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL")
        if not api_key:
            raise EnvironmentError(
                "LLM_API_KEY is not set.  Copy .env.example to .env and fill it in, "
                "or export the variable in your shell."
            )
        if not base_url:
            raise EnvironmentError(
                "LLM_BASE_URL is not set.  See .env.example."
            )
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def call_claude(
    *,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    model: Optional[str] = None,
    extra_messages: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Call the configured LLM and return the assistant's text response.

    Parameters
    ----------
    system:
        System prompt.  Large reused prompts benefit from any caching the
        upstream proxy/provider supports.
    user:
        Final user message.
    max_tokens:
        Cap on assistant tokens.
    temperature:
        0.0 for deterministic generation (default for code/spec output).
    model:
        Override the default model.  Examples:
            "anthropic/claude-sonnet-latest"
            "google/gemini-pro-latest"
    extra_messages:
        Optional messages to prepend before the final user turn (e.g. prior
        assistant attempts, lint error context).

    Returns
    -------
    str
        The text content of the assistant's response.
    """
    client = get_client()

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": user})

    response = client.chat.completions.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=messages,
    )

    choice = response.choices[0]
    content = choice.message.content
    if content is None:
        raise ValueError(
            f"LLM returned no text content. finish_reason={choice.finish_reason!r}"
        )
    return content
