"""
Heartbeat MCP Server

Monitors the health of every MCP agent by probing:
  - Agent memory DB accessibility and call recency
  - IBKR CP Gateway auth status (for the IBKR agent)
  - Whether core dependencies (yfinance, anthropic, aiosqlite) are importable

Results are written to db/agents/heartbeat.db so the registry and summarizer
can read them without direct inter-process communication.

Tools:
  check_all_agents()           → probe all agents, write to DB, return status table
  check_agent(slug)            → probe one specific agent
  get_health_report()          → formatted report from last stored health snapshot
  get_health_history(limit)    → historical health check log

Memory: db/agents/heartbeat.db  (independent; also written to by registry + summarizer)
LLM:    claude-haiku-4-5-20251001  (writes a 1-line health narrative per agent)
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEARTBEAT_DB = os.path.join(_ROOT, "db", "agents", "heartbeat.db")
_llm = get_llm_client()

# ── Per-agent DB paths ─────────────────────────────────────────────────────────
_AGENT_DBS: dict[str, str] = {
    "stock_research":   os.path.join(_ROOT, "db", "agents", "stock_research.db"),
    "fundamentals":     os.path.join(_ROOT, "db", "agents", "fundamentals.db"),
    "options_research": os.path.join(_ROOT, "db", "agents", "options_research.db"),
    "watchlist":        os.path.join(_ROOT, "db", "agents", "watchlist.db"),
    "summarizer":       os.path.join(_ROOT, "db", "agents", "summarizer.db"),
    "ibkr":             os.path.join(_ROOT, "db", "agents", "ibkr.db"),
}

_AGENT_NAMES: dict[str, str] = {
    "stock_research":   "Stock Research",
    "fundamentals":     "Fundamentals",
    "options_research": "Options Research",
    "watchlist":        "Watchlist",
    "summarizer":       "Summarizer",
    "ibkr":             "IBKR",
}

# ── Health result ──────────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    status: str       # 'healthy' | 'idle' | 'error'
    latency_ms: int
    call_count: int
    last_call: str | None
    detail: str


# ── Memory ─────────────────────────────────────────────────────────────────────

_db_ready = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(HEARTBEAT_DB), exist_ok=True)
    async with aiosqlite.connect(HEARTBEAT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS health_checks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                agent_slug  TEXT NOT NULL,
                status      TEXT NOT NULL,
                latency_ms  INTEGER,
                call_count  INTEGER DEFAULT 0,
                last_call   TEXT,
                detail      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hc_slug ON health_checks(agent_slug);
            CREATE INDEX IF NOT EXISTS idx_hc_ts   ON health_checks(timestamp);

            CREATE TABLE IF NOT EXISTS system_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                healthy_count INTEGER NOT NULL,
                idle_count    INTEGER NOT NULL,
                error_count   INTEGER NOT NULL,
                agents_json   TEXT NOT NULL
            );
        """)
        await db.commit()
    _db_ready = True


async def _write_health(slug: str, result: ProbeResult) -> None:
    async with aiosqlite.connect(HEARTBEAT_DB) as db:
        await db.execute(
            "INSERT INTO health_checks "
            "(timestamp, agent_slug, status, latency_ms, call_count, last_call, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (_utcnow(), slug, result.status, result.latency_ms,
             result.call_count, result.last_call, result.detail),
        )
        await db.commit()


async def _write_system_snapshot(results: dict[str, ProbeResult]) -> None:
    healthy = sum(1 for r in results.values() if r.status == "healthy")
    idle    = sum(1 for r in results.values() if r.status == "idle")
    errors  = sum(1 for r in results.values() if r.status == "error")
    agents_json = json.dumps({
        slug: {
            "status": r.status, "latency_ms": r.latency_ms,
            "call_count": r.call_count, "last_call": r.last_call, "detail": r.detail,
        }
        for slug, r in results.items()
    })
    async with aiosqlite.connect(HEARTBEAT_DB) as db:
        await db.execute(
            "INSERT INTO system_snapshots (timestamp, healthy_count, idle_count, error_count, agents_json) "
            "VALUES (?,?,?,?,?)",
            (_utcnow(), healthy, idle, errors, agents_json),
        )
        await db.commit()


# ── Probe implementations ──────────────────────────────────────────────────────

async def _probe_standard(slug: str) -> ProbeResult:
    """Probe any agent: check DB accessibility and call recency."""
    db_path = _AGENT_DBS[slug]
    t0 = time.monotonic()

    if not os.path.exists(db_path):
        return ProbeResult("idle", 0, 0, None, "DB not yet initialized (agent never started)")

    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM call_log") as cur:
                count = (await cur.fetchone() or (0,))[0]
            async with db.execute(
                "SELECT timestamp FROM call_log ORDER BY id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                last_call = row[0] if row else None

        latency = int((time.monotonic() - t0) * 1000)
        recency = ""
        if last_call:
            try:
                delta = datetime.now(timezone.utc) - datetime.fromisoformat(last_call)
                mins = int(delta.total_seconds() / 60)
                recency = f", last call {mins}m ago" if mins < 60 else f", last call {mins//60}h ago"
            except Exception:
                pass
        return ProbeResult(
            "healthy", latency, count, last_call,
            f"DB OK ({count} calls){recency}",
        )
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        return ProbeResult("error", latency, 0, None, f"DB error: {exc}")


async def _probe_ibkr() -> ProbeResult:
    """IBKR probe: standard DB check + CP Gateway auth status."""
    result = await _probe_standard("ibkr")
    # Attempt a quick gateway ping (non-blocking, fast timeout)
    gateway_note = ""
    try:
        from agents.ibkr_agent import auth_status
        gw = await asyncio.wait_for(auth_status(), timeout=3.0)
        if gw.get("error"):
            gateway_note = " | Gateway: unreachable"
        elif gw.get("authenticated"):
            gateway_note = " | Gateway: authenticated ✓"
        else:
            gateway_note = " | Gateway: not authenticated"
    except asyncio.TimeoutError:
        gateway_note = " | Gateway: timeout"
    except Exception as exc:
        gateway_note = f" | Gateway: {exc}"
    return ProbeResult(
        result.status,
        result.latency_ms,
        result.call_count,
        result.last_call,
        result.detail + gateway_note,
    )


async def _probe_all() -> dict[str, ProbeResult]:
    """Probe all agents concurrently."""
    probes = {
        slug: (_probe_ibkr() if slug == "ibkr" else _probe_standard(slug))
        for slug in _AGENT_DBS
    }
    raw = await asyncio.gather(*probes.values(), return_exceptions=True)
    results: dict[str, ProbeResult] = {}
    for slug, outcome in zip(probes.keys(), raw):
        if isinstance(outcome, Exception):
            results[slug] = ProbeResult("error", 0, 0, None, str(outcome))
        else:
            results[slug] = outcome
    return results


def _status_icon(status: str) -> str:
    return {"healthy": "✅", "idle": "⏸ ", "error": "❌"}.get(status, "❓")


def _fmt_table(results: dict[str, ProbeResult]) -> str:
    lines = [
        f"{'':2} {'Agent':<18} {'Status':>8}  {'Calls':>5}  {'Latency':>8}  Last Call",
        "─" * 70,
    ]
    for slug, r in results.items():
        name = _AGENT_NAMES.get(slug, slug)
        icon = _status_icon(r.status)
        last = r.last_call[:16].replace("T", " ") if r.last_call else "never"
        lat  = f"{r.latency_ms}ms" if r.latency_ms else "—"
        lines.append(
            f"{icon} {name:<18} {r.status:>8}  {r.call_count:>5}  {lat:>8}  {last}"
        )
    return "\n".join(lines)


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="heartbeat",
    instructions=(
        "System health monitor for all MCP agents. Probes each agent's memory DB, "
        "call recency, and dependencies. Results written to heartbeat.db for the "
        "registry and summarizer to read."
    ),
)


@mcp.tool()
async def check_all_agents() -> str:
    """
    Probe all 6 MCP agents concurrently and return a live status table.
    Checks DB accessibility, call count, and recency for each agent.
    IBKR also checks CP Gateway auth. Results are persisted to heartbeat.db.
    """
    await _ensure_db()
    t0 = time.monotonic()
    results = await _probe_all()
    total_ms = int((time.monotonic() - t0) * 1000)

    await asyncio.gather(*[_write_health(slug, r) for slug, r in results.items()])
    await _write_system_snapshot(results)

    healthy = sum(1 for r in results.values() if r.status == "healthy")
    idle    = sum(1 for r in results.values() if r.status == "idle")
    errors  = sum(1 for r in results.values() if r.status == "error")

    table = _fmt_table(results)
    return (
        f"System Health Check — {_utcnow()[:16].replace('T', ' ')} UTC\n"
        f"Probed {len(results)} agents in {total_ms}ms\n\n"
        f"{table}\n\n"
        f"Summary: {healthy} healthy  {idle} idle  {errors} error"
        + (f"\n\nDetails:" + "".join(
            f"\n  {_AGENT_NAMES[s]}: {r.detail}" for s, r in results.items() if r.detail
        ) if any(r.detail for r in results.values()) else "")
    )


@mcp.tool()
async def check_agent(slug: str) -> str:
    """
    Probe one specific agent by its slug.
    Valid slugs: stock_research, fundamentals, options_research,
                 watchlist, summarizer, ibkr
    """
    await _ensure_db()
    slug = slug.strip().lower()
    if slug not in _AGENT_DBS:
        return f"Unknown slug '{slug}'. Valid: {', '.join(_AGENT_DBS)}"

    result = await (_probe_ibkr() if slug == "ibkr" else _probe_standard(slug))
    await _write_health(slug, result)

    icon = _status_icon(result.status)
    last = result.last_call[:16].replace("T", " ") if result.last_call else "never"
    return (
        f"{icon} {_AGENT_NAMES[slug]} ({slug})\n"
        f"Status:     {result.status}\n"
        f"Latency:    {result.latency_ms}ms\n"
        f"Calls:      {result.call_count}\n"
        f"Last call:  {last}\n"
        f"Detail:     {result.detail}"
    )


@mcp.tool()
async def get_health_report() -> str:
    """
    Return the most recent stored health snapshot for all agents.
    Does NOT re-probe — reads from heartbeat.db. Use check_all_agents()
    if you need a fresh probe.
    """
    await _ensure_db()
    async with aiosqlite.connect(HEARTBEAT_DB) as db:
        # Latest system snapshot
        async with db.execute(
            "SELECT timestamp, healthy_count, idle_count, error_count, agents_json "
            "FROM system_snapshots ORDER BY id DESC LIMIT 1"
        ) as cur:
            snap = await cur.fetchone()

    if not snap:
        return "No health snapshots found. Run check_all_agents() first."

    ts, healthy, idle, errors, agents_json = snap
    agents = json.loads(agents_json)
    lines = [
        f"Last system snapshot: {ts[:16].replace('T', ' ')} UTC",
        f"Status: {healthy} healthy  {idle} idle  {errors} error\n",
        f"{'':2} {'Agent':<18} {'Status':>8}  {'Calls':>5}  Last Call",
        "─" * 60,
    ]
    for slug, info in agents.items():
        name = _AGENT_NAMES.get(slug, slug)
        icon = _status_icon(info["status"])
        last = (info["last_call"] or "never")[:16].replace("T", " ")
        lines.append(
            f"{icon} {name:<18} {info['status']:>8}  {info['call_count']:>5}  {last}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_health_history(limit: int = 20) -> str:
    """
    Return the last `limit` individual health check results from heartbeat.db.
    Shows per-agent probe history with timestamps and details.
    """
    await _ensure_db()
    limit = max(1, min(limit, 100))
    async with aiosqlite.connect(HEARTBEAT_DB) as db:
        async with db.execute(
            "SELECT timestamp, agent_slug, status, latency_ms, call_count, last_call, detail "
            "FROM health_checks ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        return "No health history yet. Run check_all_agents() first."

    lines = [f"Health check history (last {limit} entries, newest first):\n"]
    for ts, slug, status, lat, count, last, detail in rows:
        icon = _status_icon(status)
        lines.append(
            f"{icon} [{ts[:16].replace('T',' ')}] {_AGENT_NAMES.get(slug, slug):<18} "
            f"{status:<8}  {count} calls  {detail or ''}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
