"""
Fundamentals MCP Server

Independent agent for company financial analysis: P/E, EPS, revenue trends,
margins, debt/equity — plus LLM-written investment perspective via Claude Haiku.

Tools:
  get_company_fundamentals(ticker)          → full fundamentals + LLM commentary
  compare_companies(tickers_csv)            → side-by-side multi-ticker comparison
  recall_fundamentals(ticker, limit)        → history from agent memory

Memory: db/agents/fundamentals.db  (independent from main state.db)
LLM:    claude-haiku-4-5-20251001
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

from tools.market_data import get_fundamentals  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "fundamentals.db")
SYSTEM = (
    "You are a fundamental analyst. Given a company's financial metrics, write a concise "
    "2-3 sentence investment perspective covering valuation, growth quality, and balance "
    "sheet strength. Be specific about the numbers. No disclaimers."
)

_llm = get_llm_client()

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
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                pe_ratio        REAL,
                revenue_growth  REAL,
                profit_margin   REAL,
                commentary      TEXT,
                output          TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snap_ticker ON snapshots(ticker);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save_snapshot(ticker: str, data: dict, commentary: str, output: str) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO snapshots (ticker, timestamp, pe_ratio, revenue_growth, "
            "profit_margin, commentary, output) VALUES (?,?,?,?,?,?,?)",
            (ticker, _utcnow(), data.get("pe_ratio"), data.get("revenue_growth_yoy_pct"),
             data.get("profit_margin_pct"), commentary, output),
        )
        await db.commit()


async def _log_call(tool: str, ticker: str, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, ticker, duration_ms),
        )
        await db.commit()


mcp = FastMCP(
    name="fundamentals",
    instructions=(
        "Company financial analysis: P/E, EPS, revenue growth, margins, debt/equity, "
        "quarterly revenue trend, and Claude Haiku investment perspective."
    ),
)


def _fmt_fundamentals(ticker: str, data: dict) -> str:
    name     = data.get("company_name", ticker)
    pe       = data.get("pe_ratio", "N/A")
    fwd_pe   = data.get("forward_pe", "N/A")
    eps      = data.get("eps_ttm", "N/A")
    margin   = data.get("profit_margin_pct", "N/A")
    gmargin  = data.get("gross_margin_pct", "N/A")
    rev_yoy  = data.get("revenue_growth_yoy_pct", "N/A")
    de       = data.get("debt_to_equity", "N/A")
    mcap     = data.get("market_cap")
    sector   = data.get("sector", "N/A")
    qtrs     = data.get("quarterly_revenues", [])
    mcap_str = f"${mcap/1e9:.1f}B" if mcap else "N/A"

    lines = [
        f"{name} ({ticker})  —  {sector}",
        f"Market Cap:     {mcap_str}",
        f"P/E (TTM):      {pe}   |  Forward P/E:   {fwd_pe}",
        f"EPS (TTM):      ${eps}",
        f"Revenue YoY:    {rev_yoy}%",
        f"Profit Margin:  {margin}%   |  Gross Margin:  {gmargin}%",
        f"Debt/Equity:    {de}",
    ]

    if qtrs:
        lines.append("\nQuarterly Revenue (B):")
        for q in qtrs[-4:]:
            qoq = f"  ({'+' if (q.get('qoq_pct') or 0) >= 0 else ''}{q.get('qoq_pct', 'N/A')}% QoQ)" if q.get("qoq_pct") is not None else ""
            lines.append(f"  {q['period']}  ${q['revenue_b']}B{qoq}")

    return "\n".join(lines)


@mcp.tool()
async def get_company_fundamentals(ticker: str) -> str:
    """
    Full company fundamentals with Claude's investment perspective.
    Covers P/E, forward P/E, EPS, revenue growth, profit/gross margins,
    debt/equity, and last 4 quarters of revenue.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    data = await get_fundamentals(ticker)
    if "error" in data:
        return f"Error fetching fundamentals for {ticker}: {data['error']}"

    metrics_text = _fmt_fundamentals(ticker, data)

    pe       = data.get("pe_ratio", "N/A")
    fwd_pe   = data.get("forward_pe", "N/A")
    margin   = data.get("profit_margin_pct", "N/A")
    rev_yoy  = data.get("revenue_growth_yoy_pct", "N/A")
    de       = data.get("debt_to_equity", "N/A")

    prompt = (
        f"{data.get('company_name', ticker)} ({ticker}):\n"
        f"P/E: {pe} (forward: {fwd_pe}), Revenue YoY: {rev_yoy}%, "
        f"Profit Margin: {margin}%, D/E: {de}.\n"
        f"Write a 2-3 sentence investment perspective."
    )

    try:
        commentary = await _llm.complete(SYSTEM, prompt, max_tokens=200)
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        val = "stretched" if isinstance(pe, float) and pe > 30 else "fair" if isinstance(pe, float) and pe > 15 else "cheap"
        commentary = (
            f"Valuation appears {val} at a {pe}x trailing P/E. "
            f"Revenue is growing at {rev_yoy}% YoY with {margin}% profit margins."
        )

    output = metrics_text + f"\n\nInvestment Perspective:\n{commentary}"
    await _save_snapshot(ticker, data, commentary, output)
    await _log_call("get_company_fundamentals", ticker, int((time.monotonic() - t0) * 1000))
    return output


@mcp.tool()
async def compare_companies(tickers_csv: str) -> str:
    """
    Side-by-side comparison of 2–4 companies.
    Pass tickers as comma-separated string e.g. "AAPL,MSFT,GOOGL".
    Claude provides a comparative investment take.
    """
    await _ensure_db()
    t0 = time.monotonic()
    tickers = [t.strip().upper() for t in tickers_csv.split(",") if t.strip()][:4]
    if len(tickers) < 2:
        return "Please provide at least 2 tickers, comma-separated."

    import asyncio
    results = await asyncio.gather(*[get_fundamentals(t) for t in tickers], return_exceptions=True)

    valid: list[tuple[str, dict]] = []
    errors: list[str] = []
    for ticker, result in zip(tickers, results):
        if isinstance(result, Exception) or "error" in result:
            errors.append(ticker)
        else:
            valid.append((ticker, result))

    if not valid:
        return f"Could not fetch fundamentals for any of: {', '.join(tickers)}"

    rows = [f"{'Metric':<20}" + "  ".join(f"{t:<10}" for t, _ in valid)]
    rows.append("─" * (20 + 12 * len(valid)))

    def _val(d: dict, key: str, fmt: str = "") -> str:
        v = d.get(key, "N/A")
        if v is None or v == "N/A":
            return "N/A       "
        try:
            return f"{float(v):{fmt}}"[:10].ljust(10) if fmt else str(v)[:10].ljust(10)
        except Exception:
            return str(v)[:10].ljust(10)

    metrics = [
        ("P/E (TTM)", "pe_ratio", ".1f"),
        ("Forward P/E", "forward_pe", ".1f"),
        ("Rev Growth %", "revenue_growth_yoy_pct", ".1f"),
        ("Profit Margin", "profit_margin_pct", ".1f"),
        ("Gross Margin", "gross_margin_pct", ".1f"),
        ("Debt/Equity", "debt_to_equity", ".1f"),
    ]
    for label, key, fmt in metrics:
        rows.append(f"{label:<20}" + "  ".join(_val(d, key, fmt) for _, d in valid))

    table = "\n".join(rows)

    prompt = (
        f"Compare these companies on valuation and growth:\n"
        + "\n".join(
            f"{t}: P/E {d.get('pe_ratio','N/A')}, Rev YoY {d.get('revenue_growth_yoy_pct','N/A')}%, "
            f"Margin {d.get('profit_margin_pct','N/A')}%"
            for t, d in valid
        )
        + "\nWrite 2-3 sentences comparing investment merit."
    )

    try:
        commentary = await _llm.complete(SYSTEM, prompt, max_tokens=250)
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        commentary = "Direct comparison: see metrics table above."

    suffix = f"\n\nErrors: {', '.join(errors)}" if errors else ""
    output = f"Comparison: {', '.join(t for t, _ in valid)}\n\n{table}\n\nAnalysis:\n{commentary}{suffix}"
    await _log_call("compare_companies", tickers_csv, int((time.monotonic() - t0) * 1000))
    return output


@mcp.tool()
async def recall_fundamentals(ticker: str, limit: int = 5) -> str:
    """
    Retrieve past fundamental snapshots for a ticker from agent memory.
    Returns most recent `limit` entries.
    """
    await _ensure_db()
    ticker = ticker.strip().upper()
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, pe_ratio, revenue_growth, profit_margin, commentary "
            "FROM snapshots WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (ticker, max(1, min(limit, 20))),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return f"No previous fundamental snapshots found for {ticker}."
    lines = [f"Past fundamentals for {ticker} (newest first):"]
    for ts, pe, rev, margin, commentary in rows:
        lines.append(
            f"\n[{ts[:10]}] P/E: {pe}  Rev YoY: {rev}%  Margin: {margin}%\n{commentary}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
