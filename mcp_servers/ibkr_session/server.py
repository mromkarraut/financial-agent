"""
IBKR Session MCP Server  (ib_insync / TWS socket)

Manages IB Gateway connection lifecycle: status, keepalive, account summary.
Connects to IB Gateway at localhost:4002 (paper) or 4001 (live).

Prerequisites:
  1. Start IB Gateway: python start.py  (or run ibkr_gateway/start_ibgateway.bat)
  2. Log in via the IB Gateway GUI
  3. Enable API: Edit → Global Configuration → API → Settings → Socket port 4002

Tools:
  get_connection_status()          → IB Gateway connection + account + mode
  get_account_summary(acct_id)     → net liq, cash, buying power, margins
  list_accounts()                  → all managed accounts
  get_session_log(limit)           → recent events from agent memory

Memory: db/agents/ibkr_session.db
LLM:    configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from tools.ibkr_tws import connect_ib, get_account_id, is_paper_account, paper_label  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr_session.db")
CLIENT_ID = config.IBKR_CLIENT_ID_SESSION

_llm      = get_llm_client()
_db_ready = False
_keepalive_task: asyncio.Task | None = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS session_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                event         TEXT NOT NULL,
                connected     INTEGER,
                account       TEXT,
                detail        TEXT
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


async def _log(event: str, connected: bool | None = None, account: str = "", detail: str = "") -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO session_log (timestamp, event, connected, account, detail) VALUES (?,?,?,?,?)",
            (_utcnow(), event, int(connected) if connected is not None else None, account, detail),
        )
        await db.commit()


async def _log_call(tool: str, ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, duration_ms) VALUES (?,?,?)",
            (_utcnow(), tool, ms),
        )
        await db.commit()


async def _keepalive_loop() -> None:
    """Ping IB Gateway every 55s to keep the connection alive."""
    while True:
        await asyncio.sleep(55)
        try:
            ib = await connect_ib(CLIENT_ID)
            ib.reqCurrentTime()   # lightweight server ping
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Session keepalive failed: %s", exc)


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _keepalive_task
    await _ensure_db()
    _keepalive_task = asyncio.create_task(_keepalive_loop())
    yield
    if _keepalive_task:
        _keepalive_task.cancel()


mcp = FastMCP(
    name="ibkr-session",
    instructions=(
        "IB Gateway session manager via ib_insync TWS socket. "
        "Auto-runs a 55s keepalive. Shows account type (paper/live) and connection state."
    ),
    lifespan=lifespan,
)


@mcp.tool()
async def get_connection_status() -> str:
    """
    Check IB Gateway connection status, account ID, and trading mode.
    Attempts a live socket connection — shows clear error if gateway is not running.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib      = await connect_ib(CLIENT_ID)
        account = await get_account_id(ib)
        mode    = paper_label() if not account else (
            "📄 PAPER TRADING" if is_paper_account(account) else "⚠ LIVE TRADING — REAL MONEY"
        )
        accounts = ib.managedAccounts()
        ms = int((time.monotonic() - t0) * 1000)
        await _log("status_check", True, account)
        await _log_call("get_connection_status", ms)
        return (
            f"IB Gateway  ✅ Connected\n"
            f"{'─'*36}\n"
            f"Mode       : {mode}\n"
            f"Accounts   : {', '.join(accounts)}\n"
            f"Host:Port  : {config.IBKR_TWS_HOST}:{config.IBKR_TWS_PORT}\n"
            f"Client ID  : {CLIENT_ID}\n"
            f"Keepalive  : {'running ✅' if _keepalive_task and not _keepalive_task.done() else 'stopped'}\n"
            f"Latency    : {ms}ms"
        )
    except ConnectionError as exc:
        ms = int((time.monotonic() - t0) * 1000)
        await _log("status_check", False, "", str(exc))
        await _log_call("get_connection_status", ms)
        return (
            f"IB Gateway  ❌ Not connected\n\n"
            f"{exc}\n\n"
            f"Steps to fix:\n"
            f"  1. Run: python start.py  (starts IB Gateway automatically)\n"
            f"     OR:  run ibkr_gateway/start_ibgateway.bat\n"
            f"  2. Log in with {'paper' if config.IBKR_PAPER_TRADING else 'live'} credentials\n"
            f"  3. Enable API: Edit → Global Configuration → API → Settings\n"
            f"     Socket port: {config.IBKR_TWS_PORT}"
        )


@mcp.tool()
async def list_accounts() -> str:
    """List all managed accounts on this IB Gateway connection."""
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib       = await connect_ib(CLIENT_ID)
        accounts = ib.managedAccounts()
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("list_accounts", ms)
        lines = [f"Accounts ({len(accounts)}, {ms}ms):"]
        for a in accounts:
            mode = "📄 paper" if is_paper_account(a) else "⚠ live"
            lines.append(f"  {a}  [{mode}]")
        return "\n".join(lines)
    except ConnectionError as exc:
        return f"Not connected: {exc}"


@mcp.tool()
async def get_account_summary(account_id: str = "") -> str:
    """
    Fetch account summary: net liquidation, cash, buying power, margin.
    Defaults to first managed account if account_id is empty.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        ib = await connect_ib(CLIENT_ID)
        if not account_id:
            account_id = await get_account_id(ib)
        if not account_id:
            return "No accounts found."

        tags = ["NetLiquidation", "TotalCashValue", "BuyingPower",
                "EquityWithLoanValue", "MaintMarginReq", "InitMarginReq",
                "UnrealizedPnL", "RealizedPnL"]
        await ib.reqAccountSummaryAsync()
        vals_list = await ib.accountSummaryAsync(account_id)
        ms = int((time.monotonic() - t0) * 1000)
        await _log_call("get_account_summary", ms)

        col  = 22
        rows = {v.tag: v.value for v in vals_list if v.tag in tags and v.currency == "USD"}
        mode = "📄 PAPER" if is_paper_account(account_id) else "⚠ LIVE"
        lines = [f"Account Summary — {account_id}  [{mode}]  ({ms}ms)\n"]
        for tag in tags:
            val = rows.get(tag, "N/A")
            try:
                val = f"${float(val):,.2f}"
            except (ValueError, TypeError):
                pass
            lines.append(f"  {tag:<{col}} {val}")
        return "\n".join(lines)
    except ConnectionError as exc:
        return f"Not connected: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
async def get_session_log(limit: int = 20) -> str:
    """Return recent session events from agent memory."""
    await _ensure_db()
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, event, connected, account, detail "
            "FROM session_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return "No session events yet."
    lines = [f"Session log (last {limit}):\n"]
    for ts, event, conn, account, detail in rows:
        icon = "✅" if conn else "❌" if conn is not None else "—"
        lines.append(f"  [{ts[:16].replace('T',' ')}] {icon} {event:<20} {account or ''}  {detail[:60]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
