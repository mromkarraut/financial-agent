"""
IBKRAgent — Interactive Brokers Client Portal Gateway agent.

Connects to the local CP Gateway (https://localhost:5000) and provides:
  • Session management  — auth check, tickle keepalive, reauthenticate
  • Contract lookup     — secdef search → strikes → conid (with SQLite cache)
  • Vertical spreads    — place, confirm, cancel (credit & debit)
  • Positions & P&L     — live account snapshot
  • Order history       — persisted in ibkr_orders table

CP Gateway must be running and authenticated via browser before use.
  Download: https://download2.interactivebrokers.com/portal/clientportal.gw.zip
  Start:    ./bin/run.sh root/conf.yaml
  Auth:     open https://localhost:5000 in a browser on this machine

The CP Gateway session times out after ~5-6 minutes without activity.
This agent sends a /tickle every 55 seconds in the background while active.

Combo order format (vertical spread):
  conidex = "28812380;;;{short_conid}/-1,{long_conid}/1"   ← credit spread (sell high, buy low)
  conidex = "28812380;;;{long_conid}/1,{short_conid}/-1"   ← debit spread  (buy high, sell low)
  28812380 = IBKR's permanent USD spread contract conid (hardcoded, does not change)
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx

import config

logger = logging.getLogger(__name__)

GATEWAY_URL   = "https://localhost:5000/v1/api"
USD_COMBO_CID = 28812380  # permanent USD spread conid (never changes)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_code(expiry: str) -> str:
    """Convert '2026-06-18' → 'JUN26'."""
    months = ["JAN","FEB","MAR","APR","MAY","JUN",
               "JUL","AUG","SEP","OCT","NOV","DEC"]
    try:
        d = datetime.strptime(expiry, "%Y-%m-%d")
        return f"{months[d.month - 1]}{str(d.year)[-2:]}"
    except Exception:
        return expiry


# ── HTTP client (async httpx, verify=False for self-signed cert) ──────────────

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GATEWAY_URL,
        verify=False,        # CP Gateway uses a self-signed cert
        timeout=30.0,
    )


async def _get(path: str, **params) -> Any:
    async with _client() as c:
        r = await c.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None) -> Any:
    async with _client() as c:
        r = await c.post(path, json=body or {})
        r.raise_for_status()
        return r.json()


async def _delete(path: str) -> Any:
    async with _client() as c:
        r = await c.delete(path)
        r.raise_for_status()
        return r.json()


# ── Conid cache (SQLite) ──────────────────────────────────────────────────────

async def _cache_get(symbol: str, expiry: str, right: str, strike: float) -> int | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT conid FROM ibkr_conid_cache "
            "WHERE symbol=? AND expiry=? AND right=? AND strike=?",
            (symbol.upper(), expiry, right.upper(), strike),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _cache_set(symbol: str, expiry: str, right: str, strike: float, conid: int) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ibkr_conid_cache "
            "(symbol, sectype, expiry, right, strike, conid, cached_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol.upper(), "OPT", expiry, right.upper(), strike, conid, _utcnow()),
        )
        await db.commit()


async def _save_order(account_id: str, ticker: str, strategy: str,
                      short_strike: float, long_strike: float,
                      opt_type: str, expiry: str, net_price: float,
                      quantity: int, ibkr_order_id: str, status: str,
                      raw: dict) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO ibkr_orders "
            "(timestamp, account_id, ticker, strategy, short_strike, long_strike, "
            " option_type, expiry, net_price, quantity, ibkr_order_id, status, raw_response) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_utcnow(), account_id, ticker, strategy, short_strike, long_strike,
             opt_type, expiry, net_price, quantity, ibkr_order_id, status, json.dumps(raw)),
        )
        await db.commit()


# ── Session ───────────────────────────────────────────────────────────────────

async def auth_status() -> dict:
    """Check if CP Gateway session is authenticated and connected."""
    try:
        return await _post("/iserver/auth/status")
    except httpx.ConnectError:
        return {"authenticated": False, "connected": False,
                "error": "CP Gateway not reachable — is it running on localhost:5000?"}
    except Exception as exc:
        return {"authenticated": False, "connected": False, "error": str(exc)}


async def tickle() -> dict:
    """Keep session alive. Call every 55 seconds."""
    return await _post("/tickle")


async def reauthenticate() -> dict:
    """Re-open brokerage session without browser re-login (if SSO cookie still valid)."""
    return await _post("/iserver/reauthenticate")


# ── Accounts ──────────────────────────────────────────────────────────────────

async def get_accounts() -> list[str]:
    data = await _get("/iserver/accounts")
    return data.get("accounts", [])


async def get_account_summary(account_id: str) -> dict:
    await _get("/portfolio/accounts")  # must call first to init portfolio session
    return await _get(f"/portfolio/{account_id}/summary")


async def get_pnl() -> dict:
    return await _get("/iserver/account/pnl/partitioned")


async def get_positions(account_id: str) -> list[dict]:
    await _get("/portfolio/accounts")
    return await _get(f"/portfolio/{account_id}/positions/0")


# ── Contract discovery ────────────────────────────────────────────────────────

async def get_underlying_conid(symbol: str) -> int:
    """Step 1: Get the stock/ETF conid for a symbol."""
    results = await _post("/iserver/secdef/search",
                          {"symbol": symbol.upper(), "name": False, "secType": "STK"})
    if not results:
        raise ValueError(f"No underlying found for {symbol}")
    return int(results[0]["conid"])


async def get_option_conid(symbol: str, expiry: str, right: str, strike: float,
                           exchange: str = "SMART") -> int:
    """
    Get the conid for a specific option contract.
    expiry: '2026-06-18'  right: 'P' or 'C'  strike: 290.0

    Sequence: secdef/search → secdef/strikes (warms up session) → secdef/info
    Results are cached in SQLite to avoid repeated API calls.
    """
    # Check cache first
    cached = await _cache_get(symbol, expiry, right, strike)
    if cached:
        return cached

    # Step 1: get underlying conid
    und_conid = await get_underlying_conid(symbol)

    # Step 2: warm up the strikes cache (required before secdef/info)
    month = _month_code(expiry)
    await _get("/iserver/secdef/strikes",
               conid=und_conid, sectype="OPT", month=month, exchange=exchange)

    # Step 3: get the specific option conid
    results = await _get("/iserver/secdef/info",
                         conid=und_conid, sectype="OPT", month=month,
                         right=right.upper(), strike=strike, exchange=exchange)
    if not results:
        raise ValueError(f"No contract found: {symbol} {right}{strike} {month}")

    conid = int(results[0]["conid"])
    await _cache_set(symbol, expiry, right, strike, conid)
    return conid


# ── Order placement ───────────────────────────────────────────────────────────

async def _place_with_confirmation(account_id: str, payload: dict) -> dict:
    """
    Post an order and handle IBKR's multi-step confirmation flow.
    IBKR may return a list of warnings/questions requiring a /reply/{id} confirmation.
    """
    resp = await _post(f"/iserver/account/{account_id}/orders", payload)

    if not isinstance(resp, list):
        return resp

    # Walk through any confirmation prompts
    result = resp
    for item in resp:
        if "id" in item and "message" in item:
            logger.info("IBKR order confirmation required: %s", item.get("message"))
            result = await _post(f"/iserver/reply/{item['id']}", {"confirmed": True})

    return result[0] if isinstance(result, list) else result


async def place_vertical_spread(
    account_id: str,
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,           # "P" (put spread) or "C" (call spread)
    expiry: str,          # "2026-06-18"
    net_price: float,     # credit → positive, debit → negative
    quantity: int = 1,
    tif: str = "DAY",
    exchange: str = "SMART",
) -> dict:
    """
    Place a vertical spread order via the CP Gateway.

    Credit spread (e.g., bull put): short_strike > long_strike (puts), net_price > 0
    Debit spread  (e.g., bear put): short_strike < long_strike (puts), net_price < 0

    The function determines whether this is a BUY or SELL combo from the price sign
    and strike order, matching tastytrade conventions.
    """
    is_credit = net_price > 0

    # Fetch conids for both legs (cached after first call)
    short_conid = await get_option_conid(ticker, expiry, right, short_strike, exchange)
    long_conid  = await get_option_conid(ticker, expiry, right, long_strike,  exchange)

    # conidex format: "28812380;;;{sell_conid}/-1,{buy_conid}/1"
    # For a credit spread: sell short_strike (higher put / lower call), buy long_strike
    # For a debit spread:  buy short_strike, sell long_strike
    if is_credit:
        conidex = f"{USD_COMBO_CID};;;{short_conid}/-1,{long_conid}/1"
        side    = "SELL"
        price   = abs(net_price)
    else:
        conidex = f"{USD_COMBO_CID};;;{short_conid}/1,{long_conid}/-1"
        side    = "BUY"
        price   = abs(net_price)

    payload = {
        "orders": [{
            "conidex":        conidex,
            "secType":        "BAG",
            "orderType":      "LMT",
            "price":          round(price, 2),
            "side":           side,
            "tif":            tif,
            "quantity":       quantity,
            "listingExchange": exchange,
            "outsideRTH":     False,
            "cOID":           f"{ticker}-{expiry}-{right}{short_strike}/{long_strike}-{_utcnow()[:10]}",
        }]
    }

    logger.info("Placing vertical spread: %s %s%s/%s %s @ %.2f x%d",
                ticker, right, short_strike, long_strike, expiry, net_price, quantity)

    result = await _place_with_confirmation(account_id, payload)
    ibkr_order_id = str(result.get("order_id", ""))
    status        = result.get("order_status", "unknown")

    # Persist to DB
    await _save_order(
        account_id=account_id, ticker=ticker,
        strategy=f"{'Credit' if is_credit else 'Debit'} {right.replace('P','Put').replace('C','Call')} Vertical",
        short_strike=short_strike, long_strike=long_strike,
        opt_type=right, expiry=expiry, net_price=net_price,
        quantity=quantity, ibkr_order_id=ibkr_order_id,
        status=status, raw=result,
    )
    return result


async def cancel_order(account_id: str, order_id: str) -> dict:
    return await _delete(f"/iserver/account/{account_id}/order/{order_id}")


async def get_orders() -> list[dict]:
    data = await _get("/iserver/account/orders")
    return data.get("orders", [])


async def order_history(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT id, timestamp, ticker, strategy, short_strike, long_strike, "
            "option_type, expiry, net_price, quantity, ibkr_order_id, status "
            "FROM ibkr_orders ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "ticker": r[2], "strategy": r[3],
         "short_strike": r[4], "long_strike": r[5], "option_type": r[6],
         "expiry": r[7], "net_price": r[8], "quantity": r[9],
         "ibkr_order_id": r[10], "status": r[11]}
        for r in rows
    ]


# ── High-level agent class ────────────────────────────────────────────────────

class IBKRAgent:
    """
    High-level agent wrapping the CP Gateway.
    All methods are async. The agent does not manage a background tickle thread —
    call tickle_loop() to keep the session alive from an external coroutine.
    """

    # ── Status ────────────────────────────────────────────────────────────────

    async def status(self) -> str:
        """Return a formatted status card — gateway, auth, account."""
        s = await auth_status()
        auth = s.get("authenticated", False)
        conn = s.get("connected",     False)
        err  = s.get("error", "")

        if err:
            return (
                f"<b>IBKR Gateway</b>  ❌ Unreachable\n\n"
                f"<code>{err}</code>\n\n"
                f"<i>Start the CP Gateway:\n"
                f"  cd clientportal.gw\n"
                f"  ./bin/run.sh root/conf.yaml\n"
                f"Then open https://localhost:5000 in your browser.</i>"
            )

        status_icon = "✅" if (auth and conn) else "⚠️"
        lines = [
            f"<b>IBKR Gateway</b>  {status_icon}",
            f"<code>Authenticated : {'✅ Yes' if auth else '❌ No'}</code>",
            f"<code>Connected     : {'✅ Yes' if conn else '❌ No'}</code>",
        ]

        if auth and conn:
            try:
                accounts = await get_accounts()
                lines.append(f"<code>Account(s)    : {', '.join(accounts)}</code>")
                if accounts:
                    acct = accounts[0]
                    pnl_data = await get_pnl()
                    upnl = pnl_data.get("upnl", {})
                    lines.append(f"\n<b>P&amp;L  —  {acct}</b>")
                    for key, val in upnl.items():
                        if isinstance(val, dict):
                            dpl  = val.get("dpl", 0)
                            upl  = val.get("upl", 0)
                            nl   = val.get("nl", 0)
                            sign = lambda v: "+" if v >= 0 else ""
                            lines.append(
                                f"<code>"
                                f"Day P&amp;L   {sign(dpl)}${dpl:,.2f}  │  "
                                f"Unreal.  {sign(upl)}${upl:,.2f}  │  "
                                f"Net Liq  ${nl:,.2f}"
                                f"</code>"
                            )
            except Exception as exc:
                lines.append(f"<i>Could not fetch account data: {exc}</i>")
        else:
            lines.append(
                f"\n<i>Open <code>https://localhost:5000</code> in your browser to log in.</i>"
            )

        return "\n".join(lines)

    # ── Execute a spread ──────────────────────────────────────────────────────

    async def execute_spread(
        self,
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
        Place a vertical spread and return a formatted confirmation/error message.
        net_price > 0 = credit received, < 0 = debit paid.
        """
        s = await auth_status()
        if not s.get("authenticated"):
            return (
                "❌ <b>Not authenticated.</b>\n"
                "Open <code>https://localhost:5000</code> and log in first."
            )

        accounts = await get_accounts()
        if not accounts:
            return "❌ No trading accounts found."
        account_id = accounts[0]

        is_credit  = net_price > 0
        right_word = "Put" if right.upper() == "P" else "Call"
        try:
            result = await place_vertical_spread(
                account_id=account_id,
                ticker=ticker, short_strike=short_strike,
                long_strike=long_strike, right=right,
                expiry=expiry, net_price=net_price,
                quantity=quantity, tif=tif,
            )
        except Exception as exc:
            logger.error("IBKRAgent.execute_spread failed: %s", exc)
            return f"❌ Order failed: <code>{exc}</code>"

        order_id = result.get("order_id", "—")
        status   = result.get("order_status", "—")
        icon     = "✅" if "submit" in status.lower() or "fill" in status.lower() else "⚠️"

        net_sign = "+" if is_credit else "-"
        return (
            f"{icon} <b>Order {'Submitted' if icon == '✅' else 'Status: ' + status}</b>\n\n"
            f"<b>The Trade</b>\n"
            f"  Sell a {right_word} at <b>${short_strike:.0f}</b>\n"
            f"  Buy a {right_word} at <b>${long_strike:.0f}</b> <i>(protection)</i>\n"
            f"  Expiring <b>{expiry}</b>  ·  {quantity} contract{'s' if quantity > 1 else ''}\n\n"
            f"<code>"
            f"Net {'credit' if is_credit else 'debit'}  {net_sign}${abs(net_price):.2f}/share  "
            f"({net_sign}${abs(int(net_price*100))}/contract)\n"
            f"IBKR Order ID   {order_id}\n"
            f"Status          {status}\n"
            f"TIF             {tif}\n"
            f"Account         {account_id}"
            f"</code>"
        )

    # ── Positions ─────────────────────────────────────────────────────────────

    async def positions_summary(self) -> str:
        s = await auth_status()
        if not s.get("authenticated"):
            return "❌ Not authenticated."

        accounts = await get_accounts()
        if not accounts:
            return "❌ No accounts found."
        account_id = accounts[0]

        try:
            positions = await get_positions(account_id)
        except Exception as exc:
            return f"❌ Could not fetch positions: <code>{exc}</code>"

        if not positions:
            return f"<b>Positions — {account_id}</b>\n<i>No open positions.</i>"

        lines = [f"<b>Positions — {account_id}</b>"]
        for p in positions[:20]:
            desc  = p.get("contractDesc") or p.get("conid", "?")
            qty   = p.get("position", 0)
            price = p.get("mktPrice", 0)
            val   = p.get("mktValue", 0)
            upnl  = p.get("unrealizedPnl", 0)
            sign  = "+" if upnl >= 0 else ""
            lines.append(
                f"<code>{desc:<28} {qty:>5}  ${price:>8.2f}  "
                f"${val:>10.2f}  {sign}${upnl:>8.2f}</code>"
            )
        return "\n".join(lines)

    # ── Order history ─────────────────────────────────────────────────────────

    async def orders_summary(self) -> str:
        history = await order_history(limit=10)
        if not history:
            return "<b>Order History</b>\n<i>No orders placed yet.</i>"

        lines = ["<b>Recent Orders</b>"]
        for o in history:
            ts   = o["timestamp"][:16].replace("T", " ")
            icon = "✅" if "fill" in (o["status"] or "").lower() else (
                   "🔄" if "submit" in (o["status"] or "").lower() else "⚫")
            net  = f"{'+' if o['net_price'] >= 0 else ''}{o['net_price']:.2f}"
            lines.append(
                f"{icon} <code>{ts}  {o['ticker']:>5} "
                f"${o['short_strike']:.0f}/{o['long_strike']:.0f} "
                f"{'P' if o['option_type']=='P' else 'C'}  {o['expiry']}  "
                f"{net}  x{o['quantity']}  {o['status'] or '?'}</code>"
            )
        return "\n".join(lines)

    # ── Keepalive ─────────────────────────────────────────────────────────────

    async def tickle_loop(self, interval: int = 55) -> None:
        """Coroutine that keeps the CP Gateway session alive. Run as a background task."""
        while True:
            await asyncio.sleep(interval)
            try:
                result = await tickle()
                if not result.get("authenticated"):
                    logger.warning("IBKR session no longer authenticated — attempting reauth")
                    await reauthenticate()
            except Exception as exc:
                logger.warning("IBKR tickle failed: %s", exc)
