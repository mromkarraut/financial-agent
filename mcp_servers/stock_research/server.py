"""
Stock Research MCP Server

Independent agent for price-action analysis: RSI-14, MA-20/50, trend stance,
and LLM-written narrative using Claude Haiku.

Tools:
  analyze_stock(ticker)             → full analysis with LLM narrative
  get_price_snapshot(ticker)        → raw price/RSI/MA data as JSON
  recall_analyses(ticker, limit)    → history from agent memory

Memory: db/agents/stock_research.db  (independent from main state.db)
LLM:    claude-haiku-4-5-20251001
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

# Project root on sys.path so tools/ and config work regardless of invocation method
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from tools.market_data import get_stock_data  # noqa: E402

logger = logging.getLogger(__name__)

# ── Agent-level config ─────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "stock_research.db")
SYSTEM = (
    "You are a quantitative stock analyst. Given price data, write a concise 2-3 sentence "
    "analysis covering trend, momentum, and a clear stance (Bullish/Bearish/Neutral). "
    "Be direct and data-driven. No disclaimers, no filler."
)

# ── Independent LLM client (provider + model set in config.py / .env) ─────────
_llm = get_llm_client()

# ── Independent memory ─────────────────────────────────────────────────────────
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
            CREATE TABLE IF NOT EXISTS analyses (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker    TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                price     REAL,
                rsi       REAL,
                ma20      REAL,
                ma50      REAL,
                stance    TEXT,
                narrative TEXT,
                output    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save_analysis(ticker: str, data: dict, stance: str, narrative: str, output: str) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO analyses (ticker, timestamp, price, rsi, ma20, ma50, stance, narrative, output) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, _utcnow(), data.get("current_price"), data.get("rsi_14"),
             data.get("ma_20"), data.get("ma_50"), stance, narrative, output),
        )
        await db.commit()


async def _log_call(tool: str, ticker: str, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, ticker, duration_ms),
        )
        await db.commit()


# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="stock-research",
    instructions=(
        "Price action analysis: RSI-14, MA-20/50, trend detection, "
        "and Claude Haiku-written analyst narrative."
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def analyze_stock(ticker: str) -> str:
    """
    Full stock analysis with LLM narrative.
    Fetches live price, RSI-14, MA-20/50, determines trend stance
    (Bullish/Bearish/Neutral), and asks Claude to write a 2-3 sentence analyst
    commentary. Result is saved to agent memory.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    data = await get_stock_data(ticker)
    if "error" in data:
        return f"Error fetching data for {ticker}: {data['error']}"

    price = data["current_price"]
    rsi   = data["rsi_14"]
    ma20  = data.get("ma_20")
    ma50  = data.get("ma_50")
    chg   = data["price_change_pct"]
    name  = data.get("company_name", ticker)

    vs_ma20 = "above" if ma20 and price > ma20 else "below"
    vs_ma50 = "above" if ma50 and price > float(ma50) else "below"
    rsi_lbl = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
    stance  = (
        "Bullish" if vs_ma20 == vs_ma50 == "above" and rsi < 70
        else "Bearish" if vs_ma20 == vs_ma50 == "below"
        else "Neutral"
    )

    prompt = (
        f"{name} ({ticker}) trades at ${price} ({'+' if chg >= 0 else ''}{chg}%). "
        f"It is {vs_ma20} its 20-day MA (${ma20}) and {vs_ma50} its 50-day MA (${ma50}). "
        f"RSI-14 is {rsi} ({rsi_lbl}). 52-week range: ${data['week52_low']}–${data['week52_high']}. "
        f"Write a 2-3 sentence analyst commentary."
    )

    try:
        narrative = await _llm.complete(SYSTEM, prompt, max_tokens=200)
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        trend = "uptrend" if stance == "Bullish" else "downtrend" if stance == "Bearish" else "consolidation"
        narrative = (
            f"{ticker} is in a {trend}. RSI at {rsi} is {rsi_lbl}. Stance: {stance}."
        )

    output = (
        f"{name} ({ticker})\n"
        f"Price:  ${price}  ({'+' if chg >= 0 else ''}{chg}%)\n"
        f"52W:    ${data['week52_low']} – ${data['week52_high']}\n"
        f"RSI-14: {rsi}  |  MA20: ${ma20}  |  MA50: ${ma50}\n"
        f"Stance: {stance}\n\n"
        f"{narrative}"
    )

    await _save_analysis(ticker, data, stance, narrative, output)
    await _log_call("analyze_stock", ticker, int((time.monotonic() - t0) * 1000))
    return output


@mcp.tool()
async def get_price_snapshot(ticker: str) -> str:
    """
    Raw price data as JSON — no LLM, no formatting.
    Returns current price, RSI-14, MA-20/50, 52-week range, volume.
    Useful when you need the numbers without prose.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    data = await get_stock_data(ticker)
    await _log_call("get_price_snapshot", ticker, int((time.monotonic() - t0) * 1000))
    if "error" in data:
        return json.dumps({"error": data["error"]})
    return json.dumps(data, indent=2)


@mcp.tool()
async def recall_analyses(ticker: str, limit: int = 5) -> str:
    """
    Retrieve past analyses for a ticker from agent memory.
    Returns the most recent `limit` entries with price, stance, and narrative.
    """
    await _ensure_db()
    ticker = ticker.strip().upper()
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, price, stance, narrative FROM analyses "
            "WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (ticker, max(1, min(limit, 20))),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return f"No previous analyses found for {ticker}."
    lines = [f"Past analyses for {ticker} (newest first):"]
    for ts, price, stance, narrative in rows:
        lines.append(f"\n[{ts[:10]}] ${price}  {stance}\n{narrative}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
