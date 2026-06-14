# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Activate the shared venv (required for all commands)
source /home/omkar/venvs/bin/activate

# Prerequisites — must be started first:
lms server start          # starts LM Studio API on Windows side (alias in ~/.bashrc → /mnt/c/.../lms.exe)

# Telegram bot (also auto-starts a cloudflared tunnel and sets WEB_SERVER_URL)
python main.py

# Web dashboard at http://localhost:8000
python server.py
# or with auto-reload:
uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Run UI test suite (hits the live server, takes ~10s)
python -c "
import asyncio, re
from agents.ui_testing_agent import UITestingAgent
async def run():
    r = await UITestingAgent().run_all()
    print(re.sub(r'<[^>]+>', '', r))
asyncio.run(run())
"

# Quick agent smoke-test (no server needed)
python -c "
import asyncio
from agents.options_research_agent import OptionsResearchAgent
async def t():
    r = await OptionsResearchAgent().run({'ticker': 'AAPL', 'outlook': 'bullish', 'chat_id': 'test'})
    print(r['output'][-400:])
asyncio.run(t())
"

# DB migration (run after adding tables to database.py)
python -c "import asyncio; from db.database import init_db; asyncio.run(init_db())"
```

## Architecture

### Two entry points, one shared DB

| Process | File | Purpose |
|---|---|---|
| Telegram bot | `main.py` | Receives messages, calls `Orchestrator`, sends HTML replies |
| Web dashboard | `server.py` | FastAPI + HTMX UI; reads same `db/state.db` |

Both processes share `db/state.db` (SQLite). The web server also fires a background `IBKRAgent.tickle_loop()` to keep the IBKR CP Gateway session alive.

### Message flow (Telegram)

```
Telegram message
  → main.py._handle_text()
  → Orchestrator.process()
  → _try_watchlist_fast_path()        [regex — no LLM]  → WatchlistAgent
  → if len(text) > LONG_MESSAGE_THRESHOLD (300):
      → SummarizerAgent               [LLM — extracts 3 bullet points]
  → memory.get_context(chat_id)       [load summary + recent 6 turns]
  → _route_with_llm(working_text)
      → _build_calls(text)            [regex routing]
          options keywords present?   → OptionsResearchAgent only
          ticker(s) present?          → StockResearchAgent + FundamentalsAgent (parallel)
          no match?                   → _general_reply()  [LLM fallback for general questions]
  → asyncio.gather(*agent_calls)
  → "\n\n".join(outputs)
  → memory.save_turn()                [then compress_if_needed()]
  → TelegramSender.reply()            [HTML parse mode]
```

### Routing rules (orchestrator.py)

- Ticker detection runs on `text.upper()` to catch lowercase input.
- `_SKIP_WORDS` contains ~40 options/analysis terms (CALL, PUT, BULL, BEAR, SPREAD, OTM, ATM, DTE, …) that are **not** tickers — always extend this set when adding new keyword routes to avoid false positive dispatch.
- When options keywords are detected, **only** `OptionsResearchAgent` is dispatched (skip stock/fundamentals to avoid mixing LLM garbage with clean HTML).
- `_general_reply` falls back without conversation history if LM Studio returns 400 (corrupted memory guard).
- `TELEGRAM_CHANNEL_ID` (comma-separated) in `.env` restricts which chats the bot responds to; empty = all chats.

### Agent contract

Every agent returns `AgentResult` (TypedDict in `agents/base_agent.py`):

```python
{"agent": str, "version": str, "output": str,
 "confidence": float,   # 0.0 = hard failure
 "metadata": dict}
```

`output` is always Telegram HTML (`<b>`, `<i>`, `<code>`, `<pre>` tags). The same HTML renders correctly in the browser because those tags are valid HTML.

### LLM situation

The local model (via LM Studio) frequently outputs `?????` garbage. Both `StockResearchAgent` and `FundamentalsAgent` guard against this:

```python
q_ratio = raw.count("?") / max(len(raw), 1)
if not raw or q_ratio > 0.3:
    # fall back to deterministic rule-based text
```

`OptionsResearchAgent`, `UIResearcherAgent`, `UITestingAgent`, and `IBKRAgent` have **zero LLM dependency** — pure data + math. `ANTHROPIC_API_KEY` is already in `.env` for future Claude API swap.

### Options agents — two exist, one is active

`agents/options_agent.py` (`OptionsAgent`) — legacy agent: ATM-based chain table + simple spread charts. Registered in `_dispatch` as `"run_options_analysis"` but **never dispatched** by `_build_calls`. Still functional if called directly.

`agents/options_research_agent.py` (`OptionsResearchAgent`) — **active agent**, dispatched for all options keywords. No LLM calls — only yfinance + Black-Scholes:

1. `get_options_chain(ticker)` → fetches calls/puts for 4 expirations + 52-week rolling HV
2. `ivr_rank(current_iv, hv_series)` from `tools/options_math.py`
3. `_generate_strategies()` → produces **vertical spreads only** (`_is_vertical()` guard):
   - Generates Narrow + Wide variants per expiration per type
   - Uses per-leg `impliedVolatility` (not ATM-only) for accurate Greeks
   - Ranks: best 3 credit verticals (by POP) + best 2 debit verticals (by POP)
4. Output includes `output_html` saved to `options_research_memory` for instant web history replay

`tools/options_math.py` — pure math module: `bs_delta`, `bs_theta_daily`, `pop_credit_spread`, `pop_debit_spread`, `p50` (tastytrade-style: POP + 32% of remaining), `ivr_rank`, `expected_move`.

### Conversation memory (db/memory.py)

`MemoryManager` keeps per-chat context across turns:

- Stores raw turns in `conversation_turns`; compresses into a summary when count exceeds 16
- After compression: keeps the most recent 6 turns raw, deletes the rest, saves LLM summary to `conversation_summaries`
- Periodic sweep every 30 min (`_periodic_compression` task in `main.py`) catches any chats that didn't auto-compress
- `get_context()` returns `[summary_message, recent_turns...]` ready to prepend to any LLM call

### Web UI (server.py)

HTMX-powered — routes return HTML fragments, not JSON:

- `POST /search` returns `<div class="result-wrap">…</div>` + `<div id="history-list" hx-swap-oob="true">…</div>` in one response
- Three colour themes (🌙/💚/🟡) stored in `localStorage`, applied as body CSS class
- `enhanceOutput()` JS runs on `htmx:afterSwap`: colours `+$` green, `-$` red, `%` yellow; highlights `⭐` and `◀ATM` rows with left-border spans inside `<pre>` blocks
- Mobile breakpoint at 660px: sidebar becomes a slide-in drawer (`position:fixed`, toggled by `☰` hamburger)

### IBKRAgent (agents/ibkr_agent.py)

Connects to the CP Gateway at `https://localhost:5000` (self-signed cert, `verify=False`).

Key facts to keep correct:
- **USD spread conid = `28812380`** (permanent, hardcoded by IBKR, never changes)
- `conidex` format: `"28812380;;;{sell_conid}/-1,{buy_conid}/1"` — negative ratio = sell that leg
- Three-step contract lookup required: `secdef/search` → `secdef/strikes` (warms session) → `secdef/info`; results cached in `ibkr_conid_cache`
- Order placement is two-step: first POST may return `{"id": …, "message": […]}` requiring a `POST /iserver/reply/{id} {"confirmed": true}` before the order is accepted
- Session times out in ~5-6 min; `tickle_loop()` runs every 55s in background

### Database schema (db/state.db)

New tables are added in `db/database.py → init_db()` using `CREATE TABLE IF NOT EXISTS`. Schema changes to existing tables use `ALTER TABLE ADD COLUMN` wrapped in try/except (non-destructive migration pattern). Tables:

- `messages`, `agent_logs` — audit logs
- `watchlist` — persisted tickers
- `conversation_turns`, `conversation_summaries` — per-chat LLM memory with auto-compression
- `options_research_memory` — options research history including `output_html` for instant web replay
- `ui_research_memory` — UIResearcherAgent findings (5 seeded, all ✅ implemented)
- `ui_test_results` — UITestingAgent results with pass/fail + duration_ms
- `ibkr_conid_cache` — option conid cache keyed by (symbol, expiry, right, strike)
- `ibkr_orders` — order history with ibkr_order_id and status

### Environment variables (.env)

| Key | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `TELEGRAM_CHANNEL_ID` | `""` | Comma-separated allowed chat IDs; empty = all chats |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | LM Studio OpenAI-compat API |
| `LLM_MODEL` | `local-model` | Must match model loaded in LM Studio |
| `LLM_MAX_TOKENS` | `2048` | Max tokens for agent LLM calls |
| `LLM_ORCHESTRATOR_MAX_TOKENS` | `1024` | Max tokens for orchestrator/routing LLM calls |
| `LONG_MESSAGE_THRESHOLD` | `300` | Messages longer than this are auto-summarized before routing |
| `ANTHROPIC_API_KEY` | — | Set; ready for Claude API swap |
| `WEB_SERVER_URL` | `http://localhost:8000` | Auto-overwritten at bot startup by cloudflared tunnel URL |
| `DB_PATH` | `db/state.db` | |
| `POLYGON_API_KEY` | — | Optional; enables real-time quotes over yfinance |

### WSL2 / networking

This runs on WSL2 with `networkingMode=mirrored` (`~/.wslconfig`). `localhost` in WSL equals `localhost` on Windows — no proxy needed. The `lms` alias in `~/.bashrc` calls `/mnt/c/Users/User/AppData/Local/Programs/LM Studio/resources/app/.webpack/lms.exe` directly via WSL interop. cloudflared is installed at `~/.local/bin/cloudflared` and is auto-started by `main.py` on bot startup, setting `WEB_SERVER_URL` to the live public tunnel URL.
