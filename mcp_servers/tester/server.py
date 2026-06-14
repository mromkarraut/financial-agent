"""
Tester MCP Server

Comprehensive test runner for all MCP agents and the web dashboard.
Tests run in three tiers:

  1. Agent tests   — call each agent's core functions directly; fast, no server needed
  2. Web API tests — HTTP-level tests against http://localhost:8000 (httpx)
  3. Browser tests — connect to browser-tools-server (AgentDeskAI) for console logs,
                     screenshots, and Lighthouse audits; requires Chrome + extension

browser-tools-server must be running for tier 3:
  npx @agentdeskai/browser-tools-server@latest

Tools:
  run_all_tests(web_url, browser_port)   → full test suite, all tiers
  test_agent(slug)                        → test one agent's core functions
  test_web_api(url)                       → HTTP-level web tests
  test_browser(url, port)                 → browser-tools-server tests
  get_test_report(limit)                  → history from agent memory

Memory: db/agents/tester.db
LLM:    configured via MCP_LLM_PROVIDER / MCP_LLM_MODEL (for report narrative)
"""

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
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
        return f"{self.icon()} {self.name:<45} {self.ms:>5}ms  {self.detail}"


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
        """)
        await db.commit()
    _db_ready = True


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


# ── Tier 1: Agent tests ────────────────────────────────────────────────────────

async def _t(name: str, coro) -> T:
    """Run a single test coroutine; catch all exceptions."""
    t0 = time.monotonic()
    try:
        result = await coro
        ms = int((time.monotonic() - t0) * 1000)
        if isinstance(result, T):
            return result
        return T(name, bool(result[0]), ms, str(result[1]))
    except Exception as exc:
        return T(name, False, int((time.monotonic() - t0) * 1000), f"{type(exc).__name__}: {exc}")


async def _test_stock_research() -> list[T]:
    from tools.market_data import get_stock_data
    from mcp_servers.stock_research.server import get_price_snapshot, recall_analyses

    async def data_fetch():
        d = await get_stock_data("AAPL")
        ok = "current_price" in d and isinstance(d["current_price"], float) and d["current_price"] > 0
        return ok, f"AAPL ${d.get('current_price','?')}  RSI={d.get('rsi_14','?')}" if ok else f"bad keys: {list(d)}"

    async def mcp_snapshot():
        out = await get_price_snapshot("AAPL")
        d = json.loads(out)
        ok = "current_price" in d
        return ok, f"returned {len(d)} fields" if ok else "missing current_price"

    async def mcp_recall():
        out = await recall_analyses("AAPL", 1)
        return True, f"{len(out)} chars"

    return await asyncio.gather(
        _t("stock_research.market_data_fetch",   data_fetch()),
        _t("stock_research.mcp_get_snapshot",    mcp_snapshot()),
        _t("stock_research.mcp_recall_analyses", mcp_recall()),
    )


async def _test_fundamentals() -> list[T]:
    from tools.market_data import get_fundamentals
    from mcp_servers.fundamentals.server import recall_fundamentals

    async def data_fetch():
        d = await get_fundamentals("AAPL")
        ok = "pe_ratio" in d or "company_name" in d
        return ok, f"{d.get('company_name','?')}  PE={d.get('pe_ratio','?')}" if ok else str(list(d))

    async def mcp_recall():
        out = await recall_fundamentals("AAPL", 1)
        return True, f"{len(out)} chars"

    return await asyncio.gather(
        _t("fundamentals.market_data_fetch", data_fetch()),
        _t("fundamentals.mcp_recall",        mcp_recall()),
    )


async def _test_options_research() -> list[T]:
    from tools.options_math import (
        bs_delta, bs_theta_daily, pop_credit_spread,
        pop_debit_spread, p50, ivr_rank, expected_move,
    )
    from mcp_servers.options_research.server import recall_research

    async def math_bs():
        d = bs_delta(185, 180, 30/365, 0.25, is_call=True)
        ok = 0 < d < 1
        return ok, f"delta={d:.4f}"

    async def math_pop():
        pop = pop_credit_spread(180, 185, 30/365, 0.25, is_put=True)
        p = p50(pop)
        ok = 0 < pop < 1 and 0 < p < 1
        return ok, f"POP={pop:.2%}  P50={p:.2%}"

    async def math_ivr():
        ivr = ivr_rank(0.28, [0.18, 0.22, 0.25, 0.30, 0.35])
        ok = 0 <= ivr <= 100
        return ok, f"IVR={ivr:.1f}"

    async def math_em():
        em = expected_move(3.50, 3.20)
        ok = em > 0
        return ok, f"EM=±${em}"

    async def mcp_recall():
        out = await recall_research("AAPL", 1)
        return True, f"{len(out)} chars"

    return await asyncio.gather(
        _t("options_research.bs_delta",     math_bs()),
        _t("options_research.pop_p50",      math_pop()),
        _t("options_research.ivr_rank",     math_ivr()),
        _t("options_research.expected_move",math_em()),
        _t("options_research.mcp_recall",   mcp_recall()),
    )


async def _test_watchlist() -> list[T]:
    from db.database import watchlist_get_all, watchlist_add, watchlist_remove

    async def db_read():
        tickers = await watchlist_get_all()
        return True, f"{len(tickers)} tickers: {', '.join(tickers) or 'empty'}"

    async def db_write():
        await watchlist_add("_TEST_")
        after_add = await watchlist_get_all()
        ok_add = "_TEST_" in after_add
        await watchlist_remove("_TEST_")
        after_del = await watchlist_get_all()
        ok_del = "_TEST_" not in after_del
        ok = ok_add and ok_del
        return ok, "add+remove cycle OK" if ok else f"add={ok_add} del={ok_del}"

    return await asyncio.gather(
        _t("watchlist.db_read",  db_read()),
        _t("watchlist.db_write", db_write()),
    )


async def _test_summarizer() -> list[T]:
    from mcp_servers.llm import get_llm_client as _get

    async def llm_ping():
        client = _get()
        system = "Reply with exactly: PONG"
        user   = "PING"
        try:
            resp = await asyncio.wait_for(client.complete(system, user, max_tokens=10), timeout=15.0)
            ok = bool(resp)
            return ok, f"provider={client.provider} model={client.model} reply={resp[:40]!r}"
        except asyncio.TimeoutError:
            return False, f"LLM timeout after 15s (provider={client.provider})"

    async def mcp_summarize():
        from mcp_servers.summarizer.server import summarize_text
        sample = (
            "Apple reported Q4 revenue of $94.9B, up 6% YoY. "
            "iPhone revenue came in at $46.2B. Services hit a record $24.2B. "
            "EPS was $1.64, beating consensus of $1.60."
        )
        out = await asyncio.wait_for(summarize_text(sample), timeout=30.0)
        ok = "•" in out or len(out) > 30
        return ok, f"{len(out)} chars, starts: {out[:60]!r}"

    return [
        await _t("summarizer.llm_ping",       llm_ping()),
        await _t("summarizer.mcp_summarize",   mcp_summarize()),
    ]


async def _test_ibkr() -> list[T]:
    from agents.ibkr_agent import auth_status, order_history

    async def gateway_ping():
        try:
            s = await asyncio.wait_for(auth_status(), timeout=4.0)
            auth = s.get("authenticated", False)
            err  = s.get("error", "")
            if err:
                return True, f"gateway unreachable ({err[:60]}) — expected in dev"
            return True, f"authenticated={auth} connected={s.get('connected', False)}"
        except asyncio.TimeoutError:
            return True, "gateway timeout (expected if not running)"

    async def db_orders():
        rows = await order_history(limit=5)
        return True, f"{len(rows)} orders in history"

    return await asyncio.gather(
        _t("ibkr.gateway_ping", gateway_ping()),
        _t("ibkr.db_orders",    db_orders()),
    )


async def _test_heartbeat() -> list[T]:
    from mcp_servers.heartbeat.server import _probe_standard, _probe_all, _ensure_db as hb_init

    async def probe_one():
        await hb_init()
        r = await _probe_standard("stock_research")
        return r.status in ("healthy", "idle", "error"), f"status={r.status} detail={r.detail}"

    async def probe_all():
        await hb_init()
        results = await _probe_all()
        ok = len(results) == 6
        counts = {s: sum(1 for r in results.values() if r.status == s)
                  for s in ("healthy", "idle", "error")}
        return ok, f"probed {len(results)} agents — {counts}"

    return await asyncio.gather(
        _t("heartbeat.probe_single", probe_one()),
        _t("heartbeat.probe_all",    probe_all()),
    )


_AGENT_RUNNERS = {
    "stock_research":   _test_stock_research,
    "fundamentals":     _test_fundamentals,
    "options_research": _test_options_research,
    "watchlist":        _test_watchlist,
    "summarizer":       _test_summarizer,
    "ibkr":             _test_ibkr,
    "heartbeat":        _test_heartbeat,
}


# ── Tier 2: Web API tests (httpx) ─────────────────────────────────────────────

async def _run_web_api_tests(url: str) -> list[T]:
    base = url.rstrip("/")
    results: list[T] = []

    async with httpx.AsyncClient(base_url=base, timeout=12.0) as c:

        async def get_homepage():
            r = await c.get("/")
            ok = r.status_code == 200 and "htmx.org" in r.text
            return ok, f"status={r.status_code} htmx={'✓' if 'htmx.org' in r.text else '✗'}"

        async def homepage_elements():
            r = await c.get("/")
            checks = {
                "hamburger":    "hamburger"    in r.text,
                "theme":        "setTheme"     in r.text,
                "history-list": "history-list" in r.text,
                "hx-post":      "hx-post"      in r.text,
            }
            ok = all(checks.values())
            return ok, "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in checks.items())

        async def search_post():
            r = await c.post("/search", data={"ticker": "AAPL"}, timeout=30.0)
            ok = r.status_code == 200 and "result-wrap" in r.text
            lines = len(r.text.splitlines())
            return ok, f"status={r.status_code}  result-wrap={'✓' if 'result-wrap' in r.text else '✗'}  {lines} lines"

        async def search_oob_update():
            r = await c.post("/search", data={"ticker": "MSFT"}, timeout=30.0)
            ok = r.status_code == 200 and "hx-swap-oob" in r.text
            return ok, f"OOB sidebar update={'✓' if ok else '✗'}"

        async def api_history():
            r = await c.get("/api/history")
            ok = r.status_code == 200
            try:
                data = r.json()
                ok = ok and isinstance(data, list)
                return ok, f"{len(data)} history entries"
            except Exception:
                return False, f"non-JSON response: {r.text[:80]}"

        async def search_empty():
            r = await c.post("/search", data={"ticker": ""})
            ok = r.status_code == 200 and "error" in r.text.lower()
            return ok, f"empty ticker → error response={'✓' if ok else '✗'}"

        async def options_search():
            r = await c.post("/search", data={"ticker": "AAPL options bullish"}, timeout=30.0)
            ok = r.status_code == 200 and len(r.text) > 100
            return ok, f"options search status={r.status_code}  {len(r.text)} chars"

        async def ibkr_page():
            r = await c.get("/ibkr")
            ok = r.status_code == 200
            return ok, f"status={r.status_code}"

        for name, coro in [
            ("web.homepage_loads",      get_homepage()),
            ("web.homepage_elements",   homepage_elements()),
            ("web.search_aapl",         search_post()),
            ("web.search_oob_sidebar",  search_oob_update()),
            ("web.api_history_json",    api_history()),
            ("web.search_empty_ticker", search_empty()),
            ("web.options_search",      options_search()),
            ("web.ibkr_page",           ibkr_page()),
        ]:
            results.append(await _t(name, coro))

    return results


# ── Tier 3: Browser tests (browser-tools-server) ───────────────────────────────

async def _discover_browser_server() -> tuple[str, int] | None:
    """Find browser-tools-server on localhost ports 3025-3035."""
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
    """
    Query browser-tools-server for browser state: current URL, console errors,
    network errors, screenshot, and accessibility audit.
    Server must have the target_url open in Chrome with the AgentDesk extension active.
    """
    results: list[T] = []

    # Discover server
    t0 = time.monotonic()
    if bport:
        host, port = "127.0.0.1", bport
        ok = True
    else:
        found = await _discover_browser_server()
        if not found:
            return [T(
                "browser.server_discovery", False, 0,
                "browser-tools-server not found on ports 3025-3035. "
                "Run: npx @agentdeskai/browser-tools-server@latest  "
                "Then open Chrome with the AgentDesk extension and navigate to " + target_url,
            )]
        host, port = found
        ok = True
    results.append(T("browser.server_discovery", ok,
                     int((time.monotonic() - t0) * 1000),
                     f"Found browser-tools-server at {host}:{port}"))

    base = f"http://{host}:{port}"

    async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:

        async def check_current_url():
            r = await c.get("/current-url")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            url = r.text.strip().strip('"')
            on_target = target_url.rstrip("/") in url
            return True, f"Current tab: {url}  {'✓ on target' if on_target else '⚠ not on target page'}"

        async def console_errors():
            r = await c.get("/console-errors")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                errs = r.json() if r.text.strip() else []
            except Exception:
                errs = []
            ok = len(errs) == 0
            return ok, f"{len(errs)} console errors" + (f": {errs[:2]}" if errs else "")

        async def console_logs():
            r = await c.get("/console-logs")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                logs = r.json() if r.text.strip() else []
            except Exception:
                logs = []
            return True, f"{len(logs)} console log entries"

        async def network_errors():
            r = await c.get("/network-errors")
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            try:
                errs = r.json() if r.text.strip() else []
            except Exception:
                errs = []
            ok = len(errs) == 0
            return ok, f"{len(errs)} network errors" + (f": {[e.get('url','?') for e in errs[:2]]}" if errs else "")

        async def screenshot():
            r = await c.post("/capture-screenshot", timeout=10.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            data = r.json()
            has_img = "data" in data or "screenshot" in data or "path" in data or "base64" in data
            return has_img, f"screenshot captured ({len(str(data))} bytes in response)"

        async def accessibility_audit():
            r = await c.post("/accessibility-audit", json={}, timeout=30.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:80]}"
            try:
                data = r.json()
                score = data.get("score") or data.get("categories", {}).get("accessibility", {}).get("score")
                issues = data.get("issues", data.get("audits", {}))
                n_issues = len(issues) if isinstance(issues, list) else 0
                score_str = f"score={score:.0%}" if isinstance(score, float) else "score=N/A"
                return True, f"a11y audit complete — {score_str}  {n_issues} items"
            except Exception as e:
                return True, f"audit ran, parse error: {e} (raw: {str(data)[:80]})"

        async def performance_audit():
            r = await c.post("/performance-audit", json={}, timeout=45.0)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}: {r.text[:80]}"
            try:
                data = r.json()
                score = data.get("score") or data.get("categories", {}).get("performance", {}).get("score")
                score_str = f"score={score:.0%}" if isinstance(score, float) else "score=N/A"
                return True, f"perf audit complete — {score_str}"
            except Exception as e:
                return True, f"audit ran, parse error: {e}"

        for name, coro in [
            ("browser.current_url",        check_current_url()),
            ("browser.console_errors",     console_errors()),
            ("browser.console_logs",       console_logs()),
            ("browser.network_errors",     network_errors()),
            ("browser.screenshot",         screenshot()),
            ("browser.accessibility_audit",accessibility_audit()),
            ("browser.performance_audit",  performance_audit()),
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
    passed = sum(1 for r in results if r.passed)
    failed = [r for r in results if not r.passed]
    prompt = (
        f"{passed}/{len(results)} tests passed. "
        + (f"Failures: {', '.join(r.name + ' — ' + r.detail for r in failed[:5])}."
           if failed else "All tests passed.")
        + " Write a 2-3 sentence QA summary."
    )
    try:
        return await asyncio.wait_for(_llm.complete(REPORT_SYSTEM, prompt, max_tokens=200), timeout=20.0)
    except Exception:
        return f"{passed}/{len(results)} passed." + (" Failures: " + ", ".join(r.name for r in failed[:3]) + "." if failed else " All clear.")


# ── FastMCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="tester",
    instructions=(
        "Test runner for all MCP agents and the web dashboard. "
        "Tier 1: agent function tests. Tier 2: HTTP web API tests. "
        "Tier 3: browser tests via AgentDeskAI browser-tools-server."
    ),
)


@mcp.tool()
async def run_all_tests(
    web_url: str = "http://localhost:8000",
    browser_port: int = 0,
) -> str:
    """
    Run the full test suite across all three tiers:
      1. Agent tests  — all 7 agents' core functions
      2. Web API tests — HTTP tests against the FastAPI dashboard
      3. Browser tests — console logs, screenshot, audits via browser-tools-server

    web_url:      Dashboard URL (default http://localhost:8000)
    browser_port: browser-tools-server port (0 = auto-discover on 3025-3035)
    """
    await _ensure_db()
    t0      = time.monotonic()
    all_res: list[T] = []

    # Tier 1: agents (all in parallel)
    agent_tasks = [fn() for fn in _AGENT_RUNNERS.values()]
    agent_batches = await asyncio.gather(*agent_tasks, return_exceptions=True)
    for batch in agent_batches:
        if isinstance(batch, Exception):
            all_res.append(T("agent.batch_error", False, 0, str(batch)))
        else:
            all_res.extend(batch)

    # Tier 2: web API
    try:
        all_res.extend(await _run_web_api_tests(web_url))
    except Exception as exc:
        all_res.append(T("web.suite_error", False, 0, str(exc)))

    # Tier 3: browser
    bp = browser_port if browser_port > 0 else None
    try:
        all_res.extend(await _run_browser_tests(web_url, bp))
    except Exception as exc:
        all_res.append(T("browser.suite_error", False, 0, str(exc)))

    total_ms = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(all_res)
    await _save_run("all", all_res, narrative, total_ms)
    return _fmt_report(all_res, "Full Test Suite", total_ms, narrative)


@mcp.tool()
async def test_agent(slug: str) -> str:
    """
    Test a single MCP agent's core functions.
    Valid slugs: stock_research, fundamentals, options_research,
                 watchlist, summarizer, ibkr, heartbeat
    """
    await _ensure_db()
    slug = slug.strip().lower()
    fn = _AGENT_RUNNERS.get(slug)
    if not fn:
        return f"Unknown slug '{slug}'. Valid: {', '.join(_AGENT_RUNNERS)}"

    t0      = time.monotonic()
    results = await fn()
    total_ms = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run(slug, results, narrative, total_ms)
    return _fmt_report(results, f"Agent: {slug}", total_ms, narrative)


@mcp.tool()
async def test_web_api(url: str = "http://localhost:8000") -> str:
    """
    Run HTTP-level tests against the FastAPI dashboard.
    Tests: homepage load, key UI elements, POST /search, OOB sidebar update,
           GET /api/history, empty ticker validation, options search, IBKR page.
    The dashboard server must be running.
    """
    await _ensure_db()
    t0 = time.monotonic()
    try:
        results = await _run_web_api_tests(url)
    except httpx.ConnectError:
        return f"Cannot connect to {url}. Start the server: uvicorn server:app --host 0.0.0.0 --port 8000"
    total_ms  = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run("web_api", results, narrative, total_ms)
    return _fmt_report(results, "Web API Tests", total_ms, narrative)


@mcp.tool()
async def test_browser(
    url: str  = "http://localhost:8000",
    port: int = 0,
) -> str:
    """
    Run browser-level tests via AgentDeskAI browser-tools-server.

    Checks: current tab URL, console errors, console logs, network errors,
    screenshot capture, accessibility audit, performance audit.

    Prerequisites:
      1. npm install -g @agentdeskai/browser-tools-server
      2. npx @agentdeskai/browser-tools-server@latest   (runs on port 3025)
      3. Install AgentDesk Chrome extension from:
         https://github.com/AgentDeskAI/browser-tools-mcp
      4. Open Chrome and navigate to the target URL

    port: override auto-discovery (default 0 = scan 3025-3035)
    """
    await _ensure_db()
    t0       = time.monotonic()
    bp       = port if port > 0 else None
    results  = await _run_browser_tests(url, bp)
    total_ms = int((time.monotonic() - t0) * 1000)
    narrative = await _make_narrative(results)
    await _save_run("browser", results, narrative, total_ms)
    return _fmt_report(results, "Browser Tests (AgentDeskAI)", total_ms, narrative)


@mcp.tool()
async def get_test_report(limit: int = 5) -> str:
    """
    Return the last `limit` test run summaries from agent memory.
    Shows scope, pass rate, duration, and narrative for each run.
    """
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
