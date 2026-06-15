"""
IBKR Orders MCP Server  (ib_insync / TWS socket)

Place, cancel, and review vertical spread orders via IB Gateway.

Tools:
  place_spread(...)             → submit a vertical spread order
  get_risk_briefing(...)        → LLM pre-trade risk review (no order placed)
  cancel_open_order(order_id)   → cancel a live order
  get_live_orders()             → orders currently on exchange
  get_order_history(limit)      → from local DB

Memory: db/agents/ibkr_orders.db  +  db/state.db (ibkr_orders table)
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
from ib_insync import LimitOrder
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from db.database import order_history  # noqa: E402  (reuse existing DB layer)
from tools.ibkr_tws import (  # noqa: E402
    connect_ib, get_account_id, is_paper_account,
    make_option_contract, make_vertical_spread, paper_label,
)

logger = logging.getLogger(__name__)

_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB  = os.path.join(_ROOT, "db", "agents", "ibkr_orders.db")
CLIENT_ID = config.IBKR_CLIENT_ID_ORDERS

_llm      = get_llm_client()
_db_ready = False

RISK_SYSTEM = (
    "You are a risk-aware options trader. Given spread parameters, write 3-4 sentences: "
    "what the trade does, max risk in dollars, ideal scenario, and one key risk. "
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


async def _save_order_to_db(account_id, ticker, strategy, short_strike, long_strike,
                             opt_type, expiry, net_price, quantity, ibkr_order_id, status, raw) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO ibkr_orders "
            "(timestamp, account_id, ticker, strategy, short_strike, long_strike, "
            " option_type, expiry, net_price, quantity, ibkr_order_id, status, raw_response) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_utcnow(), account_id, ticker, strategy, short_strike, long_strike,
             opt_type, expiry, net_price, quantity, str(ibkr_order_id), status, json.dumps(raw)),
        )
        await db.commit()


def _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity) -> str:
    opt    = "Put" if right.upper() == "P" else "Call"
    kind   = "Credit" if net_price > 0 else "Debit"
    spread = abs(short_strike - long_strike)
    mx_p   = round(abs(net_price) * 100 * quantity)
    mx_l   = round((spread - abs(net_price)) * 100 * quantity) if net_price > 0 else round(abs(net_price) * 100 * quantity)
    be     = (round(short_strike - abs(net_price), 2) if right.upper() == "P" and net_price > 0
              else round(short_strike + abs(net_price), 2) if right.upper() == "C" and net_price > 0
              else round(min(short_strike, long_strike) + abs(net_price), 2) if right.upper() == "C"
              else round(max(short_strike, long_strike) - abs(net_price), 2))
    col = 18
    return (
        f"{kind} {opt} Vertical — {ticker}  ×{quantity}\n\n"
        f"{'Sell ' + opt:<{col}} ${short_strike:.0f}\n"
        f"{'Buy ' + opt:<{col}} ${long_strike:.0f}\n"
        f"{'Expiry':<{col}} {expiry}\n"
        f"{'Net ' + ('credit' if net_price>0 else 'debit'):<{col}} "
        f"{'+'if net_price>0 else '-'}${abs(net_price):.2f}/share  "
        f"({'+'if net_price>0 else '-'}${abs(int(net_price*100))}/contract)\n"
        f"{'Break-even':<{col}} ${be}\n"
        f"{'Max profit':<{col}} +${mx_p}\n"
        f"{'Max loss':<{col}} -${mx_l}"
    )


def _paper_header() -> str:
    return f"{paper_label()}\n\n"


mcp = FastMCP(
    name="ibkr-orders",
    instructions=(
        "IBKR order management via ib_insync TWS socket. "
        "Place/cancel vertical spreads, view live and historical orders."
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
    Place a vertical spread via IB Gateway socket.

    ticker:       Stock symbol (e.g. AAPL)
    short_strike: Strike you sell (higher put / lower call for credit spreads)
    long_strike:  Strike you buy (protection leg)
    right:        P = puts, C = calls
    expiry:       YYYY-MM-DD (e.g. 2026-07-18)
    net_price:    Credit received (>0) or debit paid (<0) per share
    quantity:     Number of contracts (default 1)
    tif:          DAY | GTC

    WARNING: places a live order. Use get_risk_briefing() first.
    """
    await _ensure_db()
    t0    = time.monotonic()
    right = right.strip().upper()
    ticker = ticker.strip().upper()
    expiry_ib = expiry.replace("-", "")  # IBKR uses YYYYMMDD

    try:
        ib        = await connect_ib(CLIENT_ID)
        account   = await get_account_id(ib)
        is_credit = net_price > 0

        # Qualify both option legs to get conids
        short_contract = make_option_contract(ticker, expiry_ib, right, short_strike)
        long_contract  = make_option_contract(ticker, expiry_ib, right, long_strike)

        await ib.qualifyContractsAsync(short_contract, long_contract)
        if not short_contract.conId or not long_contract.conId:
            return f"Could not qualify contracts for {ticker} {right} {expiry}. Check strikes and expiry."

        # Build combo contract
        combo = make_vertical_spread(
            ticker, short_contract.conId, long_contract.conId, is_credit
        )

        # IBKR BAG combo convention: always BUY the combo; the leg actions (SELL/BUY per leg)
        # define the spread direction. Credit spreads use a negative limit price (you receive that
        # amount); debit spreads use a positive limit price (you pay that amount).
        # Sending SELL + positive price causes IBKR to reverse the legs AND flag the price as invalid.
        action     = "BUY"
        ibkr_price = -round(abs(net_price), 2) if is_credit else round(abs(net_price), 2)
        order      = LimitOrder(action, quantity, ibkr_price)
        order.tif       = tif
        order.account   = account
        order.outsideRth = False
        order.overridePercentConstraints = True
        order.transmit  = False  # stage in TWS without sending — user must click Transmit in TWS

        trade = ib.placeOrder(combo, order)
        await asyncio.sleep(1)  # let TWS confirm

        ms         = int((time.monotonic() - t0) * 1000)
        order_id   = trade.order.orderId
        status_str = trade.orderStatus.status or "Staged"
        icon       = "📋"

        summary = _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity)
        opt_type = "Put" if right == "P" else "Call"
        strategy = f"{'Credit' if is_credit else 'Debit'} {opt_type} Vertical"

        await _save_order_to_db(
            account, ticker, strategy, short_strike, long_strike,
            right, expiry, net_price, quantity, order_id, status_str, {}
        )
        await _log_call("place_spread", ms, f"{ticker} {right}{short_strike}/{long_strike} {expiry}")

        return (
            f"{_paper_header()}{icon} Staged in TWS — open TWS and click Transmit to send\n\n"
            f"{summary}\n\n"
            f"IBKR Order ID  {order_id}\n"
            f"Account        {account}\n"
            f"TIF            {tif}  ({ms}ms)"
        )

    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        logger.error("place_spread failed: %s", exc)
        return f"Order failed: {exc}"


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
    LLM pre-trade risk briefing for a proposed spread — does NOT place any order.
    Review this before calling place_spread().
    """
    await _ensure_db()
    t0      = time.monotonic()
    summary = _spread_summary(ticker, short_strike, long_strike, right, expiry, net_price, quantity)
    try:
        briefing = await asyncio.wait_for(
            _llm.complete(RISK_SYSTEM, f"Review this trade:\n{summary}", max_tokens=250),
            timeout=20.0,
        )
    except Exception as exc:
        briefing = f"LLM unavailable ({exc})"
    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_risk_briefing", ms, f"{ticker} {right}{short_strike}/{long_strike}")
    return f"{_paper_header()}{summary}\n\nRisk Briefing:\n{briefing}"


@mcp.tool()
async def cancel_open_order(order_id: int) -> str:
    """Cancel a live/pending order by its IBKR order ID (integer)."""
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib     = await connect_ib(CLIENT_ID)
        trades = ib.trades()
        target = next((t for t in trades if t.order.orderId == order_id), None)
        if not target:
            return f"Order {order_id} not found in open trades."
        ib.cancelOrder(target.order)
        await asyncio.sleep(0.5)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("cancel_open_order", ms, str(order_id))
        return f"Cancel request sent for order {order_id} ({ms}ms)."
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Cancel failed: {exc}"


async def cancel_all_open_orders() -> str:
    """Cancel every open/pending order currently on the exchange."""
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib = await connect_ib(CLIENT_ID)
        await ib.reqAllOpenOrdersAsync()
        await asyncio.sleep(0.3)
        open_trades = ib.openTrades()
        if not open_trades:
            return "No open orders to cancel."
        for trade in open_trades:
            ib.cancelOrder(trade.order)
        await asyncio.sleep(0.5)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("cancel_all_open_orders", ms, f"cancelled {len(open_trades)} orders")
        return f"Cancel request sent for {len(open_trades)} order(s) ({ms}ms)."
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Cancel all failed: {exc}"


@mcp.tool()
async def get_live_orders() -> str:
    """Fetch all open/submitted orders currently on the exchange."""
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib     = await connect_ib(CLIENT_ID)
        trades = ib.openTrades()
        ms     = int((time.monotonic() - t0) * 1000)
        await _log_call("get_live_orders", ms)

        if not trades:
            return f"No live orders ({ms}ms)."

        lines = [f"Live Orders ({len(trades)}, {ms}ms)\n",
                 f"{'ID':<8} {'Symbol':<10} {'Action':<6} {'Qty':>4}  {'Limit':>8}  {'Status':<16}  TIF"]
        lines.append("─" * 65)
        for t in trades[:20]:
            o = t.order
            c = t.contract
            lines.append(
                f"{o.orderId:<8} {c.symbol:<10} {o.action:<6} {o.totalQuantity:>4.0f}"
                f"  ${o.lmtPrice:>7.2f}  {t.orderStatus.status:<16}  {o.tif}"
            )
        return "\n".join(lines)
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_order_history(limit: int = 10) -> str:
    """Return recent order history from local DB."""
    await _ensure_db()
    rows = await order_history(limit=limit)
    if not rows:
        return "No orders in history."
    lines = [f"Order History ({len(rows)} shown)\n",
             f"{'Date':<18} {'Ticker':<6} {'Strategy':<22} {'Strikes':<14} {'Net':>6}  Status"]
    lines.append("─" * 80)
    for o in rows:
        ts   = (o.get("timestamp") or "")[:16].replace("T", " ")
        icon = "✅" if "fill" in str(o.get("status","")).lower() else "🔄" if "submit" in str(o.get("status","")).lower() else "⚫"
        net  = f"{'+' if (o.get('net_price',0) or 0) >= 0 else ''}${o.get('net_price',0):.2f}"
        strikes = f"${o.get('short_strike',0):.0f}/{o.get('long_strike',0):.0f}"
        lines.append(f"{icon} {ts:<16} {o.get('ticker','?'):<6} {o.get('strategy','?'):<22} {strikes:<14} {net:>6}  {o.get('status','?')}")
    return "\n".join(lines)


async def get_live_order_statuses() -> tuple[set[str], dict[str, str]]:
    """
    Fetch all currently-open orders from IBKR (across all client sessions via reqAllOpenOrders).
    Returns (open_order_ids, {ibkr_order_id: status_string}).
    open_order_ids: order IDs still live on the exchange and eligible for cancellation.
    """
    ib = await connect_ib(CLIENT_ID)
    await ib.reqAllOpenOrdersAsync()
    await asyncio.sleep(0.3)

    open_trades = ib.openTrades()
    all_trades  = ib.trades()

    statuses: dict[str, str] = {str(t.order.orderId): t.orderStatus.status for t in all_trades}
    open_ids:  set[str] = set()
    for t in open_trades:
        oid = str(t.order.orderId)
        open_ids.add(oid)
        statuses[oid] = t.orderStatus.status

    return open_ids, statuses


if __name__ == "__main__":
    mcp.run()
