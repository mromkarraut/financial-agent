import logging

from openai import AsyncOpenAI

import config
from agents.base_agent import AgentResult, BaseAgent
from tools.market_data import get_stock_data

logger = logging.getLogger(__name__)


class StockResearchAgent(BaseAgent):
    name = "stock_research"
    version = "1.0.0"

    _SYSTEM = "Complete the stock report below using only the data provided. No extra text."

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key="lm-studio",
        )

    async def run(self, input: dict) -> AgentResult:
        ticker: str = input.get("ticker", "").strip().upper()
        if not ticker:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="No ticker provided.",
                confidence=0.0,
                metadata={"error": "missing ticker"},
            )

        try:
            data = await get_stock_data(ticker)
            if "error" in data:
                return AgentResult(
                    agent=self.name,
                    version=self.version,
                    output=f"Could not fetch data for {ticker}: {data['error']}",
                    confidence=0.0,
                    metadata=data,
                )

            price    = data["current_price"]
            ma20     = data["ma_20"]
            ma50     = data.get("ma_50", "N/A")
            rsi      = data["rsi_14"]
            chg      = data["price_change_pct"]

            vs_ma20  = "above" if ma20 and price > ma20 else "below"
            vs_ma50  = "above" if ma50 and ma50 != "N/A" and price > float(ma50) else "below"
            rsi_lbl  = "overbought" if rsi > 70 else ("oversold" if rsi < 30 else "neutral")
            stance   = "Bullish" if (vs_ma20 == "above" and vs_ma50 == "above" and rsi < 70) \
                       else ("Bearish" if (vs_ma20 == "below" and vs_ma50 == "below") else "Neutral")

            prompt = (
                f"Fill in the blanks only. No extra text.\n\n"
                f"Trend: {ticker} at ${price} is {vs_ma20} MA20 (${ma20}) and {vs_ma50} MA50 (${ma50}), "
                f"indicating ___.\n"
                f"Momentum: RSI {rsi} is {rsi_lbl}, so near-term price action looks ___.\n"
                f"Stance: {stance} — ___."
            )

            response = await self._client.chat.completions.create(
                model=config.LLM_MODEL,
                max_tokens=config.LLM_MAX_TOKENS,
                messages=[
                    {"role": "user", "content": f"{self._SYSTEM}\n\n{prompt}"},
                ],
            )
            snapshot = (response.choices[0].message.content or "").strip()

            header = (
                f"<b>{data.get('company_name', ticker)} ({ticker})</b>\n"
                f"Price: <code>${data['current_price']}</code>  "
                f"({'+' if data['price_change_pct'] >= 0 else ''}{data['price_change_pct']}%)\n"
                f"52W: <code>${data['week52_low']} – ${data['week52_high']}</code>  "
                f"RSI-14: <code>{data['rsi_14']}</code>\n"
                f"MA20: <code>${data['ma_20']}</code>  MA50: <code>${data.get('ma_50', 'N/A')}</code>\n\n"
            )

            return AgentResult(
                agent=self.name,
                version=self.version,
                output=header + snapshot,
                confidence=0.88,
                metadata={"market_data": data},
            )

        except Exception as exc:
            logger.error("StockResearchAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
