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
