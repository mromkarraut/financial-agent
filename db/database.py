import json
import logging
import os
from datetime import datetime, timezone

import aiosqlite

import config

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                chat_id       TEXT    NOT NULL,
                message_id    TEXT    NOT NULL,
                text          TEXT    NOT NULL,
                classification TEXT   DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS agent_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                agent_name    TEXT    NOT NULL,
                agent_version TEXT    NOT NULL,
                input_json    TEXT    NOT NULL,
                output_json   TEXT    NOT NULL,
                confidence    REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker   TEXT    UNIQUE NOT NULL,
                added_at TEXT    NOT NULL,
                notes    TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS conversation_turns (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      TEXT    NOT NULL,
                user_msg     TEXT    NOT NULL,
                assistant_msg TEXT   NOT NULL,
                created_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_summaries (
                chat_id      TEXT    PRIMARY KEY,
                summary      TEXT    NOT NULL DEFAULT '',
                updated_at   TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS options_research_memory (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      TEXT    NOT NULL,
                ticker       TEXT    NOT NULL,
                timestamp    TEXT    NOT NULL,
                price        REAL,
                outlook      TEXT,
                ivr          REAL,
                recommended  TEXT,
                strategies   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_orm_chat_ticker
                ON options_research_memory(chat_id, ticker);

            CREATE TABLE IF NOT EXISTS ibkr_conid_cache (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT    NOT NULL,
                sectype    TEXT    NOT NULL,
                expiry     TEXT    NOT NULL,
                right      TEXT    NOT NULL,
                strike     REAL    NOT NULL,
                conid      INTEGER NOT NULL,
                cached_at  TEXT    NOT NULL,
                UNIQUE(symbol, sectype, expiry, right, strike)
            );

            CREATE TABLE IF NOT EXISTS ibkr_orders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                account_id    TEXT    NOT NULL,
                ticker        TEXT    NOT NULL,
                strategy      TEXT    NOT NULL,
                short_strike  REAL,
                long_strike   REAL,
                option_type   TEXT,
                expiry        TEXT,
                net_price     REAL,
                quantity      INTEGER DEFAULT 1,
                ibkr_order_id TEXT,
                status        TEXT    DEFAULT 'pending',
                raw_response  TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS ui_test_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                test_name   TEXT    NOT NULL,
                passed      INTEGER NOT NULL,
                detail      TEXT    DEFAULT '',
                duration_ms INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ui_research_memory (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                topic          TEXT    NOT NULL,
                timestamp      TEXT    NOT NULL,
                summary        TEXT    NOT NULL,
                recommendation TEXT    NOT NULL,
                score          INTEGER DEFAULT 0,
                implemented    INTEGER DEFAULT 0,
                source         TEXT    DEFAULT ''
            );
        """)
        await db.commit()
        # Non-destructive migrations
        try:
            await db.execute("ALTER TABLE options_research_memory ADD COLUMN output_html TEXT DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # column already exists


async def log_message(
    chat_id: str | int,
    message_id: str | int,
    text: str,
    classification: str = "",
) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (timestamp, chat_id, message_id, text, classification) "
            "VALUES (?, ?, ?, ?, ?)",
            (_utcnow(), str(chat_id), str(message_id), text, classification),
        )
        await db.commit()


async def log_agent_call(
    agent_name: str,
    agent_version: str,
    input_data: dict,
    result: dict,
) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO agent_logs "
            "(timestamp, agent_name, agent_version, input_json, output_json, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _utcnow(),
                agent_name,
                agent_version,
                json.dumps(input_data),
                json.dumps(result),
                float(result.get("confidence", 0)),
            ),
        )
        await db.commit()


async def watchlist_add(ticker: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
            (ticker.upper(), _utcnow()),
        )
        await db.commit()


async def watchlist_remove(ticker: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))
        await db.commit()


async def watchlist_get_all() -> list[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT ticker FROM watchlist ORDER BY added_at") as cur:
            rows = await cur.fetchall()
    return [row[0] for row in rows]


async def order_history(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT id, timestamp, ticker, strategy, short_strike, long_strike, "
            "option_type, expiry, net_price, quantity, ibkr_order_id, status "
            "FROM ibkr_orders ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "ticker": r[2], "strategy": r[3],
         "short_strike": r[4], "long_strike": r[5], "option_type": r[6],
         "expiry": r[7], "net_price": r[8], "quantity": r[9],
         "ibkr_order_id": r[10], "status": r[11]}
        for r in rows
    ]


async def update_order_status(ibkr_order_id: str, status: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE ibkr_orders SET status = ? WHERE ibkr_order_id = ?",
            (status, str(ibkr_order_id)),
        )
        await db.commit()


async def delete_order_by_ibkr_id(ibkr_order_id: str) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM ibkr_orders WHERE ibkr_order_id = ?",
            (str(ibkr_order_id),),
        )
        await db.commit()
