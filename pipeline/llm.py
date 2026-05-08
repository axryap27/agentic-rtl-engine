"""Shared Anthropic client factory and prompt-cached call helper.

Usage
-----
    from pipeline.llm import call_claude

    response_text = call_claude(
        system="You are a hardware design expert ...",  # cached
        user="Generate a TLA+ spec for ...",
    )

The system prompt is automatically wrapped with cache_control so large,
reused prompts hit Anthropic's prompt cache on subsequent calls within
the same TTL window (5 minutes by default).

Model
-----
    claude-sonnet-4-6  (hard-coded; change MODEL constant if needed)
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

MODEL = "claude-sonnet-4-6"

# Module-level singleton — constructed once per process.  The SDK is
# thread-safe so sharing across nodes is fine.
_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """Return (or lazily create) the shared Anthropic client."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable is not set."
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def call_claude(
    *,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    extra_messages: list[dict[str, Any]] | None = None,
) -> str:
    """Call Claude with prompt caching on the system prompt.

    Parameters
    ----------
    system:
        The system prompt.  It will be sent with ``cache_control`` so that
        repeated calls with the same system text benefit from Anthropic's
        prompt cache (cache TTL is 5 minutes for Sonnet).
    user:
        The user turn content.
    max_tokens:
        Maximum tokens in the assistant reply.
    temperature:
        Sampling temperature (0.0 for deterministic output).
    extra_messages:
        Optional list of additional message dicts to prepend before the
        final user turn (useful for injecting prior assistant drafts or
        lint error context).

    Returns
    -------
    str
        The text of the first content block in the assistant response.
    """
    client = get_client()

    # Build message list.
    messages: list[dict[str, Any]] = []
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": user})

    # System prompt with cache_control on the last text block so the full
    # system text is eligible for caching.
    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=messages,
    )

    # Extract text from the first content block.
    for block in response.content:
        if hasattr(block, "text"):
            return block.text

    raise ValueError(
        f"Claude returned no text content block. "
        f"Stop reason: {response.stop_reason!r}, "
        f"content types: {[type(b).__name__ for b in response.content]}"
    )