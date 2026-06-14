"""
IBKR Positions MCP Server

Live portfolio positions, P&L, and account allocation from the CP Gateway.

Tools:
  get_positions(account_id)    → all open positions with P&L per leg
  get_pnl()                    → day P&L and unrealized P&L across accounts
  get_portfolio_summary()      → combined positions + P&L in one view
  get_allocation(account_id)   → breakdown by asset class / sector

Memory: db/agents/ibkr_positions.db
LLM:    configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agents.ibkr_agent import (  # noqa: E402
    auth_status, get_accounts, get_pnl, get_positions,
)
import httpx  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr_positions.db")
GATEWAY  = "https://localhost:5000/v1/api"

_llm = get_llm_client()
_db_ready = False

SYSTEM = (
    "You are a portfolio analyst. Given a positions snapshot, write 2-3 sentences "
    "summarising the overall exposure, the largest position, and any notable risk. "
    "Be specific about tickers and dollar amounts."
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                account_id  TEXT NOT NULL,
                position_count INTEGER,
                total_value REAL,
                snapshot_json TEXT
            );
            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _log_call(tool: str, ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, duration_ms) VALUES (?,?,?)",
            (_utcnow(), tool, ms),
        )
        await db.commit()


def _require_auth(s: dict) -> str | None:
    if s.get("error"):
        return f"Gateway unreachable: {s['error']}"
    if not s.get("authenticated"):
        return "Not authenticated. Open https://localhost:5000 to log in."
    return None


mcp = FastMCP(
    name="ibkr-positions",
    instructions="Live IBKR portfolio positions, P&L, and account allocation from CP Gateway.",
)


@mcp.tool()
async def get_open_positions(account_id: str = "") -> str:
    """
    Fetch all open positions for an account.
    Shows symbol, position size, market price, market value, average cost, and unrealized P&L.
    Defaults to the first account if account_id is empty.
    """
    await _ensure_db()
    t0 = time.monotonic()

    s = await auth_status()
    if err := _require_auth(s):
        return err

    if not account_id:
        accounts = await get_accounts()
        account_id = accounts[0] if accounts else ""
    if not account_id:
        return "No accounts found."

    positions = await get_positions(account_id)
    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_open_positions", ms)

    if not positions:
        return f"No open positions in account {account_id}."

    col = 30
    lines = [
        f"Positions — {account_id}  ({len(positions)} open, {ms}ms)\n",
        f"{'Symbol':<{col}} {'Qty':>6}  {'Price':>9}  {'Value':>12}  {'Unreal P&L':>12}",
        "─" * 78,
    ]
    total_value = 0.0
    for p in positions:
        desc  = (p.get("contractDesc") or p.get("ticker") or str(p.get("conid", "?")))[:col]
        qty   = p.get("position", 0)
        price = p.get("mktPrice", 0) or 0
        val   = p.get("mktValue", 0) or 0
        upnl  = p.get("unrealizedPnl", 0) or 0
        total_value += val
        sign  = "+" if upnl >= 0 else ""
        lines.append(
            f"{desc:<{col}} {qty:>6.0f}  ${price:>8.2f}  ${val:>11,.2f}  {sign}${upnl:>10,.2f}"
        )

    lines.append("─" * 78)
    lines.append(f"{'Total market value':<{col+8}} ${total_value:>11,.2f}")

    # LLM summary
    try:
        prompt = (
            f"Account {account_id} has {len(positions)} open positions. "
            f"Total market value: ${total_value:,.0f}. "
            f"Top positions: " +
            ", ".join(
                f"{(p.get('contractDesc') or p.get('ticker','?'))[:20]} "
                f"(${p.get('mktValue',0):,.0f}, P&L {p.get('unrealizedPnl',0):+,.0f})"
                for p in sorted(positions, key=lambda x: abs(x.get("mktValue",0) or 0), reverse=True)[:3]
            ) + "."
        )
        narrative = await asyncio.wait_for(_llm.complete(SYSTEM, prompt, max_tokens=150), timeout=15.0)
        lines.append(f"\nAnalysis:\n{narrative}")
    except Exception:
        pass

    # Save snapshot
    import json
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO snapshots (timestamp, account_id, position_count, total_value, snapshot_json) VALUES (?,?,?,?,?)",
            (_utcnow(), account_id, len(positions), total_value, json.dumps(positions[:10])),
        )
        await db.commit()

    return "\n".join(lines)


@mcp.tool()
async def get_live_pnl() -> str:
    """
    Fetch live day P&L and unrealized P&L across all accounts from the CP Gateway.
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    pnl_data = await get_pnl()
    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_live_pnl", ms)

    upnl = pnl_data.get("upnl", {})
    if not upnl:
        return f"No P&L data available ({ms}ms). Are there open positions?"

    lines = [f"Live P&L  ({ms}ms)\n", f"{'Account':<16} {'Day P&L':>12}  {'Unrealized':>12}  {'Net Liq':>14}"]
    lines.append("─" * 60)
    for acct, val in upnl.items():
        if isinstance(val, dict):
            dpl  = val.get("dpl", 0) or 0
            upl  = val.get("upl", 0) or 0
            nl   = val.get("nl",  0) or 0
            d_s  = f"{'+'if dpl>=0 else ''}${dpl:,.2f}"
            u_s  = f"{'+'if upl>=0 else ''}${upl:,.2f}"
            lines.append(f"{acct:<16} {d_s:>12}  {u_s:>12}  ${nl:>13,.2f}")
    return "\n".join(lines)


@mcp.tool()
async def get_portfolio_summary() -> str:
    """
    Combined view: P&L header + all open positions in one response.
    Convenience wrapper around get_live_pnl + get_open_positions.
    """
    await _ensure_db()
    pnl_section = await get_live_pnl()
    pos_section = await get_open_positions()
    return pnl_section + "\n\n" + pos_section


@mcp.tool()
async def get_allocation(account_id: str = "") -> str:
    """
    Fetch portfolio allocation breakdown by asset class from the CP Gateway.
    Defaults to first account if account_id is empty.
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    if not account_id:
        accounts = await get_accounts()
        account_id = accounts[0] if accounts else ""
    if not account_id:
        return "No accounts found."

    try:
        async with httpx.AsyncClient(base_url=GATEWAY, verify=False, timeout=15.0) as c:
            await c.get("/portfolio/accounts")
            r = await c.get(f"/portfolio/{account_id}/allocation")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return f"Allocation fetch failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_allocation", ms)

    lines = [f"Allocation — {account_id}  ({ms}ms)\n"]
    for section_name, section_data in data.items():
        if isinstance(section_data, dict):
            lines.append(f"{section_name}:")
            total = sum(abs(v) for v in section_data.values() if isinstance(v, (int, float)))
            for key, val in sorted(section_data.items(), key=lambda x: -abs(x[1] if isinstance(x[1], (int,float)) else 0)):
                if isinstance(val, (int, float)) and total > 0:
                    pct = abs(val) / total * 100
                    lines.append(f"  {key:<25} ${val:>12,.0f}  ({pct:.1f}%)")
            lines.append("")
    return "\n".join(lines) or "No allocation data returned."


if __name__ == "__main__":
    mcp.run()
