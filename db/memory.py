"""
Per-chat conversation memory with periodic compression.

Each chat keeps:
  - A rolling set of recent turns (user + assistant pairs) in the DB
  - An optional summary of older turns, produced by the LLM
  - Compression triggers when turns exceed MAX_TURNS_BEFORE_COMPRESS

Context returned to the LLM:
  [{"role":"user","content":"<summary context>"}, <recent turns...>]
"""

import logging
from datetime import datetime, timezone

import aiosqlite
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

RECENT_TURNS = 6                  # turns to keep raw after compression
MAX_TURNS_BEFORE_COMPRESS = 16    # trigger compression above this count
COMPRESS_INTERVAL_SECONDS = 1800  # periodic sweep every 30 min

_COMPRESS_PROMPT = (
    "You are a memory assistant. Summarize the following conversation history into "
    "a short paragraph (3-5 sentences) that captures the key topics discussed, "
    "stocks or tickers mentioned, user preferences, and any conclusions reached. "
    "Be factual and concise — this summary will be prepended to future conversations "
    "so the assistant remembers context.\n\nConversation:\n{history}"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryManager:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_context(self, chat_id: str | int) -> list[dict]:
        """Return messages list ready to prepend to an LLM call."""
        chat_id = str(chat_id)
        summary, turns = await self._load(chat_id)

        messages: list[dict] = []
        if summary:
            messages.append({
                "role": "user",
                "content": f"[Conversation summary so far]: {summary}",
            })
            messages.append({"role": "assistant", "content": "Understood, I have that context."})

        for turn in turns:
            messages.append({"role": "user",      "content": turn["user_msg"]})
            messages.append({"role": "assistant",  "content": turn["assistant_msg"]})

        return messages

    async def save_turn(
        self,
        chat_id: str | int,
        user_msg: str,
        assistant_msg: str,
    ) -> None:
        chat_id = str(chat_id)
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "INSERT INTO conversation_turns (chat_id, user_msg, assistant_msg, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, user_msg, assistant_msg, _utcnow()),
            )
            await db.commit()
        await self.compress_if_needed(chat_id)

    async def compress_if_needed(self, chat_id: str | int) -> None:
        chat_id = str(chat_id)
        count = await self._turn_count(chat_id)
        if count > MAX_TURNS_BEFORE_COMPRESS:
            await self._compress(chat_id)

    async def compress_all_chats(self) -> None:
        """Sweep all chats and compress any that exceed the threshold."""
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT chat_id, COUNT(*) as n FROM conversation_turns GROUP BY chat_id"
            ) as cur:
                rows = await cur.fetchall()

        for chat_id, count in rows:
            if count > MAX_TURNS_BEFORE_COMPRESS:
                logger.info("Periodic compression for chat %s (%d turns)", chat_id, count)
                await self._compress(chat_id)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _load(self, chat_id: str) -> tuple[str, list[dict]]:
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT summary FROM conversation_summaries WHERE chat_id = ?", (chat_id,)
            ) as cur:
                row = await cur.fetchone()
            summary = row[0] if row else ""

            async with db.execute(
                "SELECT user_msg, assistant_msg FROM conversation_turns "
                "WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, RECENT_TURNS),
            ) as cur:
                rows = await cur.fetchall()

        turns = [{"user_msg": r[0], "assistant_msg": r[1]} for r in reversed(rows)]
        return summary, turns

    async def _turn_count(self, chat_id: str) -> int:
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE chat_id = ?", (chat_id,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _compress(self, chat_id: str) -> None:
        """Summarize all but the most recent RECENT_TURNS turns, then delete them."""
        async with aiosqlite.connect(config.DB_PATH) as db:
            # Existing summary
            async with db.execute(
                "SELECT summary FROM conversation_summaries WHERE chat_id = ?", (chat_id,)
            ) as cur:
                row = await cur.fetchone()
            existing_summary = row[0] if row else ""

            # IDs of turns to keep (most recent RECENT_TURNS)
            async with db.execute(
                "SELECT id FROM conversation_turns WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, RECENT_TURNS),
            ) as cur:
                keep_ids = {r[0] for r in await cur.fetchall()}

            # Turns to compress
            async with db.execute(
                "SELECT id, user_msg, assistant_msg FROM conversation_turns "
                "WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ) as cur:
                all_turns = await cur.fetchall()

        to_compress = [(t[0], t[1], t[2]) for t in all_turns if t[0] not in keep_ids]
        if not to_compress:
            return

        history_text = ""
        if existing_summary:
            history_text += f"[Previous summary]: {existing_summary}\n\n"
        for _, user_msg, assistant_msg in to_compress:
            history_text += f"User: {user_msg}\nAssistant: {assistant_msg}\n\n"

        try:
            response = await self._client.chat.completions.create(
                model=config.LLM_MODEL,
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": _COMPRESS_PROMPT.format(history=history_text.strip()),
                }],
            )
            new_summary = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error("Compression LLM call failed for chat %s: %s", chat_id, exc)
            return

        compress_ids = [t[0] for t in to_compress]
        placeholders = ",".join("?" * len(compress_ids))

        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                f"DELETE FROM conversation_turns WHERE id IN ({placeholders})",
                compress_ids,
            )
            await db.execute(
                "INSERT INTO conversation_summaries (chat_id, summary, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                (chat_id, new_summary, _utcnow()),
            )
            await db.commit()

        logger.info(
            "Compressed %d turns for chat %s into summary (%d chars)",
            len(to_compress), chat_id, len(new_summary),
        )
