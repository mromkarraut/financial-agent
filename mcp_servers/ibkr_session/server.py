"""
IBKR Session MCP Server

Manages the CP Gateway session lifecycle: authentication checks, keepalive
tickle loop (auto-started on server launch), reauthentication, and account listing.

Gateway start command:
  cd ibkr_gateway && ./bin/run.sh root/conf.yaml
Then authenticate at https://localhost:5000 in Chrome.

Tools:
  get_auth_status()            → auth + connection state
  reauthenticate()             → re-open brokerage session (no browser needed if SSO valid)
  get_accounts()               → list account IDs
  get_account_summary(acct_id) → net liq, cash, buying power
  start_tickle_loop()          → start/confirm 55s keepalive (auto-runs on server start)

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

from agents.ibkr_agent import (  # noqa: E402
    auth_status, get_account_summary, get_accounts,
    reauthenticate, tickle,
)

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "ibkr_session.db")
_llm     = get_llm_client()

_db_ready    = False
_tickle_task: asyncio.Task | None = None


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
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                event       TEXT NOT NULL,
                authenticated INTEGER,
                connected   INTEGER,
                detail      TEXT
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


async def _log_session(event: str, auth: bool | None, connected: bool | None, detail: str = "") -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO session_log (timestamp, event, authenticated, connected, detail) VALUES (?,?,?,?,?)",
            (_utcnow(), event, int(auth) if auth is not None else None,
             int(connected) if connected is not None else None, detail),
        )
        await db.commit()


async def _log_call(tool: str, ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, duration_ms) VALUES (?,?,?)",
            (_utcnow(), tool, ms),
        )
        await db.commit()


async def _tickle_loop() -> None:
    """Keep the CP Gateway session alive. Runs every 55 seconds."""
    logger.info("IBKR tickle loop started")
    while True:
        await asyncio.sleep(55)
        try:
            result = await tickle()
            if not result.get("iserver", {}).get("authStatus", {}).get("authenticated", True):
                logger.warning("IBKR session expired — attempting reauth")
                await reauthenticate()
                await _log_session("reauth_attempt", None, None)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Tickle failed: %s", exc)


@asynccontextmanager
async def lifespan(server: FastMCP):
    global _tickle_task
    await _ensure_db()
    _tickle_task = asyncio.create_task(_tickle_loop())
    logger.info("IBKR session server started — tickle loop active")
    yield
    if _tickle_task:
        _tickle_task.cancel()


mcp = FastMCP(
    name="ibkr-session",
    instructions=(
        "IBKR CP Gateway session manager. Auto-runs a 55s tickle keepalive. "
        "Handles auth checks, reauthentication, and account listing."
    ),
    lifespan=lifespan,
)


@mcp.tool()
async def get_auth_status() -> str:
    """
    Check CP Gateway authentication and connection state.
    Returns auth status, connectivity, and a formatted status card.
    """
    t0 = time.monotonic()
    s  = await auth_status()
    ms = int((time.monotonic() - t0) * 1000)

    auth = s.get("authenticated", False)
    conn = s.get("connected", False)
    err  = s.get("error", "")

    await _log_session("status_check", auth, conn, err or "ok")
    await _log_call("get_auth_status", ms)

    if err:
        return (
            f"Gateway: UNREACHABLE\n"
            f"Error: {err}\n\n"
            f"Start the gateway:\n"
            f"  cd ibkr_gateway && ./bin/run.sh root/conf.yaml\n"
            f"Then open https://localhost:5000 in Chrome."
        )

    icon = "✅" if (auth and conn) else "❌"
    return (
        f"Gateway Status  {icon}\n\n"
        f"Authenticated : {'Yes ✅' if auth else 'No ❌'}\n"
        f"Connected     : {'Yes ✅' if conn else 'No ❌'}\n"
        f"Tickle loop   : {'running' if _tickle_task and not _tickle_task.done() else 'stopped'}\n"
        f"Checked in    : {ms}ms"
    )


@mcp.tool()
async def reauthenticate_session() -> str:
    """
    Re-open the brokerage session without requiring a browser login.
    Works if the SSO cookie is still valid (within the session window).
    """
    t0  = time.monotonic()
    res = await reauthenticate()
    ms  = int((time.monotonic() - t0) * 1000)
    await _log_session("reauthenticate", None, None, json.dumps(res))
    await _log_call("reauthenticate_session", ms)
    return f"Reauthentication result ({ms}ms):\n{json.dumps(res, indent=2)}"


@mcp.tool()
async def list_accounts() -> str:
    """List all IBKR account IDs linked to this gateway session."""
    t0       = time.monotonic()
    accounts = await get_accounts()
    ms       = int((time.monotonic() - t0) * 1000)
    await _log_call("list_accounts", ms)
    if not accounts:
        return "No accounts found. Are you authenticated?"
    return f"Accounts ({len(accounts)}):\n" + "\n".join(f"  • {a}" for a in accounts)


@mcp.tool()
async def get_account_details(account_id: str = "") -> str:
    """
    Fetch account summary: net liquidation value, cash balance, buying power,
    equity, and maintenance margin. Defaults to first account if account_id is empty.
    """
    t0 = time.monotonic()
    if not account_id:
        accounts = await get_accounts()
        if not accounts:
            return "No accounts found."
        account_id = accounts[0]

    summary = await get_account_summary(account_id)
    ms = int((time.monotonic() - t0) * 1000)
    await _log_call("get_account_details", ms)

    def _val(key: str) -> str:
        v = summary.get(key, {})
        if isinstance(v, dict):
            return f"${v.get('amount', v.get('value', 'N/A')):,.2f}" if isinstance(v.get('amount', v.get('value')), (int, float)) else str(v)
        return str(v)

    col = 22
    rows = [
        ("Account",          account_id),
        ("Net Liquidation",  _val("netliquidation")),
        ("Cash Balance",     _val("totalcashvalue")),
        ("Buying Power",     _val("buyingpower")),
        ("Equity w/ Loan",   _val("equitywithloanvalue")),
        ("Maint. Margin",    _val("maintenancemarginreq")),
        ("Init. Margin",     _val("initmarginreq")),
    ]
    lines = [f"Account Summary — {account_id}  ({ms}ms)\n"]
    lines += [f"  {label:<{col}} {value}" for label, value in rows]
    return "\n".join(lines)


@mcp.tool()
async def get_session_log(limit: int = 20) -> str:
    """Return recent session events (auth checks, reauths) from agent memory."""
    await _ensure_db()
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, event, authenticated, connected, detail "
            "FROM session_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return "No session events recorded yet."
    lines = [f"Session log (last {limit}, newest first):\n"]
    for ts, event, auth, conn, detail in rows:
        a = "✅" if auth else "❌" if auth is not None else "—"
        c = "✅" if conn else "❌" if conn is not None else "—"
        lines.append(f"  [{ts[:16].replace('T',' ')}]  {event:<20} auth={a} conn={c}  {detail[:60]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
