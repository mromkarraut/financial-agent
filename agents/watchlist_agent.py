import asyncio
import logging

from agents.base_agent import AgentResult, BaseAgent
from db.database import watchlist_add, watchlist_get_all, watchlist_remove

logger = logging.getLogger(__name__)


class WatchlistAgent(BaseAgent):
    name = "watchlist"
    version = "1.0.0"

    async def run(self, input: dict) -> AgentResult:
        action: str = input.get("action", "").lower()
        ticker: str = input.get("ticker", "").strip().upper()

        try:
            if action == "add":
                return await self._add(ticker)
            if action == "remove":
                return await self._remove(ticker)
            if action == "check":
                return await self._check()
            return AgentResult(
                agent=self.name,
                version=self.version,
                output=f"Unknown action '{action}'. Use add, remove, or check.",
                confidence=0.0,
                metadata={"error": f"invalid action: {action}"},
            )
        except Exception as exc:
            logger.error("WatchlistAgent failed: %s", exc)
            return self._error_result(exc)

    async def _add(self, ticker: str) -> AgentResult:
        if not ticker:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="Please provide a ticker to add.",
                confidence=0.0,
                metadata={"error": "missing ticker"},
            )
        await watchlist_add(ticker)
        all_tickers = await watchlist_get_all()
        return AgentResult(
            agent=self.name,
            version=self.version,
            output=f"✔ <b>{ticker}</b> added to watchlist.\nCurrently watching: {', '.join(all_tickers) or 'none'}",
            confidence=1.0,
            metadata={"action": "add", "ticker": ticker, "watchlist": all_tickers},
        )

    async def _remove(self, ticker: str) -> AgentResult:
        if not ticker:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="Please provide a ticker to remove.",
                confidence=0.0,
                metadata={"error": "missing ticker"},
            )
        await watchlist_remove(ticker)
        all_tickers = await watchlist_get_all()
        return AgentResult(
            agent=self.name,
            version=self.version,
            output=f"✔ <b>{ticker}</b> removed from watchlist.\nCurrently watching: {', '.join(all_tickers) or 'none'}",
            confidence=1.0,
            metadata={"action": "remove", "ticker": ticker, "watchlist": all_tickers},
        )

    async def _check(self) -> AgentResult:
        # Lazy import to avoid circular dependency at module load time
        from agents.stock_research import StockResearchAgent

        tickers = await watchlist_get_all()
        if not tickers:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="Your watchlist is empty. Use 'watch $TICKER' to add stocks.",
                confidence=1.0,
                metadata={"action": "check", "watchlist": []},
            )

        researcher = StockResearchAgent()
        tasks = [researcher.run({"ticker": t}) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        parts: list[str] = [f"<b>Watchlist Digest — {len(tickers)} stocks</b>\n"]
        for ticker, result in zip(tickers, results):
            if isinstance(result, Exception):
                parts.append(f"<b>{ticker}</b>: ⚠ error — {result}\n")
            elif result.get("confidence", 0) == 0:
                err = result.get("metadata", {}).get("error", "unknown error")
                parts.append(f"<b>{ticker}</b>: ⚠ {err}\n")
            else:
                parts.append(result["output"])
            parts.append("\n─────────────────────\n")

        digest = "\n".join(parts)
        return AgentResult(
            agent=self.name,
            version=self.version,
            output=digest,
            confidence=0.9,
            metadata={"action": "check", "watchlist": tickers, "count": len(tickers)},
        )
