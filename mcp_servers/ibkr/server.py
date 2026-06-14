"""
IBKR MCP Server

Independent agent for Interactive Brokers CP Gateway integration:
session management, positions, orders, and vertical spread execution.
Claude Sonnet provides trade explanation for any placed order.

Tools:
  get_gateway_status()                → auth status, connection, P&L
  get_open_positions()                → live positions from CP Gateway
  get_recent_orders(limit)           → order history from local DB
  place_vertical_spread(...)          → execute a vertical spread order
  explain_trade(details)              → LLM trade explanation for risk review
  cancel_order(order_id)             → cancel a pending order

Shared data:  db/state.db  (ibkr_orders + ibkr_conid_cache shared with web UI)
Agent memory: db/agents/ibkr.db  (call log + trade explanations, independent)
LLM:          claude-sonnet-4-6  (used for explain_trade tool)
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

from agents.ibkr_agent import (  # noqa: E402
    IBKRAgent,
    auth_status,
    cancel_order as _cancel_order,
    get_accounts,
    get_pnl,
    get_positions,
    order_history,
    place_vertical_spread,
)

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr.db")
SYSTEM = (
    "You are a risk-aware options trader reviewing a trade before execution. "
    "Given trade parameters, provide a clear 3-4 sentence risk briefing: "
    "what the trade does, max risk, ideal scenario, and one key risk to watch. "
    "Be specific about dollar amounts and scenarios."
)

_llm = get_llm_client()
_ibkr = IBKRAgent()

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
            CREATE TABLE IF NOT EXISTS trade_explanations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                ticker      TEXT,
                details     TEXT NOT NULL,
                explanation TEXT NOT NULL
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
    name="ibkr",
    instructions=(
        "Interactive Brokers CP Gateway agent: session status, positions, order history, "
        "vertical spread execution, and Claude Sonnet trade explanation. "
        "Gateway must be running at https://localhost:5000."
    ),
)


@mcp.tool()
async def get_gateway_status() -> str:
    """
    Check CP Gateway connection status and retrieve live P&L.
    Returns auth state, connectivity, account list, and day/unrealized P&L.
    """
    await _ensure_db()
    t0 = time.monotonic()
    result = await _ibkr.status()
    await _log_call("get_gateway_status", "", int((time.monotonic() - t0) * 1000))
    return result


@mcp.tool()
async def get_open_positions() -> str:
    """
    Fetch all open positions from the CP Gateway.
    Shows symbol, quantity, market price, market value, and unrealized P&L.
    """
    await _ensure_db()
    t0 = time.monotonic()
    result = await _ibkr.positions_summary()
    await _log_call("get_open_positions", "", int((time.monotonic() - t0) * 1000))
    return result


@mcp.tool()
async def get_recent_orders(limit: int = 10) -> str:
    """
    Retrieve recent order history from the local database (not live from gateway).
    Returns timestamp, ticker, strategy, strikes, net price, quantity, and status.
    """
    await _ensure_db()
    t0 = time.monotonic()
    result = await _ibkr.orders_summary()
    await _log_call("get_recent_orders", str(limit), int((time.monotonic() - t0) * 1000))
    return result


@mcp.tool()
async def place_vertical_spread(
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,
    expiry: str,
    net_price: float,
    quantity: int = 1,
) -> str:
    """
    Execute a vertical spread order via the CP Gateway.

    Parameters:
      ticker        Stock symbol (e.g. "AAPL")
      short_strike  The strike you're selling (higher strike for puts / lower for calls)
      long_strike   The strike you're buying (protection leg)
      right         "P" for puts, "C" for calls
      expiry        Expiration date as "YYYY-MM-DD"
      net_price     Credit received (positive) or debit paid (negative)
      quantity      Number of contracts (default 1)

    Credit spread: net_price > 0 (you receive premium)
    Debit spread:  net_price < 0 (you pay premium)

    WARNING: This places a live order. Use explain_trade first to review risk.
    """
    await _ensure_db()
    t0 = time.monotonic()
    right = right.upper()
    if right not in ("P", "C"):
        return "Error: right must be 'P' (put) or 'C' (call)."

    result = await _ibkr.execute_spread(
        ticker=ticker.strip().upper(),
        short_strike=short_strike,
        long_strike=long_strike,
        right=right,
        expiry=expiry,
        net_price=net_price,
        quantity=quantity,
    )
    await _log_call(
        "place_vertical_spread",
        f"{ticker} {right}{short_strike}/{long_strike} {expiry} @{net_price} x{quantity}",
        int((time.monotonic() - t0) * 1000),
    )
    return result


@mcp.tool()
async def explain_trade(
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,
    expiry: str,
    net_price: float,
    quantity: int = 1,
) -> str:
    """
    Get a Claude Sonnet risk briefing for a proposed vertical spread BEFORE placing it.
    Use this to understand the trade: max risk, ideal scenario, and key risks.
    Does NOT place any order — purely educational/review.
    """
    await _ensure_db()
    t0 = time.monotonic()
    right = right.upper()
    opt_type   = "Put" if right == "P" else "Call"
    is_credit  = net_price > 0
    spread     = abs(short_strike - long_strike)
    max_profit = round(abs(net_price) * 100 * quantity)
    max_loss   = round((spread - abs(net_price)) * 100 * quantity) if is_credit else round(abs(net_price) * 100 * quantity)
    breakeven  = (
        round(short_strike - abs(net_price), 2) if right == "P" and is_credit
        else round(short_strike + abs(net_price), 2) if right == "C" and is_credit
        else round(min(short_strike, long_strike) + abs(net_price), 2) if right == "C"
        else round(max(short_strike, long_strike) - abs(net_price), 2)
    )

    details = (
        f"{ticker} {opt_type} vertical — {'Credit' if is_credit else 'Debit'} spread\n"
        f"Sell {opt_type} at ${short_strike} / Buy {opt_type} at ${long_strike}\n"
        f"Expiry: {expiry}  |  Quantity: {quantity} contract(s)\n"
        f"Net {'credit' if is_credit else 'debit'}: ${abs(net_price):.2f}/share  "
        f"(${abs(int(net_price*100))}/contract)\n"
        f"Max profit: ${max_profit}  |  Max loss: ${max_loss}  |  Breakeven: ${breakeven}"
    )

    prompt = (
        f"Review this options trade:\n{details}\n\n"
        f"Provide a 3-4 sentence risk briefing covering: what the trade does, "
        f"max risk scenario, ideal outcome, and one key risk."
    )

    try:
        explanation = await _llm.complete(SYSTEM, prompt, max_tokens=300)
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        explanation = (
            f"{'Credit' if is_credit else 'Debit'} {opt_type.lower()} spread on {ticker}. "
            f"Max profit ${max_profit} if stock stays {'below' if right=='P' and is_credit else 'above' if right=='C' and is_credit else 'moves'} "
            f"${short_strike} by {expiry}. Max loss: ${max_loss}."
        )

    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO trade_explanations (timestamp, ticker, details, explanation) VALUES (?,?,?,?)",
            (_utcnow(), ticker.upper(), details, explanation),
        )
        await db.commit()

    await _log_call("explain_trade", f"{ticker} {right}{short_strike}/{long_strike}",
                    int((time.monotonic() - t0) * 1000))
    return f"{details}\n\nRisk Briefing:\n{explanation}"


@mcp.tool()
async def cancel_order(order_id: str) -> str:
    """
    Cancel a pending order by IBKR order ID.
    Only works for orders in submitted/pending state — already-filled orders cannot be cancelled.
    """
    await _ensure_db()
    t0 = time.monotonic()
    s = await auth_status()
    if not s.get("authenticated"):
        return "Not authenticated. Open https://localhost:5000 and log in first."

    accounts = await get_accounts()
    if not accounts:
        return "No trading accounts found."

    try:
        result = await _cancel_order(accounts[0], order_id)
        output = f"Cancel request sent for order {order_id}.\nResponse: {json.dumps(result, indent=2)}"
    except Exception as exc:
        output = f"Cancel failed: {exc}"

    await _log_call("cancel_order", order_id, int((time.monotonic() - t0) * 1000))
    return output


if __name__ == "__main__":
    mcp.run()
