"""
Watchlist MCP Server

Independent agent for managing a persistent ticker watchlist and generating
a live digest with price snapshots and Claude Haiku portfolio summary.

Tools:
  add_ticker(ticker)          → add to watchlist (writes to shared state.db)
  remove_ticker(ticker)       → remove from watchlist
  list_watchlist()            → return all tickers
  get_watchlist_digest()      → live prices for all tickers + LLM portfolio summary

Shared data:  db/state.db  (watchlist table shared with main Telegram bot)
Agent memory: db/agents/watchlist.db  (call log + digest history, independent)
LLM:          claude-haiku-4-5-20251001
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from db.database import watchlist_add, watchlist_get_all, watchlist_remove  # noqa: E402
from tools.market_data import get_stock_data  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "watchlist.db")
SYSTEM = (
    "You are a portfolio monitor. Given a watchlist snapshot, write a concise 2-3 sentence "
    "summary highlighting the strongest mover, the weakest, and overall portfolio tone "
    "(risk-on/risk-off/mixed). Be specific about tickers and percentages."
)

_llm = get_llm_client()

_db_ready = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS digest_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                tickers   TEXT NOT NULL,
                summary   TEXT NOT NULL,
                output    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                detail      TEXT,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _log_call(tool: str, detail: str, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, detail, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, detail, duration_ms),
        )
        await db.commit()


mcp = FastMCP(
    name="watchlist",
    instructions=(
        "Persistent ticker watchlist with live price digest and Claude Haiku portfolio commentary. "
        "Watchlist state is shared with the Telegram bot via state.db."
    ),
)


@mcp.tool()
async def add_ticker(ticker: str) -> str:
    """
    Add a ticker to the persistent watchlist.
    The change is immediately visible to the Telegram bot and web UI.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    if not ticker:
        return "Ticker cannot be empty."
    await watchlist_add(ticker)
    all_tickers = await watchlist_get_all()
    await _log_call("add_ticker", ticker, int((time.monotonic() - t0) * 1000))
    return f"Added {ticker}. Watchlist: {', '.join(all_tickers) or 'empty'}"


@mcp.tool()
async def remove_ticker(ticker: str) -> str:
    """
    Remove a ticker from the persistent watchlist.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    if not ticker:
        return "Ticker cannot be empty."
    await watchlist_remove(ticker)
    all_tickers = await watchlist_get_all()
    await _log_call("remove_ticker", ticker, int((time.monotonic() - t0) * 1000))
    return f"Removed {ticker}. Watchlist: {', '.join(all_tickers) or 'empty'}"


@mcp.tool()
async def list_watchlist() -> str:
    """
    Return all tickers currently on the watchlist.
    """
    await _ensure_db()
    tickers = await watchlist_get_all()
    if not tickers:
        return "Watchlist is empty. Use add_ticker to add stocks."
    return f"Watchlist ({len(tickers)} tickers): {', '.join(tickers)}"


@mcp.tool()
async def get_watchlist_digest() -> str:
    """
    Fetch live price snapshots for every ticker on the watchlist and generate
    a Claude Haiku portfolio summary highlighting movers and overall tone.
    """
    await _ensure_db()
    t0 = time.monotonic()
    tickers = await watchlist_get_all()
    if not tickers:
        return "Watchlist is empty."

    import asyncio
    results = await asyncio.gather(*[get_stock_data(t) for t in tickers], return_exceptions=True)

    rows: list[dict] = []
    lines = [f"Watchlist Digest — {len(tickers)} stocks\n"]
    lines.append(f"{'Ticker':<6}  {'Price':>8}  {'Chg%':>6}  {'RSI':>5}  {'vs MA20':>8}  {'vs MA50':>8}")
    lines.append("─" * 56)

    for ticker, result in zip(tickers, results):
        if isinstance(result, Exception) or "error" in result:
            lines.append(f"{ticker:<6}  ERROR: {result if isinstance(result, Exception) else result.get('error','?')}")
            continue
        p    = result["current_price"]
        chg  = result["price_change_pct"]
        rsi  = result["rsi_14"]
        ma20 = result.get("ma_20")
        ma50 = result.get("ma_50")
        vm20 = "↑" if ma20 and p > ma20 else "↓"
        vm50 = "↑" if ma50 and p > float(ma50) else "↓"
        sign = "+" if chg >= 0 else ""
        lines.append(
            f"{ticker:<6}  ${p:>7.2f}  {sign}{chg:>5.1f}%  {rsi:>5.1f}  "
            f"{'above' if vm20=='↑' else 'below':>8}  {'above' if vm50=='↑' else 'below':>8}"
        )
        rows.append({"ticker": ticker, "price": p, "chg": chg, "rsi": rsi})

    table = "\n".join(lines)

    summary = ""
    if rows:
        best  = max(rows, key=lambda r: r["chg"])
        worst = min(rows, key=lambda r: r["chg"])
        green = sum(1 for r in rows if r["chg"] > 0)
        tone  = "risk-on" if green > len(rows) / 2 else "risk-off" if green < len(rows) / 2 else "mixed"
        prompt = (
            f"Watchlist snapshot: {len(rows)} stocks. "
            f"Best: {best['ticker']} +{best['chg']:.1f}%. "
            f"Worst: {worst['ticker']} {worst['chg']:.1f}%. "
            f"{green}/{len(rows)} are green. Overall tone: {tone}. "
            f"Write a 2-3 sentence portfolio summary."
        )
        try:
            summary = await _llm.complete(SYSTEM, prompt, max_tokens=150)
        except Exception as exc:
            logger.warning("LLM call failed: %s", exc)
            summary = (
                f"Portfolio is {tone} with {green}/{len(rows)} stocks advancing. "
                f"Top mover: {best['ticker']} (+{best['chg']:.1f}%). "
                f"Laggard: {worst['ticker']} ({worst['chg']:.1f}%)."
            )

    output = table + (f"\n\nSummary:\n{summary}" if summary else "")
    if summary:
        async with aiosqlite.connect(AGENT_DB) as db:
            await db.execute(
                "INSERT INTO digest_log (timestamp, tickers, summary, output) VALUES (?,?,?,?)",
                (_utcnow(), json.dumps(tickers), summary, output),
            )
            await db.commit()

    await _log_call("get_watchlist_digest", ",".join(tickers), int((time.monotonic() - t0) * 1000))
    return output


if __name__ == "__main__":
    mcp.run()
