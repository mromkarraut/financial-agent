# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Activate the shared venv (required for all commands)
source /home/omkar/venvs/bin/activate

# Start everything (recommended)
python start.py                # TWS + web + Telegram bot (paper trading)
python start.py --no-lms      # skip LM Studio (already running)
python start.py --live        # LIVE trading account (real money — use with caution)
python start.py --live        # LIVE trading account (real money — use with caution)
python start.py --web-only    # web dashboard only, no Telegram bot

# Logs → logs/web.log

# DB migration (run after adding tables to database.py)
python -c "import asyncio; from db.database import init_db; asyncio.run(init_db())"

# Smoke-test an agent directly (no server needed)
python -c "
import asyncio
from agents.options_research_agent import OptionsResearchAgent
async def t():
    r = await OptionsResearchAgent().run({'ticker': 'AAPL', 'outlook': 'bullish', 'chat_id': 'test'})
    print(r['output'][-400:])
asyncio.run(t())
"

# Smoke-test the data_pull layer
python -c "
import asyncio
from mcp_servers.data_pull.fetch import get_stock_data, get_source_status
async def t():
    print(await get_source_status())
    print(await get_stock_data('SPY'))
asyncio.run(t())
"

# List all MCP servers and their tools
python -m mcp_servers.registry

# Run an individual MCP server manually
python -m mcp_servers.data_pull.server
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
  → _route_with_llm(text, history)   returns (reply, agents_ran: bool)
      _build_calls(text):
        options keywords?            → OptionsResearchAgent only
        ticker(s) present?           → StockResearchAgent + FundamentalsAgent (parallel)
        no match?                    → _general_reply() [LLM fallback]
  → if agents_ran: memory.clear_context(chat_id)   ← clears stale cross-ticker context
  → memory.save_turn()
  → TelegramSender.reply()           [HTML parse mode]
```

### Context clearing rule

`_route_with_llm` returns `agents_ran=True` only when `run_stock_research` or `run_options_research` succeeds with `confidence > 0`. `FundamentalsAgent` is excluded from this gate because yfinance returns a partial dict (and `confidence=0.85`) even for non-existent tickers. This prevents general questions from incorrectly clearing context.

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

`StockResearchAgent` uses local LM Studio and guards against garbage output with a `q_ratio` check, falling back to deterministic text.

`OptionsResearchAgent` and `FundamentalsAgent` use `mcp_servers.llm → get_llm_client()` — provider-agnostic, defaults to LM Studio, switch to Claude by setting `MCP_LLM_PROVIDER=anthropic` in `.env`. These agents prioritise analytical depth and pass large token budgets (700–1000 tokens).

`UIResearcherAgent`, `UITestingAgent` — no LLM dependency.

`ANTHROPIC_API_KEY` is in `.env`. Set `MCP_LLM_PROVIDER=anthropic` to switch all agents and MCP servers to Claude Sonnet.

### Data pull layer (`mcp_servers/data_pull/`)

**Centralized data ingress** — all agents that need market data must import from here, not from `tools/market_data` directly:

```python
from mcp_servers.data_pull import get_stock_data, get_fundamentals, get_options_chain
```

Priority chain:
- **Stock data**: yfinance (+ Polygon overlay if `POLYGON_API_KEY` set)
- **Fundamentals**: TWS (Reuters Refinitiv) → SEC EDGAR via edgartools → Yahoo Finance
- **Options chain**: TWS (real-time) → Yahoo Finance (delayed)

Features: in-memory TTL cache (stock 60s, fundamentals/options 5min), every fetch logged to `db/agents/data_pull.db` with source + latency + cache-hit flag.

`fetch.py` exports the three public functions. `server.py` wraps them as MCP tools (`fetch_stock`, `fetch_fundamentals`, `fetch_options_chain`, `check_data_sources`, `get_fetch_history`, `clear_ticker_cache`).

### MCP server layer

14 active MCP servers in `mcp_servers/`. (`mcp_servers/ibkr/` is a legacy directory for the old CP Gateway REST integration — it still exists on disk but is not registered or used; the four `ibkr_*` TWS-socket servers replaced it.) Claude Code auto-starts them via `.claude/settings.json`. Each server:
- Uses `FastMCP` with `instructions=` (not `description=`) for the server-level string
- Has its own per-agent SQLite DB under `db/agents/<slug>.db`
- Gets its LLM via `mcp_servers/llm.py → get_llm_client()` — never import `anthropic` directly
- Calls `_llm.complete(system, user, max_tokens)` — provider-agnostic
- Exposes a `call_log` table in its DB so heartbeat can track recency

To add a new MCP server: create `mcp_servers/<slug>/server.py`, add entry to `mcp_servers/registry.py`, add slug+DB path to `mcp_servers/heartbeat/server.py → _AGENT_DBS`, register in `.claude/settings.json`.

**Server listing:**

| Slug | Purpose |
|---|---|
| `data_pull` | Centralized market data (IBKR → yfinance); TTL cache; no LLM |
| `stock_research` | Price/RSI/MA analysis + LLM narrative |
| `fundamentals` | P/E, EPS, margins, quarterly revenue + deep LLM analysis (700 tokens) |
| `options_research` | Chain data, Black-Scholes, vertical spread ranking + deep LLM analysis (800 tokens) |
| `watchlist` | Persistent ticker list + live digest |
| `summarizer` | Text NLP — summarize, extract entities, sentiment |
| `heartbeat` | Probes all agent DBs; writes `heartbeat.db` |
| `tester` | 3-tier test runner (agent / HTTP / browser) |
| `ibkr_session` | TWS session lifecycle |
| `ibkr_positions` | Live P&L and open positions |
| `ibkr_orders` | Spread order placement + cancel |
| `ibkr_market_data` | Contract lookup, live quotes, conid cache |
| `html_css` | `hc-*` component renderer — no LLM |
| `charting` | Plotly chart generator — no LLM |

### IBKR integration (ib_insync TWS socket)

All IBKR functionality goes through **TWS** (Trader Workstation, port 7497 paper / 7496 live) via `ib_insync`.

**Connection helpers** — `tools/ibkr_tws.py → connect_ib(client_id)`:
- Returns a cached `IB` instance keyed by `(client_id, id(event_loop))` — prevents cross-loop reuse between uvicorn and test scripts
- Default timeout 20s (paper accounts are slow)
- Client IDs 1–5 reserved: SESSION=1, POSITIONS=2, ORDERS=3, MARKET_DATA=4, OPTIONS_RESEARCH=5

**Python 3.14 / eventkit fix** — `eventkit/util.py` is patched in the venv:
- `main_event_loop` is a `_DynamicLoopProxy` that always routes to the current running loop
- `register_event_loop(loop)` must be called at server startup (already in `server.py`) so ib_insync reader threads can schedule callbacks back to uvicorn's loop
- `asyncio.set_event_loop(loop)` also called at startup for non-async thread compatibility

**TWS setup — two modes:**

*WSL mode* (default when running inside WSL): `start.py` auto-launches `IBKR_TWS_EXE` via `cmd.exe` and waits up to 120s for GUI login:
- API: File → Global Configuration → API → Settings → Enable ActiveX and Socket Clients, port 7497
- "Allow connections from localhost only" checked — suppresses per-connection dialog

*Native Linux mode* (detected via `/proc/version`): TWS must be started manually on the Windows machine; `start.py` prints one-time setup instructions and waits up to 120s:
- Uncheck "Allow connections from localhost only" (remote IP, not loopback)
- Add this machine's LAN IP to `TrustedIPs` in `C:\Jts\<ver>\jts.ini`
- Set `IBKR_TWS_HOST=<windows-ip>` in `.env`

**Reuters Fundamentals (error 10358)** — not available on demo account `DUM941592`. `get_fundamentals()` tries `reqFundamentalDataAsync("ReportSnapshot")` first; falls back to Yahoo Finance. Auto-uses TWS on live accounts with Reuters subscription.

**Spread construction:** `make_vertical_spread()` builds a BAG combo contract from two `ComboLeg` objects. Credit = SELL short_conid, BUY long_conid. Debit = reversed.

### Options research agent (`agents/options_research_agent.py`)

**Active agent** for all options keywords. (`agents/options_agent.py` is a legacy file — it predates the MCP data layer and is no longer dispatched by the orchestrator; ignore it when working on options features.) Uses `mcp_servers.llm → get_llm_client()` for analysis:

1. `get_options_chain(ticker)` → TWS first, yfinance fallback (via `tools/market_data`)
2. Filters chains by `term` param: `short` ≤ 45 DTE, `long` > 21 DTE (furthest chains)
3. `_generate_strategies()` → debit-only verticals (Long Call Spread, Long Put Spread), ranked by POP
4. `_get_llm_analysis()` → 1000-token deep analysis with four structured sections:
   - **IV Environment** — IV vs HV relationship, IVR interpretation, term structure
   - **Trade Thesis** — why this strategy/strikes/expiry for this outlook; comparison to other candidates
   - **Key Levels to Watch** — breakeven(s), max profit trigger, delta sensitivity
   - **Risk Factors** — theta decay, IV crush, directional failure, exit criteria
5. Analysis injected into Telegram output (`<pre>` block) and web output (`hc-section` card after recommendation header)
6. Web output saved to `options_research_memory` for history replay

**Debit calculator panel** (`_web_debit_calculator`): interactive `hc-section` with two control rows:
- **Debit slider** (green) — adjusts net debit per share; range `0.05` to `spread - 0.01`
- **Qty slider** (blue) — adjusts number of contracts (1–20); syncs with the order form's `quantity` field

The JS `calcUpdate(debit, qty)` in `server.py` updates: metric grid, Key Numbers table, Payoff table, Plotly chart — all multiplied by `qty`. Delta and theta also scale by contracts.

`tools/options_math.py` — pure math: `bs_delta`, `bs_theta_daily`, `pop_credit_spread`, `pop_debit_spread`, `p50`, `ivr_rank`, `expected_move`.

### Fundamentals agent (`agents/fundamentals_agent.py`)

**v3.0.0** — uses `mcp_servers.llm → get_llm_client()` (not local LM Studio directly). No `q_ratio` garbage check — provider-agnostic client handles quality.

Prompt feeds: all 6 quarters of revenue with QoQ deltas, gross + net margins, PE / fwd PE, D/E, computed PEG-like ratio (PE ÷ revenue growth), revenue trend label (accelerating / stable / decelerating).

LLM produces four structured sections (700 tokens):
- **Valuation Assessment** — cheap/fair/expensive, PE compression from trailing→forward, PEG interpretation, margin vs sector benchmarks
- **Revenue Quality** — quarter-by-quarter trajectory, inflection points, whether momentum justifies valuation
- **Profitability and Efficiency** — gross margin → pricing power, step-down to net margin → cost structure, D/E → leverage risk
- **Catalysts and Risks** — 2 specific catalysts + 2 specific risks with exact numbers

### Web UI (`server.py`)

FastAPI + HTMX — routes return HTML fragments. All styling uses the **`hc-*` design system** (defined in `_CSS`):

| Class | Purpose |
|---|---|
| `hc-section` | Bordered card with header |
| `hc-section-header` | Card header — flex row, uppercase dim text |
| `hc-metric-grid` + `hc-metric` | Key metric badges |
| `hc-table` + `hc-table-wrap` | Data tables — first col left-aligned, rest right |
| `hc-badge-{green,red,yellow,blue,dim}` | Coloured pill badges |
| `hc-alert-{info,warning,success,error}` | Alert strips |
| `hc-row-{profit,loss,current,be,max}` | Table row colour classes |

`tools/html_components.py` exposes Python helpers (`section_card`, `metric_grid`, `data_table`, `alert`, `legs_list`, etc.) that emit the same `hc-*` HTML. Use these in agents producing web output.

**Key routes:**
- `POST /search` → `asyncio.gather(OptionsResearchAgent.run(), _fundamentals_card())` — options + fundamentals in parallel
- `GET /positions` / `GET /api/positions-fragment` — live IBKR P&L + positions as `hc-metric-grid` + `hc-table`; 60s HTMX auto-refresh
- `POST /api/place-order` → `ibkr_orders.place_spread()` via ib_insync
- `GET /ibkr` → session status + order history

**Positions tab** pulls structured data via `get_pnl_dict()` / `get_positions_dict()` from `mcp_servers/ibkr_positions/server.py` (not the text-returning MCP tools). Strategy history table uses `pos-table` class with `data-cancelled` attr for the toggle.

**Breakeven formula:**
- Credit + Put: `short_strike - abs(net)` | Credit + Call: `short_strike + abs(net)`
- Debit + Put: `max(short, long) - abs(net)` | Debit + Call: `min(short, long) + abs(net)`

Three colour themes stored in `localStorage`. `enhanceOutput()` JS runs on `htmx:afterSwap`.

### Database schema (`db/state.db`)

New tables via `CREATE TABLE IF NOT EXISTS` in `db/database.py → init_db()`. Schema changes use `ALTER TABLE ADD COLUMN` in try/except. Key tables:
- `options_research_memory` — includes `output_html` for instant web replay
- `ibkr_conid_cache` — option conids keyed by (symbol, expiry, right, strike)
- `ibkr_orders` — order history; `db/database.py → order_history()` is the canonical read function
- `conversation_turns`, `conversation_summaries` — per-chat memory; `db/memory.py → MemoryManager`
  - `clear_context(chat_id)` — called automatically after successful stock/options research

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
| `MCP_LLM_BASE_URL` | (falls back to `LMSTUDIO_BASE_URL`) | Base URL for lmstudio/openai providers |
| `MCP_LLM_MAX_TOKENS` | `512` | Default token budget per MCP server completion |
| `OPENAI_API_KEY` | — | Required when `MCP_LLM_PROVIDER=openai` |
| `POLYGON_API_KEY` | — | Optional; real-time quotes over yfinance |
| `EDGAR_IDENTITY` | `Financial Agent research@example.com` | SEC EDGAR User-Agent (`Name email`); required by SEC fair-use policy |
| `IBKR_PAPER_TRADING` | `true` | Set `false` for live (real money) |
| `IBKR_TWS_HOST` | `127.0.0.1` | |
| `IBKR_TWS_PORT` | `7497` (paper) / `7496` (live) | TWS socket port |
| `IBKR_TWS_EXE` | `C:\Jts\tws.exe` | TWS exe path (WSL mode only) |
| `DB_PATH` | `db/state.db` | |

### WSL2 / networking

`start.py` auto-detects WSL via `/proc/version` and picks the appropriate IBKR launch strategy. In WSL2 with `networkingMode=mirrored`, `localhost` in WSL equals `localhost` on Windows. In native Linux, set `IBKR_TWS_HOST` to the Windows machine's LAN IP. cloudflared is auto-started by `main.py` and sets `WEB_SERVER_URL` to the live public tunnel URL.

## Known issues and future improvements

### Data layer
- **`data_pull` cache is in-process only** — each MCP server process and the uvicorn server maintain separate `_cache` dicts. A short-TTL Redis or SQLite-backed cache would deduplicate IBKR calls across processes.
- **`FundamentalsAgent` confidence bug** — `yf.Ticker(x).info` returns a partial dict even for non-existent tickers, so `FundamentalsAgent` always returns `confidence=0.85`. The fix is to validate that `company_name != ticker` and `market_cap is not None` before reporting success.
- **yfinance options chain missing Greeks** — when IBKR is unavailable, the yfinance fallback returns bid/ask/IV but no delta/gamma/theta/vega. Black-Scholes approximations from `tools/options_math.py` could fill these in from the IV.
- **TWS paper account limitations** — error 10358 (Reuters fundamentals not available on DU* accounts) and error 10089 (market data subscription missing for options) are expected on `DUM941592`. Positions show `$0.00` unrealized P&L because live quotes require a market data subscription.
- **TWS generic tick list error 321** — options contracts reject `genericTickList="106,107"` on paper accounts; only `106` (impvolat) is valid for OPT. `tools/ibkr_options.py` should be updated to request only `"106"`.

### Orchestrator / routing
- **Ticker regex false positives** — `_TICKER_RE` matches any 2–5 uppercase letter sequence. Common English words not in `_SKIP_WORDS` (e.g. "WHAT", "DOES", "RANK") get extracted as tickers and dispatch agents that fail. Extending `_SKIP_WORDS` or requiring a `$` prefix for unlabelled tickers would reduce noise.
- **Conversation memory is Telegram-only** — the web dashboard (`server.py`) has no session memory; each `/search` is stateless. A session cookie + DB-backed context would let the web UI remember the previous ticker for follow-up questions.
- **No web-side context clearing** — `memory.clear_context()` only fires in the Telegram path. If the same chat_id is shared between web and Telegram, web searches don't reset the Telegram memory.

### Options research
- **LLM analysis adds latency** — `_get_llm_analysis()` runs sequentially after strategy generation (not parallelised with data fetching). Response time is now dominated by the LLM call (~2–5s on LM Studio, ~1–2s on Claude). If speed matters, the LLM call can be fire-and-forget with streaming.
- **Debit spreads only** — `_generate_strategies()` only builds Long Call Spread and Long Put Spread. Credit spreads (bull-put, bear-call), iron condors, and calendars are not generated.
- **Qty slider max is 20** — hardcoded in `_web_debit_calculator`. The order form itself accepts up to 100 but the slider stops at 20; a user typing in the number input can exceed this.
- **No multi-leg Plotly chart** — the P&L chart in the calc panel shows a single-spread payoff. For iron condors or future multi-leg strategies, the chart would need to sum leg payoffs.

### Web UI
- **`hc-table td:first-child` override required for custom tables** — the design system sets `color: var(--dim)` on first-child cells; any table that needs a non-dim first column must add a CSS override (e.g. `.pos-live-table td:first-child`). Document this pattern when adding new tables.
- **Plotly charts not theme-aware** — chart backgrounds and axis colours are hardcoded; they don't update when the user switches between dark/light/blue themes via the `localStorage` toggle.
- **HTMX positions fragment polls every 60s regardless of visibility** — the auto-refresh trigger fires even when the positions tab is not active, causing unnecessary IBKR connections in the background.

### MCP / heartbeat
- **Heartbeat probes DB recency, not liveness** — ✅ fixed: `_process_running(module_arg)` in `heartbeat/server.py` scans `/proc/*/cmdline` for each server's module; result shown as `process: live | not running` in the detail string. Not-running is normal when the server hasn't been invoked yet.
- **`ibkr` registry entry is stale** — ✅ fixed: removed the `ibkr` slug (old CP Gateway / REST). Registry now has 14 agents. Heartbeat now monitors all four TWS-based IBKR agents (`ibkr_session`, `ibkr_positions`, `ibkr_orders`, `ibkr_market_data`) plus `tester` — none of which were previously tracked.
