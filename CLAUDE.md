# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Activate the shared venv (required for all commands)
source /home/omkar/venvs/bin/activate

# Start everything (recommended)
python start.py            # LM Studio + TWS + web + Telegram bot (paper trading)
python start.py --no-lms  # skip LM Studio (already running)
python start.py --live    # LIVE trading account (real money — use with caution)
python start.py --web-only # web dashboard only, no Telegram bot

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

# Start/stop LM Studio server manually
/home/omkar/.lmstudio/bin/lms server start
/home/omkar/.lmstudio/bin/lms server stop
```

## Architecture

### Two entry points, one shared DB

| Process | File | Purpose |
|---|---|---|
| Telegram bot | `main.py` | Receives messages, calls `Orchestrator`, sends HTML replies |
| Web dashboard | `server.py` | FastAPI + HTMX UI; reads same `db/state.db` |

Both share `db/state.db` (SQLite). `start.py` orchestrates all services and handles Ctrl+C shutdown. It loads `.env` via `dotenv` before reading any env vars.

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

`_route_with_llm` returns `agents_ran=True` only when `run_stock_research` or `run_options_research` succeeds with `confidence > 0`. `FundamentalsAgent` is excluded from this gate because yfinance returns a partial dict (and `confidence=0.85`) even for non-existent tickers.

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

`agents/options_agent.py` is a legacy file — predates the MCP data layer and is no longer dispatched. Use `agents/options_research_agent.py` for all options work.

### LLM situation

**LM Studio** — installed natively at `/home/omkar/.lmstudio/bin/lms`. Currently loaded model: `qwen/qwen3-vl-4b` on port 1234.

`StockResearchAgent` uses LM Studio directly via `config.LMSTUDIO_BASE_URL` and guards against garbage output with a `q_ratio` check, falling back to deterministic text.

`OptionsResearchAgent` and `FundamentalsAgent` use `mcp_servers/llm.py → get_llm_client()` — provider-agnostic. Switch providers by setting `MCP_LLM_PROVIDER` in `.env`:
- `lmstudio` (default) — local model via LM Studio
- `anthropic` — Claude Sonnet via `ANTHROPIC_API_KEY`
- `openai` — OpenAI via `OPENAI_API_KEY`

All MCP servers use the same interface: `await _llm.complete(system, user, max_tokens)`. Never import `anthropic` directly in MCP servers.

### Data pull layer (`mcp_servers/data_pull/`)

**Centralized data ingress** — all agents that need market data must import from here, not from `tools/market_data` directly:

```python
from mcp_servers.data_pull import get_stock_data, get_fundamentals, get_options_chain
```

Priority chain:
- **Stock data**: yfinance (+ Polygon overlay if `POLYGON_API_KEY` set)
- **Fundamentals**: TWS (Reuters Refinitiv) → SEC EDGAR via edgartools → Yahoo Finance
- **Options chain**: TWS (real-time) → Yahoo Finance (delayed)

In-memory TTL cache (stock 60s, fundamentals/options 5min). Every fetch logged to `db/agents/data_pull.db` with source + latency + cache-hit flag. Cache is in-process only — each MCP server process has its own cache dict.

`fetch.py` exports the three public functions. `server.py` wraps them as MCP tools.

### MCP server layer

14 active MCP servers in `mcp_servers/`. Claude Code auto-starts them via `.claude/settings.json`. `mcp_servers/ibkr/` is a legacy directory — not registered, not used. Each server:
- Uses `FastMCP` with `instructions=` (not `description=`) for the server-level string
- Has its own per-agent SQLite DB under `db/agents/<slug>.db`
- Gets its LLM via `mcp_servers/llm.py → get_llm_client()` — never import `anthropic` directly
- Exposes a `call_log` table in its DB so heartbeat can track recency

To add a new MCP server: create `mcp_servers/<slug>/server.py`, add entry to `mcp_servers/registry.py`, add slug+DB path to `mcp_servers/heartbeat/server.py → _AGENT_DBS`, register in `.claude/settings.json`.

**Server listing:**

| Slug | Purpose |
|---|---|
| `data_pull` | Centralized market data (TWS → yfinance); TTL cache; no LLM |
| `stock_research` | Price/RSI/MA analysis + LLM narrative |
| `fundamentals` | P/E, EPS, margins, quarterly revenue + deep LLM analysis (700 tokens) |
| `options_research` | Chain data, Black-Scholes, vertical spread ranking + deep LLM analysis (800 tokens) |
| `watchlist` | Persistent ticker list + live digest |
| `summarizer` | Text NLP — summarize, extract entities, sentiment |
| `heartbeat` | Probes all agent DBs + TWS connectivity; writes `heartbeat.db` |
| `tester` | 3-tier test runner (agent / HTTP / browser) |
| `ibkr_session` | TWS session lifecycle |
| `ibkr_positions` | Live P&L and open positions |
| `ibkr_orders` | Spread order placement + cancel |
| `ibkr_market_data` | Contract lookup, live quotes, conid cache |
| `html_css` | `hc-*` component renderer — no LLM |
| `charting` | Plotly chart generator — no LLM |

### IBKR integration (ib_insync TWS socket)

All IBKR functionality goes through **TWS** (Trader Workstation, port 7497 paper / 7496 live) via `ib_insync`. Currently running on Windows at `192.168.8.249:7497` (set via `IBKR_TWS_HOST` in `.env`).

**Connection helpers** — `tools/ibkr_tws.py → connect_ib(client_id)`:
- Returns a cached `IB` instance keyed by `(client_id, id(event_loop))` — prevents cross-loop reuse between uvicorn and test scripts
- Default timeout 20s (paper accounts are slow)
- Client IDs reserved: SESSION=1, POSITIONS=2, ORDERS=3, MARKET_DATA=4, OPTIONS_RESEARCH=5

**Python 3.14 / eventkit fix** — `eventkit/util.py` is patched in the venv:
- `main_event_loop` is a `_DynamicLoopProxy` that always routes to the current running loop
- `register_event_loop(loop)` must be called at server startup (already in `server.py`) so ib_insync reader threads can schedule callbacks back to uvicorn's loop

**TWS setup — two modes:**

*WSL mode* — `start.py` auto-launches `IBKR_TWS_EXE` via `cmd.exe`:
- API: File → Global Configuration → API → Settings → Enable ActiveX and Socket Clients, port 7497
- "Allow connections from localhost only" checked

*Native Linux mode* (current) — TWS runs on a separate Windows machine:
- Uncheck "Allow connections from localhost only"
- Add this machine's LAN IP to `TrustedIPs` in `C:\Jts\<ver>\jts.ini`
- Set `IBKR_TWS_HOST=<windows-ip>` in `.env`

**Known TWS limitations on paper account `DUM941592`:**
- Error 10358: Reuters fundamentals not available → falls back to SEC EDGAR then Yahoo Finance
- Error 10089: market data subscription missing for options → yfinance fallback
- Positions show `$0.00` unrealized P&L (live quotes require subscription)
- Error 321: options reject `genericTickList="106,107"` — only `"106"` (impvolat) valid for OPT

**Spread construction:** `make_vertical_spread()` builds a BAG combo contract from two `ComboLeg` objects. Credit = SELL short_conid, BUY long_conid. Debit = reversed.

### Options research agent (`agents/options_research_agent.py`)

1. `get_options_chain(ticker)` → TWS first, yfinance fallback
2. Filters chains by `term` param: `short` ≤ 45 DTE, `long` > 21 DTE
3. `_generate_strategies()` → debit-only verticals (Long Call Spread, Long Put Spread), ranked by POP
4. `_get_llm_analysis()` → 1000-token deep analysis: IV Environment, Trade Thesis, Key Levels, Risk Factors
5. Analysis injected into Telegram output (`<pre>` block) and web output (`hc-section` card)
6. Web output saved to `options_research_memory` for history replay

**Debit calculator panel** (`_web_debit_calculator`): two sliders — debit (green, `0.05` to `spread - 0.01`) and qty (blue, 1–20). JS `calcUpdate(debit, qty)` in `server.py` updates metric grid, Key Numbers, Payoff table, Plotly chart — all multiplied by qty.

`tools/options_math.py` — pure math: `bs_delta`, `bs_theta_daily`, `pop_credit_spread`, `pop_debit_spread`, `p50`, `ivr_rank`, `expected_move`.

### Fundamentals agent (`agents/fundamentals_agent.py`)

Uses `mcp_servers.llm → get_llm_client()`. Prompt feeds 6 quarters of revenue with QoQ deltas, gross + net margins, PE / fwd PE, D/E, PEG-like ratio. LLM produces 700 tokens across four sections: Valuation Assessment, Revenue Quality, Profitability and Efficiency, Catalysts and Risks.

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

**`hc-table td:first-child` override pattern** — the design system dims first-child cells; tables needing a non-dim first column must add a CSS override (e.g. `.pos-live-table td:first-child`).

**Key routes:**
- `POST /search` → `asyncio.gather(OptionsResearchAgent.run(), _fundamentals_card())` — options + fundamentals in parallel
- `GET /positions` / `GET /api/positions-fragment` — live IBKR P&L; 60s HTMX auto-refresh
- `POST /api/place-order` → `ibkr_orders.place_spread()` via ib_insync
- `GET /ibkr` → session status + order history

**Positions tab** pulls structured data via `get_pnl_dict()` / `get_positions_dict()` from `mcp_servers/ibkr_positions/server.py` (not the text-returning MCP tools).

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
| `MCP_LLM_BASE_URL` | (falls back to `LMSTUDIO_BASE_URL`) | |
| `MCP_LLM_MAX_TOKENS` | `512` | Default token budget per MCP server completion |
| `OPENAI_API_KEY` | — | Required when `MCP_LLM_PROVIDER=openai` |
| `POLYGON_API_KEY` | — | Optional; real-time quotes overlay on yfinance |
| `EDGAR_IDENTITY` | `Financial Agent research@example.com` | SEC EDGAR User-Agent (`Name email`); required by SEC fair-use policy |
| `IBKR_PAPER_TRADING` | `true` | Set `false` for live (real money) |
| `IBKR_TWS_HOST` | `127.0.0.1` | Set to Windows machine IP on native Linux |
| `IBKR_TWS_PORT` | `7497` (paper) / `7496` (live) | |
| `IBKR_TWS_EXE` | `C:\Jts\tws.exe` | WSL mode only |
| `DB_PATH` | `db/state.db` | |

### WSL2 / networking

`start.py` auto-detects WSL via `/proc/version`. In native Linux mode, set `IBKR_TWS_HOST` to the Windows machine's LAN IP. cloudflared is auto-started by `main.py` and sets `WEB_SERVER_URL` to the live public tunnel URL.

## Known issues and future improvements

### Data layer
- **`data_pull` cache is in-process only** — each MCP server process maintains a separate `_cache` dict; a Redis or SQLite-backed cache would deduplicate IBKR calls across processes.
- **`FundamentalsAgent` confidence bug** — `yf.Ticker(x).info` returns a partial dict even for non-existent tickers, so `FundamentalsAgent` always returns `confidence=0.85`. Fix: validate `company_name != ticker` and `market_cap is not None`.
- **yfinance options chain missing Greeks** — yfinance fallback returns bid/ask/IV but no delta/gamma/theta/vega. `tools/options_math.py` Black-Scholes approximations could fill these in.

### Orchestrator / routing
- **Ticker regex false positives** — `_TICKER_RE` matches any 2–5 uppercase letter sequence. Extending `_SKIP_WORDS` or requiring a `$` prefix would reduce noise.
- **Conversation memory is Telegram-only** — the web dashboard has no session memory; each `/search` is stateless.

### Options research
- **LLM analysis adds latency** — `_get_llm_analysis()` runs sequentially after strategy generation (~2–5s on LM Studio, ~1–2s on Claude).
- **Debit spreads only** — `_generate_strategies()` only builds Long Call Spread and Long Put Spread. Credit spreads, iron condors, and calendars are not generated.
- **Qty slider max is 20** — hardcoded in `_web_debit_calculator`; the order form accepts up to 100.

### Web UI
- **Plotly charts not theme-aware** — chart backgrounds and axis colours are hardcoded; don't update when the user switches themes.
- **HTMX positions fragment polls every 60s regardless of tab visibility** — causes unnecessary IBKR connections in the background.
