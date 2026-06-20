"""
IBKR Market Data MCP Server  (ib_insync / TWS socket)

Contract lookup, live quotes, option chain discovery via TWS.
Conid results cached in db/state.db.

Tools:
  get_stock_conid(symbol)                           → qualify stock/ETF
  get_option_contract_conid(sym, expiry, right, K) → qualify + cache option
  get_market_snapshot(symbols_csv)                  → live bid/ask/last/Greeks
  get_option_chain(symbol, expiry)                  → available strikes
  search_contract(query, sec_type)                  → fuzzy contract search
  clear_conid_cache(symbol)                         → purge cached conids

Shared: db/state.db (ibkr_conid_cache)
Memory: db/agents/ibkr_market_data.db
LLM:    configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from ib_insync import Contract, Index, Option, Stock
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from tools.ibkr_tws import (  # noqa: E402
    connect_ib, make_option_contract, paper_label,
)
_STATE_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "db", "state.db",
)


async def _cache_get(symbol: str, expiry: str, right: str, strike: float):
    async with aiosqlite.connect(_STATE_DB) as db:
        async with db.execute(
            "SELECT conid FROM ibkr_conid_cache WHERE symbol=? AND expiry=? AND right=? AND strike=?",
            (symbol, expiry, right, strike),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _cache_set(symbol: str, expiry: str, right: str, strike: float, conid: int) -> None:
    async with aiosqlite.connect(_STATE_DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ibkr_conid_cache (symbol, expiry, right, strike, conid) VALUES (?,?,?,?,?)",
            (symbol, expiry, right, strike, conid),
        )
        await db.commit()

logger = logging.getLogger(__name__)

_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB  = os.path.join(_ROOT, "db", "agents", "ibkr_market_data.db")
CLIENT_ID = config.IBKR_CLIENT_ID_MARKET_DATA

_llm      = get_llm_client()
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
            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                duration_ms INTEGER,
                detail      TEXT
            );
        """)
        await db.commit()
    _db_ready = True


async def _log_call(tool: str, ms: int, detail: str = "") -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, duration_ms, detail) VALUES (?,?,?,?)",
            (_utcnow(), tool, ms, detail),
        )
        await db.commit()


mcp = FastMCP(
    name="ibkr-market-data",
    instructions=(
        "IBKR contract lookup and live market data via ib_insync TWS socket. "
        "Conid results cached. Live quotes include bid/ask/last and Greeks for options."
    ),
)


@mcp.tool()
async def get_stock_conid(symbol: str) -> str:
    """
    Qualify a stock/ETF symbol and return its conid from TWS.
    The conid is required for order placement and market data subscriptions.
    """
    await _ensure_db()
    t0     = time.monotonic()
    symbol = symbol.strip().upper()
    try:
        ib       = await connect_ib(CLIENT_ID)
        contract = Stock(symbol, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_stock_conid", ms, symbol)
        if not qualified:
            return f"Could not qualify {symbol}."
        c = qualified[0]
        return f"{symbol} ({c.primaryExchange}): conid={c.conId}  ({ms}ms)"
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_option_contract_conid(
    symbol: str,
    expiry: str,
    right: str,
    strike: float,
    exchange: str = "SMART",
) -> str:
    """
    Look up and cache the conid for a specific option contract.
    expiry: YYYY-MM-DD  right: P or C  strike: numeric
    Cached results return instantly.
    """
    await _ensure_db()
    t0     = time.monotonic()
    symbol = symbol.strip().upper()
    right  = right.strip().upper()
    expiry_ib = expiry.replace("-", "")

    cached = await _cache_get(symbol, expiry, right, strike)
    if cached:
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_option_contract_conid", ms, f"{symbol} {right}{strike} {expiry} [cached]")
        return f"{symbol} {right}{strike:.0f} {expiry} → conid={cached}  (cached, {ms}ms)"

    try:
        ib       = await connect_ib(CLIENT_ID)
        contract = make_option_contract(symbol, expiry_ib, right, strike, exchange)
        qualified = await ib.qualifyContractsAsync(contract)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_option_contract_conid", ms, f"{symbol} {right}{strike} {expiry}")

        if not qualified:
            return f"Could not qualify {symbol} {right}{strike:.0f} {expiry}."
        conid = qualified[0].conId
        await _cache_set(symbol, expiry, right, strike, conid)
        return f"{symbol} {right}{strike:.0f} {expiry} → conid={conid}  ({ms}ms)"
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_market_snapshot(symbols_csv: str) -> str:
    """
    Fetch live market data for one or more stock symbols.
    symbols_csv: comma-separated list e.g. "AAPL,MSFT,SPY"
    Returns bid, ask, last, volume, and change for each.

    Note: TWS may take a moment to subscribe — call twice if values are missing.
    """
    await _ensure_db()
    t0      = time.monotonic()
    symbols = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]

    try:
        ib        = await connect_ib(CLIENT_ID)
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        qualified = await ib.qualifyContractsAsync(*contracts)

        tickers = [ib.reqMktData(c, "", False, False) for c in qualified]

        # Poll until all symbols have a price or plateau (no new data for 3s)
        deadline     = time.monotonic() + 300.0
        last_filled  = -1
        plateau_time = time.monotonic()
        while time.monotonic() < deadline:
            filled = sum(1 for t in tickers if (t.last and t.last > 0) or (t.bid and t.bid > 0))
            if filled != last_filled:
                last_filled  = filled
                plateau_time = time.monotonic()
            elif time.monotonic() - plateau_time >= 3.0:
                break
            if filled == len(tickers):
                break
            await asyncio.sleep(0.5)

        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_market_snapshot", ms, symbols_csv)

        col = 6
        lines = [f"Market Snapshot  [{paper_label()}]  ({ms}ms)\n",
                 f"{'Symbol':<{col}} {'Last':>8}  {'Bid':>8}  {'Ask':>8}  {'Volume':>10}  {'Chg%':>7}"]
        lines.append("─" * 58)
        for c, t in zip(qualified, tickers):
            last   = t.last   if t.last   and t.last   > 0 else t.close or 0
            bid    = t.bid    if t.bid    and t.bid    > 0 else 0
            ask    = t.ask    if t.ask    and t.ask    > 0 else 0
            vol    = t.volume if t.volume and t.volume > 0 else 0
            close  = t.close  if t.close  and t.close  > 0 else last
            chg_pct = ((last - close) / close * 100) if close and last else 0
            sign = "+" if chg_pct >= 0 else ""
            lines.append(
                f"{c.symbol:<{col}} ${last:>7.2f}  ${bid:>7.2f}  ${ask:>7.2f}  "
                f"{int(vol):>10,}  {sign}{chg_pct:>6.2f}%"
            )

        # Cancel subscriptions
        for c in qualified:
            ib.cancelMktData(c)

        return "\n".join(lines)

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_option_chain(symbol: str, expiry: str) -> str:
    """
    Fetch available option strikes for a symbol and expiry date.
    symbol: underlying (e.g. AAPL)
    expiry: YYYY-MM-DD
    """
    await _ensure_db()
    t0        = time.monotonic()
    symbol    = symbol.strip().upper()
    expiry_ib = expiry.replace("-", "")

    try:
        ib       = await connect_ib(CLIENT_ID)
        und      = Stock(symbol, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(und)
        if not qualified:
            return f"Could not qualify {symbol}."

        chains = await ib.reqSecDefOptParamsAsync(
            symbol, "", qualified[0].secType, qualified[0].conId
        )
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_option_chain", ms, f"{symbol} {expiry}")

        # Find the chain matching the requested expiry
        target = next((c for c in chains if expiry_ib in c.expirations), None)
        if not target:
            available = sorted({e for c in chains for e in c.expirations})[:8]
            return (
                f"Expiry {expiry} not found for {symbol}.\n"
                f"Available: {', '.join(available[:8])}"
            )

        strikes = sorted(target.strikes)
        lines = [f"Option Chain — {symbol} {expiry}  ({len(strikes)} strikes, {ms}ms)\n",
                 f"{'Strike':>8}  Exchange: {target.exchange}"]
        lines.append("─" * 25)
        for s in strikes:
            lines.append(f"  ${s:>8.2f}")
        return "\n".join(lines)

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def search_contract(query: str, sec_type: str = "STK") -> str:
    """
    Search for any contract by symbol or company name.
    sec_type: STK, OPT, FUT, CASH, IND, etc.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib      = await connect_ib(CLIENT_ID)
        results = await ib.reqMatchingSymbolsAsync(query)
        ms      = int((time.monotonic() - t0) * 1000)
        await _log_call("search_contract", ms, query)

        filtered = [r for r in results if r.contract.secType == sec_type.upper()][:15]
        if not filtered:
            return f"No {sec_type} results for '{query}'."

        lines = [f"Contract Search: '{query}' ({sec_type})  {len(filtered)} results  ({ms}ms)\n",
                 f"{'Conid':<12} {'Symbol':<10} {'Company':<35} {'Exchange':<10}"]
        lines.append("─" * 72)
        for r in filtered:
            c = r.contract
            lines.append(
                f"{c.conId:<12} {c.symbol:<10} "
                f"{str(r.contractDescription or '')[:34]:<35} {c.primaryExch or c.exchange:<10}"
            )
        return "\n".join(lines)
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def clear_conid_cache(symbol: str) -> str:
    """Remove all cached option conids for a symbol."""
    await _ensure_db()
    symbol = symbol.strip().upper()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM ibkr_conid_cache WHERE symbol = ?", (symbol,)
        )
        deleted = cur.rowcount
        await db.commit()
    await _log_call("clear_conid_cache", 0, symbol)
    return f"Cleared {deleted} cached conids for {symbol}."


if __name__ == "__main__":
    mcp.run()
