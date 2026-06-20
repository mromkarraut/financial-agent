"""
MCP Agent Registry

Central registry describing every independent MCP agent in this project.
Each entry contains: metadata, capabilities, tools, memory location, LLM config,
and the command needed to run the server.

Usage:
  # Import programmatically
  from mcp_servers.registry import REGISTRY, get_agent, list_agents

  # Run as CLI to print the registry
  python -m mcp_servers.registry

  # Run as MCP server (exposes registry_list_agents + registry_describe_agent tools)
  python -m mcp_servers.registry --serve
"""

from __future__ import annotations

import json
import os
import sys
from typing import TypedDict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HEARTBEAT_DB = os.path.join(_ROOT, "db", "agents", "heartbeat.db")

# Load config so registry reflects the live LLM setting
sys.path.insert(0, _ROOT)
import config as _config  # noqa: E402

_LLM = {"provider": _config.MCP_LLM_PROVIDER, "model": _config.MCP_LLM_MODEL}


class ToolSchema(TypedDict):
    name: str
    description: str
    parameters: dict[str, dict]


class AgentEntry(TypedDict):
    slug: str
    name: str
    description: str
    version: str
    capabilities: list[str]
    llm: dict[str, str]         # {"provider": ..., "model": ...}
    memory: dict[str, str]      # {"type": ..., "path": ...}
    shared_data: list[str]      # paths to shared DBs (if any)
    transport: str              # "stdio"
    command: list[str]          # how to start the server
    tools: list[ToolSchema]


REGISTRY: dict[str, AgentEntry] = {
    "data_pull": {
        "slug": "data_pull",
        "name": "Data Pull Agent",
        "description": (
            "Centralized market data ingress: stock snapshots (yfinance + Polygon), "
            "fundamentals (TWS Reuters → Yahoo Finance), and options chains "
            "(TWS real-time only). In-memory TTL cache prevents duplicate "
            "API calls across agents. Every fetch logged to data_pull.db. "
            "No LLM — pure data fetching."
        ),
        "version": "1.0.0",
        "capabilities": [
            "stock_snapshot",
            "fundamentals_data",
            "options_chain_data",
            "ibkr_data_priority",
            "yfinance_fallback",
            "ttl_cache",
            "fetch_logging",
            "source_status_check",
        ],
        "llm": {"provider": "none", "model": "none"},
        "memory": {"type": "sqlite", "path": "db/agents/data_pull.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.data_pull.server"],
        "tools": [
            {
                "name": "fetch_stock",
                "description": "Stock price snapshot: price, RSI-14, MA-20/50, volume.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL)"},
                },
            },
            {
                "name": "fetch_fundamentals",
                "description": "Company financials: P/E, EPS, revenue growth, margins, D/E. IBKR→yfinance.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "fetch_options_chain",
                "description": "Raw options chain JSON (calls+puts, IVs, Greeks). IBKR only.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "check_data_sources",
                "description": "Check IBKR/yfinance reachability + cache stats.",
                "parameters": {},
            },
            {
                "name": "get_fetch_history",
                "description": "Recent fetch log from data_pull.db — source, latency, cache hits.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Filter by ticker (empty = all)"},
                    "limit":  {"type": "integer", "description": "Number of entries (default 20)"},
                },
            },
            {
                "name": "clear_ticker_cache",
                "description": "Evict ticker from in-memory TTL cache (empty = clear all).",
                "parameters": {
                    "ticker": {"type": "string", "description": "Ticker to clear (empty = all)"},
                },
            },
        ],
    },

    "stock_research": {
        "slug": "stock_research",
        "name": "Stock Research Agent",
        "description": (
            "Price-action analysis: live price, RSI-14, MA-20/50, trend stance "
            "(Bullish/Bearish/Neutral), and Claude Haiku-written analyst narrative. "
            "Remembers past analyses per ticker."
        ),
        "version": "1.0.0",
        "capabilities": [
            "price_analysis",
            "rsi_calculation",
            "moving_averages",
            "trend_detection",
            "llm_narrative",
            "analysis_history",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/stock_research.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.stock_research.server"],
        "tools": [
            {
                "name": "analyze_stock",
                "description": "Full analysis with LLM narrative: price, RSI, MAs, stance.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL)"},
                },
            },
            {
                "name": "get_price_snapshot",
                "description": "Raw price/RSI/MA data as JSON — no LLM, just numbers.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "recall_analyses",
                "description": "Retrieve past analyses for a ticker from agent memory.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "limit":  {"type": "integer", "description": "Number of results (default 5, max 20)"},
                },
            },
        ],
    },

    "fundamentals": {
        "slug": "fundamentals",
        "name": "Fundamentals Agent",
        "description": (
            "Company financial analysis: P/E, forward P/E, EPS, revenue growth YoY, "
            "profit/gross margins, debt/equity, quarterly revenue trend, market cap. "
            "Claude Haiku provides an investment perspective. Multi-company comparison supported."
        ),
        "version": "1.0.0",
        "capabilities": [
            "pe_ratio",
            "eps_analysis",
            "revenue_growth",
            "margin_analysis",
            "debt_equity",
            "quarterly_trends",
            "multi_company_comparison",
            "llm_investment_perspective",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/fundamentals.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.fundamentals.server"],
        "tools": [
            {
                "name": "get_company_fundamentals",
                "description": "Full fundamentals + LLM investment perspective for one ticker.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "compare_companies",
                "description": "Side-by-side fundamental comparison of 2-4 tickers.",
                "parameters": {
                    "tickers_csv": {"type": "string", "description": "Comma-separated tickers e.g. 'AAPL,MSFT,GOOGL'"},
                },
            },
            {
                "name": "recall_fundamentals",
                "description": "Retrieve past fundamental snapshots for a ticker from agent memory.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "limit":  {"type": "integer", "description": "Number of results (default 5, max 20)"},
                },
            },
        ],
    },

    "options_research": {
        "slug": "options_research",
        "name": "Options Research Agent",
        "description": (
            "Tastytrade-style options research: live chain data, Black-Scholes Greeks "
            "(delta, theta), IVR (52-week range), P50, probability of profit, and "
            "vertical spread ranking (credit + debit). Zero LLM for math; "
            "Claude Sonnet provides market context when requested."
        ),
        "version": "1.0.0",
        "capabilities": [
            "options_chain",
            "black_scholes_greeks",
            "iv_rank",
            "vertical_spreads",
            "pop_calculation",
            "p50_calculation",
            "roc_calculation",
            "expected_move",
            "llm_market_context",
            "research_history",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/options_research.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.options_research.server"],
        "tools": [
            {
                "name": "research_options",
                "description": "Full options research: chain, IVR, ranked spreads, recommendation.",
                "parameters": {
                    "ticker":  {"type": "string", "description": "Stock ticker symbol"},
                    "outlook": {"type": "string", "description": "bullish | bearish | neutral (default: neutral)"},
                },
            },
            {
                "name": "get_options_chain_data",
                "description": "Raw options chain JSON for the first 4 expirations.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "calculate_iv_rank",
                "description": "IVR, ATM IV, HV-30, expected move for the nearest expiry.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "recall_research",
                "description": "Retrieve past options research for a ticker from agent memory.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                    "limit":  {"type": "integer", "description": "Number of results (default 5, max 20)"},
                },
            },
        ],
    },

    "watchlist": {
        "slug": "watchlist",
        "name": "Watchlist Agent",
        "description": (
            "Persistent ticker watchlist with live price digest. Watchlist state is shared "
            "with the Telegram bot and web UI via state.db. Generates Claude Haiku "
            "portfolio summaries highlighting movers and overall market tone."
        ),
        "version": "1.0.0",
        "capabilities": [
            "watchlist_crud",
            "live_price_digest",
            "portfolio_summary",
            "llm_market_tone",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/watchlist.db"},
        "shared_data": ["db/state.db (watchlist table)"],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.watchlist.server"],
        "tools": [
            {
                "name": "add_ticker",
                "description": "Add a ticker to the shared persistent watchlist.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "remove_ticker",
                "description": "Remove a ticker from the shared persistent watchlist.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol"},
                },
            },
            {
                "name": "list_watchlist",
                "description": "Return all tickers currently on the watchlist.",
                "parameters": {},
            },
            {
                "name": "get_watchlist_digest",
                "description": "Live price snapshots for all watched tickers + LLM portfolio summary.",
                "parameters": {},
            },
        ],
    },

    "summarizer": {
        "slug": "summarizer",
        "name": "Summarizer Agent",
        "description": (
            "Financial text processing via Claude Sonnet: bullet-point summarization, "
            "structured entity extraction (tickers, companies, metrics, dates), "
            "and market sentiment classification with confidence score and signals."
        ),
        "version": "1.0.0",
        "capabilities": [
            "text_summarization",
            "entity_extraction",
            "sentiment_classification",
            "confidence_scoring",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/summarizer.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.summarizer.server"],
        "tools": [
            {
                "name": "summarize_text",
                "description": "Summarize financial text into 3 investor-relevant bullet points.",
                "parameters": {
                    "text": {"type": "string", "description": "Financial text to summarize"},
                },
            },
            {
                "name": "extract_financial_entities",
                "description": "Extract tickers, companies, metrics, dates, and sentiment keywords as JSON.",
                "parameters": {
                    "text": {"type": "string", "description": "Financial text to analyze"},
                },
            },
            {
                "name": "classify_market_sentiment",
                "description": "Classify text as bullish/bearish/neutral with confidence and signals.",
                "parameters": {
                    "text": {"type": "string", "description": "Financial text to classify"},
                },
            },
        ],
    },

    "tester": {
        "slug": "tester",
        "name": "Tester Agent",
        "description": (
            "Three-tier test runner: (1) agent function tests for all 7 agents, "
            "(2) HTTP-level web API tests against the FastAPI dashboard, "
            "(3) browser tests via AgentDeskAI browser-tools-server — "
            "console logs, network errors, screenshots, and Lighthouse audits."
        ),
        "version": "1.0.0",
        "capabilities": [
            "agent_testing",
            "web_api_testing",
            "browser_testing",
            "lighthouse_audits",
            "screenshot_capture",
            "test_history",
            "llm_test_narrative",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/tester.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.tester.server"],
        "tools": [
            {
                "name": "run_all_tests",
                "description": "Full test suite: all agents + web API + browser tests.",
                "parameters": {
                    "web_url":      {"type": "string",  "description": "Dashboard URL (default http://localhost:8000)"},
                    "browser_port": {"type": "integer", "description": "browser-tools-server port (0 = auto-discover)"},
                },
            },
            {
                "name": "test_agent",
                "description": "Test one agent's core functions by slug.",
                "parameters": {
                    "slug": {"type": "string", "description": "Agent slug (e.g. stock_research, ibkr)"},
                },
            },
            {
                "name": "test_web_api",
                "description": "HTTP tests against the FastAPI dashboard.",
                "parameters": {
                    "url": {"type": "string", "description": "Dashboard URL (default http://localhost:8000)"},
                },
            },
            {
                "name": "test_browser",
                "description": "Browser tests via AgentDeskAI browser-tools-server.",
                "parameters": {
                    "url":  {"type": "string",  "description": "Target URL to test in browser"},
                    "port": {"type": "integer", "description": "browser-tools-server port (0 = auto-discover)"},
                },
            },
            {
                "name": "get_test_report",
                "description": "Return past test run summaries from agent memory.",
                "parameters": {
                    "limit": {"type": "integer", "description": "Number of runs to return (default 5)"},
                },
            },
        ],
    },

    "heartbeat": {
        "slug": "heartbeat",
        "name": "Heartbeat Agent",
        "description": (
            "System health monitor for all MCP agents. Probes each agent's memory DB, "
            "call recency, and TWS connectivity. Writes results to "
            "heartbeat.db so the registry and summarizer can read health status without "
            "direct inter-process communication."
        ),
        "version": "1.0.0",
        "capabilities": [
            "health_monitoring",
            "db_probe",
            "tws_probe",
            "call_recency_tracking",
            "system_snapshots",
        ],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/heartbeat.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.heartbeat.server"],
        "tools": [
            {
                "name": "check_all_agents",
                "description": "Probe all 6 agents concurrently and return a live status table.",
                "parameters": {},
            },
            {
                "name": "check_agent",
                "description": "Probe one specific agent by slug.",
                "parameters": {
                    "slug": {"type": "string", "description": "Agent slug (e.g. stock_research, ibkr)"},
                },
            },
            {
                "name": "get_health_report",
                "description": "Return most recent stored health snapshot (no re-probe).",
                "parameters": {},
            },
            {
                "name": "get_health_history",
                "description": "Return the last N individual health check results.",
                "parameters": {
                    "limit": {"type": "integer", "description": "Number of entries (default 20, max 100)"},
                },
            },
        ],
    },

    "ibkr_session": {
        "slug": "ibkr_session",
        "name": "IBKR Session Agent",
        "description": (
            "TWS session lifecycle via ib_insync: connection status, 55s keepalive (auto-started), "
            "account listing, and account summary. "
            "Requires TWS running on Windows with API socket enabled."
        ),
        "version": "1.0.0",
        "capabilities": ["session_management", "auth_check", "tickle_keepalive", "account_listing"],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/ibkr_session.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.ibkr_session.server"],
        "tools": [
            {"name": "get_auth_status",      "description": "Check TWS auth + connection state.", "parameters": {}},
            {"name": "reauthenticate_session","description": "Re-open brokerage session without browser.", "parameters": {}},
            {"name": "list_accounts",        "description": "List all linked account IDs.", "parameters": {}},
            {"name": "get_account_details",  "description": "Net liq, cash, buying power for an account.",
             "parameters": {"account_id": {"type": "string", "description": "Account ID (empty = first)"}}},
            {"name": "get_session_log",      "description": "Recent session events from memory.",
             "parameters": {"limit": {"type": "integer", "description": "Number of entries"}}},
        ],
    },

    "ibkr_positions": {
        "slug": "ibkr_positions",
        "name": "IBKR Positions Agent",
        "description": (
            "Live portfolio positions, P&L, and account allocation via TWS. "
            "Generates LLM portfolio narrative. Also provides asset-class allocation breakdown."
        ),
        "version": "1.0.0",
        "capabilities": ["live_positions", "pnl", "portfolio_summary", "allocation", "llm_narrative"],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/ibkr_positions.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.ibkr_positions.server"],
        "tools": [
            {"name": "get_open_positions",  "description": "All open positions with P&L.",
             "parameters": {"account_id": {"type": "string", "description": "Account ID (empty = first)"}}},
            {"name": "get_live_pnl",        "description": "Day P&L and unrealized P&L across accounts.", "parameters": {}},
            {"name": "get_portfolio_summary","description": "P&L + positions in one combined view.", "parameters": {}},
            {"name": "get_allocation",       "description": "Allocation by asset class / sector.",
             "parameters": {"account_id": {"type": "string", "description": "Account ID (empty = first)"}}},
        ],
    },

    "ibkr_orders": {
        "slug": "ibkr_orders",
        "name": "IBKR Orders Agent",
        "description": (
            "Place, confirm, cancel, and review vertical spread orders via TWS. "
            "Includes LLM pre-trade risk briefing and persisted order history."
        ),
        "version": "1.0.0",
        "capabilities": ["order_execution", "order_cancellation", "order_history", "llm_risk_briefing", "two_step_confirmation"],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/ibkr_orders.db"},
        "shared_data": ["db/state.db (ibkr_orders)", "db/state.db (ibkr_conid_cache)"],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.ibkr_orders.server"],
        "tools": [
            {"name": "place_spread",       "description": "Submit a vertical spread order.", "parameters": {
                "ticker": {"type": "string"}, "short_strike": {"type": "number"},
                "long_strike": {"type": "number"}, "right": {"type": "string"},
                "expiry": {"type": "string"}, "net_price": {"type": "number"},
                "quantity": {"type": "integer"}, "tif": {"type": "string"},
            }},
            {"name": "get_risk_briefing",  "description": "LLM pre-trade risk briefing — no order placed.", "parameters": {
                "ticker": {"type": "string"}, "short_strike": {"type": "number"},
                "long_strike": {"type": "number"}, "right": {"type": "string"},
                "expiry": {"type": "string"}, "net_price": {"type": "number"},
                "quantity": {"type": "integer"},
            }},
            {"name": "confirm_order",      "description": "Send IBKR two-step confirmation reply.",
             "parameters": {"reply_id": {"type": "string", "description": "Reply ID from initial order response"}}},
            {"name": "cancel_open_order",  "description": "Cancel a pending order by order ID.",
             "parameters": {"order_id": {"type": "string"}}},
            {"name": "get_live_orders",    "description": "Orders currently live on exchange.", "parameters": {}},
            {"name": "get_order_history",  "description": "Recent orders from local DB.",
             "parameters": {"limit": {"type": "integer"}}},
        ],
    },

    "html_css": {
        "slug": "html_css",
        "name": "HTML/CSS Agent",
        "description": (
            "Styled HTML component renderer for the financial agent web UI. "
            "Generates hc-* CSS class components: metric grids, data tables, "
            "option strategy cards (legs, key numbers, profit table), alert strips, "
            "and generic section cards. No LLM — pure deterministic rendering. "
            "Used by other agents to produce web-ready HTML from raw financial data."
        ),
        "version": "1.0.0",
        "capabilities": [
            "html_rendering",
            "metric_grid",
            "data_table",
            "strategy_card",
            "legs_display",
            "profit_table",
            "alert_component",
            "section_card",
        ],
        "llm": {"provider": "none", "model": "none"},
        "memory": {"type": "sqlite", "path": "db/agents/html_css.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.html_css.server"],
        "tools": [
            {
                "name": "render_metric_grid",
                "description": "Metric badge grid from JSON metrics array.",
                "parameters": {
                    "metrics_json": {"type": "string", "description": 'JSON: [{"label","value","color"(opt)}]'},
                    "title":        {"type": "string", "description": "Optional section card title"},
                },
            },
            {
                "name": "render_data_table",
                "description": "Styled table with optional per-row colour classes.",
                "parameters": {
                    "headers_json":    {"type": "string", "description": 'JSON: ["Col1","Col2"]'},
                    "rows_json":       {"type": "string", "description": 'JSON: [["v1","v2"],...]'},
                    "title":           {"type": "string", "description": "Optional section card title"},
                    "icon":            {"type": "string", "description": "Optional emoji prefix"},
                    "row_classes_json":{"type": "string", "description": 'JSON: ["hc-row-profit",...]'},
                },
            },
            {
                "name": "render_strategy_card",
                "description": "Full options strategy card: legs + metrics + profit table.",
                "parameters": {
                    "strategy_json": {"type": "string", "description": "JSON strategy dict from OptionsResearchAgent"},
                    "current_price": {"type": "number", "description": "Current underlying price"},
                },
            },
            {
                "name": "render_legs_card",
                "description": "BUY/SELL leg pills card for a vertical spread.",
                "parameters": {
                    "strategy_json": {"type": "string", "description": "JSON strategy dict"},
                },
            },
            {
                "name": "render_alert",
                "description": "Styled alert strip — info, warning, success, or error.",
                "parameters": {
                    "message": {"type": "string", "description": "Alert message (HTML allowed)"},
                    "level":   {"type": "string", "description": "info | warning | success | error"},
                },
            },
            {
                "name": "render_section_card",
                "description": "Wrap arbitrary HTML in a titled section card.",
                "parameters": {
                    "title":     {"type": "string", "description": "Card header text"},
                    "body_html": {"type": "string", "description": "Inner HTML"},
                    "icon":      {"type": "string", "description": "Optional emoji prefix"},
                },
            },
            {
                "name": "recall_renders",
                "description": "List previously rendered components from agent memory.",
                "parameters": {
                    "context": {"type": "string",  "description": "Filter by context/title substring"},
                    "limit":   {"type": "integer", "description": "Number of entries (default 5)"},
                },
            },
        ],
    },

    "charting": {
        "slug": "charting",
        "name": "Charting Agent",
        "description": (
            "Interactive Plotly charts for financial data: candlestick/line price history "
            "with volume, fundamentals dashboards (revenue + margins), options spread payoff "
            "diagrams, multi-ticker comparison (price return, P/E, margins), and ad-hoc "
            "custom charts. Returns embed-ready Plotly HTML divs."
        ),
        "version": "1.0.0",
        "capabilities": [
            "price_charts",
            "candlestick",
            "volume_chart",
            "fundamentals_chart",
            "options_payoff_chart",
            "multi_ticker_comparison",
            "custom_chart",
            "chart_history",
        ],
        "llm": {"provider": "none", "model": "none"},
        "memory": {"type": "sqlite", "path": "db/agents/charting.db"},
        "shared_data": [],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.charting.server"],
        "tools": [
            {
                "name": "plot_price_history",
                "description": "Candlestick or line price chart with volume subplot for a ticker.",
                "parameters": {
                    "ticker":     {"type": "string", "description": "Stock symbol (e.g. AAPL)"},
                    "period":     {"type": "string", "description": "yfinance period: 1d 5d 1mo 3mo 6mo 1y 2y 5y (default: 3mo)"},
                    "chart_type": {"type": "string", "description": "'candlestick' or 'line' (default: candlestick)"},
                },
            },
            {
                "name": "plot_fundamentals",
                "description": "Fundamentals dashboard: quarterly revenue bars + gross/net margin lines.",
                "parameters": {
                    "ticker": {"type": "string", "description": "Stock symbol (e.g. AAPL)"},
                },
            },
            {
                "name": "plot_options_payoff",
                "description": "Spread P&L payoff diagram at expiration. Max profit/loss annotated.",
                "parameters": {
                    "ticker":       {"type": "string",  "description": "Underlying symbol"},
                    "short_strike": {"type": "number",  "description": "Strike being sold"},
                    "long_strike":  {"type": "number",  "description": "Strike being bought (hedge)"},
                    "right":        {"type": "string",  "description": "'C' for calls, 'P' for puts"},
                    "expiry":       {"type": "string",  "description": "Expiration date YYYY-MM-DD"},
                    "net_price":    {"type": "number",  "description": "Premium received (>0) or paid (<0)"},
                    "spread_type":  {"type": "string",  "description": "'credit' or 'debit' (default: credit)"},
                },
            },
            {
                "name": "plot_comparison",
                "description": "Multi-ticker comparison: price_return, pe_ratio, revenue_growth, or profit_margin.",
                "parameters": {
                    "tickers_csv": {"type": "string", "description": "Comma-separated symbols e.g. 'AAPL,MSFT,GOOGL'"},
                    "metric":      {"type": "string", "description": "price_return | pe_ratio | revenue_growth | profit_margin"},
                },
            },
            {
                "name": "plot_custom",
                "description": "Ad-hoc Plotly chart from arbitrary JSON data — line, bar, scatter, area, or pie.",
                "parameters": {
                    "title":      {"type": "string", "description": "Chart title"},
                    "chart_type": {"type": "string", "description": "'line' | 'bar' | 'scatter' | 'area' | 'pie'"},
                    "data_json":  {"type": "string", "description": 'JSON array e.g. [{"name":"S1","x":[...],"y":[...]}]'},
                },
            },
            {
                "name": "recall_charts",
                "description": "List previously generated charts from agent memory.",
                "parameters": {
                    "ticker": {"type": "string",  "description": "Filter by symbol (empty = all)"},
                    "limit":  {"type": "integer", "description": "Number of results (default 5, max 20)"},
                },
            },
        ],
    },

    "ibkr_market_data": {
        "slug": "ibkr_market_data",
        "name": "IBKR Market Data Agent",
        "description": (
            "Contract lookup (conid), live market snapshots (bid/ask/last/Greeks), "
            "option strike discovery, and contract search via TWS. "
            "Conid results cached in SQLite."
        ),
        "version": "1.0.0",
        "capabilities": ["conid_lookup", "live_quotes", "option_greeks", "strike_discovery", "contract_search", "conid_cache"],
        "llm": _LLM,
        "memory": {"type": "sqlite", "path": "db/agents/ibkr_market_data.db"},
        "shared_data": ["db/state.db (ibkr_conid_cache)"],
        "transport": "stdio",
        "command": ["python", "-m", "mcp_servers.ibkr_market_data.server"],
        "tools": [
            {"name": "get_stock_conid",           "description": "Look up conid for a stock/ETF.",
             "parameters": {"symbol": {"type": "string"}}},
            {"name": "get_option_contract_conid", "description": "Look up option conid (cached).",
             "parameters": {"symbol": {"type": "string"}, "expiry": {"type": "string"},
                            "right": {"type": "string"}, "strike": {"type": "number"},
                            "exchange": {"type": "string"}}},
            {"name": "get_market_snapshot",       "description": "Live bid/ask/last/Greeks for comma-separated conids.",
             "parameters": {"conids": {"type": "string"}}},
            {"name": "get_option_strikes",        "description": "Available strikes for symbol+month.",
             "parameters": {"symbol": {"type": "string"}, "month": {"type": "string"}}},
            {"name": "search_contract",           "description": "Search any contract by name or ticker.",
             "parameters": {"query": {"type": "string"}, "sec_type": {"type": "string"}}},
            {"name": "clear_conid_cache",         "description": "Remove cached conids for a symbol.",
             "parameters": {"symbol": {"type": "string"}}},
        ],
    },
}


# ── Public API ─────────────────────────────────────────────────────────────────

def get_agent(slug: str) -> AgentEntry | None:
    return REGISTRY.get(slug)


def list_agents() -> list[str]:
    return list(REGISTRY.keys())


def get_all() -> dict[str, AgentEntry]:
    return REGISTRY


def summary_table() -> str:
    lines = [
        f"{'Agent':<20} {'LLM Model':<32} {'Tools':>5}  Description",
        "─" * 100,
    ]
    for slug, entry in REGISTRY.items():
        desc = entry.get("description", "")
        lines.append(
            f"{entry['name']:<20} {entry['llm']['model']:<32} {len(entry['tools']):>5}  "
            f"{desc[:55]}{'…' if len(desc) > 55 else ''}"
        )
    return "\n".join(lines)


# ── MCP server (registry as an agent itself) ───────────────────────────────────

def _run_as_server() -> None:
    from mcp.server.fastmcp import FastMCP

    registry_mcp = FastMCP(
        name="agent-registry",
        instructions="Lists and describes all MCP agents in this financial agent system.",
    )

    @registry_mcp.tool()
    def registry_list_agents() -> str:
        """
        List all registered MCP agents with their slugs, names, and brief descriptions.
        Use registry_describe_agent(slug) for detailed tool schemas.
        """
        lines = [f"Available agents ({len(REGISTRY)} total):\n"]
        for slug, entry in REGISTRY.items():
            tools_str = ", ".join(t["name"] for t in entry["tools"])
            lines.append(
                f"  {slug}\n"
                f"    Name:    {entry['name']}\n"
                f"    LLM:     {entry['llm']['model']}\n"
                f"    Memory:  {entry['memory']['path']}\n"
                f"    Tools:   {tools_str}\n"
                f"    Run:     {' '.join(entry['command'])}\n"
            )
        return "\n".join(lines)

    @registry_mcp.tool()
    def registry_describe_agent(slug: str) -> str:
        """
        Return full details for a specific agent including all tool schemas,
        capabilities, LLM config, memory location, and run command.
        """
        entry = REGISTRY.get(slug)
        if not entry:
            available = ", ".join(REGISTRY.keys())
            return f"Agent '{slug}' not found. Available: {available}"
        return json.dumps(entry, indent=2)

    @registry_mcp.tool()
    async def registry_get_system_status() -> str:
        """
        Return a combined view of all registered agents with their last known health status
        from heartbeat.db. Run the heartbeat agent's check_all_agents() first to populate
        fresh health data; this tool reads whatever is already stored.
        """
        import aiosqlite

        # Load latest per-agent health from heartbeat DB
        health: dict[str, dict] = {}
        if os.path.exists(_HEARTBEAT_DB):
            try:
                async with aiosqlite.connect(_HEARTBEAT_DB) as db:
                    # Latest snapshot for each agent
                    async with db.execute(
                        "SELECT agent_slug, status, call_count, last_call, detail "
                        "FROM health_checks h "
                        "WHERE id = (SELECT MAX(id) FROM health_checks WHERE agent_slug = h.agent_slug) "
                        "GROUP BY agent_slug"
                    ) as cur:
                        for slug, status, count, last, detail in await cur.fetchall():
                            health[slug] = {
                                "status": status, "call_count": count,
                                "last_call": last, "detail": detail,
                            }
            except Exception:
                pass

        icons = {"healthy": "✅", "idle": "⏸ ", "error": "❌"}
        lines = [f"System Status — {len(REGISTRY)} registered agents\n"]
        for slug, entry in REGISTRY.items():
            h = health.get(slug)
            if h:
                icon   = icons.get(h["status"], "❓")
                last   = (h["last_call"] or "never")[:16].replace("T", " ")
                status = f"{icon} {h['status']}"
                detail = f"  {h['call_count']} calls, last: {last}"
            else:
                status = "❓ unknown"
                detail = "  (run heartbeat check_all_agents to populate)"
            lines.append(
                f"  {slug:<20} {status:<18} {detail}\n"
                f"  {'':20} LLM: {entry['llm']['model']}\n"
                f"  {'':20} Run: {' '.join(entry['command'])}\n"
            )

        healthy = sum(1 for h in health.values() if h["status"] == "healthy")
        idle    = sum(1 for h in health.values() if h["status"] == "idle")
        errors  = sum(1 for h in health.values() if h["status"] == "error")
        unknown = len(REGISTRY) - len(health)
        lines.append(
            f"\nHealth summary: {healthy} healthy  {idle} idle  {errors} error  {unknown} unknown"
        )
        return "\n".join(lines)

    @registry_mcp.tool()
    def registry_find_capable_agents(capability: str) -> str:
        """
        Find all agents that have a specific capability (e.g. 'llm_narrative',
        'options_chain', 'order_execution'). Returns matching agents and their tools.
        """
        matches = [
            (slug, entry) for slug, entry in REGISTRY.items()
            if capability.lower() in [c.lower() for c in entry["capabilities"]]
        ]
        if not matches:
            all_caps = sorted({c for e in REGISTRY.values() for c in e["capabilities"]})
            return (
                f"No agents found with capability '{capability}'.\n"
                f"All available capabilities:\n" + "\n".join(f"  {c}" for c in all_caps)
            )
        lines = [f"Agents with '{capability}' capability:"]
        for slug, entry in matches:
            lines.append(f"\n  {slug} ({entry['name']})")
            lines.append(f"  Run: {' '.join(entry['command'])}")
        return "\n".join(lines)

    registry_mcp.run()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--serve" in sys.argv:
        _run_as_server()
    else:
        print(summary_table())
        print()
        for slug, entry in REGISTRY.items():
            print(f"\n{'='*60}")
            print(f"  {entry['name']}  (slug: {slug})")
            print(f"  {entry['description']}")
            print(f"\n  LLM:     {entry['llm']['provider']} / {entry['llm']['model']}")
            print(f"  Memory:  {entry['memory']['path']}")
            if entry["shared_data"]:
                print(f"  Shared:  {', '.join(entry['shared_data'])}")
            print(f"  Run:     {' '.join(entry['command'])}")
            print(f"\n  Tools:")
            for tool in entry["tools"]:
                params = ", ".join(
                    f"{k}: {v.get('type','str')}"
                    for k, v in tool["parameters"].items()
                )
                print(f"    • {tool['name']}({params})")
                print(f"      {tool['description']}")
