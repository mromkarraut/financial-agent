"""
IBKR Market Data MCP Server

Contract lookup, live market snapshots, and option chain discovery via CP Gateway.
Conid results are cached in db/state.db to avoid repeated API round-trips.

Tools:
  get_stock_conid(symbol)              → underlying stock/ETF conid
  get_option_conid(symbol, expiry, right, strike) → option contract conid (cached)
  get_market_snapshot(conids)          → live bid/ask/last/volume for 1+ conids
  get_option_strikes(symbol, month)    → available strikes for a symbol+month
  search_contract(query)               → fuzzy search for any contract
  clear_conid_cache(symbol)            → remove cached conids for a symbol

Shared data: db/state.db (ibkr_conid_cache table)
Memory:      db/agents/ibkr_market_data.db
LLM:         configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
import httpx
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from agents.ibkr_agent import (  # noqa: E402
    auth_status, get_option_conid, get_underlying_conid,
    _cache_get, _cache_set, _client, _month_code,
)

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr_market_data.db")
GATEWAY  = "https://localhost:5000/v1/api"

_llm = get_llm_client()
_db_ready = False

# IBKR field IDs for market data snapshot
_FIELDS = {
    "31":   "last",
    "84":   "bid",
    "86":   "ask",
    "85":   "bid_sz",
    "88":   "ask_sz",
    "7762": "volume",
    "7295": "open",
    "7296": "high",
    "7293": "low",
    "7741": "close",
    "7644": "iv",
    "7718": "delta",
    "7720": "gamma",
    "7719": "vega",
    "7721": "theta",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshot_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                conids      TEXT NOT NULL,
                response    TEXT
            );
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


def _require_auth(s: dict) -> str | None:
    if s.get("error"):
        return f"Gateway unreachable: {s['error']}"
    if not s.get("authenticated"):
        return "Not authenticated. Open https://localhost:5000 to log in."
    return None


mcp = FastMCP(
    name="ibkr-market-data",
    instructions=(
        "IBKR contract lookup and live market data via CP Gateway. "
        "Conid results are cached in SQLite. Provides live snapshots with "
        "bid/ask/last and Greeks for options."
    ),
)


@mcp.tool()
async def get_stock_conid(symbol: str) -> str:
    """
    Look up the conid (contract ID) for a stock or ETF symbol.
    The conid is required for all other market data and order calls.
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    symbol = symbol.strip().upper()
    try:
        conid = await get_underlying_conid(symbol)
        ms    = int((time.monotonic() - t0) * 1000)
        await _log_call("get_stock_conid", ms, symbol)
        return f"{symbol} conid: {conid}  ({ms}ms)"
    except Exception as exc:
        return f"Lookup failed for {symbol}: {exc}"


@mcp.tool()
async def get_option_contract_conid(
    symbol: str,
    expiry: str,
    right: str,
    strike: float,
    exchange: str = "SMART",
) -> str:
    """
    Look up the conid for a specific option contract.
    Results are cached — subsequent calls for the same contract return instantly.

    symbol:   Underlying symbol (e.g. AAPL)
    expiry:   Expiration date YYYY-MM-DD
    right:    P (put) or C (call)
    strike:   Strike price as a number
    exchange: Routing exchange (default SMART)
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    symbol = symbol.strip().upper()
    right  = right.strip().upper()

    cached = await _cache_get(symbol, expiry, right, strike)
    if cached:
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_option_contract_conid", ms, f"{symbol} {right}{strike} {expiry} [cached]")
        return f"{symbol} {right}{strike:.0f} {expiry} → conid: {cached}  (cached, {ms}ms)"

    try:
        conid = await get_option_conid(symbol, expiry, right, strike, exchange)
        ms    = int((time.monotonic() - t0) * 1000)
        await _log_call("get_option_contract_conid", ms, f"{symbol} {right}{strike} {expiry}")
        return f"{symbol} {right}{strike:.0f} {expiry} → conid: {conid}  ({ms}ms)"
    except Exception as exc:
        return f"Lookup failed: {exc}"


@mcp.tool()
async def get_market_snapshot(conids: str) -> str:
    """
    Fetch a live market data snapshot for one or more conids.
    conids: comma-separated list of conids (e.g. "265598,12345")
    Returns bid, ask, last, volume, and Greeks for options.

    Note: IBKR CP Gateway returns data asynchronously — call twice if
    fields are missing on the first call (gateway needs to subscribe first).
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    fields_param = ",".join(_FIELDS.keys())
    try:
        async with _client() as c:
            r = await c.get(
                "/iserver/marketdata/snapshot",
                params={"conids": conids.replace(" ", ""), "fields": fields_param},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return f"Snapshot failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_market_snapshot", ms, conids[:80])

    # Log raw response
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO snapshot_log (timestamp, conids, response) VALUES (?,?,?)",
            (_utcnow(), conids, json.dumps(data)[:2000]),
        )
        await db.commit()

    if not data:
        return f"No snapshot data returned for conids: {conids}. Try calling again — gateway may need a moment to subscribe."

    col = 12
    lines = [f"Market Snapshot  ({ms}ms)\n",
             f"{'Field':<{col}} " + "  ".join(f"{c[:10]:<10}" for c in conids.split(",")[:5])]
    lines.append("─" * (col + 14 * len(conids.split(",")[:5])))

    rows_by_field: dict[str, list] = {name: [] for name in _FIELDS.values()}
    for item in data:
        for fid, fname in _FIELDS.items():
            val = item.get(fid) or item.get(fname) or "—"
            rows_by_field[fname].append(str(val)[:10])

    for fname, values in rows_by_field.items():
        if any(v != "—" for v in values):
            lines.append(f"{fname:<{col}} " + "  ".join(f"{v:<10}" for v in values[:5]))

    return "\n".join(lines)


@mcp.tool()
async def get_option_strikes(symbol: str, month: str) -> str:
    """
    Fetch available option strikes for a symbol and expiry month.
    symbol: underlying (e.g. AAPL)
    month:  expiry month as MMMYY (e.g. JUN26) or YYYY-MM-DD (converted automatically)
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    symbol = symbol.strip().upper()
    if "-" in month:
        month = _month_code(month)

    try:
        conid = await get_underlying_conid(symbol)
        async with _client() as c:
            r = await c.get(
                "/iserver/secdef/strikes",
                params={"conid": conid, "sectype": "OPT", "month": month, "exchange": "SMART"},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return f"Strike fetch failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_option_strikes", ms, f"{symbol} {month}")

    calls = data.get("call", [])
    puts  = data.get("put",  [])
    all_s = sorted(set(calls) | set(puts))

    if not all_s:
        return f"No strikes found for {symbol} {month}."

    lines = [f"Option Strikes — {symbol} {month}  ({len(all_s)} strikes, {ms}ms)\n",
             f"{'Strike':>8}  {'Call':>6}  {'Put':>6}"]
    lines.append("─" * 26)
    for s in all_s:
        c_mark = "✓" if s in calls else "—"
        p_mark = "✓" if s in puts  else "—"
        lines.append(f"${s:>7.0f}  {c_mark:>6}  {p_mark:>6}")
    return "\n".join(lines)


@mcp.tool()
async def search_contract(query: str, sec_type: str = "STK") -> str:
    """
    Search for any contract by name or symbol.
    query:    company name or ticker (e.g. "Apple" or "AAPL")
    sec_type: STK, OPT, FUT, CASH, IND, etc. (default STK)
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    try:
        async with _client() as c:
            r = await c.post(
                "/iserver/secdef/search",
                json={"symbol": query, "name": True, "secType": sec_type},
            )
            r.raise_for_status()
            results = r.json()
    except Exception as exc:
        return f"Search failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("search_contract", ms, query)

    if not results:
        return f"No results for '{query}' ({sec_type})."

    lines = [f"Contract Search: '{query}' ({sec_type})  {len(results)} results  ({ms}ms)\n",
             f"{'Conid':<12} {'Symbol':<10} {'Company':<35} {'Exchange':<10} Type"]
    lines.append("─" * 80)
    for r in results[:15]:
        lines.append(
            f"{str(r.get('conid','?')):<12} "
            f"{r.get('symbol','?'):<10} "
            f"{str(r.get('companyName', r.get('description','?')))[:34]:<35} "
            f"{r.get('primaryExch', r.get('exchange','?')):<10} "
            f"{r.get('secType','?')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def clear_conid_cache(symbol: str) -> str:
    """
    Remove all cached option conids for a symbol from the local DB.
    Useful when option chains roll to new strikes/expirations.
    """
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
