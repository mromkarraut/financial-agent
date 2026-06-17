"""
IBKR Positions MCP Server  (ib_insync / TWS socket)

Live portfolio positions, P&L, and allocation from IB Gateway.

Tools:
  get_open_positions(account_id)   → all open positions with P&L
  get_live_pnl()                   → day + unrealized P&L
  get_portfolio_summary()          → combined positions + P&L
  get_allocation(account_id)       → asset class breakdown

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

import config  # noqa: E402
from tools.ibkr_tws import connect_ib, get_account_id, is_paper_account, paper_label  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB  = os.path.join(_ROOT, "db", "agents", "ibkr_positions.db")
CLIENT_ID = config.IBKR_CLIENT_ID_POSITIONS

_llm      = get_llm_client()
_db_ready = False

SYSTEM = (
    "You are a portfolio analyst. Given a positions snapshot, write 2-3 sentences "
    "summarising overall exposure, the largest position, and key risk. "
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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT NOT NULL,
                account_id TEXT NOT NULL,
                pos_count  INTEGER,
                total_value REAL
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


mcp = FastMCP(
    name="ibkr-positions",
    instructions="Live IBKR portfolio positions and P&L via ib_insync TWS socket.",
)


@mcp.tool()
async def get_open_positions(account_id: str = "") -> str:
    """
    Fetch all open positions with market value and unrealized P&L.
    Defaults to first account if account_id is empty.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib = await connect_ib(CLIENT_ID)
        if not account_id:
            account_id = await get_account_id(ib)

        positions = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=10)
        if account_id:
            positions = [p for p in positions if p.account == account_id]
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_open_positions", ms)

        if not positions:
            return f"No open positions in account {account_id}."

        col = 30
        lines = [
            f"Positions — {account_id}  [{paper_label()}]  ({len(positions)} open, {ms}ms)\n",
            f"{'Contract':<{col}} {'Qty':>8}  {'Avg Cost':>10}  {'Mkt Value':>12}  {'Unreal P&L':>12}",
            "─" * 82,
        ]
        total_val  = 0.0
        total_upnl = 0.0
        for p in positions:
            c    = p.contract
            desc = f"{c.symbol} {c.secType}" + (f" {c.lastTradeDateOrContractMonth} {c.right}{c.strike}" if c.secType == "OPT" else "")
            qty  = p.position
            avg  = p.avgCost
            val  = qty * avg  # approximate if no market data
            upnl = 0.0
            # Try to get market data for accurate value
            try:
                ticker = ib.ticker(c)
                if ticker and ticker.marketPrice() and ticker.marketPrice() > 0:
                    val  = qty * ticker.marketPrice()
                    upnl = val - qty * avg
            except Exception:
                pass
            total_val  += abs(val)
            total_upnl += upnl
            sign = "+" if upnl >= 0 else ""
            lines.append(
                f"{desc[:col]:<{col}} {qty:>8.2f}  ${avg:>9.2f}  ${val:>11,.2f}  {sign}${upnl:>10,.2f}"
            )

        lines += ["─" * 82,
                  f"{'Total':<{col+10}} ${total_val:>11,.2f}  {'+'if total_upnl>=0 else ''}${total_upnl:>10,.2f}"]

        # LLM narrative
        try:
            top = sorted(positions, key=lambda p: abs(p.position * p.avgCost), reverse=True)[:3]
            prompt = (
                f"Account {account_id}: {len(positions)} positions, total ~${total_val:,.0f}. "
                f"Top: " + ", ".join(f"{p.contract.symbol} qty={p.position:.0f}" for p in top) + "."
            )
            narrative = await asyncio.wait_for(_llm.complete(SYSTEM, prompt, max_tokens=120), timeout=15)
            lines.append(f"\nAnalysis:\n{narrative}")
        except Exception:
            pass

        # Save snapshot
        async with aiosqlite.connect(AGENT_DB) as db:
            await db.execute(
                "INSERT INTO snapshots (timestamp, account_id, pos_count, total_value) VALUES (?,?,?,?)",
                (_utcnow(), account_id, len(positions), total_val),
            )
            await db.commit()

        return "\n".join(lines)

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_live_pnl() -> str:
    """
    Fetch live day P&L and unrealized P&L from IB Gateway.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib      = await connect_ib(CLIENT_ID)
        account = await get_account_id(ib)
        if not account:
            return "No accounts found."

        # accountValues is auto-populated on connect and kept live by IB push
        tags = {"DayPnL", "UnrealizedPnL", "RealizedPnL", "NetLiquidation"}
        vals = {v.tag: v.value for v in ib.accountValues(account) if v.tag in tags and v.currency in ("USD", "")}
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_live_pnl", ms)

        def _f(k: str) -> str:
            try:
                n = float(vals.get(k, 0) or 0)
                return f"{'+'if n>=0 else ''}${n:,.2f}"
            except Exception:
                return "N/A"

        nl = vals.get("NetLiquidation", "N/A")
        try: nl = f"${float(nl):,.2f}"
        except Exception: pass

        lines = [f"Live P&L  [{paper_label()}]  ({ms}ms)\n",
                 f"{'Account':<16} {'Day P&L':>12}  {'Unrealized':>12}  {'Realized':>12}  {'Net Liq':>14}",
                 "─" * 72,
                 f"{account:<16} {_f('DayPnL'):>12}  {_f('UnrealizedPnL'):>12}  {_f('RealizedPnL'):>12}  {nl:>14}"]
        return "\n".join(lines)

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_portfolio_summary() -> str:
    """P&L + positions in one combined view."""
    await _ensure_db()
    pnl = await get_live_pnl()
    pos = await get_open_positions()
    return pnl + "\n\n" + pos


@mcp.tool()
async def get_allocation(account_id: str = "") -> str:
    """
    Portfolio allocation by asset class (STK, OPT, FUT, CASH, etc.)
    derived from open positions.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib = await connect_ib(CLIENT_ID)
        if not account_id:
            account_id = await get_account_id(ib)

        positions  = [p for p in ib.positions(account=account_id)]
        ms         = int((time.monotonic() - t0) * 1000)
        await _log_call("get_allocation", ms)

        if not positions:
            return f"No positions in {account_id}."

        by_type: dict[str, float] = {}
        for p in positions:
            sec  = p.contract.secType
            val  = abs(p.position * p.avgCost)
            by_type[sec] = by_type.get(sec, 0.0) + val

        total = sum(by_type.values()) or 1
        lines = [f"Allocation — {account_id}  ({ms}ms)\n",
                 f"{'Asset Type':<12} {'Value':>12}  {'%':>6}"]
        lines.append("─" * 34)
        for sec, val in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"{sec:<12} ${val:>11,.0f}  {val/total*100:>5.1f}%")
        return "\n".join(lines)

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


async def get_pnl_dict() -> dict:
    """Return P&L data as a structured dict for web rendering (not an MCP tool)."""
    t0 = time.monotonic()
    try:
        ib      = await connect_ib(CLIENT_ID)
        account = await get_account_id(ib)
        if not account:
            return {"error": "No accounts found"}
        tags = {"DayPnL", "UnrealizedPnL", "RealizedPnL", "NetLiquidation"}
        vals = {v.tag: v.value for v in ib.accountValues(account)
                if v.tag in tags and v.currency in ("USD", "")}
        def _f(k: str) -> float:
            try:
                return float(vals.get(k) or 0)
            except Exception:
                return 0.0
        return {
            "account":    account,
            "paper":      is_paper_account(account),
            "day_pnl":    _f("DayPnL"),
            "unrealized": _f("UnrealizedPnL"),
            "realized":   _f("RealizedPnL"),
            "net_liq":    _f("NetLiquidation"),
            "ms":         int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        return {"error": str(exc)}


async def get_positions_dict() -> dict:
    """Return open positions as a structured dict for web rendering (not an MCP tool)."""
    t0 = time.monotonic()
    try:
        ib         = await connect_ib(CLIENT_ID)
        account_id = await get_account_id(ib)
        positions  = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=10)
        if account_id:
            positions = [p for p in positions if p.account == account_id]

        rows: list[dict] = []
        total_val  = 0.0
        total_upnl = 0.0
        for p in positions:
            c    = p.contract
            qty  = p.position
            avg  = p.avgCost
            val  = qty * avg
            upnl = 0.0
            try:
                t = ib.ticker(c)
                if t and t.marketPrice() and t.marketPrice() > 0:
                    val  = qty * t.marketPrice()
                    upnl = val - qty * avg
            except Exception:
                pass
            total_val  += abs(val)
            total_upnl += upnl

            if c.secType == "OPT":
                try:
                    from datetime import datetime as _dt
                    exp_fmt = _dt.strptime(c.lastTradeDateOrContractMonth, "%Y%m%d").strftime("%b %d")
                except Exception:
                    exp_fmt = c.lastTradeDateOrContractMonth
                desc = f"{c.symbol} {c.right}{c.strike:.0f} {exp_fmt}"
            else:
                desc = f"{c.symbol} {c.secType}"

            rows.append({
                "desc":      desc,
                "symbol":    c.symbol,
                "sec_type":  c.secType,
                "qty":       qty,
                "avg_cost":  avg,
                "mkt_value": val,
                "upnl":      upnl,
            })

        return {
            "account":    account_id,
            "paper":      is_paper_account(account_id),
            "positions":  rows,
            "total_val":  total_val,
            "total_upnl": total_upnl,
            "ms":         int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:
        return {"error": str(exc), "positions": []}


if __name__ == "__main__":
    mcp.run()
