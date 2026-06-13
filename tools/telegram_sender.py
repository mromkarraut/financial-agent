"""
Thin wrapper around python-telegram-bot for sending formatted replies.
Uses HTML parse mode — simpler escaping than MarkdownV2.
"""

import logging

from telegram import Bot, Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def reply(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
        parse_html: bool = True,
    ) -> Message | None:
        try:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML if parse_html else None,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
            return None

    async def reply_plain(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> Message | None:
        return await self.reply(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_html=False,
        )

    # ── Formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def section(title: str, body: str) -> str:
        return f"<b>{_escape(title)}</b>\n{body}"

    @staticmethod
    def code(text: str) -> str:
        return f"<code>{_escape(text)}</code>"

    @staticmethod
    def bold(text: str) -> str:
        return f"<b>{_escape(text)}</b>"

    @staticmethod
    def italic(text: str) -> str:
        return f"<i>{_escape(text)}</i>"

    @staticmethod
    def truncate(text: str, max_chars: int = 4000) -> str:
        """Telegram message limit is 4096 chars."""
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 20] + "\n<i>… (truncated)</i>"
