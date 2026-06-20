"""
mcp_servers/data_pull/fetch.py

Centralized data fetching layer for all agents.

Priority chain:
  Stock data:    yfinance (+ Polygon overlay if key set)
  Fundamentals:  TWS (Reuters Refinitiv) → Yahoo Finance
  Options chain: TWS (real-time) → Yahoo Finance (delayed)

Features over tools.market_data:
  - In-memory TTL cache (stock: 60s, fundamentals/options: 5min) avoids
    duplicate API calls when multiple agents request the same ticker
  - Every fetch logged to data_pull.db for heartbeat monitoring and history
  - Unified source tracking — data["source"] always set

All public functions match the tools.market_data signature exactly so callers
can swap the import with no other changes.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import aiosqlite

from tools.market_data import get_stock_data as _raw_stock
from tools.market_data import get_fundamentals as _raw_fundamentals
from tools.market_data import get_options_chain as _raw_options

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "data_pull.db")

# In-memory TTL cache: {key: (data, expires_monotonic)}
_cache: dict[str, tuple[dict, float]] = {}
_TTL = {"stock": 60, "fundamentals": 300, "options": 300}

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
            CREATE TABLE IF NOT EXISTS fetch_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                data_type   TEXT NOT NULL,
                source      TEXT,
                latency_ms  INTEGER,
                cache_hit   INTEGER DEFAULT 0,
                error       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fl_ticker ON fetch_log(ticker);
            CREATE INDEX IF NOT EXISTS idx_fl_ts     ON fetch_log(timestamp);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                duration_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_cl_ts ON call_log(timestamp);
        """)
        await db.commit()
    _db_ready = True


async def _log_fetch(
    ticker: str, data_type: str, source: str | None,
    latency_ms: int, cache_hit: bool, error: str | None = None,
) -> None:
    try:
        await _ensure_db()
        async with aiosqlite.connect(AGENT_DB) as db:
            await db.execute(
                "INSERT INTO fetch_log "
                "(timestamp, ticker, data_type, source, latency_ms, cache_hit, error) "
                "VALUES (?,?,?,?,?,?,?)",
                (_utcnow(), ticker, data_type, source, latency_ms, int(cache_hit), error),
            )
            await db.commit()
    except Exception as exc:
        logger.debug("fetch_log write failed: %s", exc)


async def log_call(tool: str, ticker: str, duration_ms: int) -> None:
    """Log an MCP tool call to call_log (used by heartbeat for recency tracking)."""
    try:
        await _ensure_db()
        async with aiosqlite.connect(AGENT_DB) as db:
            await db.execute(
                "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
                (_utcnow(), tool, ticker, duration_ms),
            )
            await db.commit()
    except Exception as exc:
        logger.debug("call_log write failed: %s", exc)


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    _cache.pop(key, None)
    return None


def _cache_set(key: str, data: dict, ttl: int) -> None:
    _cache[key] = (data, time.monotonic() + ttl)


def clear_cache(ticker: str | None = None) -> int:
    """Clear in-memory cache. Pass ticker to clear one; None to clear all. Returns count cleared."""
    global _cache
    if ticker is None:
        count = len(_cache)
        _cache = {}
        return count
    prefix = ticker.strip().upper() + ":"
    keys = [k for k in _cache if k.startswith(f"stock:{prefix.rstrip(':')}") or
            k.startswith(f"fundamentals:{prefix.rstrip(':')}") or
            k.startswith(f"options:{prefix.rstrip(':')}")]
    # Simpler: match any key that contains the ticker
    keys = [k for k in _cache if k.split(":", 1)[-1] == ticker.strip().upper()]
    for k in keys:
        _cache.pop(k, None)
    return len(keys)


# ── Public data fetch functions ───────────────────────────────────────────────

async def get_stock_data(ticker: str) -> dict:
    """
    Stock price, RSI-14, MA-20/50, 52-week range, volume.
    Polygon real-time overlay if API key set; yfinance otherwise.
    Cached 60s in memory.
    """
    ticker = ticker.strip().upper()
    key = f"stock:{ticker}"

    cached = _cache_get(key)
    if cached:
        await _log_fetch(ticker, "stock", cached.get("source", "cache"), 0, True)
        return cached

    t0 = time.monotonic()
    data = await _raw_stock(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    source = data.get("source", "Yahoo Finance")
    error = data.get("error")

    if not error:
        if "source" not in data:
            data["source"] = "Yahoo Finance"
        _cache_set(key, data, _TTL["stock"])

    await _log_fetch(ticker, "stock", source, ms, False, error)
    return data


async def get_fundamentals(ticker: str) -> dict:
    """
    P/E, forward P/E, EPS, revenue growth YoY, margins, debt/equity.
    TWS (Reuters Refinitiv) first; Yahoo Finance fallback.
    Cached 5min in memory.
    """
    ticker = ticker.strip().upper()
    key = f"fundamentals:{ticker}"

    cached = _cache_get(key)
    if cached:
        await _log_fetch(ticker, "fundamentals", cached.get("source", "cache"), 0, True)
        return cached

    t0 = time.monotonic()
    data = await _raw_fundamentals(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    source = data.get("source", "Yahoo Finance")
    error = data.get("error")

    if not error:
        _cache_set(key, data, _TTL["fundamentals"])

    await _log_fetch(ticker, "fundamentals", source, ms, False, error)
    return data


async def get_options_chain(ticker: str) -> dict:
    """
    Full options chain: calls + puts per expiry, IVs, Greeks.
    TWS (real-time) first; Yahoo Finance (delayed) fallback.
    No yfinance chain fallback — returns error dict if both unavailable.
    Cached 5min in memory.
    """
    ticker = ticker.strip().upper()
    key = f"options:{ticker}"

    cached = _cache_get(key)
    if cached:
        await _log_fetch(ticker, "options", cached.get("source", "cache"), 0, True)
        return cached

    t0 = time.monotonic()
    data = await _raw_options(ticker)
    ms = int((time.monotonic() - t0) * 1000)
    source = data.get("source", "unknown")
    error = data.get("error")

    if not error:
        _cache_set(key, data, _TTL["options"])

    await _log_fetch(ticker, "options", source, ms, False, error)
    return data


async def get_source_status() -> dict:
    """Check availability of each data source. Returns dict with source→status."""
    import socket

    status: dict[str, str] = {}

    # IBKR TWS (socket)
    import config as _cfg
    tws_label = f"TWS ({_cfg.IBKR_TWS_HOST}:{_cfg.IBKR_TWS_PORT})"
    try:
        s = socket.create_connection((_cfg.IBKR_TWS_HOST, _cfg.IBKR_TWS_PORT), timeout=2)
        s.close()
        status[tws_label] = "reachable"
    except OSError:
        status[tws_label] = "unreachable"

    # yfinance — quick ticker check
    try:
        import yfinance as yf
        t = yf.Ticker("SPY")
        info = t.fast_info
        status["Yahoo Finance"] = "ok" if info else "degraded"
    except Exception as exc:
        status["Yahoo Finance"] = f"error: {exc}"

    return status
