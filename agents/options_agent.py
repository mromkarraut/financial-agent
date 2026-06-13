import logging
from typing import Literal

from openai import AsyncOpenAI

import config
from agents.base_agent import AgentResult, BaseAgent
from tools.market_data import get_options_data

logger = logging.getLogger(__name__)

Outlook = Literal["bullish", "bearish", "neutral"]


class OptionsAgent(BaseAgent):
    name = "options"
    version = "1.0.0"

    _SYSTEM = (
        "You are an experienced options trader and educator. "
        "Given a stock's current price, market outlook, days to expiration (DTE), "
        "and any available implied volatility data, suggest exactly 2 option spread strategies. "
        "For each strategy provide: "
        "(1) strategy name, "
        "(2) specific strikes (use round numbers near current price, spaced realistically), "
        "(3) rationale in 1–2 sentences, "
        "(4) max profit, max loss, and break-even price, "
        "(5) ideal market conditions for this trade. "
        "Format each strategy as a clearly labeled block. "
        "Use concrete numbers — do not leave strikes as variables. "
        "Remind the user this is educational, not financial advice."
    )

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key="lm-studio",
        )

    async def run(self, input: dict) -> AgentResult:
        ticker: str = input.get("ticker", "").strip().upper()
        outlook: str = input.get("outlook", "neutral").lower()
        dte: int = int(input.get("dte", 30))

        if not ticker:
            return AgentResult(
                agent=self.name,
                version=self.version,
                output="No ticker provided.",
                confidence=0.0,
                metadata={"error": "missing ticker"},
            )
        if outlook not in ("bullish", "bearish", "neutral"):
            outlook = "neutral"

        try:
            mkt = await get_options_data(ticker)
            if "error" in mkt:
                return AgentResult(
                    agent=self.name,
                    version=self.version,
                    output=f"Could not fetch market data for {ticker}: {mkt['error']}",
                    confidence=0.0,
                    metadata=mkt,
                )

            current_price = mkt.get("current_price") or 0.0
            iv_note = (
                f"Implied volatility: {mkt['implied_volatility']}%"
                if mkt.get("implied_volatility")
                else "Implied volatility: not available (assume moderate IV)"
            )
            exp_note = (
                f"Nearest available expirations: {', '.join(mkt['available_expirations'][:3])}"
                if mkt.get("available_expirations")
                else f"Target DTE: ~{dte} days"
            )

            prompt = (
                f"Stock: {ticker} ({mkt.get('company_name', ticker)})\n"
                f"Current price: ${current_price}\n"
                f"Market outlook: {outlook}\n"
                f"Desired DTE: {dte} days\n"
                f"{iv_note}\n"
                f"{exp_note}\n\n"
                "Suggest 2 option spread strategies appropriate for this setup."
            )

            response = await self._client.chat.completions.create(
                model=config.LLM_MODEL,
                max_tokens=config.LLM_MAX_TOKENS,
                messages=[
                    {"role": "user", "content": f"{self._SYSTEM}\n\n{prompt}"},
                ],
            )
            analysis = (response.choices[0].message.content or "").strip()

            header = (
                f"<b>Options Strategies — {ticker}</b>\n"
                f"Price: <code>${current_price}</code>  "
                f"Outlook: <code>{outlook.capitalize()}</code>  "
                f"DTE: <code>{dte}d</code>\n\n"
            )

            return AgentResult(
                agent=self.name,
                version=self.version,
                output=header + analysis,
                confidence=0.82,
                metadata={"market_data": mkt, "outlook": outlook, "dte": dte},
            )

        except Exception as exc:
            logger.error("OptionsAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
