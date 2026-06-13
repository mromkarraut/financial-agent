# Financial Agent 🤖

A modular, multi-agent financial research bot that runs entirely on your machine.
Send a stock ticker to your Telegram bot and get price snapshots, fundamental analysis,
quarterly revenue trends, and options strategies — powered by a local LLM via LM Studio.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Agents](#agents)
- [Memory System](#memory-system)
- [Setup](#setup)
- [Configuration](#configuration)
- [Usage](#usage)
- [File Layout](#file-layout)
- [Database Schema](#database-schema)
- [Extending the System](#extending-the-system)

---

## How It Works

```
You (Telegram)
      │
      │  "$AAPL"
      ▼
┌─────────────────────────────────────────────────────┐
│                    Orchestrator                      │
│                                                     │
│  1. Fast-path regex check (watchlist commands)      │
│  2. Auto-summarize if message > 300 chars           │
│  3. Load conversation memory for this chat          │
│  4. Detect tickers + keywords via regex routing     │
│  5. Dispatch agents in parallel                     │
│  6. Concatenate results → send reply                │
│  7. Save turn to memory, compress if needed         │
└──────────────┬──────────────────────────────────────┘
               │ parallel asyncio.gather()
       ┌───────┴────────┐
       ▼                ▼
StockResearchAgent   FundamentalsAgent
  (price, RSI,        (PE, revenue,
   MAs, volume)        margins, D/E,
                       quarterly trend)
       │                │
       └───────┬────────┘
               ▼
        LM Studio API
        (Qwen 0.5B — local, no internet)
               │
               ▼
        Telegram reply
```

---

## Architecture

<details>
<summary><b>Routing — how messages are classified</b></summary>

The orchestrator never sends the user message to the LLM to decide what to do.
Instead it uses fast regex patterns, which makes routing instant and reliable
even on a tiny local model.

| Pattern | Action |
|---|---|
| `watch $TICKER` | Watchlist add (no LLM call) |
| `unwatch $TICKER` | Watchlist remove (no LLM call) |
| `watchlist` / `check watchlist` | Watchlist check (no LLM call) |
| Message > 300 chars | Auto-summarize first, then route tickers |
| `$TICKER` or `AAPL` style words | StockResearch + Fundamentals agents |
| + `options`/`calls`/`puts`/`spread`/`hedge` | Also runs OptionsAgent |
| No tickers found | General LLM reply (finance assistant fallback) |

Common financial acronyms (`PE`, `RSI`, `ETF`, `FED`, `GDP`…) are excluded from ticker detection to avoid false matches.

</details>

<details>
<summary><b>LLM calls — template fill-in prompts</b></summary>

Because the local model (Qwen 0.5B) is small, prompts pre-compute the factual
decisions and ask the model only to complete a structured template:

```
Fill in the blanks only. No extra text.

Trend: AAPL at $291 is below MA20 ($303) and above MA50 ($285), indicating ___.
Momentum: RSI 44 is neutral, so near-term price action looks ___.
Stance: Neutral — ___.
```

This produces concise, on-topic completions instead of hallucinated narratives.
All token-expensive synthesis between agents is skipped — each agent output is
concatenated directly.

</details>

<details>
<summary><b>Data sources</b></summary>

| Data | Source | Latency |
|---|---|---|
| Price, RSI, MAs, volume | yfinance (15-min delayed) | ~1-2s |
| Fundamentals, PE, margins | yfinance `.info` | ~1-2s |
| Quarterly revenue | yfinance `.quarterly_financials` | ~1-2s |
| Options chain, IV, expirations | yfinance `.options` | ~1s |
| Real-time price (optional) | Polygon.io (set `POLYGON_API_KEY`) | <100ms |

RSI-14 and moving averages are computed locally from 1-year daily history.
TA-Lib is used if installed; otherwise falls back to a pure-Python EWM implementation.

</details>

---

## Agents

<details>
<summary><b>StockResearchAgent</b></summary>

**Trigger**: any stock ticker in the message.

**Data fetched**: current price, % change, 52-week range, RSI-14, MA-20, MA-50, volume vs 30-day average.

**Output**:
```
Apple Inc. (AAPL)
Price: $291.13  (-1.52%)
52W: $194.87 – $315.20  RSI-14: 44.09
MA20: $303.88  MA50: $285.36

Trend: AAPL is below MA20 and above MA50, indicating consolidation.
Momentum: RSI 44 is neutral, so near-term price action looks range-bound.
Stance: Neutral — price sits between key averages with no clear breakout.
```

</details>

<details>
<summary><b>FundamentalsAgent</b></summary>

**Trigger**: any stock ticker in the message (runs in parallel with StockResearchAgent).

**Data fetched**: PE, forward PE, EPS (TTM + forward), revenue growth YoY, debt/equity, profit margin, gross margin, market cap, quarterly revenue for last 4-6 quarters.

**Output**:
```
Fundamentals — Apple Inc. (AAPL)
Sector: Technology  Mkt Cap: $4275.9B
PE: 35.2  Fwd PE: 30.34  D/E: 79.55
Rev Growth YoY: 5.04%  Profit Margin: 27.15%  Gross Margin: 47.86%

Quarterly Revenue
  2025-06-30: $50.32B (-0.3%)
  2025-09-30: $54.12B (+7.6%)
  2025-12-31: $74.53B (+37.7%)   ← holiday quarter
  2026-03-31: $56.40B (-24.3%)

Valuation: PE 35.2 vs fwd PE 30.34 — stretched relative to sector.
Revenue: 5.04% YoY, accelerating trend, strong margins (27.15%).
Risk: Dependence on iPhone revenues in a saturating smartphone market.
```

The quarterly revenue table is built directly from data — no LLM involved.
Only the 3-line analysis at the bottom is LLM-generated.

</details>

<details>
<summary><b>OptionsAgent</b></summary>

**Trigger**: ticker + any of `options`, `calls`, `puts`, `spread`, `hedge`.

**Inferred outlook**: scans message for `bullish`/`buy`/`long` → bullish, `bearish`/`sell`/`short` → bearish, else neutral.

**Data fetched**: current price, available expirations, implied volatility (if available), beta.

**Output**: 2 option spread strategies with specific strikes, max profit/loss, and rationale.

</details>

<details>
<summary><b>SummarizerAgent</b></summary>

**Trigger**: message length > `LONG_MESSAGE_THRESHOLD` (default 300 chars). Runs automatically before routing.

**Output**: 3 bullet points extracting the key investor-relevant facts. The compressed summary is then routed like a normal message for ticker detection.

</details>

<details>
<summary><b>WatchlistAgent</b></summary>

**Trigger**: `watch $TICKER`, `unwatch $TICKER`, `check watchlist` — intercepted before any LLM call.

**Storage**: SQLite `watchlist` table, persisted across restarts.

**`check watchlist`**: runs StockResearchAgent on every watched ticker in parallel.

</details>

---

## Memory System

Each chat has persistent, auto-compressing conversation memory.

```
┌─────────────────────────────────────────────────────┐
│                  MemoryManager                      │
│                                                     │
│  conversation_summaries  ←  compressed old history  │
│  conversation_turns      ←  recent 6 turns (raw)    │
│                                                     │
│  get_context(chat_id)                               │
│    → [summary msg] + [last 6 turns]                 │
│    → prepended to every LLM call                    │
│                                                     │
│  save_turn(chat_id, user, assistant)                │
│    → insert turn → check if compression needed      │
│                                                     │
│  compress_if_needed(chat_id)                        │
│    → if turns > 16: LLM summarizes oldest turns     │
│    → deletes compressed turns, stores summary       │
│                                                     │
│  compress_all_chats()  ← runs every 30 min          │
└─────────────────────────────────────────────────────┘
```

**Why compression?** Without it, conversation history grows indefinitely and slows down every LLM call. Compression keeps the context window small while preserving long-term memory of what was discussed.

| Setting | Value | Description |
|---|---|---|
| `RECENT_TURNS` | 6 | Turns kept raw after compression |
| `MAX_TURNS_BEFORE_COMPRESS` | 16 | Trigger threshold |
| `COMPRESS_INTERVAL_SECONDS` | 1800 | Periodic sweep interval (30 min) |

---

## Setup

**Requirements**: Python 3.11+, [LM Studio](https://lmstudio.ai), a Telegram bot token.

```bash
git clone <repo>
cd financial-agent

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN and LMSTUDIO_BASE_URL at minimum
```

**LM Studio setup**:
1. Download and open [LM Studio](https://lmstudio.ai)
2. Search for `Qwen2.5-0.5B-Instruct`, download `Q4_K_M` quantization
3. Go to **Developer** tab → load the model → **Start Server**
4. The API will be available at `http://localhost:1234/v1`

**Telegram bot setup**:
1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Run `/newbot` and follow the prompts
3. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
4. Start a conversation with your bot (send `/start`) before running

```bash
python main.py
```

---

## Configuration

All settings in `.env` (copy from `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | — | _(all chats)_ | Comma-separated chat IDs to restrict. Empty = respond to everyone |
| `LMSTUDIO_BASE_URL` | — | `http://localhost:1234/v1` | LM Studio server URL |
| `LLM_MODEL` | — | `qwen2.5-0.5b-instruct` | Model ID as shown in LM Studio |
| `LLM_MAX_TOKENS` | — | `400` | Max tokens per agent LLM call |
| `LLM_ORCHESTRATOR_MAX_TOKENS` | — | `300` | Max tokens for memory compression |
| `POLYGON_API_KEY` | — | — | Enables real-time Polygon quotes on top of yfinance |
| `DB_PATH` | — | `db/state.db` | SQLite database path |
| `LONG_MESSAGE_THRESHOLD` | — | `300` | Char count above which messages are auto-summarized |

**Swapping models**: change `LLM_MODEL` in `.env` to any model loaded in LM Studio.
Larger models (e.g. `Qwen2.5-3B-Instruct`, `Phi-3-mini-instruct`) give better analysis
at the cost of slower responses.

---

## Usage

Send any of these to your Telegram bot:

```
$AAPL                          → price snapshot + fundamentals
NVDA                           → same, without $ prefix
What do you think about TSLA?  → ticker detected in natural language
AAPL bull call spread          → adds OptionsAgent to the response
bearish on MSFT options        → outlook inferred as bearish

watch $GOOGL                   → add to watchlist
unwatch $GOOGL                 → remove from watchlist
check watchlist                → run StockResearch on all watched tickers

What is a P/E ratio?           → general finance question (no ticker needed)
Explain options trading        → general assistant fallback

[paste long article]           → auto-summarized, then tickers extracted
```

---

## File Layout

```
financial-agent/
├── main.py                   # Telegram bot entry point + periodic compression task
├── orchestrator.py           # Regex router, agent dispatch, memory wiring
├── config.py                 # All settings loaded from .env
├── requirements.txt
│
├── agents/
│   ├── base_agent.py         # AgentResult TypedDict + BaseAgent ABC
│   ├── stock_research.py     # Price, RSI, MA snapshot
│   ├── fundamentals_agent.py # PE, margins, quarterly revenue
│   ├── options_agent.py      # Spread strategy suggestions
│   ├── summarizer.py         # 3-bullet text summarizer
│   └── watchlist_agent.py    # SQLite watchlist CRUD
│
├── tools/
│   ├── market_data.py        # yfinance (async) + optional Polygon overlay
│   └── telegram_sender.py    # HTML-mode reply helpers + truncation
│
└── db/
    ├── database.py           # Schema init, message/agent/watchlist logging
    ├── memory.py             # MemoryManager — per-chat history + compression
    └── state.db              # SQLite file (git-ignored)
```

---

## Database Schema

<details>
<summary><b>Tables</b></summary>

**`messages`** — every inbound Telegram message, for auditing.

**`agent_logs`** — every agent invocation: name, version, input JSON, output JSON, confidence score. Useful for future fine-tuning or debugging.

**`watchlist`** — persisted ticker list with timestamps.

**`conversation_turns`** — raw recent turns per chat (user message + assistant reply pairs).

**`conversation_summaries`** — LLM-generated summary of compressed old turns per chat.

</details>

---

## Extending the System

<details>
<summary><b>Adding a new agent</b></summary>

1. Create `agents/my_agent.py`:

```python
from agents.base_agent import AgentResult, BaseAgent

class MyAgent(BaseAgent):
    name = "my_agent"
    version = "1.0.0"

    async def run(self, input: dict) -> AgentResult:
        # ... fetch data, call LLM ...
        return AgentResult(
            agent=self.name, version=self.version,
            output="<b>My Agent</b>\n...",
            confidence=0.9,
            metadata={},
        )
```

2. Add it to `Orchestrator.__init__` in `orchestrator.py`:
```python
self._agents["my_agent"] = MyAgent()
```

3. Add a routing rule to `_build_calls()` — either extend `_extract_tickers()` or add a new regex pattern.

4. Add it to the `_dispatch` mapping:
```python
"run_my_agent": ("my_agent", tool_input),
```

No other files need to change.

</details>

<details>
<summary><b>Switching to a cloud LLM (OpenAI, Claude, etc.)</b></summary>

The agents use the `openai` Python SDK pointed at LM Studio's local endpoint.
Any OpenAI-compatible API works — just update `.env`:

```env
LMSTUDIO_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

For Anthropic/Claude, use the [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) SDK
and replace the `AsyncOpenAI` client in each agent and `orchestrator.py`.

</details>

<details>
<summary><b>Agent contracts</b></summary>

Every agent returns an `AgentResult` — a typed dict:

```python
class AgentResult(TypedDict):
    agent:      str    # agent name, e.g. "stock_research"
    version:    str    # semver, e.g. "1.0.0"
    output:     str    # HTML-formatted reply text
    confidence: float  # 0.0–1.0; 0.0 signals a hard failure
    metadata:   dict   # raw data, errors, or debug info
```

Agents never raise exceptions — failures return `confidence: 0.0` with the error
in `metadata`. The orchestrator sends a degraded response rather than crashing.

</details>
