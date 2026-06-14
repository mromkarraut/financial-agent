"""
Master orchestrator — receives raw Telegram text, uses the local LM Studio model
(OpenAI-compatible API) with tool/function calling to decide which sub-agents to
invoke, runs them in parallel, and returns a single formatted reply.

Routing heuristics (applied before the LLM sees the message):
  • Long messages (>LONG_MESSAGE_THRESHOLD chars)  → summarizer first
  • "watch $TICKER"                                 → watchlist add
  • "unwatch $TICKER"                               → watchlist remove
  • "check watchlist" / "watchlist"                 → watchlist check
  All other routing is handled by the model via function calling.

Note: function/tool calling requires a compatible model loaded in LM Studio
(e.g. Llama-3-Instruct, Qwen2.5-Instruct, Mistral-Nemo-Instruct).
"""

import asyncio
import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI

import config
from agents.base_agent import AgentResult
from agents.fundamentals_agent import FundamentalsAgent
from agents.options_agent import OptionsAgent
from agents.options_research_agent import OptionsResearchAgent
from agents.stock_research_agent import StockResearchAgent
from agents.summarizer_agent import SummarizerAgent
from agents.watchlist_agent import WatchlistAgent
from db.database import log_agent_call, log_message
from db.memory import MemoryManager

logger = logging.getLogger(__name__)


_SYNTHESIZER_PROMPT = """You are a financial research assistant. Synthesize the following tool results into a single clear reply for the user. Use the data directly — no hedging, no disclaimers. Be concise.

Tool results:
{results}

User's original question: {question}"""

_GENERAL_SYSTEM = (
    "You are a helpful financial assistant. Answer the user's question clearly and concisely. "
    "You can discuss markets, investing concepts, economics, personal finance, or any other topic. "
    "Keep responses brief and practical."
)

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|(?<!\w)([A-Z]{2,5})(?!\w)")
_OPTIONS_RE = re.compile(r"\b(options?|calls?|puts?|spread|hedge|hedging)\b", re.IGNORECASE)
_BEARISH_RE = re.compile(r"\b(bearish|short|downside|sell)\b", re.IGNORECASE)
_BULLISH_RE = re.compile(r"\b(bullish|long|upside|buy)\b", re.IGNORECASE)

# Common words to exclude from ticker detection
_SKIP_WORDS = {
    # Common English words
    "I", "A", "AN", "THE", "IN", "ON", "AT", "TO", "OF", "OR", "AND", "BUT",
    "FOR", "IF", "IS", "IT", "BE", "DO", "GO", "MY", "NO", "SO", "UP", "US",
    "BY", "ME", "HE", "WE", "YO", "AM", "AS", "RE", "OK", "PM", "EU",
    # Geo / org abbreviations
    "AI", "ML", "TV", "UK", "VC", "IPO", "CEO", "CFO", "CTO",
    # Financial metrics (already present)
    "PE", "EPS", "RSI", "ETF", "SEC", "FED", "GDP", "CPI", "IMF", "USD", "EUR",
    # Options-specific words that are NOT tickers
    "CALL", "CALLS", "PUT", "PUTS", "OPTION", "OPTIONS",
    "BULL", "BEAR", "BULLISH", "BEARISH", "NEUTRAL",
    "SPREAD", "HEDGE", "HEDGING", "STRADDLE", "STRANGLE",
    "OTM", "ITM", "ATM", "DTE", "IV", "IVR", "POP", "ROC", "PNL",
    "LONG", "SHORT", "BUY", "SELL",
    # Analysis terms
    "MA", "EMA", "SMA", "MACD", "ATR", "BB", "VWAP",
}


class Orchestrator:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key="lm-studio",
        )
        self._agents: dict[str, Any] = {
            "stock_research":    StockResearchAgent(),
            "fundamentals":      FundamentalsAgent(),
            "options":           OptionsAgent(),
            "options_research":  OptionsResearchAgent(),
            "summarizer":        SummarizerAgent(),
            "watchlist":         WatchlistAgent(),
        }
        self.memory = MemoryManager(self._client)

    # ── Public entry point ────────────────────────────────────────────────────

    async def process(
        self,
        text: str,
        chat_id: str | int = "",
        message_id: str | int = "",
    ) -> str:
        await log_message(chat_id, message_id, text)

        try:
            # Fast-path: watchlist commands (no LLM call needed)
            fast = self._try_watchlist_fast_path(text)
            if fast is not None:
                result = await self._agents["watchlist"].run(fast)
                await self._log_result(fast, result)
                reply = result["output"] or "Watchlist updated."
                await self.memory.save_turn(chat_id, text, reply)
                return reply

            # Pre-process: summarize long pastes before routing
            working_text = text
            if len(text) > config.LONG_MESSAGE_THRESHOLD:
                sum_result = await self._agents["summarizer"].run({"text": text})
                await self._log_result({"text": text}, sum_result)
                if sum_result["confidence"] > 0:
                    working_text = (
                        f"[Auto-summarized from {len(text)}-char message]\n"
                        f"{sum_result['output']}\n\n"
                        f"[Original excerpt]: {text[:200]}…"
                    )

            history = await self.memory.get_context(chat_id)
            reply = await self._route_with_llm(working_text, history, chat_id=chat_id)
            await self.memory.save_turn(chat_id, text, reply)
            return reply

        except Exception as exc:
            logger.exception("Orchestrator.process failed: %s", exc)
            return (
                "⚠ An internal error occurred while processing your request. "
                "Please try again in a moment."
            )

    # ── Fast-path watchlist detection ─────────────────────────────────────────

    _WATCH_RE = re.compile(
        r"(?:^|\s)(?P<cmd>watch|unwatch)\s+\$?(?P<ticker>[A-Z]{1,5})\b",
        re.IGNORECASE,
    )
    _CHECK_RE = re.compile(r"\bcheck\s+watchlist\b|\bwatchlist\b", re.IGNORECASE)

    def _try_watchlist_fast_path(self, text: str) -> dict | None:
        m = self._WATCH_RE.search(text)
        if m:
            cmd = m.group("cmd").lower()
            ticker = m.group("ticker").upper()
            return {"action": "add" if cmd == "watch" else "remove", "ticker": ticker}
        if self._CHECK_RE.search(text):
            return {"action": "check", "ticker": ""}
        return None

    # ── Regex-based routing ───────────────────────────────────────────────────

    async def _route_with_llm(self, text: str, history: list[dict], chat_id: str | int = "") -> str:
        calls = self._build_calls(text, chat_id=chat_id)
        if not calls:
            return await self._general_reply(text, history)

        # Execute all agent calls in parallel
        tasks = [self._dispatch(call["tool"], call.get("args", {})) for call in calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log and collect outputs
        parts: list[str] = []
        for call, result in zip(calls, results):
            if isinstance(result, Exception):
                logger.error("Agent call failed for %s: %s", call["tool"], result)
            else:
                await self._log_result(call.get("args", {}), result)
                if result.get("output"):
                    parts.append(result["output"])

        if not parts:
            return "⚠ All agents failed to return results."

        return "\n\n".join(parts)

    async def _general_reply(self, text: str, history: list[dict]) -> str:
        user_msg = {"role": "user", "content": f"{_GENERAL_SYSTEM}\n\n{text}"}
        for messages in [history + [user_msg], [user_msg]]:
            try:
                response = await self._client.chat.completions.create(
                    model=config.LLM_MODEL,
                    max_tokens=config.LLM_MAX_TOKENS,
                    messages=messages,
                )
                return (response.choices[0].message.content or "I'm not sure how to answer that.").strip()
            except Exception as exc:
                if messages is not history + [user_msg]:
                    raise
                logger.warning("_general_reply failed with history (corrupt?), retrying without: %s", exc)
        return "I'm not sure how to answer that."

    def _build_calls(self, text: str, chat_id: str | int = "") -> list[dict]:
        calls: list[dict] = []
        tickers = self._extract_tickers(text)
        wants_options = bool(_OPTIONS_RE.search(text))

        for ticker in tickers:
            if wants_options:
                # Options research is self-contained — skip stock/fundamentals to keep output clean
                outlook = (
                    "bearish" if _BEARISH_RE.search(text)
                    else "bullish" if _BULLISH_RE.search(text)
                    else "neutral"
                )
                calls.append({"tool": "run_options_research", "args": {
                    "ticker": ticker, "outlook": outlook, "chat_id": str(chat_id),
                }})
            else:
                calls.append({"tool": "run_stock_research", "args": {"ticker": ticker}})
                calls.append({"tool": "run_fundamentals", "args": {"ticker": ticker}})

        return calls

    def _extract_tickers(self, text: str) -> list[str]:
        seen: set[str] = set()
        tickers: list[str] = []
        for m in _TICKER_RE.finditer(text.upper()):
            ticker = (m.group(1) or m.group(2)).upper()
            if ticker not in _SKIP_WORDS and ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
        return tickers

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def _dispatch(self, tool_name: str, tool_input: dict) -> AgentResult:
        mapping = {
            "run_stock_research":   ("stock_research",   tool_input),
            "run_fundamentals":     ("fundamentals",     tool_input),
            "run_options_analysis": ("options",          tool_input),
            "run_options_research": ("options_research", tool_input),
            "run_summarizer":       ("summarizer",       tool_input),
            "run_watchlist":        ("watchlist",        tool_input),
        }
        entry = mapping.get(tool_name)
        if entry is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        key, agent_input = entry
        return await self._agents[key].run(agent_input)

    # ── Audit logging ─────────────────────────────────────────────────────────

    async def _log_result(self, input_data: dict, result: AgentResult) -> None:
        try:
            await log_agent_call(
                agent_name=result.get("agent", "unknown"),
                agent_version=result.get("version", "?"),
                input_data=input_data,
                result=dict(result),
            )
        except Exception as exc:
            logger.warning("Failed to log agent call: %s", exc)
