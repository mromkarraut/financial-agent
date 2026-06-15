import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Comma-separated list of allowed chat IDs (e.g. "-1001234567890,987654321").
# Leave empty to accept messages from all chats.
TELEGRAM_CHANNEL_ID: str = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# ── LM Studio (OpenAI-compatible local API) ───────────────────────────────────
# Enable the local server in LM Studio → Developer tab → Start Server.
# The model name must match exactly what is loaded in LM Studio.
# Tool/function calling requires a model that supports it (e.g. Llama-3, Qwen2.5).
LMSTUDIO_BASE_URL: str = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "local-model")
LLM_MAX_TOKENS: int = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
LLM_ORCHESTRATOR_MAX_TOKENS: int = int(os.environ.get("LLM_ORCHESTRATOR_MAX_TOKENS", "1024"))

# ── Data providers ────────────────────────────────────────────────────────────
POLYGON_API_KEY: str = os.environ.get("POLYGON_API_KEY", "")

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH: str = os.environ.get("DB_PATH", "db/state.db")

# ── Behaviour ─────────────────────────────────────────────────────────────────
# Messages longer than this go through the summarizer before routing.
LONG_MESSAGE_THRESHOLD: int = int(os.environ.get("LONG_MESSAGE_THRESHOLD", "300"))

# ── Web UI ─────────────────────────────────────────────────────────────────────
# URL of the FastAPI web server, sent as a link in Telegram replies.
WEB_SERVER_URL: str = os.environ.get("WEB_SERVER_URL", "http://localhost:8000")

# ── MCP Agent LLM ─────────────────────────────────────────────────────────────
# Controls the LLM used by every MCP server in mcp_servers/.
# Change MCP_LLM_PROVIDER + MCP_LLM_MODEL in .env to switch providers globally.
#
#   Provider       | Base URL                          | Key required
#   ───────────────┼───────────────────────────────────┼──────────────────────
#   lmstudio       | MCP_LLM_BASE_URL (localhost:1234) | none (uses "lm-studio")
#   anthropic      | Anthropic API                     | ANTHROPIC_API_KEY
#   openai         | OpenAI API                        | OPENAI_API_KEY
#
# Example .env for LM Studio:
#   MCP_LLM_PROVIDER=lmstudio
#   MCP_LLM_MODEL=qwen2.5-7b-instruct    # must match model loaded in LM Studio
#   MCP_LLM_BASE_URL=http://localhost:1234/v1
#   MCP_LLM_MAX_TOKENS=512
#
MCP_LLM_PROVIDER: str = os.environ.get("MCP_LLM_PROVIDER", "lmstudio")
MCP_LLM_MODEL: str    = os.environ.get("MCP_LLM_MODEL", os.environ.get("LLM_MODEL", "local-model"))
MCP_LLM_BASE_URL: str = os.environ.get("MCP_LLM_BASE_URL", os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"))
MCP_LLM_MAX_TOKENS: int = int(os.environ.get("MCP_LLM_MAX_TOKENS", "512"))
OPENAI_API_KEY: str   = os.environ.get("OPENAI_API_KEY", "")

# ── IBKR Gateway ──────────────────────────────────────────────────────────────
# IBKR_PAPER_TRADING=true (default) — use paper trading account (DU prefix).
# IBKR_PAPER_TRADING=false          — use live account (U prefix). REAL MONEY.
#
# Gateway is auto-started by start.py. To run manually:
#   cd ibkr_gateway && ./bin/run.sh root/conf.paper.yaml
# Then open https://localhost:5000 in Chrome and log in with PAPER credentials.
#
_ibkr_paper_raw = os.environ.get("IBKR_PAPER_TRADING", "true").strip().lower()
IBKR_PAPER_TRADING: bool = _ibkr_paper_raw in ("1", "true", "yes")
IBKR_GATEWAY_CONF: str   = os.environ.get(
    "IBKR_GATEWAY_CONF",
    "root/conf.paper.yaml" if IBKR_PAPER_TRADING else "root/conf.yaml",
)
IBKR_GATEWAY_URL: str  = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
IBKR_GATEWAY_DIR: str  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibkr_gateway")
# Java executable — uses Windows Java via WSL interop by default
IBKR_JAVA_BIN: str = os.environ.get(
    "IBKR_JAVA_BIN",
    "/mnt/c/Program Files/Java/jre1.8.0_461/bin/java.exe",
)

# ── TWS / IB Gateway socket connection (ib_insync) ────────────────────────────
# IB Gateway paper port: 4002  live port: 4001
# TWS            paper port: 7497  live port: 7496
IBKR_TWS_HOST: str = os.environ.get("IBKR_TWS_HOST", "127.0.0.1")
IBKR_TWS_PORT: int = int(os.environ.get(
    "IBKR_TWS_PORT",
    "4002" if IBKR_PAPER_TRADING else "4001",   # IB Gateway defaults
))
# clientId range 1-4 reserved for the 4 IBKR MCP servers; 5 = options research (main bot process)
IBKR_CLIENT_ID_SESSION:          int = int(os.environ.get("IBKR_CLIENT_ID_SESSION",          "1"))
IBKR_CLIENT_ID_POSITIONS:        int = int(os.environ.get("IBKR_CLIENT_ID_POSITIONS",        "2"))
IBKR_CLIENT_ID_ORDERS:           int = int(os.environ.get("IBKR_CLIENT_ID_ORDERS",           "3"))
IBKR_CLIENT_ID_MARKET_DATA:      int = int(os.environ.get("IBKR_CLIENT_ID_MARKET_DATA",      "4"))
IBKR_CLIENT_ID_OPTIONS_RESEARCH: int = int(os.environ.get("IBKR_CLIENT_ID_OPTIONS_RESEARCH", "5"))
IBKR_GATEWAY_EXE: str = os.environ.get(
    "IBKR_GATEWAY_EXE",
    r"C:\Jts\ibgateway\1039\ibgateway.exe",
)
