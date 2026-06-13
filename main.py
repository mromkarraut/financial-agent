"""
Entry point — starts the Telegram bot and wires it to the Orchestrator.

Supported update types:
  • Private messages to the bot
  • Group / supergroup messages
  • Channel posts

If TELEGRAM_CHANNEL_ID is set in config (comma-separated chat IDs), only messages
from those chats are processed; all others are silently ignored.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys

from telegram import Message, Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from db.database import init_db
from db.memory import COMPRESS_INTERVAL_SECONDS
from orchestrator import Orchestrator
from tools.telegram_sender import TelegramSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_orchestrator: Orchestrator | None = None
_sender: TelegramSender | None = None

# Allowed chat IDs (empty set = allow all)
_ALLOWED_CHAT_IDS: set[int] = set()


def _build_allowed_set() -> set[int]:
    raw = config.TELEGRAM_CHANNEL_ID.strip()
    if not raw:
        return set()
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                logger.warning("Ignoring invalid chat ID in TELEGRAM_CHANNEL_ID: %r", part)
    return result


def _is_allowed(chat_id: int) -> bool:
    return not _ALLOWED_CHAT_IDS or chat_id in _ALLOWED_CHAT_IDS


# ── Handlers ──────────────────────────────────────────────────────────────────

async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.message or update.channel_post
    if msg is None or not msg.text:
        return

    chat_id = msg.chat_id
    message_id = msg.message_id
    text = msg.text.strip()

    if not _is_allowed(chat_id):
        return
    if not text:
        return

    logger.info("Received message [chat=%s msg=%s]: %r", chat_id, message_id, text[:80])

    assert _orchestrator is not None
    assert _sender is not None

    # Show typing indicator while processing
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    reply = await _orchestrator.process(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
    )

    reply = _sender.truncate(reply, max_chars=4000)
    await _sender.reply(
        chat_id=chat_id,
        text=reply,
        reply_to_message_id=message_id,
    )


async def _periodic_compression(orchestrator: Orchestrator) -> None:
    while True:
        await asyncio.sleep(COMPRESS_INTERVAL_SECONDS)
        logger.info("Running periodic memory compression sweep…")
        try:
            await orchestrator.memory.compress_all_chats()
        except Exception as exc:
            logger.error("Periodic compression failed: %s", exc)


def _start_cloudflare_tunnel() -> str | None:
    """Start a cloudflared quick tunnel and return the public URL, or None."""
    cloudflared = shutil.which("cloudflared") or os.path.expanduser("~/.local/bin/cloudflared")
    if not os.path.exists(cloudflared):
        logger.warning("cloudflared not found — skipping tunnel")
        return None
    try:
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", "http://localhost:8000", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # cloudflared prints the URL to stderr/stdout; poll until we see it
        url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        for line in proc.stdout:  # type: ignore[union-attr]
            m = url_re.search(line)
            if m:
                url = m.group(0)
                logger.info("Cloudflare tunnel active: %s", url)
                return url
            if proc.poll() is not None:
                break
    except Exception as exc:
        logger.warning("Could not start cloudflared: %s", exc)
    return None


async def _on_startup(application: Application) -> None:  # type: ignore[type-arg]
    global _orchestrator, _sender, _ALLOWED_CHAT_IDS
    logger.info("Initialising database…")
    await init_db()
    _orchestrator = Orchestrator()
    _sender = TelegramSender(application.bot)
    _ALLOWED_CHAT_IDS = _build_allowed_set()
    asyncio.create_task(_periodic_compression(_orchestrator))

    # Start Cloudflare tunnel and update WEB_SERVER_URL so the link in
    # Telegram replies always points to the live public URL.
    tunnel_url = await asyncio.to_thread(_start_cloudflare_tunnel)
    if tunnel_url:
        config.WEB_SERVER_URL = tunnel_url
        logger.info("WEB_SERVER_URL set to %s", tunnel_url)
    else:
        logger.info("WEB_SERVER_URL: %s (no tunnel)", config.WEB_SERVER_URL)

    logger.info("Bot ready. Allowed chats: %s", _ALLOWED_CHAT_IDS or "all")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)
    if not config.LMSTUDIO_BASE_URL:
        logger.error("LMSTUDIO_BASE_URL is not set. Exiting.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(token)
        .post_init(_on_startup)
        .build()
    )

    text_filter = filters.TEXT & ~filters.COMMAND

    # Handle regular messages (private + groups)
    app.add_handler(MessageHandler(text_filter, _handle_text))
    # Handle channel posts
    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POSTS & text_filter, _handle_text)
    )

    logger.info("Starting polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
