"""
Central LLM client factory for all MCP servers.

Every agent imports this module and calls get_llm_client() to get a
provider-agnostic client. Switch providers by editing .env — no server
code needs to change.

Provider is read from config.MCP_LLM_PROVIDER:
  "lmstudio"  — OpenAI-compatible local model via LM Studio  (default)
  "anthropic" — Anthropic Claude API  (requires ANTHROPIC_API_KEY)
  "openai"    — OpenAI API            (requires OPENAI_API_KEY)

All servers use the same interface:
  _llm = get_llm_client()
  text = await _llm.complete(system="...", user="...", max_tokens=200)

Relevant .env keys (all optional — see config.py for defaults):
  MCP_LLM_PROVIDER   lmstudio | anthropic | openai
  MCP_LLM_MODEL      model name that matches LM Studio or the API
  MCP_LLM_BASE_URL   base URL for lmstudio/openai (default: http://localhost:1234/v1)
  MCP_LLM_MAX_TOKENS default max tokens per completion (default: 512)
"""

from __future__ import annotations

import os
import sys

# Ensure project root is on the path when this module is loaded directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


class MCPLLMClient:
    """
    Provider-agnostic LLM client for MCP agents.

    Wraps OpenAI-compatible (LM Studio / OpenAI) and Anthropic APIs behind
    a single async complete() call so servers don't care which backend is live.
    """

    def __init__(self) -> None:
        self._provider = config.MCP_LLM_PROVIDER
        self._model    = config.MCP_LLM_MODEL

        if self._provider == "anthropic":
            import anthropic as _anthropic
            self._backend = _anthropic.AsyncAnthropic()
        elif self._provider == "openai":
            from openai import AsyncOpenAI
            self._backend = AsyncOpenAI()
        else:
            # "lmstudio" (default) — OpenAI-compatible endpoint
            from openai import AsyncOpenAI
            self._backend = AsyncOpenAI(
                base_url=config.MCP_LLM_BASE_URL,
                api_key="lm-studio",
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
    ) -> str:
        """
        Send a system + user prompt and return the assistant text.
        Raises on network/auth errors so callers can fall back gracefully.
        """
        tokens = max_tokens or config.MCP_LLM_MAX_TOKENS

        if self._provider == "anthropic":
            resp = await self._backend.messages.create(
                model=self._model,
                max_tokens=tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        else:
            # lmstudio / openai — OpenAI chat completions API
            resp = await self._backend.chat.completions.create(
                model=self._model,
                max_tokens=tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            return (resp.choices[0].message.content or "").strip()

    def __repr__(self) -> str:
        return f"MCPLLMClient(provider={self._provider!r}, model={self._model!r})"


def get_llm_client() -> MCPLLMClient:
    """Return a new MCPLLMClient configured from config.py / .env."""
    return MCPLLMClient()


def provider_info() -> str:
    """Human-readable description of the current LLM config."""
    return f"{config.MCP_LLM_PROVIDER} / {config.MCP_LLM_MODEL}"
