"""
Summarizer MCP Server

Independent agent for text processing: financial text summarization,
entity extraction, and sentiment classification — all via Claude Sonnet.

Tools:
  summarize_text(text)               → 3 bullet-point summary
  extract_financial_entities(text)   → tickers, companies, metrics, dates
  classify_market_sentiment(text)    → bullish/bearish/neutral with confidence + reasoning
  get_agent_health_summary()         → LLM narrative of system health from heartbeat.db

Memory: db/agents/summarizer.db  (independent from main state.db)
LLM:    claude-sonnet-4-6  (higher quality for NLP tasks)
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

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "summarizer.db")
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
            CREATE TABLE IF NOT EXISTS summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                input_len   INTEGER NOT NULL,
                tool        TEXT NOT NULL,
                output      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                input_len   INTEGER,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save_and_log(tool: str, input_len: int, output: str, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO summaries (timestamp, input_len, tool, output) VALUES (?,?,?,?)",
            (_utcnow(), input_len, tool, output),
        )
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, input_len, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, input_len, duration_ms),
        )
        await db.commit()


mcp = FastMCP(
    name="summarizer",
    instructions=(
        "Financial text processing: bullet-point summarization, entity extraction "
        "(tickers/companies/metrics), and market sentiment classification via Claude Sonnet."
    ),
)


@mcp.tool()
async def summarize_text(text: str) -> str:
    """
    Summarize financial text into exactly 3 bullet points covering the most
    investor-relevant facts. Works on earnings reports, news, research notes, etc.
    Each bullet starts with '• '.
    """
    await _ensure_db()
    t0 = time.monotonic()
    text = text.strip()
    if not text:
        return "No text provided."

    system = (
        "You are a concise financial analyst. Summarize the provided text into exactly "
        "3 bullet points, each starting with '• '. Focus on facts most relevant to investors: "
        "numbers, guidance, risks, catalysts. No filler. No disclaimers."
    )
    try:
        output = await _llm.complete(system, text, max_tokens=300)
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return f"Summarization failed: {exc}"

    duration = int((time.monotonic() - t0) * 1000)
    await _save_and_log("summarize_text", len(text), output, duration)
    return output


@mcp.tool()
async def extract_financial_entities(text: str) -> str:
    """
    Extract structured financial entities from text:
    - tickers (e.g. AAPL, $MSFT)
    - company names
    - financial metrics mentioned (revenue, EPS, margin, etc.)
    - dates and time periods
    Returns a JSON object with categorized entities.
    """
    await _ensure_db()
    t0 = time.monotonic()
    text = text.strip()
    if not text:
        return json.dumps({"error": "No text provided."})

    system = (
        "You are a financial NLP system. Extract entities from the text and return a JSON object "
        "with these keys: "
        "\"tickers\" (list of stock symbols), "
        "\"companies\" (list of company names), "
        "\"metrics\" (list of financial metrics/numbers mentioned, e.g. '${revenue}B revenue', 'EPS $X'), "
        "\"dates\" (list of time references), "
        "\"sentiment_keywords\" (list of bullish/bearish words). "
        "Return ONLY valid JSON, no prose."
    )
    try:
        raw = await _llm.complete(system, text, max_tokens=400)
        # Validate it's JSON
        parsed = json.loads(raw)
        output = json.dumps(parsed, indent=2)
    except json.JSONDecodeError:
        output = raw  # return raw if not valid JSON
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return json.dumps({"error": str(exc)})

    duration = int((time.monotonic() - t0) * 1000)
    await _save_and_log("extract_financial_entities", len(text), output, duration)
    return output


@mcp.tool()
async def classify_market_sentiment(text: str) -> str:
    """
    Classify the market sentiment of financial text as bullish, bearish, or neutral.
    Returns a structured result with: sentiment label, confidence (0-100),
    key signals found, and a 1-sentence reasoning.
    """
    await _ensure_db()
    t0 = time.monotonic()
    text = text.strip()
    if not text:
        return json.dumps({"error": "No text provided."})

    system = (
        "You are a financial sentiment classifier. Analyze the provided text and return a JSON "
        "object with these keys: "
        "\"sentiment\" (one of: bullish, bearish, neutral), "
        "\"confidence\" (integer 0-100), "
        "\"signals\" (list of 2-4 specific phrases/data points that drove the classification), "
        "\"reasoning\" (one sentence explaining the verdict). "
        "Return ONLY valid JSON."
    )
    try:
        raw = await _llm.complete(system, text, max_tokens=300)
        parsed = json.loads(raw)
        # Format nicely
        output = (
            f"Sentiment:  {parsed.get('sentiment', 'N/A').upper()}\n"
            f"Confidence: {parsed.get('confidence', 'N/A')}%\n"
            f"Signals:    {', '.join(parsed.get('signals', []))}\n"
            f"Reasoning:  {parsed.get('reasoning', '')}"
        )
    except json.JSONDecodeError:
        output = raw
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return f"Classification failed: {exc}"

    duration = int((time.monotonic() - t0) * 1000)
    await _save_and_log("classify_market_sentiment", len(text), output, duration)
    return output


@mcp.tool()
async def get_agent_health_summary() -> str:
    """
    Read the latest system health snapshot from heartbeat.db and generate
    a Claude Sonnet narrative describing the overall system state, which agents
    are active, any idle or erroring agents, and a recommended action.

    Requires the heartbeat agent's check_all_agents() to have been run at least once.
    Returns a plain-English status report plus the raw health table.
    """
    await _ensure_db()
    t0 = time.monotonic()

    heartbeat_db = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "db", "agents", "heartbeat.db",
    )

    if not os.path.exists(heartbeat_db):
        return (
            "No heartbeat data found. Run the heartbeat agent first:\n"
            "  python -m mcp_servers.heartbeat.server\n"
            "Then call check_all_agents()."
        )

    try:
        async with aiosqlite.connect(heartbeat_db) as db:
            async with db.execute(
                "SELECT timestamp, healthy_count, idle_count, error_count, agents_json "
                "FROM system_snapshots ORDER BY id DESC LIMIT 1"
            ) as cur:
                snap = await cur.fetchone()
    except Exception as exc:
        return f"Could not read heartbeat DB: {exc}"

    if not snap:
        return "Heartbeat DB found but no snapshots yet. Run check_all_agents() on the heartbeat server."

    ts, healthy, idle, errors, agents_json = snap
    agents = json.loads(agents_json)

    # Build raw table for display
    icons = {"healthy": "✅", "idle": "⏸ ", "error": "❌"}
    table_lines = [
        f"{'':2} {'Agent':<20} {'Status':>8}  {'Calls':>5}  Last Call",
        "─" * 60,
    ]
    for slug, info in agents.items():
        icon = icons.get(info["status"], "❓")
        last = (info.get("last_call") or "never")[:16].replace("T", " ")
        table_lines.append(
            f"{icon} {slug:<20} {info['status']:>8}  {info.get('call_count',0):>5}  {last}"
        )
    table = "\n".join(table_lines)

    # Build LLM context for narrative
    agent_summaries = "; ".join(
        f"{slug}={info['status']} ({info.get('call_count', 0)} calls)"
        for slug, info in agents.items()
    )
    prompt = (
        f"System health snapshot from {ts[:16].replace('T',' ')} UTC.\n"
        f"{healthy} healthy, {idle} idle, {errors} error out of {len(agents)} agents.\n"
        f"Per-agent: {agent_summaries}.\n\n"
        f"Write a 3-4 sentence system health narrative for an operator: overall state, "
        f"which agents are active, any concerns, and one recommended action if needed."
    )

    health_system = (
        "You are a DevOps assistant monitoring a financial AI agent system. "
        "Write a concise, actionable health narrative. Be specific about which agents "
        "are healthy vs idle. If all are idle, note that no requests have been made yet."
    )
    try:
        narrative = await _llm.complete(health_system, prompt, max_tokens=250)
    except Exception as exc:
        logger.warning("LLM call failed for health summary: %s", exc)
        narrative = (
            f"System snapshot at {ts[:16]}: {healthy}/{len(agents)} agents healthy, "
            f"{idle} idle, {errors} erroring. "
            + ("All systems nominal." if errors == 0 else f"Action needed: {errors} agent(s) in error state.")
        )

    output = (
        f"Agent System Health — {ts[:16].replace('T', ' ')} UTC\n\n"
        f"{table}\n\n"
        f"Summary: {healthy} healthy  {idle} idle  {errors} error\n\n"
        f"Narrative:\n{narrative}"
    )

    duration = int((time.monotonic() - t0) * 1000)
    await _save_and_log("get_agent_health_summary", len(agents_json), output, duration)
    return output


if __name__ == "__main__":
    mcp.run()
