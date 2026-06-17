"""
HTML/CSS Agent MCP Server

Provides styled HTML component rendering for financial data.
All output uses the hc-* CSS design system in server.py.

Tools:
  render_metric_grid(metrics_json, title)        → styled metric badge grid
  render_data_table(headers_json, rows_json, title, row_classes_json)  → styled table
  render_strategy_card(strategy_json, price)     → full options strategy card
  render_legs_card(strategy_json)                → BUY/SELL leg pills
  render_alert(message, level)                   → info/warning/success/error strip
  render_section_card(title, body_html, icon)    → generic card wrapper
  recall_renders(context, limit)                 → past render history

Memory: db/agents/html_css.db
LLM:    none — pure HTML generation, no AI inference
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools import html_components as hc  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "html_css.db")

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
            CREATE TABLE IF NOT EXISTS renders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                context     TEXT,
                html        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_render_ctx ON renders(context);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save(tool: str, context: str, html: str) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO renders (timestamp, tool, context, html) VALUES (?,?,?,?)",
            (_utcnow(), tool, context, html),
        )
        await db.commit()


async def _log(tool: str, context: str, ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, context or "", ms),
        )
        await db.commit()


mcp = FastMCP(
    name="html-css",
    instructions=(
        "Styled HTML component renderer for financial agent UI. "
        "Returns hc-* CSS class components (metric grids, data tables, "
        "strategy cards, alert strips, section cards) that embed directly "
        "in .result-wrap. Requires plotly.js and the hc-* CSS from server.py."
    ),
)


@mcp.tool()
async def render_metric_grid(metrics_json: str, title: str = "") -> str:
    """
    Render a grid of labelled metric values.

    Args:
        metrics_json: JSON array of {"label","value","color"(opt: pos|neg|dim|yellow|blue)}
                      e.g. '[{"label":"POP","value":"65%","color":"pos"}]'
        title:        Optional card title (wraps grid in a section card)

    Returns styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        metrics = json.loads(metrics_json)
        html    = hc.metric_grid(metrics)
        if title:
            html = hc.section_card(title, html)
        await _save("render_metric_grid", title or "untitled", html)
        await _log("render_metric_grid", title, int((time.monotonic() - t0) * 1000))
        return html
    except Exception as exc:
        return hc.alert(f"render_metric_grid error: {exc}", "error")


@mcp.tool()
async def render_data_table(
    headers_json: str,
    rows_json: str,
    title: str = "",
    icon: str = "",
    row_classes_json: str = "[]",
) -> str:
    """
    Render a styled HTML data table.

    Args:
        headers_json:    JSON array of column header strings  e.g. '["Price","P&L","Note"]'
        rows_json:       JSON 2-D array of cell values        e.g. '[["$109","−$170","max loss"]]'
        title:           Optional section card title
        icon:            Optional emoji prefix for title
        row_classes_json: JSON array of CSS class strings per row
                         Available: hc-row-profit  hc-row-loss  hc-row-current
                                    hc-row-be      hc-row-max

    Returns styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        headers     = json.loads(headers_json)
        rows        = json.loads(rows_json)
        row_classes = json.loads(row_classes_json) if row_classes_json else []
        html        = hc.data_table(headers, rows, row_classes or None)
        if title:
            html = hc.section_card(title, html, icon)
        await _save("render_data_table", title or "table", html)
        await _log("render_data_table", title, int((time.monotonic() - t0) * 1000))
        return html
    except Exception as exc:
        return hc.alert(f"render_data_table error: {exc}", "error")


@mcp.tool()
async def render_strategy_card(strategy_json: str, current_price: float = 0.0) -> str:
    """
    Render a complete options strategy card: legs, key metrics, and profit table.

    Args:
        strategy_json: JSON dict of a strategy from OptionsResearchAgent.
                       Required keys: kind, name, buy_strike, sell_strike,
                       buy_price, sell_price, net, max_profit, max_loss,
                       breakeven, pop, p50, roc, pos_delta, pos_theta, dte, exp.
        current_price: Current underlying stock price (for profit table row marker).

    Returns combined styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        s    = json.loads(strategy_json)
        legs = hc.strategy_legs_card(s)
        metrics  = hc.strategy_metrics(s)
        pt   = hc.profit_table(s, current_price) if current_price else ""
        html = legs + "\n" + metrics + ("\n" + pt if pt else "")
        await _save("render_strategy_card", s.get("name", "strategy"), html)
        await _log("render_strategy_card", s.get("name"), int((time.monotonic() - t0) * 1000))
        return html
    except Exception as exc:
        return hc.alert(f"render_strategy_card error: {exc}", "error")


@mcp.tool()
async def render_legs_card(strategy_json: str) -> str:
    """
    Render just the BUY/SELL leg pills for a vertical spread strategy.

    Args:
        strategy_json: JSON dict with kind, buy_strike, sell_strike,
                       buy_price, sell_price, net, max_loss keys.

    Returns styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        s    = json.loads(strategy_json)
        html = hc.strategy_legs_card(s)
        await _save("render_legs_card", s.get("name", "legs"), html)
        await _log("render_legs_card", s.get("name"), int((time.monotonic() - t0) * 1000))
        return html
    except Exception as exc:
        return hc.alert(f"render_legs_card error: {exc}", "error")


@mcp.tool()
async def render_alert(message: str, level: str = "info") -> str:
    """
    Render a styled alert strip.

    Args:
        message: Alert text (HTML allowed)
        level:   info | warning | success | error

    Returns styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    html = hc.alert(message, level)
    await _save("render_alert", level, html)
    await _log("render_alert", level, int((time.monotonic() - t0) * 1000))
    return html


@mcp.tool()
async def render_section_card(title: str, body_html: str, icon: str = "") -> str:
    """
    Wrap arbitrary HTML in a titled section card.

    Args:
        title:     Card header text
        body_html: Inner HTML for the card body
        icon:      Optional emoji prefix in the header

    Returns styled HTML string.
    """
    await _ensure_db()
    t0 = time.monotonic()
    html = hc.section_card(title, body_html, icon)
    await _save("render_section_card", title, html)
    await _log("render_section_card", title, int((time.monotonic() - t0) * 1000))
    return html


@mcp.tool()
async def recall_renders(context: str = "", limit: int = 5) -> str:
    """
    Retrieve previously rendered HTML components from agent memory.

    Args:
        context: Filter by context/title substring (empty = all)
        limit:   Number of entries (default 5, max 20)

    Returns a text summary of past renders (not the full HTML).
    """
    await _ensure_db()
    limit = max(1, min(limit, 20))
    async with aiosqlite.connect(AGENT_DB) as db:
        if context:
            async with db.execute(
                "SELECT timestamp, tool, context FROM renders "
                "WHERE context LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{context}%", limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT timestamp, tool, context FROM renders ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
    if not rows:
        return "No renders found. Call render_* tools to generate styled HTML components."
    lines = [f"Past renders{f' matching \"{context}\"' if context else ''} (newest first):"]
    for ts, tool, ctx in rows:
        lines.append(f"  [{ts[:10]}]  {tool}  — {ctx or '(untitled)'}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
