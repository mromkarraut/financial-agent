# Financial Agent

A modular, agentic financial research system that listens to a Telegram channel, classifies incoming messages, and dispatches to specialized sub-agents powered by Claude.

## Architecture

```
Telegram message
       │
       ▼
  Orchestrator  ──── Claude tool-use ────► routes to sub-agents
       │
       ├── SummarizerAgent      long pastes → 3-bullet summary
       ├── StockResearchAgent   price / RSI / MA snapshot
       ├── FundamentalsAgent    PE / EPS / revenue / moat
       ├── OptionsAgent         spread strategies with strikes
       └── WatchlistAgent       SQLite-backed watchlist
```

Each agent is an independent Python module with a strict `AgentResult` contract. Swapping the Claude model or upgrading an agent requires no changes to any other component.

## Requirements

- Python 3.11+
- A [Telegram bot token](https://core.telegram.org/bots#botfather)
- An [Anthropic API key](https://console.anthropic.com/)
- (Optional) A [Polygon.io API key](https://polygon.io/) for real-time quotes

**TA-Lib** (optional, for faster RSI/MA): requires the system C library before `pip install`:
```bash
# macOS
brew install ta-lib

# Ubuntu / Debian
sudo apt-get install libta-lib-dev
```
If not installed, the system falls back to a pure-Python implementation automatically.

## Setup

```bash
git clone <repo>
cd financial-agent

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
```

## Configuration

All configuration lives in `.env` (see `.env.example`):

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key |
| `TELEGRAM_CHANNEL_ID` | — | Comma-separated chat IDs to restrict the bot. Empty = respond to all chats |
| `CLAUDE_MODEL` | — | Model used by all agents. Default: `claude-sonnet-4-6` |
| `POLYGON_API_KEY` | — | Enables real-time Polygon quotes on top of yfinance |
| `DB_PATH` | — | SQLite file path. Default: `db/state.db` |
| `LONG_MESSAGE_THRESHOLD` | — | Char count above which messages are auto-summarized. Default: `300` |

**Swapping models**: change `CLAUDE_MODEL` in `.env` — all agents read it from `config.py` at runtime.

## Running

```bash
python main.py
```

The bot initialises the SQLite database on first run, then starts polling Telegram.

## Usage

Send messages to any chat the bot is in (or the configured channel):

### Stock research
```
$AAPL
What do you think about NVDA?
TSLA looks interesting
```
→ Runs **StockResearchAgent** + **FundamentalsAgent** in parallel for each ticker.

### Options strategies
```
AAPL bull call spread 30 DTE
bearish on TSLA, what spreads make sense?
iron condor on SPY
```
→ Runs **OptionsAgent** with inferred outlook and DTE.

### Watchlist
```
watch $MSFT
unwatch $TSLA
check watchlist
```
→ Watchlist commands are intercepted before Claude sees them (no API call needed for add/remove). `check watchlist` runs StockResearchAgent on every tracked ticker in parallel.

### News / article summarization
Paste any block of text longer than 300 characters and it is automatically summarized into 3 key points before routing.

## File layout

```
financial-agent/
├── main.py                  # Telegram bot entry point
├── orchestrator.py          # Master router — Claude tool-use loop
├── config.py                # Single source of truth for all settings
├── agents/
│   ├── base_agent.py        # AgentResult TypedDict + BaseAgent ABC
│   ├── summarizer.py
│   ├── stock_research.py
│   ├── options_agent.py
│   ├── fundamentals_agent.py
│   └── watchlist_agent.py
├── tools/
│   ├── market_data.py       # yfinance (async) + optional Polygon overlay
│   └── telegram_sender.py   # HTML-mode reply helpers
└── db/
    └── database.py          # aiosqlite: messages, agent_logs, watchlist
```

## Database schema

Three tables in `db/state.db`:

**`messages`** — every inbound Telegram message logged for auditing.  
**`agent_logs`** — every agent invocation: name, version, input JSON, output JSON, confidence. Used for auditing and future fine-tuning.  
**`watchlist`** — persisted ticker list with timestamps.

## Agent contracts

Every agent implements `BaseAgent` and returns an `AgentResult`:

```python
class AgentResult(TypedDict):
    agent:      str    # agent name
    version:    str    # semver, e.g. "1.0.0"
    output:     str    # formatted reply text (HTML-safe)
    confidence: float  # 0.0–1.0; 0.0 signals a hard failure
    metadata:   dict   # raw data, errors, or debug info
```

Agents never raise — failures return `confidence: 0` with the error in `metadata`. The orchestrator sends a degraded response rather than crashing.

## Extending the system

**Adding a new agent:**
1. Create `agents/my_agent.py` inheriting `BaseAgent`, set `name` and `version`.
2. Add a tool definition to the `_TOOLS` list in `orchestrator.py`.
3. Add the agent to `_agents` dict and `_dispatch` mapping in `Orchestrator`.
4. Export it from `agents/__init__.py`.

No other files need to change.
