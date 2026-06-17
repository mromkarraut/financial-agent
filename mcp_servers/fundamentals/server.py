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
import re
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp_servers.data_pull import get_fundamentals  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "fundamentals.db")
SYSTEM = (
    "You are a senior fundamental equity analyst. Given detailed financial data, write a "
    "rigorous multi-section analysis. Be specific — quote exact figures, compute ratios "
    "inline, explain what trends mean. No disclaimers, no generic filler."
)

_llm = get_llm_client()

_db_ready = False


def _output_usable(text: str) -> bool:
    if not text or len(text) < 80:
        return False
    words = text.split()
    repeats = sum(1 for a, b in zip(words, words[1:]) if a.lower() == b.lower())
    if repeats > 3:
        return False
    if text.count("?") / len(text) > 0.03:
        return False
    if re.search(r'[^\x00-\x7F]{4,}|[.,:;!?]{4,}', text):
        return False
    return True


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
    eps      = data.get("eps_ttm", "N/A")
    margin   = data.get("profit_margin_pct", "N/A")
    gmargin  = data.get("gross_margin_pct", "N/A")
    rev_yoy  = data.get("revenue_growth_yoy_pct", "N/A")
    de       = data.get("debt_to_equity", "N/A")
    sector   = data.get("sector", "N/A")
    name_str = data.get("company_name", ticker)
    mcap     = data.get("market_cap")
    mcap_str = f"${mcap/1e9:.1f}B" if mcap else "N/A"
    qtrs     = data.get("quarterly_revenues", [])

    qtr_lines = []
    for q in qtrs[-6:]:
        qoq = f" ({'+' if (q.get('qoq_pct') or 0) >= 0 else ''}{q.get('qoq_pct', 'N/A')}% QoQ)" if q.get("qoq_pct") is not None else ""
        qtr_lines.append(f"  {q['period']}: ${q['revenue_b']}B{qoq}")

    rev_trend = "N/A"
    if len(qtrs) >= 3:
        recent_qoq = [q["qoq_pct"] for q in qtrs[-3:] if q.get("qoq_pct") is not None]
        if recent_qoq:
            avg_qoq = sum(recent_qoq) / len(recent_qoq)
            rev_trend = "accelerating" if avg_qoq > 2 else ("decelerating" if avg_qoq < -2 else "stable")

    peg_note = "N/A"
    if pe != "N/A" and rev_yoy != "N/A":
        try:
            peg = float(pe) / float(rev_yoy) if float(rev_yoy) > 0 else None
            if peg is not None:
                peg_note = f"{peg:.1f}x"
        except (TypeError, ValueError):
            pass

    prompt = f"""Fundamental analysis for {name_str} ({ticker}) in {sector}.
Market Cap: {mcap_str}  |  EPS TTM: ${eps}

VALUATION
P/E (TTM): {pe}  |  Forward P/E: {fwd_pe}
PE/Revenue Growth ratio: {peg_note}

REVENUE
YoY Growth: {rev_yoy}%  |  Trend: {rev_trend}
Quarterly Revenue (last 6 quarters):
{chr(10).join(qtr_lines) if qtr_lines else "  No quarterly data available"}

PROFITABILITY
Gross Margin: {gmargin}%  |  Net Margin: {margin}%

BALANCE SHEET
Debt/Equity: {de}

Write a thorough analysis with EXACTLY these four sections. Use specific numbers throughout.

**VALUATION ASSESSMENT**
Is {name_str} cheap, fair, or expensive at {pe}x trailing and {fwd_pe}x forward P/E? What does the PE compression or expansion from trailing to forward imply about expected earnings growth? Interpret the PE/growth ratio ({peg_note}). Compare margins against typical {sector} benchmarks.

**REVENUE QUALITY**
Analyse the revenue trajectory. Walk through the most recent 3-4 quarters and call out any inflection or deceleration. Is {rev_trend} QoQ momentum a positive or negative signal at this valuation?

**PROFITABILITY AND EFFICIENCY**
What does {gmargin}% gross margin tell us about pricing power? How does the step-down from {gmargin}% gross to {margin}% net margin reflect cost structure? What does {de} debt/equity imply about financial leverage and risk?

**CATALYSTS AND RISKS**
Name 2 specific catalysts that could re-rate {name_str} higher and 2 specific risks. Be concrete — reference the actual numbers (e.g., what margin expansion is needed to justify the forward multiple, or what revenue growth would break the bull thesis)."""

    commentary = None
    try:
        raw = await _llm.complete(SYSTEM, prompt, max_tokens=700)
        if _output_usable(raw):
            commentary = raw
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
    if commentary is None:
        val = "stretched" if isinstance(pe, float) and pe > 30 else "fair" if isinstance(pe, float) and pe > 15 else "cheap"
        commentary = (
            f"Valuation: {pe}x trailing, {fwd_pe}x forward P/E — appears {val}. "
            f"Revenue growing {rev_yoy}% YoY ({rev_trend} trend) with {margin}% net margins."
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

    def _mcap_str(d: dict) -> str:
        mc = d.get("market_cap")
        return f"${mc/1e9:.1f}B" if mc else "N/A"

    company_details = "\n\n".join(
        f"{t} ({d.get('company_name', t)}) [{d.get('sector', 'N/A')}] MktCap {_mcap_str(d)}:\n"
        f"  P/E: {d.get('pe_ratio','N/A')}  Fwd P/E: {d.get('forward_pe','N/A')}  EPS: ${d.get('eps_ttm','N/A')}\n"
        f"  Rev YoY: {d.get('revenue_growth_yoy_pct','N/A')}%  Gross Margin: {d.get('gross_margin_pct','N/A')}%  Net Margin: {d.get('profit_margin_pct','N/A')}%\n"
        f"  D/E: {d.get('debt_to_equity','N/A')}"
        for t, d in valid
    )

    prompt = f"""Side-by-side fundamental comparison of {len(valid)} companies.

{company_details}

Write a thorough comparative analysis with EXACTLY these three sections:

**VALUATION COMPARISON**
Rank the companies by valuation attractiveness (cheapest to most expensive). For each, explain whether the multiple is justified by the growth rate and margins. Which offers the best value at current prices and why?

**GROWTH AND PROFITABILITY**
Compare revenue growth trajectories and margin profiles. Which company has the most durable competitive advantage implied by its gross margins? Which is compounding earnings fastest? Are any showing margin expansion or compression?

**RELATIVE INVESTMENT CASE**
If forced to rank these as long positions, what is the order and why? What is the biggest risk to the top-ranked name, and what would make you prefer one of the others instead?"""

    commentary = None
    try:
        raw = await _llm.complete(SYSTEM, prompt, max_tokens=500)
        if _output_usable(raw):
            commentary = raw
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
    if commentary is None:
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
