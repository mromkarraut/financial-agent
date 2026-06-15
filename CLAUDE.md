# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Activate the shared venv (required for all commands)
source /home/omkar/venvs/bin/activate

# Start everything (recommended)
python start.py                # IB Gateway + web + Telegram bot (paper trading)
python start.py --no-lms      # skip LM Studio (already running)
python start.py --no-gateway  # skip IB Gateway (already running)
python start.py --live        # LIVE trading account (real money — use with caution)
python start.py --web-only    # web dashboard only, no Telegram bot

# Logs → logs/web.log, logs/gateway.log

# DB migration (run after adding tables to database.py)
python -c "import asyncio; from db.database import init_db; asyncio.run(init_db())"

# Quick options research smoke-test (no server needed)
python -c "
import asyncio
from agents.options_research_agent import OptionsResearchAgent
async def t():
    r = await OptionsResearchAgent().run({'ticker': 'AAPL', 'outlook': 'bullish', 'chat_id': 'test'})
    print(r['output'][-400:])
asyncio.run(t())
"

# List all MCP servers and their tools
python -m mcp_servers.registry

# Run an individual MCP server (Claude Code auto-starts via .claude/settings.json)
python -m mcp_servers.ibkr_session.server
```

## Architecture

### Two entry points, one shared DB

| Process | File | Purpose |
|---|---|---|
| Telegram bot | `main.py` | Receives messages, calls `Orchestrator`, sends HTML replies |
| Web dashboard | `server.py` | FastAPI + HTMX UI; reads same `db/state.db` |

Both share `db/state.db` (SQLite). `start.py` orchestrates all services and handles Ctrl+C shutdown.

### Message flow (Telegram)

```
Telegram message
  → main.py._handle_text()
  → Orchestrator.process()
  → _try_watchlist_fast_path()       [regex — no LLM]  → WatchlistAgent
  → if len(text) > LONG_MESSAGE_THRESHOLD (300):
      → SummarizerAgent              [LLM]
  → memory.get_context(chat_id)
  → _build_calls(text)               [regex routing]
      options keywords?              → OptionsResearchAgent only
      ticker(s) present?             → StockResearchAgent + FundamentalsAgent (parallel)
      no match?                      → _general_reply() [LLM fallback]
  → asyncio.gather(*agent_calls)
  → memory.save_turn()
  → TelegramSender.reply()           [HTML parse mode]
```

### Routing rules (orchestrator.py)

- Ticker detection runs on `text.upper()`. `_SKIP_WORDS` (~40 entries) prevents false positives — always extend when adding new keyword routes.
- When options keywords are detected, **only** `OptionsResearchAgent` is dispatched.
- `_general_reply` falls back without history if LM Studio returns 400.
- `TELEGRAM_CHANNEL_ID` (comma-separated) in `.env` restricts which chats the bot responds to.

### Agent contract

Every agent returns `AgentResult` (TypedDict in `agents/base_agent.py`):

```python
{"agent": str, "version": str, "output": str,
 "confidence": float,   # 0.0 = hard failure
 "metadata": dict}
```

`output` is always Telegram HTML (`<b>`, `<i>`, `<code>`, `<pre>` tags). Use `<br>` for explicit line breaks — `\n` collapses in browser HTML outside `<pre>`. Both `\n` and `<br>` work in Telegram HTML mode. Use `<pre>` (block) not `<code>` (inline) when section separation matters.

### LLM situation

Local model (LM Studio) frequently outputs `?????` garbage. `StockResearchAgent` and `FundamentalsAgent` guard against this with a `q_ratio` check and fall back to deterministic text.

`OptionsResearchAgent`, `UIResearcherAgent`, `UITestingAgent` — **zero LLM dependency**.

`ANTHROPIC_API_KEY` is in `.env`. Set `MCP_LLM_PROVIDER=anthropic` to switch all MCP servers to Claude.

### MCP server layer

13 independent MCP servers in `mcp_servers/`. Claude Code auto-starts them via `.claude/settings.json`. Each server:
- Uses `FastMCP` with `instructions=` (not `description=`) for the server-level string
- Has its own per-agent SQLite DB under `db/agents/<slug>.db`
- Gets its LLM via `mcp_servers/llm.py → get_llm_client()` — never import `anthropic` directly
- Calls `_llm.complete(system, user, max_tokens)` — provider-agnostic

To add a new MCP server: create `mcp_servers/<slug>/server.py`, add to `mcp_servers/registry.py`, register in `.claude/settings.json`.

**Server listing:**

| Slug | Tools |
|---|---|
| `stock_research` | analyze_stock, get_price_snapshot, recall_analyses |
| `fundamentals` | get_company_fundamentals, compare_companies, recall_fundamentals |
| `options_research` | research_options, get_options_chain_data, calculate_iv_rank, recall_research |
| `watchlist` | add_ticker, remove_ticker, list_watchlist, get_watchlist_digest |
| `summarizer` | summarize_text, extract_financial_entities, classify_market_sentiment |
| `heartbeat` | check_all_agents, check_agent, get_health_report, get_health_history |
| `tester` | run_all_tests, test_agent, test_web_api, get_test_report |
| `ibkr_session` | get_connection_status, list_accounts, get_account_summary, get_session_log |
| `ibkr_positions` | get_open_positions, get_live_pnl, get_portfolio_summary, get_allocation |
| `ibkr_orders` | place_spread, get_risk_briefing, cancel_open_order, get_live_orders, get_order_history |
| `ibkr_market_data` | get_stock_conid, get_option_contract_conid, get_market_snapshot, get_option_chain, search_contract, clear_conid_cache |

### IBKR integration (ib_insync TWS socket)

All IBKR functionality goes through **IB Gateway** (TWS socket, port 4002 paper / 4001 live) via `ib_insync`. The CP Gateway (REST, port 5000) has been removed.

**Connection helpers** — `tools/ibkr_tws.py → connect_ib(client_id)`:
- Returns a cached `IB` instance keyed by `(client_id, id(event_loop))` — prevents cross-loop reuse between uvicorn and test scripts
- Default timeout 20s (paper accounts are slow)
- Client IDs 1–5 reserved: SESSION=1, POSITIONS=2, ORDERS=3, MARKET_DATA=4, OPTIONS_RESEARCH=5

**Python 3.14 / eventkit fix** — `eventkit/util.py` is patched in the venv:
- `main_event_loop` is a `_DynamicLoopProxy` that always routes to the current running loop
- `register_event_loop(loop)` must be called at server startup (already in `server.py`) so ib_insync reader threads can schedule callbacks back to uvicorn's loop
- `asyncio.set_event_loop(loop)` also called at startup for non-async thread compatibility

**IB Gateway setup:**
- Must be running (`C:\Jts\ibgateway\1039\ibgateway.exe`) and logged in
- API enabled: Configure → Settings → API → Enable ActiveX and Socket Clients, port 4002
- "Allow connections from localhost only" checked — suppresses per-connection dialog
- `TrustedIPs=127.0.0.1,10.5.0.2` in `C:\Jts\ibgateway\1039\jts.ini`

**Reuters Fundamentals (error 10358)** — not available on demo account `DUM941592`. `get_fundamentals()` tries `reqFundamentalDataAsync("ReportSnapshot")` first; falls back to Yahoo Finance. Auto-uses IB on live accounts with Reuters subscription.

**Spread construction:** `make_vertical_spread()` builds a BAG combo contract from two `ComboLeg` objects. Credit = SELL short_conid, BUY long_conid. Debit = reversed.

### Options research agent (`agents/options_research_agent.py`)

**Active agent** for all options keywords. No LLM — pure data + math:

1. `get_options_chain(ticker)` → tries IB Gateway first, falls back to yfinance
2. Filters chains by `term` param: `short` ≤ 45 DTE, `long` > 21 DTE (furthest chains)
3. `_generate_strategies()` → vertical spreads only, ranked by POP: best 3 credit + best 2 debit
4. Output HTML saved to `options_research_memory` for web history replay

**Output structure** (parts joined with `\n`):
1. Header — price, outlook, IVR, term label (`📅 Short Term` / `📆 Long Term`)
2. Expirations selector `<pre>`
3. Chain table `<pre>`
4. 5 Strategies Compared `<pre>`
5. 🏆 Recommended header — POP, net, ROC (bullets with `<br>`)
6. "Trade Structure" heading + `_fmt_detail_card()`:
   - The Legs (`<br>` per leg), How it works (`<br>` per point)
   - Key Numbers `<pre>` (block — use `<pre>` not `<code>` to ensure section separation)
   - Payoff at Expiration `[expiry date]` `<pre>`
7. P&L chart `<pre>`
8. Place Order button (HTMX form → `/api/place-order`)

`tools/options_math.py` — pure math: `bs_delta`, `bs_theta_daily`, `pop_credit_spread`, `pop_debit_spread`, `p50`, `ivr_rank`, `expected_move`.

### Fundamentals data flow

`tools/market_data.get_fundamentals(ticker)`:
1. Tries IB Gateway → `reqFundamentalDataAsync("ReportSnapshot")` → `_parse_ibkr_fundamentals_xml()`
2. Falls back to Yahoo Finance → `_fetch_fundamentals_sync()`
3. Return dict always includes `source` and `source_url` — displayed in web fundamentals card footer

### Web UI (`server.py`)

FastAPI + HTMX — routes return HTML fragments:
- `POST /search` → `asyncio.gather(OptionsResearchAgent.run(), _fundamentals_card())` — fundamentals in parallel
- `GET /positions` → positions page; `/api/positions-fragment` — 60s HTMX auto-refresh
- `POST /api/place-order` → calls `ibkr_orders.place_spread()` via ib_insync
- `GET /ibkr` → session status + order history
- `_page(history, active_tab, body_override, show_search=True)` — base template; `show_search=False` hides ticker search bar

**Positions tab columns:** Symbol, Strategy, Strikes, Expiry, DTE badge (🟢>21d / 🟡7–21d / 🔴<7d⚠), Max Profit, Max Loss, Breakeven, Qty, Net, Status. "Show cancelled" checkbox is client-side JS (`data-cancelled` attr on rows).

**Breakeven formula:**
- Credit + Put: `short_strike - abs(net)` | Credit + Call: `short_strike + abs(net)`
- Debit + Put: `max(short, long) - abs(net)` | Debit + Call: `min(short, long) + abs(net)`

Three colour themes stored in `localStorage`. `enhanceOutput()` JS runs on `htmx:afterSwap`.

### Database schema (`db/state.db`)

New tables via `CREATE TABLE IF NOT EXISTS` in `db/database.py → init_db()`. Schema changes use `ALTER TABLE ADD COLUMN` in try/except. Key tables:
- `options_research_memory` — includes `output_html` for instant web replay
- `ibkr_conid_cache` — option conids keyed by (symbol, expiry, right, strike)
- `ibkr_orders` — order history; `db/database.py → order_history()` is the canonical read function
- `conversation_turns`, `conversation_summaries` — per-chat memory with auto-compression at 16 turns

### Environment variables (`.env`)

| Key | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHANNEL_ID` | `""` | Comma-separated allowed chat IDs; empty = all |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | |
| `LLM_MODEL` | `local-model` | Must match model loaded in LM Studio |
| `ANTHROPIC_API_KEY` | — | Set; use `MCP_LLM_PROVIDER=anthropic` to activate |
| `MCP_LLM_PROVIDER` | `lmstudio` | `lmstudio` \| `anthropic` \| `openai` |
| `MCP_LLM_MODEL` | (falls back to `LLM_MODEL`) | |
| `POLYGON_API_KEY` | — | Optional; real-time quotes over yfinance |
| `IBKR_PAPER_TRADING` | `true` | Set `false` for live (real money) |
| `IBKR_TWS_HOST` | `127.0.0.1` | |
| `IBKR_TWS_PORT` | `4002` (paper) / `4001` (live) | IB Gateway socket port |
| `IBKR_GATEWAY_EXE` | `C:\Jts\ibgateway\1039\ibgateway.exe` | |
| `DB_PATH` | `db/state.db` | |

### WSL2 / networking

Runs on WSL2 with `networkingMode=mirrored` — `localhost` in WSL equals `localhost` on Windows. cloudflared auto-started by `main.py`, sets `WEB_SERVER_URL` to the live public tunnel URL.
