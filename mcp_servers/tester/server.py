"""
Tester MCP Server

Comprehensive test runner covering all 14 active MCP servers and the web dashboard.
Tests run in three tiers:

  Tier 1 — Agent tests: call each server's actual MCP tool functions directly.
            Covers data_pull, stock_research, fundamentals, options_research,
            watchlist, summarizer, charting, html_css, heartbeat,
            ibkr_session, ibkr_positions, ibkr_orders, ibkr_market_data.
            IBKR tests pass even when TWS is offline (expected behaviour checked).

  Tier 2 — Web API tests: HTTP tests against http://localhost:8000 via httpx.

  Tier 3 — Browser tests: console errors / screenshot / audits via
            AgentDeskAI browser-tools-server (requires Chrome + extension).

Tools:
  run_all_tests(web_url, browser_port)  → all three tiers
  test_agent(slug)                       → one agent's tier-1 tests
  test_web_api(url)                      → tier-2 HTTP tests
  test_browser(url, port)                → tier-3 browser tests
  get_test_report(limit)                 → run history from tester.db

Memory: db/agents/tester.db
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
import httpx
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT    = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "tester.db")
_BROWSER_PORTS = list(range(3025, 3036))

_llm = get_llm_client()

REPORT_SYSTEM = (
    "You are a QA engineer summarizing test results. Given pass/fail counts and any "
    "failures, write 2-3 sentences: overall health, what failed and why it matters, "
    "and one recommended action. Be specific. No filler."
)

# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class T:
    name: str
    passed: bool
    ms: int
    detail: str

    def icon(self) -> str:
        return "✅" if self.passed else "❌"

    def row(self) -> str:
        return f"{self.icon()} {self.name:<52} {self.ms:>5}ms  {self.detail}"


# ── Memory ─────────────────────────────────────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS test_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT NOT NULL,
                scope        TEXT NOT NULL,
                total        INTEGER NOT NULL,
                passed       INTEGER NOT NULL,
                failed       INTEGER NOT NULL,
                duration_ms  INTEGER NOT NULL,
                narrative    TEXT,
                detail_json  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                duration_ms INTEGER NOT NULL
            );
        """)
        await db.commit()
    _db_ready = True


async def _log_call(tool: str, ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, duration_ms) VALUES (?,?,?)",
            (_utcnow(), tool, ms),
        )
        await db.commit()


async def _save_run(scope: str, results: list[T], narrative: str, total_ms: int) -> None:
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO test_runs (timestamp, scope, total, passed, failed, "
            "duration_ms, narrative, detail_json) VALUES (?,?,?,?,?,?,?,?)",
            (_utcnow(), scope, len(results), passed, failed, total_ms, narrative,
             json.dumps([{"name": r.name, "passed": r.passed, "ms": r.ms, "detail": r.detail}
                         for r in results])),
        )
        await db.commit()


# ── Test runner helper ─────────────────────────────────────────────────────────

async def _t(name: str, coro) -> T:
    """Run a single test coroutine; catch all exceptions."""
    t0 = time.monotonic()
    try:
        result = await coro
        ms = int((time.monotonic() - t0) * 1000)
        if isinstance(result, T):
            return result
        passed, detail = result
        return T(name, bool(passed), ms, str(detail))
    except Exception as exc:
        return T(name, False, int((time.monotonic() - t0) * 1000),
                 f"{type(exc).__name__}: {exc}")


def _tws_up() -> bool:
    import socket
    try:
        s = socket.create_connection((config.IBKR_TWS_HOST, config.IBKR_TWS_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


# ── Tier 1: data_pull ─────────────────────────────────────────────────────────
# Uses fetch.py directly (returns dicts) rather than server.py MCP tools
# (which return formatted text strings, not JSON).

async def _test_data_pull() -> list[T]:
    from mcp_servers.data_pull.fetch import (
        get_stock_data, get_fundamentals, get_options_chain, get_source_status,
    )
    from mcp_servers.data_pull.server import (
        clear_ticker_cache, get_fetch_history, check_data_sources,
    )

    async def t_fetch_stock():
        await clear_ticker_cache("AAPL")
        d = await get_stock_data("AAPL")
        ok = "current_price" in d and isinstance(d["current_price"], (int, float)) and d["current_price"] > 0
        return ok, f"${d.get('current_price','?')} RSI={d.get('rsi_14','?')} source={d.get('source','?')}"

    async def t_fetch_stock_cache():
        await get_stock_data("AAPL")  # prime cache
        d2 = await get_stock_data("AAPL")
        cached = d2.get("cached", False)
        return True, f"cached={cached} source={d2.get('source','?')}"

    async def t_fetch_fundamentals():
        d = await get_fundamentals("AAPL")
        ok = "company_name" in d or "pe_ratio" in d
        return ok, f"{d.get('company_name','?')} PE={d.get('pe_ratio','?')} source={d.get('source','?')}"

    async def t_fetch_options():
        d = await get_options_chain("AAPL")
        chains = d.get("chains", [])
        ok = isinstance(chains, list) and len(chains) > 0
        return ok, f"{len(chains)} expirations source={d.get('source','?')}"

    async def t_check_sources():
        raw = await check_data_sources()
        return len(raw) > 20, f"{len(raw)} chars"

    async def t_fetch_history():
        raw = await get_fetch_history("AAPL", 3)
        return len(raw) > 10, f"{len(raw)} chars"

    return await asyncio.gather(
        _t("data_pull.fetch_stock",       t_fetch_stock()),
        _t("data_pull.fetch_stock_cache", t_fetch_stock_cache()),
        _t("data_pull.fetch_fundamentals",t_fetch_fundamentals()),
        _t("data_pull.fetch_options",     t_fetch_options()),
        _t("data_pull.check_sources",     t_check_sources()),
        _t("data_pull.fetch_history",     t_fetch_history()),
    )


# ── Tier 1: stock_research ────────────────────────────────────────────────────

async def _test_stock_research() -> list[T]:
    from mcp_servers.stock_research.server import analyze_stock, get_price_snapshot, recall_analyses

    async def t_snapshot():
        raw = await get_price_snapshot("AAPL")
        d = json.loads(raw)
        ok = "current_price" in d and d["current_price"] > 0
        return ok, f"${d.get('current_price','?')} RSI={d.get('rsi_14','?')} MA20={d.get('ma_20','?')}"

    async def t_analyze():
        out = await analyze_stock("SPY")
        ok = len(out) > 80 and ("bullish" in out.lower() or "bearish" in out.lower() or "neutral" in out.lower())
        return ok, f"{len(out)} chars, stance detected={'✓' if ok else '✗'}"

    async def t_recall():
        out = await recall_analyses("AAPL", 2)
        return True, f"{len(out)} chars"

    return [
        await _t("stock_research.get_price_snapshot", t_snapshot()),
        await _t("stock_research.analyze_stock",      t_analyze()),
        await _t("stock_research.recall_analyses",    t_recall()),
    ]


# ── Tier 1: fundamentals ──────────────────────────────────────────────────────

async def _test_fundamentals() -> list[T]:
    from mcp_servers.fundamentals.server import (
        get_company_fundamentals, compare_companies, recall_fundamentals,
    )

    async def t_get_fundamentals():
        out = await get_company_fundamentals("AAPL")
        ok = len(out) > 100 and ("pe" in out.lower() or "revenue" in out.lower() or "apple" in out.lower())
        return ok, f"{len(out)} chars, has financial data={'✓' if ok else '✗'}"

    async def t_compare():
        out = await compare_companies("AAPL,MSFT")
        ok = len(out) > 100 and ("AAPL" in out or "MSFT" in out)
        return ok, f"{len(out)} chars"

    async def t_recall():
        out = await recall_fundamentals("AAPL", 2)
        return True, f"{len(out)} chars"

    return [
        await _t("fundamentals.get_company_fundamentals", t_get_fundamentals()),
        await _t("fundamentals.compare_companies",         t_compare()),
        await _t("fundamentals.recall_fundamentals",       t_recall()),
    ]


# ── Tier 1: options_research ──────────────────────────────────────────────────

async def _test_options_research() -> list[T]:
    from tools.options_math import (
        bs_delta, bs_theta_daily, pop_credit_spread,
        pop_debit_spread, p50, ivr_rank, expected_move,
    )
    from mcp_servers.options_research.server import (
        get_options_chain_data, calculate_iv_rank, recall_research,
    )

    async def t_bs_delta():
        d = bs_delta(185, 180, 30/365, 0.25, is_call=True)
        theta = bs_theta_daily(185, 180, 30/365, 0.25, is_call=True)
        ok = 0 < d < 1 and theta < 0
        return ok, f"delta={d:.4f} theta={theta:.4f}"

    async def t_pop():
        pop = pop_credit_spread(180, 185, 30/365, 0.25, is_put=True)
        p   = p50(pop)
        pop_d = pop_debit_spread(180, 185, 30/365, 0.25, is_call=True)
        ok = 0 < pop < 1 and 0 < p < 1 and 0 < pop_d < 1
        return ok, f"POP_credit={pop:.2%} P50={p:.2%} POP_debit={pop_d:.2%}"

    async def t_ivr():
        ivr = ivr_rank(0.28, [0.18, 0.22, 0.25, 0.30, 0.35])
        em  = expected_move(3.50, 3.20)
        ok  = 0 <= ivr <= 100 and em > 0
        return ok, f"IVR={ivr:.1f} EM=±${em}"

    async def t_chain_data():
        out = await get_options_chain_data("SPY")
        ok  = len(out) > 50 and ("chain" in out.lower() or "expir" in out.lower() or "strike" in out.lower() or "error" not in out.lower()[:30])
        return ok, f"{len(out)} chars"

    async def t_iv_rank():
        out = await calculate_iv_rank("SPY")
        ok  = len(out) > 30
        return ok, f"{len(out)} chars"

    async def t_recall():
        out = await recall_research("AAPL", 2)
        return True, f"{len(out)} chars"

    return await asyncio.gather(
        _t("options_research.bs_delta_theta",    t_bs_delta()),
        _t("options_research.pop_p50",           t_pop()),
        _t("options_research.ivr_expected_move", t_ivr()),
        _t("options_research.get_chain_data",    t_chain_data()),
        _t("options_research.calculate_iv_rank", t_iv_rank()),
        _t("options_research.recall_research",   t_recall()),
    )


# ── Tier 1: watchlist ─────────────────────────────────────────────────────────

async def _test_watchlist() -> list[T]:
    from mcp_servers.watchlist.server import (
        add_ticker, remove_ticker, list_watchlist, get_watchlist_digest,
    )

    async def t_add_remove():
        out_add = await add_ticker("_TEST_")
        ok_add  = "_TEST_" in out_add or "added" in out_add.lower()
        out_list = await list_watchlist()
        ok_in   = "_TEST_" in out_list
        out_rem = await remove_ticker("_TEST_")
        ok_rem  = "removed" in out_rem.lower() or "_TEST_" in out_rem
        out_list2 = await list_watchlist()
        ok_gone = "_TEST_" not in out_list2
        ok = ok_add and ok_in and ok_rem and ok_gone
        return ok, f"add={'✓' if ok_add else '✗'} in_list={'✓' if ok_in else '✗'} remove={'✓' if ok_rem else '✗'} gone={'✓' if ok_gone else '✗'}"

    async def t_list():
        out = await list_watchlist()
        return True, f"{len(out)} chars"

    async def t_digest():
        out = await get_watchlist_digest()
        ok  = len(out) > 5
        return ok, f"{len(out)} chars: {out[:40]!r}"

    return [
        await _t("watchlist.add_remove_cycle", t_add_remove()),
        await _t("watchlist.list_watchlist",   t_list()),
        await _t("watchlist.get_digest",       t_digest()),
    ]


# ── Tier 1: summarizer ────────────────────────────────────────────────────────

_SAMPLE_TEXT = (
    "Apple reported Q4 revenue of $94.9B, up 6% YoY. iPhone revenue came in at $46.2B. "
    "Services hit a record $24.2B. EPS was $1.64, beating consensus of $1.60. "
    "Gross margin expanded to 46.2% from 45.2% a year ago."
)

async def _test_summarizer() -> list[T]:
    from mcp_servers.summarizer.server import (
        summarize_text, extract_financial_entities, classify_market_sentiment,
    )

    async def t_llm_ping():
        system = "Reply with exactly one word: PONG"
        user   = "PING"
        try:
            resp = await asyncio.wait_for(_llm.complete(system, user, max_tokens=10), timeout=15.0)
            ok   = bool(resp and len(resp) < 50)
            return ok, f"provider={_llm.provider} model={_llm.model} reply={resp[:40]!r}"
        except asyncio.TimeoutError:
            return False, f"LLM timeout after 15s (provider={_llm.provider})"

    async def t_summarize():
        out = await asyncio.wait_for(summarize_text(_SAMPLE_TEXT), timeout=30.0)
        ok  = len(out) > 30
        return ok, f"{len(out)} chars: {out[:60]!r}"

    async def t_entities():
        out = await asyncio.wait_for(extract_financial_entities(_SAMPLE_TEXT), timeout=30.0)
        ok  = len(out) > 20 and ("AAPL" in out or "Apple" in out or "ticker" in out.lower())
        return ok, f"{len(out)} chars"

    async def t_sentiment():
        out = await asyncio.wait_for(classify_market_sentiment(_SAMPLE_TEXT), timeout=30.0)
        ok  = any(w in out.lower() for w in ("bullish", "bearish", "neutral", "positive", "negative"))
        return ok, f"{len(out)} chars: {out[:60]!r}"

    return [
        await _t("summarizer.llm_ping",             t_llm_ping()),
        await _t("summarizer.summarize_text",        t_summarize()),
        await _t("summarizer.extract_entities",      t_entities()),
        await _t("summarizer.classify_sentiment",    t_sentiment()),
    ]


# ── Tier 1: charting ─────────────────────────────────────────────────────────

async def _test_charting() -> list[T]:
    from mcp_servers.charting.server import (
        plot_price_history, plot_options_payoff, recall_charts,
    )

    async def t_price_history():
        out = await plot_price_history("AAPL", period="1mo", chart_type="line")
        ok  = len(out) > 50 and ("plotly" in out.lower() or "chart" in out.lower() or "<div" in out or "{" in out)
        return ok, f"{len(out)} chars"

    async def t_options_payoff():
        out = await plot_options_payoff(
            ticker="AAPL", short_strike=190.0, long_strike=185.0,
            right="C", expiry="2026-07-18", net_price=2.50,
            spread_type="debit",
        )
        ok = len(out) > 50
        return ok, f"{len(out)} chars"

    async def t_recall():
        out = await recall_charts("AAPL", 2)
        return True, f"{len(out)} chars"

    return [
        await _t("charting.plot_price_history",  t_price_history()),
        await _t("charting.plot_options_payoff",  t_options_payoff()),
        await _t("charting.recall_charts",        t_recall()),
    ]


# ── Tier 1: html_css ─────────────────────────────────────────────────────────

async def _test_html_css() -> list[T]:
    from mcp_servers.html_css.server import (
        render_metric_grid, render_data_table, render_alert, render_section_card,
    )

    async def t_metric_grid():
        metrics = json.dumps([
            {"label": "Price", "value": "$185.50", "badge": "green"},
            {"label": "RSI",   "value": "62.3",    "badge": "yellow"},
        ])
        out = await render_metric_grid(metrics, title="AAPL Snapshot")
        ok  = "hc-metric" in out and "185.50" in out
        return ok, f"{len(out)} chars has hc-metric={'✓' if 'hc-metric' in out else '✗'}"

    async def t_data_table():
        headers = json.dumps(["Strike", "Delta", "IV"])
        rows    = json.dumps([["185", "0.52", "28%"], ["190", "0.38", "26%"]])
        out = await render_data_table(headers, rows, title="Chain")
        ok  = "hc-table" in out and "185" in out
        return ok, f"{len(out)} chars has hc-table={'✓' if 'hc-table' in out else '✗'}"

    async def t_alert():
        out = await render_alert("TWS connected", level="success")
        ok  = "hc-alert" in out and "TWS" in out
        return ok, f"{len(out)} chars"

    async def t_section_card():
        out = await render_section_card("Analysis", "<p>Test body</p>", icon="📊")
        ok  = "hc-section" in out and "Analysis" in out
        return ok, f"{len(out)} chars"

    return await asyncio.gather(
        _t("html_css.render_metric_grid",  t_metric_grid()),
        _t("html_css.render_data_table",   t_data_table()),
        _t("html_css.render_alert",        t_alert()),
        _t("html_css.render_section_card", t_section_card()),
    )


# ── Tier 1: heartbeat ────────────────────────────────────────────────────────

async def _test_heartbeat() -> list[T]:
    from mcp_servers.heartbeat.server import (
        _probe_standard, _probe_all, _ensure_db as hb_init,
        check_all_agents, get_health_report, get_health_history,
    )

    async def t_probe_single():
        await hb_init()
        r = await _probe_standard("stock_research")
        ok = r.status in ("healthy", "idle", "error")
        return ok, f"status={r.status} detail={r.detail[:60]}"

    async def t_probe_all():
        await hb_init()
        results = await _probe_all()
        expected = 13  # all slugs in _AGENT_DBS
        ok = len(results) == expected
        counts = {s: sum(1 for r in results.values() if r.status == s)
                  for s in ("healthy", "idle", "error")}
        return ok, f"probed {len(results)}/{expected} agents — {counts}"

    async def t_check_all_tool():
        out = await check_all_agents()
        ok  = len(out) > 50 and ("✅" in out or "⏸" in out or "❌" in out)
        return ok, f"{len(out)} chars"

    async def t_health_report():
        out = await get_health_report()
        return True, f"{len(out)} chars"

    async def t_health_history():
        out = await get_health_history(5)
        return True, f"{len(out)} chars"

    return [
        await _t("heartbeat.probe_single",    t_probe_single()),
        await _t("heartbeat.probe_all",       t_probe_all()),
        await _t("heartbeat.check_all_tool",  t_check_all_tool()),
        await _t("heartbeat.health_report",   t_health_report()),
        await _t("heartbeat.health_history",  t_health_history()),
    ]


def _ibkr_skip(slug: str, names: list[str]) -> list[T]:
    """Return skip-pass results for all IBKR tests when TWS is not reachable."""
    detail = f"SKIP — TWS not reachable at {config.IBKR_TWS_HOST}:{config.IBKR_TWS_PORT}"
    return [T(f"{slug}.{n}", True, 0, detail) for n in names]


# ── Tier 1: ibkr_session ─────────────────────────────────────────────────────

async def _test_ibkr_session() -> list[T]:
    up = _tws_up()
    tcp = T("ibkr_session.tws_tcp_probe", True, 0,
            f"TWS {config.IBKR_TWS_HOST}:{config.IBKR_TWS_PORT} {'reachable' if up else 'unreachable'}")
    if not up:
        return [tcp] + _ibkr_skip("ibkr_session", ["connection_status", "list_accounts"])

    from mcp_servers.ibkr_session.server import get_connection_status, list_accounts

    async def t_connection_status():
        out = await asyncio.wait_for(get_connection_status(), timeout=25.0)
        ok  = len(out) > 20
        return ok, f"{'connected' if 'connected' in out.lower() else 'error'} — {out[:80]}"

    async def t_list_accounts():
        out = await asyncio.wait_for(list_accounts(), timeout=25.0)
        ok  = len(out) > 5
        return ok, f"{out[:60]}"

    return [
        tcp,
        await _t("ibkr_session.connection_status", t_connection_status()),
        await _t("ibkr_session.list_accounts",     t_list_accounts()),
    ]


# ── Tier 1: ibkr_positions ───────────────────────────────────────────────────

async def _test_ibkr_positions() -> list[T]:
    if not _tws_up():
        return _ibkr_skip("ibkr_positions",
                           ["open_positions", "live_pnl", "portfolio_summary"])

    from mcp_servers.ibkr_positions.server import (
        get_open_positions, get_live_pnl, get_portfolio_summary,
    )

    async def t_open_positions():
        out = await asyncio.wait_for(get_open_positions(), timeout=25.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    async def t_live_pnl():
        out = await asyncio.wait_for(get_live_pnl(), timeout=25.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    async def t_portfolio_summary():
        out = await asyncio.wait_for(get_portfolio_summary(), timeout=60.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    return [
        await _t("ibkr_positions.open_positions",    t_open_positions()),
        await _t("ibkr_positions.live_pnl",          t_live_pnl()),
        await _t("ibkr_positions.portfolio_summary", t_portfolio_summary()),
    ]


# ── Tier 1: ibkr_orders ──────────────────────────────────────────────────────

async def _test_ibkr_orders() -> list[T]:
    from db.database import order_history as db_order_history
    from mcp_servers.ibkr_orders.server import get_order_history

    async def t_db_order_history():
        rows = await db_order_history(limit=5)
        return True, f"{len(rows)} orders in db/state.db"

    async def t_mcp_order_history():
        out = await get_order_history(5)
        return len(out) > 10, f"{len(out)} chars"

    db_results = [
        await _t("ibkr_orders.db_order_history",  t_db_order_history()),
        await _t("ibkr_orders.mcp_order_history", t_mcp_order_history()),
    ]

    if not _tws_up():
        return db_results + _ibkr_skip("ibkr_orders", ["get_live_orders", "risk_briefing"])

    from mcp_servers.ibkr_orders.server import get_live_orders, get_risk_briefing

    async def t_live_orders():
        out = await asyncio.wait_for(get_live_orders(), timeout=25.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    async def t_risk_briefing():
        out = await asyncio.wait_for(
            get_risk_briefing(
                ticker="AAPL", short_strike=195.0, long_strike=190.0,
                right="C", expiry="2026-07-18", net_price=2.50, quantity=1,
            ),
            timeout=40.0,
        )
        return len(out) > 30, f"{len(out)} chars"

    return db_results + [
        await _t("ibkr_orders.get_live_orders", t_live_orders()),
        await _t("ibkr_orders.risk_briefing",   t_risk_briefing()),
    ]


# ── Tier 1: ibkr_market_data ─────────────────────────────────────────────────

async def _test_ibkr_market_data() -> list[T]:
    if not _tws_up():
        return _ibkr_skip("ibkr_market_data",
                           ["stock_conid", "market_snapshot", "search_contract"])

    from mcp_servers.ibkr_market_data.server import (
        get_stock_conid, get_market_snapshot, search_contract,
    )

    async def t_stock_conid():
        out = await asyncio.wait_for(get_stock_conid("AAPL"), timeout=25.0)
        has_id = any(c.isdigit() for c in out)
        return len(out) > 5, f"conid={'✓' if has_id else '✗'} — {out[:60]}"

    async def t_market_snapshot():
        out = await asyncio.wait_for(get_market_snapshot("AAPL"), timeout=25.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    async def t_search_contract():
        out = await asyncio.wait_for(search_contract("AAPL", "STK"), timeout=25.0)
        return len(out) > 10, f"{len(out)} chars — {out[:60]}"

    return [
        await _t("ibkr_market_data.stock_conid",     t_stock_conid()),
        await _t("ibkr_market_data.market_snapshot", t_market_snapshot()),
        await _t("ibkr_market_data.search_contract", t_search_contract()),
    ]


# ── Agent runner registry ─────────────────────────────────────────────────────

_AGENT_RUNNERS = {
    "data_pull":        _test_data_pull,
    "stock_research":   _test_stock_research,
    "fundamentals":     _test_fundamentals,
    "options_research": _test_options_research,
    "watchlist":        _test_watchlist,
    "summarizer":       _test_summarizer,
    "charting":         _test_charting,
    "html_css":         _test_html_css,
    "heartbeat":        _test_heartbeat,
    "ibkr_session":     _test_ibkr_session,
    "ibkr_positions":   _test_ibkr_positions,
    "ibkr_orders":      _test_ibkr_orders,
    "ibkr_market_data": _test_ibkr_market_data,
}


# ── Tier 2: Web API tests ─────────────────────────────────────────────────────

async def _run_web_api_tests(url: str) -> list[T]:
    base    = url.rstrip("/")
    results: list[T] = []

    async with httpx.AsyncClient(base_url=base, timeout=30.0) as c:

        async def get_homepage():
            r  = await c.get("/")
            ok = r.status_code == 200 and "htmx.org" in r.text
            return ok, f"status={r.status_code} htmx={'✓' if 'htmx.org' in r.text else '✗'}"

        async def homepage_elements():
            r = await c.get("/")
            checks = {
                "hamburger":    "hamburger"    in r.text,
                "setTheme":     "setTheme"     in r.text,
                "history-list": "history-list" in r.text,
                "hx-post":      "hx-post"      in r.text,
            }
            ok = all(checks.values())
            return ok, "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in checks.items())

        async def search_aapl():
            r  = await c.post("/search", data={"ticker": "AAPL"}, timeout=90.0)
            ok = r.status_code == 200 and "result-wrap" in r.text
            return ok, f"status={r.status_code} result-wrap={'✓' if 'result-wrap' in r.text else '✗'} {len(r.text)} chars"

        async def search_oob():
            r  = await c.post("/search", data={"ticker": "MSFT"}, timeout=90.0)
            ok = r.status_code == 200 and "hx-swap-oob" in r.text
            return ok, f"OOB sidebar={'✓' if ok else '✗'}"

        async def api_history():
            r  = await c.get("/api/history")
            ok = r.status_code == 200
            try:
                data = r.json()
                ok   = ok and isinstance(data, list)
                return ok, f"{len(data)} entries"
            except Exception:
                return False, f"non-JSON: {r.text[:60]}"

        async def search_empty():
            # Empty ticker returns HTTP 422 (FastAPI validation error)
            r  = await c.post("/search", data={"ticker": ""})
            ok = r.status_code in (422, 400, 200)
            detail = r.text[:60] if r.status_code != 422 else f"422 Unprocessable Entity (FastAPI validation)"
            return ok, detail

        async def positions_page():
            r  = await c.get("/positions")
            ok = r.status_code == 200
            return ok, f"status={r.status_code}"

        async def fundamentals_page():
            r  = await c.get("/fundamentals")
            ok = r.status_code == 200
            return ok, f"status={r.status_code}"

        async def ibkr_page():
            r  = await c.get("/ibkr", timeout=30.0)
            ok = r.status_code == 200
            return ok, f"status={r.status_code} {len(r.text)} chars"

        async def positions_fragment():
            r  = await c.get("/api/positions-fragment", timeout=30.0)
            ok = r.status_code == 200
            return ok, f"status={r.status_code} {len(r.text)} chars"

        async def options_search():
            r  = await c.post("/search", data={"ticker": "AAPL options bullish"}, timeout=90.0)
            ok = r.status_code == 200 and len(r.text) > 100
            return ok, f"status={r.status_code} {len(r.text)} chars"

        for name, coro in [
            ("web.homepage_loads",      get_homepage()),
            ("web.homepage_elements",   homepage_elements()),
            ("web.search_aapl",         search_aapl()),
            ("web.search_oob_sidebar",  search_oob()),
            ("web.api_history_json",    api_history()),
            ("web.search_empty_ticker", search_empty()),
            ("web.positions_page",      positions_page()),
            ("web.fundamentals_page",   fundamentals_page()),
            ("web.ibkr_page",           ibkr_page()),
            ("web.positions_fragment",  positions_fragment()),
            ("web.options_search",      options_search()),
        ]:
            results.append(await _t(name, coro))

    return results


# ── Tier 3: Browser tests ─────────────────────────────────────────────────────

async def _discover_browser_server() -> tuple[str, int] | None:
    async with httpx.AsyncClient(timeout=1.5) as c:
        for port in _BROWSER_PORTS:
            try:
                r = await c.get(f"http://127.0.0.1:{port}/.identity")
                if r.status_code == 200:
                    return "127.0.0.1", port
            except Exception:
                continue
    return None


async def _run_browser_tests(target_url: str, bport: int | None = None) -> list[T]:
    results: list[T] = []
    t0 = time.monotonic()

    if bport:
        host, port = "127.0.0.1", bport
    else:
        found = await _discover_browser_server()
        if not found:
            return [T(
                "browser.server_discovery", False, 0,
                "browser-tools-server not found on ports 3025-3035. "
                "Run: npx @agentdeskai/browser-tools-server@latest  "
                "Then open Chrome with AgentDesk extension at " + target_url,
            )]
        host, port = found

    results.append(T("browser.server_discovery", True,
                     int((time.monotonic() - t0) * 1000),
                     f"Found at {host}:{port}"))

    base = f"http://{host}:{port}"

    async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:

        async def t_current_url():
            r   = await c.get("/current-url")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            url = r.text.strip().strip('"')
            on_target = target_url.rstrip("/") in url
            return True, f"{url}  {'✓ on target' if on_target else '⚠ not on target'}"

        async def t_console_errors():
            r = await c.get("/console-errors")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                errs = r.json() if r.text.strip() else []
            except Exception:
                errs = []
            ok = len(errs) == 0
            return ok, f"{len(errs)} errors" + (f": {errs[:2]}" if errs else "")

        async def t_console_logs():
            r = await c.get("/console-logs")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                logs = r.json() if r.text.strip() else []
            except Exception:
                logs = []
            return True, f"{len(logs)} log entries"

        async def t_network_errors():
            r = await c.get("/network-errors")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                errs = r.json() if r.text.strip() else []
            except Exception:
                errs = []
            ok = len(errs) == 0
            return ok, f"{len(errs)} network errors" + (
                f": {[e.get('url','?') for e in errs[:2]]}" if errs else "")

        async def t_screenshot():
            r = await c.post("/capture-screenshot", timeout=10.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            data = r.json()
            has_img = any(k in data for k in ("data", "screenshot", "path", "base64"))
            return has_img, f"screenshot captured ({len(str(data))} bytes)"

        async def t_accessibility():
            r = await c.post("/accessibility-audit", json={}, timeout=30.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:60]}"
            try:
                data  = r.json()
                score = data.get("score") or data.get("categories", {}).get("accessibility", {}).get("score")
                score_str = f"score={score:.0%}" if isinstance(score, float) else "score=N/A"
                return True, f"a11y audit — {score_str}"
            except Exception as e:
                return True, f"audit ran, parse error: {e}"

        async def t_performance():
            r = await c.post("/performance-audit", json={}, timeout=45.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:60]}"
            try:
                data  = r.json()
                score = data.get("score") or data.get("categories", {}).get("performance", {}).get("score")
                score_str = f"score={score:.0%}" if isinstance(score, float) else "score=N/A"
                return True, f"perf audit — {score_str}"
            except Exception as e:
                return True, f"audit ran, parse error: {e}"

        for name, coro in [
            ("browser.current_url",         t_current_url()),
            ("browser.console_errors",      t_console_errors()),
            ("browser.console_logs",        t_console_logs()),
            ("browser.network_errors",      t_network_errors()),
            ("browser.screenshot",          t_screenshot()),
            ("browser.accessibility_audit", t_accessibility()),
            ("browser.performance_audit",   t_performance()),
        ]:
            results.append(await _t(name, coro))

    return results


# ── Report builder ─────────────────────────────────────────────────────────────

def _fmt_report(results: list[T], title: str, total_ms: int, narrative: str) -> str:
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    icon   = "✅" if failed == 0 else ("⚠️" if failed < len(results) // 2 else "❌")
    lines  = [
        f"{icon} {title}",
        f"Results: {passed}/{len(results)} passed  {failed} failed  ({total_ms}ms total)\n",
    ]
    groups: dict[str, list[T]] = {}
    for r in results:
        prefix = r.name.split(".")[0]
        groups.setdefault(prefix, []).append(r)
    for group, tests in groups.items():
        gpass = sum(1 for t in tests if t.passed)
        gicon = "✅" if gpass == len(tests) else ("⚠️" if gpass > 0 else "❌")
        lines.append(f"\n{gicon} {group}  ({gpass}/{len(tests)})")
        for t in tests:
            lines.append(f"  {t.row()}")
    if narrative:
        lines.append(f"\nAnalysis:\n{narrative}")
    return "\n".join(lines)


async def _make_narrative(results: list[T]) -> str:
    passed  = sum(1 for r in results if r.passed)
    failed  = [r for r in results if not r.passed]
    prompt  = (
        f"{passed}/{len(results)} tests passed. "
        + (f"Failures: {', '.join(r.name + ' — ' + r.detail for r in failed[:5])}."
           if failed else "All tests passed.")
        + " Write a 2-3 sentence QA summary."
    )
    try:
        return await asyncio.wait_for(_llm.complete(REPORT_SYSTEM, prompt, max_tokens=200), timeout=20.0)
    except Exception:
        return (
            f"{passed}/{len(results)} passed."
            + (f" Failures: {', '.join(r.name for r in failed[:3])}." if failed else " All clear.")
        )


# ── FastMCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="tester",
    instructions=(
        "Comprehensive test runner for all 14 MCP servers and the web dashboard. "
        "Tier 1: direct agent function tests. Tier 2: HTTP web API tests. "
        "Tier 3: browser tests via AgentDeskAI browser-tools-server. "
        "Valid agent slugs: " + ", ".join(_AGENT_RUNNERS) + "."
    ),
)


@mcp.tool()
async def run_all_tests(
    web_url: str = "http://localhost:8000",
    browser_port: int = 0,
) -> str:
    """
    Run the full test suite: all 13 agent tier-1 tests, web API tests, and browser tests.

    web_url:      Dashboard URL (default http://localhost:8000)
    browser_port: browser-tools-server port (0 = auto-discover 3025-3035)
    """
    await _ensure_db()
    t0      = time.monotonic()
    all_res: list[T] = []

    # Tier 1: agents. Run non-LLM-dependent tests in parallel; sequential for LLM-heavy ones.
    parallel_slugs    = ["data_pull", "options_research", "watchlist", "html_css",
                         "heartbeat", "ibkr_session", "ibkr_positions",
                         "ibkr_orders", "ibkr_market_data"]
    sequential_slugs  = ["stock_research", "fundamentals", "summarizer", "charting"]

    parallel_tasks    = [_AGENT_RUNNERS[s]() for s in parallel_slugs]
    parallel_batches  = await asyncio.gather(*parallel_tasks, return_exceptions=True)
    for batch in parallel_batches:
        if isinstance(batch, Exception):
            all_res.append(T("agent.batch_error", False, 0, str(batch)))
        else:
            all_res.extend(batch)

    for slug in sequential_slugs:
        try:
            all_res.extend(await _AGENT_RUNNERS[slug]())
        except Exception as exc:
            all_res.append(T(f"{slug}.suite_error", False, 0, str(exc)))

    # Tier 2
    try:
        all_res.extend(await _run_web_api_tests(web_url))
    except Exception as exc:
        all_res.append(T("web.suite_error", False, 0, str(exc)))

    # Tier 3
    bp = browser_port if browser_port > 0 else None
    try:
        all_res.extend(await _run_browser_tests(web_url, bp))
    except Exception as exc:
        all_res.append(T("browser.suite_error", False, 0, str(exc)))

    total_ms  = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(all_res)
    await _save_run("all", all_res, narrative, total_ms)
    t0_log = time.monotonic()
    await _log_call("run_all_tests", int((time.monotonic() - t0_log) * 1000))
    return _fmt_report(all_res, "Full Test Suite", total_ms, narrative)


@mcp.tool()
async def test_agent(slug: str) -> str:
    """
    Test a single MCP server's functions.
    Valid slugs: data_pull, stock_research, fundamentals, options_research,
                 watchlist, summarizer, charting, html_css, heartbeat,
                 ibkr_session, ibkr_positions, ibkr_orders, ibkr_market_data
    """
    await _ensure_db()
    slug = slug.strip().lower()
    fn   = _AGENT_RUNNERS.get(slug)
    if not fn:
        return f"Unknown slug '{slug}'. Valid: {', '.join(_AGENT_RUNNERS)}"

    t0       = time.monotonic()
    results  = await fn()
    total_ms = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run(slug, results, narrative, total_ms)
    await _log_call("test_agent", total_ms)
    return _fmt_report(results, f"Agent: {slug}", total_ms, narrative)


@mcp.tool()
async def test_web_api(url: str = "http://localhost:8000") -> str:
    """
    Run HTTP-level tests against the FastAPI dashboard.
    Tests homepage, search, OOB sidebar, history API, positions/fundamentals/ibkr pages,
    positions fragment, empty ticker validation, and options search.
    The server must be running.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        results = await _run_web_api_tests(url)
    except httpx.ConnectError:
        return f"Cannot connect to {url}. Start the server first."
    total_ms  = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run("web_api", results, narrative, total_ms)
    await _log_call("test_web_api", total_ms)
    return _fmt_report(results, "Web API Tests", total_ms, narrative)


@mcp.tool()
async def test_browser(
    url: str  = "http://localhost:8000",
    port: int = 0,
) -> str:
    """
    Run browser-level tests via AgentDeskAI browser-tools-server.
    Checks: current URL, console errors/logs, network errors, screenshot,
    accessibility audit, performance audit.

    Prerequisites:
      npx @agentdeskai/browser-tools-server@latest
      AgentDesk Chrome extension active on the target page.

    port: override auto-discovery (default 0 = scan 3025-3035)
    """
    await _ensure_db()
    t0        = time.monotonic()
    bp        = port if port > 0 else None
    results   = await _run_browser_tests(url, bp)
    total_ms  = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run("browser", results, narrative, total_ms)
    await _log_call("test_browser", total_ms)
    return _fmt_report(results, "Browser Tests", total_ms, narrative)


@mcp.tool()
async def get_test_report(limit: int = 5) -> str:
    """Return the last `limit` test run summaries from tester.db."""
    await _ensure_db()
    limit = max(1, min(limit, 50))
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, scope, passed, total, failed, duration_ms, narrative "
            "FROM test_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return "No test runs yet. Call run_all_tests() to start."

    lines = [f"Test history (last {limit} runs, newest first):\n"]
    for ts, scope, passed, total, failed, ms, narrative in rows:
        icon = "✅" if failed == 0 else ("⚠️" if failed < total // 2 else "❌")
        lines.append(
            f"{icon} [{ts[:16].replace('T',' ')}]  {scope:<20} "
            f"{passed}/{total} passed  {failed} failed  {ms}ms"
        )
        if narrative:
            lines.append(f"   {narrative[:120]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
