"""
Data Pull MCP Server

Centralized market data agent. Single ingress point for all raw data:
  - Stock price/RSI/MA snapshots
  - Company fundamentals (P/E, EPS, margins, revenue)
  - Options chains (calls + puts, IVs, Greeks)

Priority for each data type:
  Stock data    → yfinance (+ Polygon real-time overlay if key set)
  Fundamentals  → IB Gateway (Reuters Refinitiv) → Yahoo Finance
  Options chain → IB Gateway (real-time) → Yahoo Finance (delayed)

Features:
  - In-memory TTL cache (stock: 60s, fundamentals/options: 5min)
  - Every fetch logged to data_pull.db with source + latency
  - Cache inspection and manual invalidation via tools
  - Source availability status check

Tools:
  fetch_stock(ticker)             → price snapshot
  fetch_fundamentals(ticker)      → financial metrics
  fetch_options_chain(ticker)     → raw chain JSON
  check_data_sources()            → IBKR / yfinance reachability
  get_fetch_history(ticker,limit) → recent fetch log from DB
  clear_ticker_cache(ticker)      → evict ticker from in-memory cache

Memory: db/agents/data_pull.db
LLM:    none — pure data, no LLM dependency
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp_servers.data_pull.fetch import (  # noqa: E402
    AGENT_DB,
    _cache,
    _ensure_db,
    clear_cache,
    get_fundamentals,
    get_options_chain,
    get_source_status,
    get_stock_data,
    log_call,
)

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="data-pull",
    instructions=(
        "Centralized market data agent. Fetches stock snapshots, fundamentals, "
        "and options chains with IBKR-first priority and yfinance fallback. "
        "TTL-cached to prevent duplicate API calls across agents. No LLM."
    ),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
async def fetch_stock(ticker: str) -> str:
    """
    Fetch stock price snapshot for a ticker.
    Returns current price, prev close, % change, 52w high/low, RSI-14,
    MA-20, MA-50, volume, avg volume, company name, and sector.
    Source: yfinance (+ Polygon overlay if POLYGON_API_KEY set).
    Results cached 60 seconds in memory.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    data = await get_stock_data(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    await log_call("fetch_stock", ticker, ms)

    if "error" in data:
        return f"Error fetching stock data for {ticker}: {data['error']}"

    p = data.get("current_price", "N/A")
    prev = data.get("prev_close", "N/A")
    pct = data.get("price_change_pct", "N/A")
    sign = "+" if isinstance(pct, float) and pct >= 0 else ""
    w52h = data.get("week52_high", "N/A")
    w52l = data.get("week52_low", "N/A")
    rsi = data.get("rsi_14", "N/A")
    ma20 = data.get("ma_20", "N/A")
    ma50 = data.get("ma_50", "N/A")
    vol = data.get("volume", "N/A")
    avgvol = data.get("avg_volume_30d", "N/A")
    name = data.get("company_name", ticker)
    sector = data.get("sector", "N/A")
    src = data.get("source", "yfinance")

    return (
        f"{name} ({ticker})  —  {sector}\n"
        f"Price:       ${p}  ({sign}{pct}%  prev ${prev})\n"
        f"52w Range:   ${w52l} – ${w52h}\n"
        f"RSI-14:      {rsi}\n"
        f"MA-20:       ${ma20}   MA-50: ${ma50}\n"
        f"Volume:      {vol:,} (30d avg {avgvol:,})\n"
        f"Source: {src}  |  Latency: {ms}ms"
        if isinstance(vol, int) and isinstance(avgvol, int)
        else (
            f"{name} ({ticker})  —  {sector}\n"
            f"Price:       ${p}  ({sign}{pct}%  prev ${prev})\n"
            f"52w Range:   ${w52l} – ${w52h}\n"
            f"RSI-14:      {rsi}\n"
            f"MA-20:       ${ma20}   MA-50: ${ma50}\n"
            f"Volume:      {vol}  (30d avg {avgvol})\n"
            f"Source: {src}  |  Latency: {ms}ms"
        )
    )


@mcp.tool()
async def fetch_fundamentals(ticker: str) -> str:
    """
    Fetch company fundamental metrics for a ticker.
    Returns P/E, forward P/E, EPS, revenue growth YoY, profit/gross margins,
    debt/equity, market cap, dividend yield, and quarterly revenue trend.
    Priority: IB Gateway (Reuters Refinitiv) → Yahoo Finance.
    Results cached 5 minutes in memory.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    data = await get_fundamentals(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    await log_call("fetch_fundamentals", ticker, ms)

    if "error" in data:
        return f"Error fetching fundamentals for {ticker}: {data['error']}"

    name = data.get("company_name", ticker)
    sector = data.get("sector", "N/A")
    pe = data.get("pe_ratio", "N/A")
    fpe = data.get("forward_pe", "N/A")
    eps = data.get("eps_ttm", "N/A")
    feps = data.get("eps_forward", "N/A")
    rev_yoy = data.get("revenue_growth_yoy_pct", "N/A")
    pmarg = data.get("profit_margin_pct", "N/A")
    gmarg = data.get("gross_margin_pct", "N/A")
    de = data.get("debt_to_equity", "N/A")
    mcap = data.get("market_cap")
    dyield = data.get("dividend_yield_pct", "N/A")
    roe = data.get("roe_pct", "N/A")
    src = data.get("source", "Yahoo Finance")

    mcap_str = f"${mcap/1e9:.1f}B" if mcap else "N/A"

    lines = [
        f"{name} ({ticker})  —  {sector}",
        f"Market Cap:      {mcap_str}",
        f"P/E (TTM):       {pe}   Forward P/E: {fpe}",
        f"EPS (TTM):       ${eps}   Forward EPS: ${feps}",
        f"Revenue YoY:     {rev_yoy}%",
        f"Profit Margin:   {pmarg}%   Gross Margin: {gmarg}%",
        f"ROE:             {roe}%",
        f"Debt/Equity:     {de}",
        f"Dividend Yield:  {dyield}%",
    ]

    qtrs = data.get("quarterly_revenues", [])
    if qtrs:
        lines.append("\nQuarterly Revenue:")
        for q in qtrs[-4:]:
            qoq = ""
            if q.get("qoq_pct") is not None:
                sign = "+" if q["qoq_pct"] >= 0 else ""
                qoq = f"  ({sign}{q['qoq_pct']}% QoQ)"
            lines.append(f"  {q['period']}  ${q['revenue_b']}B{qoq}")

    lines.append(f"\nSource: {src}  |  Latency: {ms}ms")
    return "\n".join(lines)


@mcp.tool()
async def fetch_options_chain(ticker: str) -> str:
    """
    Fetch raw options chain data for a ticker as JSON.
    Returns current price, available expirations, and per-expiry chains
    with calls + puts (strike, bid, ask, IV, delta, gamma, theta, vega).
    Priority: IB Gateway (real-time) → Yahoo Finance (delayed).
    Strikes filtered to ±15% around current price.
    Results cached 5 minutes in memory.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    data = await get_options_chain(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    await log_call("fetch_options_chain", ticker, ms)

    if "error" in data:
        return f"Error fetching options chain for {ticker}: {data['error']}"

    src = data.get("source", "unknown")
    price = data.get("current_price", "N/A")
    exps = data.get("available_expirations", [])
    chains = data.get("chains", [])

    summary = (
        f"{ticker}  Price: ${price}  |  Source: {src}  |  Latency: {ms}ms\n"
        f"Expirations ({len(exps)}): {', '.join(exps[:8])}"
        f"{'...' if len(exps) > 8 else ''}\n"
        f"Chains loaded: {len(chains)} expirations\n\n"
    )

    payload = {k: v for k, v in data.items() if k != "hv_series"}
    return summary + json.dumps(payload, indent=2)


@mcp.tool()
async def check_data_sources() -> str:
    """
    Check reachability of all data sources:
      - IB Gateway port 4002 (paper) and 4001 (live)
      - Yahoo Finance (yfinance)
    Also shows current in-memory cache size and fetch DB row count.
    """
    await _ensure_db()
    t0 = time.monotonic()

    status = await get_source_status()
    ms = int((time.monotonic() - t0) * 1000)
    await log_call("check_data_sources", "_status", ms)

    # Cache stats
    live_keys = [k for k, (_, exp) in _cache.items() if __import__("time").monotonic() < exp]
    cache_info = f"{len(live_keys)} entries ({len(_cache)} total slots)"

    # DB fetch log stats
    db_info = ""
    try:
        async with aiosqlite.connect(AGENT_DB) as db:
            async with db.execute("SELECT COUNT(*) FROM fetch_log") as cur:
                total = (await cur.fetchone() or (0,))[0]
            async with db.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE cache_hit=0 AND error IS NULL"
            ) as cur:
                hits = (await cur.fetchone() or (0,))[0]
            async with db.execute(
                "SELECT COUNT(*) FROM fetch_log WHERE cache_hit=1"
            ) as cur:
                cached = (await cur.fetchone() or (0,))[0]
        db_info = f"\nFetch log: {total} total  |  {hits} live fetches  |  {cached} cache hits"
    except Exception:
        pass

    icons = {"reachable": "✅", "ok": "✅", "unreachable": "❌", "degraded": "⚠️"}
    lines = ["Data Source Status:\n"]
    for source, state in status.items():
        icon = next((v for k, v in icons.items() if state.startswith(k)), "❓")
        lines.append(f"  {icon} {source:<30} {state}")

    lines.append(f"\nCache: {cache_info}{db_info}")
    return "\n".join(lines)


@mcp.tool()
async def get_fetch_history(ticker: str = "", limit: int = 20) -> str:
    """
    Return recent fetch log entries from data_pull.db.
    Pass ticker to filter by symbol; leave empty for all tickers.
    Shows source, latency, cache hit, and any errors.
    """
    await _ensure_db()
    ticker = ticker.strip().upper()
    limit = max(1, min(limit, 100))

    async with aiosqlite.connect(AGENT_DB) as db:
        if ticker:
            async with db.execute(
                "SELECT timestamp, ticker, data_type, source, latency_ms, cache_hit, error "
                "FROM fetch_log WHERE ticker=? ORDER BY id DESC LIMIT ?",
                (ticker, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT timestamp, ticker, data_type, source, latency_ms, cache_hit, error "
                "FROM fetch_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()

    if not rows:
        label = f" for {ticker}" if ticker else ""
        return f"No fetch history{label} yet."

    label = f" for {ticker}" if ticker else ""
    lines = [f"Fetch history{label} (last {limit}, newest first):\n"]
    for ts, tkr, dtype, src, lat, cached, err in rows:
        hit_str = " [cache]" if cached else ""
        lat_str = f" {lat}ms" if lat else ""
        err_str = f"  ❌ {err}" if err else ""
        lines.append(
            f"  [{ts[:16].replace('T',' ')}] {tkr:<6} {dtype:<14} "
            f"{(src or '?'):<35}{hit_str}{lat_str}{err_str}"
        )
    return "\n".join(lines)


@mcp.tool()
async def clear_ticker_cache(ticker: str = "") -> str:
    """
    Evict a ticker from the in-memory TTL cache.
    Pass a ticker symbol to clear that ticker only.
    Leave empty to clear the entire cache.
    Does not affect the fetch_log DB — only removes in-process cached results.
    """
    await _ensure_db()
    if ticker:
        ticker = ticker.strip().upper()
        count = clear_cache(ticker)
        await log_call("clear_ticker_cache", ticker, 0)
        return f"Cleared {count} cache entries for {ticker}."
    else:
        count = clear_cache(None)
        await log_call("clear_ticker_cache", "_all", 0)
        return f"Cleared all {count} cache entries."


if __name__ == "__main__":
    mcp.run()
