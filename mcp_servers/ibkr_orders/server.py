"""
IBKR Orders MCP Server

Place, confirm, cancel, and review vertical spread orders via the CP Gateway.
Also surfaces order history from the local DB.

Tools:
  place_spread(...)            → submit a vertical spread (credit or debit)
  explain_and_place(...)       → LLM risk briefing then place if confirmed
  confirm_order(reply_id)      → send IBKR two-step confirmation
  cancel_order(order_id)       → cancel a live/pending order
  get_live_orders()            → orders currently on exchange
  get_order_history(limit)     → persisted DB history

Memory: db/agents/ibkr_orders.db  +  db/state.db (ibkr_orders + ibkr_conid_cache)
LLM:    configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL  (for pre-trade risk briefing)
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

def _paper_header() -> str:
    if config.IBKR_PAPER_TRADING:
        return "📄 [PAPER TRADING — no real money]\n\n"
    return "⚠ [LIVE TRADING — REAL MONEY]\n\n"

from agents.ibkr_agent import (  # noqa: E402
    auth_status, cancel_order as _cancel,
    get_accounts, get_orders, order_history,
    place_vertical_spread,
)
import httpx  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr_orders.db")
GATEWAY  = "https://localhost:5000/v1/api"

_llm = get_llm_client()
_db_ready = False

RISK_SYSTEM = (
    "You are a risk-aware options trader doing a pre-trade review. "
    "Given spread parameters, write 3-4 sentences: what the trade does, "
    "the max risk in dollars, the ideal scenario, and one key risk to watch. "
    "Be specific. No filler."
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


def _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity) -> str:
    opt  = "Put" if right.upper() == "P" else "Call"
    kind = "Credit" if net_price > 0 else "Debit"
    spread = abs(short_strike - long_strike)
    mx_p = round(abs(net_price) * 100 * quantity)
    mx_l = round((spread - abs(net_price)) * 100 * quantity) if net_price > 0 else round(abs(net_price) * 100 * quantity)
    be   = (round(short_strike - abs(net_price), 2) if right.upper()=="P" and net_price>0
            else round(short_strike + abs(net_price), 2) if right.upper()=="C" and net_price>0
            else round(min(short_strike,long_strike) + abs(net_price), 2) if right.upper()=="C"
            else round(max(short_strike,long_strike) - abs(net_price), 2))
    col = 18
    return (
        f"{kind} {opt} Vertical — {ticker}  ×{quantity}\n\n"
        f"{'Sell ' + opt:<{col}} ${short_strike:.0f}  (short leg)\n"
        f"{'Buy ' + opt:<{col}} ${long_strike:.0f}  (protection)\n"
        f"{'Expiry':<{col}} {expiry}\n"
        f"{'Net ' + ('credit' if net_price>0 else 'debit'):<{col}} "
        f"{'+'if net_price>0 else '-'}${abs(net_price):.2f}/share  "
        f"({'+'if net_price>0 else '-'}${abs(int(net_price*100))}/contract)\n"
        f"{'Break-even':<{col}} ${be}\n"
        f"{'Max profit':<{col}} +${mx_p}\n"
        f"{'Max loss':<{col}} -${mx_l}"
    )


mcp = FastMCP(
    name="ibkr-orders",
    instructions=(
        "IBKR order management: place/cancel vertical spreads, confirm two-step orders, "
        "view live and historical orders. Pre-trade LLM risk briefing available."
    ),
)


@mcp.tool()
async def place_spread(
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,
    expiry: str,
    net_price: float,
    quantity: int = 1,
    tif: str = "DAY",
) -> str:
    """
    Place a vertical spread order via the CP Gateway.

    ticker:       Stock symbol (e.g. AAPL)
    short_strike: Strike you sell
    long_strike:  Strike you buy (protection)
    right:        P = puts, C = calls
    expiry:       YYYY-MM-DD
    net_price:    Credit received (>0) or debit paid (<0) per share
    quantity:     Number of contracts (default 1)
    tif:          Time-in-force: DAY | GTC (default DAY)

    WARNING: places a live order. Use get_risk_briefing() first to review.
    """
    await _ensure_db()
    t0  = time.monotonic()
    s   = await auth_status()
    if err := _require_auth(s):
        return err

    accounts = await get_accounts()
    if not accounts:
        return "No accounts found."
    account_id = accounts[0]

    try:
        result = await place_vertical_spread(
            account_id=account_id,
            ticker=ticker.upper(), short_strike=short_strike,
            long_strike=long_strike, right=right.upper(),
            expiry=expiry, net_price=net_price,
            quantity=quantity, tif=tif,
        )
    except Exception as exc:
        return f"Order failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    order_id = result.get("order_id", "—")
    status   = result.get("order_status", "—")
    icon     = "✅" if "submit" in str(status).lower() or "fill" in str(status).lower() else "⚠️"

    await _log_call("place_spread", ms, f"{ticker} {right}{short_strike}/{long_strike} {expiry}")

    summary = _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity)
    return (
        f"{_paper_header()}"
        f"{icon} Order {'Submitted' if icon=='✅' else 'Status: '+status}\n\n"
        f"{summary}\n\n"
        f"IBKR Order ID  {order_id}\n"
        f"Status         {status}\n"
        f"Account        {account_id}\n"
        f"TIF            {tif}  ({ms}ms)"
    )


@mcp.tool()
async def get_risk_briefing(
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,
    expiry: str,
    net_price: float,
    quantity: int = 1,
) -> str:
    """
    Get an LLM pre-trade risk briefing for a proposed spread WITHOUT placing any order.
    Review this before calling place_spread().
    """
    await _ensure_db()
    t0      = time.monotonic()
    summary = _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity)

    try:
        import asyncio
        briefing = await asyncio.wait_for(
            _llm.complete(RISK_SYSTEM, f"Review this trade:\n{summary}", max_tokens=250),
            timeout=20.0,
        )
    except Exception as exc:
        briefing = f"LLM unavailable: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_risk_briefing", ms, f"{ticker} {right}{short_strike}/{long_strike}")
    return f"{_paper_header()}{summary}\n\nRisk Briefing:\n{briefing}"


@mcp.tool()
async def confirm_order(reply_id: str) -> str:
    """
    Send a confirmation reply for IBKR's two-step order flow.
    IBKR sometimes returns a confirmation prompt before submitting — use this to approve it.
    reply_id comes from the 'id' field in the initial order response.
    """
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    try:
        async with httpx.AsyncClient(base_url=GATEWAY, verify=False, timeout=15.0) as c:
            r = await c.post(f"/iserver/reply/{reply_id}", json={"confirmed": True})
            r.raise_for_status()
            result = r.json()
    except Exception as exc:
        return f"Confirmation failed: {exc}"

    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("confirm_order", ms, reply_id)
    return f"Confirmation sent for reply {reply_id} ({ms}ms):\n{json.dumps(result, indent=2)}"


@mcp.tool()
async def cancel_open_order(order_id: str) -> str:
    """Cancel a pending/submitted order by IBKR order ID."""
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    accounts = await get_accounts()
    if not accounts:
        return "No accounts found."

    try:
        result = await _cancel(accounts[0], order_id)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("cancel_open_order", ms, order_id)
        return f"Cancel request sent for order {order_id} ({ms}ms):\n{json.dumps(result, indent=2)}"
    except Exception as exc:
        return f"Cancel failed: {exc}"


@mcp.tool()
async def get_live_orders() -> str:
    """Fetch orders currently live on the exchange from the CP Gateway."""
    await _ensure_db()
    t0 = time.monotonic()
    s  = await auth_status()
    if err := _require_auth(s):
        return err

    orders = await get_orders()
    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_live_orders", ms)

    if not orders:
        return f"No live orders ({ms}ms)."

    lines = [f"Live Orders ({len(orders)}, {ms}ms)\n",
             f"{'Order ID':<12} {'Symbol':<10} {'Side':<6} {'Qty':>4}  {'Price':>8}  {'Status':<16}  TIF"]
    lines.append("─" * 70)
    for o in orders[:20]:
        lines.append(
            f"{str(o.get('orderId','?')):<12} "
            f"{str(o.get('ticker', o.get('symbol','?'))):<10} "
            f"{o.get('side','?'):<6} {o.get('remainingQuantity', o.get('totalSize',0)):>4.0f}  "
            f"${o.get('price', o.get('limitPrice', 0)):>7.2f}  "
            f"{o.get('status','?'):<16}  {o.get('timeInForce','?')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_order_history(limit: int = 10) -> str:
    """Return recent order history from the local DB (not live from gateway)."""
    await _ensure_db()
    rows = await order_history(limit=limit)
    if not rows:
        return "No orders in history."

    lines = [f"Order History ({len(rows)} shown)\n",
             f"{'Date':<18} {'Ticker':<6} {'Strategy':<22} {'Strikes':<14} {'Net':>6}  {'Qty':>3}  Status"]
    lines.append("─" * 90)
    for o in rows:
        ts   = (o.get("timestamp") or "")[:16].replace("T", " ")
        icon = "✅" if "fill" in str(o.get("status","")).lower() else "🔄" if "submit" in str(o.get("status","")).lower() else "⚫"
        strikes = f"${o.get('short_strike',0):.0f}/{o.get('long_strike',0):.0f}"
        net     = f"{'+' if (o.get('net_price',0) or 0) >= 0 else ''}${o.get('net_price',0):.2f}"
        lines.append(
            f"{icon} {ts:<16} {o.get('ticker','?'):<6} "
            f"{o.get('strategy','?'):<22} {strikes:<14} "
            f"{net:>6}  {o.get('quantity',1):>3}  {o.get('status','?')}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
