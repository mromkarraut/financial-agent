import logging

from openai import AsyncOpenAI

import config
from agents.base_agent import AgentResult, BaseAgent
from tools.market_data import get_fundamentals

logger = logging.getLogger(__name__)


class FundamentalsAgent(BaseAgent):
    name = "fundamentals"
    version = "2.0.0"

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=config.LMSTUDIO_BASE_URL,
            api_key="lm-studio",
        )

    async def run(self, input: dict) -> AgentResult:
        ticker: str = input.get("ticker", "").strip().upper()
        if not ticker:
            return AgentResult(
                agent=self.name, version=self.version,
                output="No ticker provided.", confidence=0.0,
                metadata={"error": "missing ticker"},
            )

        try:
            data = await get_fundamentals(ticker)
            if "error" in data:
                return AgentResult(
                    agent=self.name, version=self.version,
                    output=f"Could not fetch fundamentals for {ticker}: {data['error']}",
                    confidence=0.0, metadata=data,
                )

            pe        = data.get("pe_ratio", "N/A")
            fwd_pe    = data.get("forward_pe", "N/A")
            margin    = data.get("profit_margin_pct", "N/A")
            gmargin   = data.get("gross_margin_pct", "N/A")
            rev_yoy   = data.get("revenue_growth_yoy_pct", "N/A")
            de        = data.get("debt_to_equity", "N/A")
            sector    = data.get("sector", "N/A")
            name      = data.get("company_name", ticker)
            mcap      = data.get("market_cap")
            mcap_str  = f"${mcap/1e9:.1f}B" if mcap else "N/A"
            qtrs      = data.get("quarterly_revenues", [])

            # Pre-compute valuation signal so the small model doesn't have to
            val_signal = "N/A"
            if pe != "N/A" and fwd_pe != "N/A":
                try:
                    val_signal = "stretched" if float(pe) > 30 else ("fair" if float(pe) > 15 else "cheap")
                except (TypeError, ValueError):
                    pass

            # Revenue trend direction
            rev_trend = "N/A"
            if len(qtrs) >= 3:
                recent_qoq = [q["qoq_pct"] for q in qtrs[-3:] if q["qoq_pct"] is not None]
                if recent_qoq:
                    avg_qoq = sum(recent_qoq) / len(recent_qoq)
                    rev_trend = "accelerating" if avg_qoq > 2 else ("declining" if avg_qoq < -2 else "stable")

            # Margin signal
            margin_signal = "N/A"
            if margin != "N/A":
                try:
                    margin_signal = "strong" if float(margin) > 20 else ("moderate" if float(margin) > 8 else "weak")
                except (TypeError, ValueError):
                    pass

            prompt = (
                f"Fill in the blanks only. No extra text.\n\n"
                f"Valuation: PE {pe} vs fwd PE {fwd_pe} — valuation is {val_signal} because ___.\n"
                f"Revenue: {rev_yoy}% YoY, {rev_trend} quarterly trend, {margin_signal} margins ({margin}%) — ___.\n"
                f"Risk: One key risk for {name} is ___."
            )

            try:
                response = await self._client.chat.completions.create(
                    model=config.LLM_MODEL,
                    max_tokens=config.LLM_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = (response.choices[0].message.content or "").strip()
                q_ratio = raw.count("?") / max(len(raw), 1)
                analysis = raw if raw and q_ratio < 0.3 else None
            except Exception:
                analysis = None

            if not analysis:
                val_note = (f"PE {pe} vs fwd PE {fwd_pe} — {val_signal} relative to sector peers"
                            if val_signal != "N/A" else f"PE {pe}, forward PE {fwd_pe}")
                rev_note = (f"{rev_yoy}% YoY, {rev_trend} quarterly trend, {margin_signal} margins ({margin}%)"
                            if rev_yoy != "N/A" else f"margins at {margin}%")
                analysis = (
                    f"Valuation: {val_note}.\n"
                    f"Revenue: {rev_note}.\n"
                    f"Risk: Macro uncertainty and sector competition remain key watch items."
                )

            # Build quarterly revenue table
            rev_table = ""
            if qtrs:
                rows = []
                for q in qtrs[-4:]:
                    qoq_str = f" ({'+' if (q['qoq_pct'] or 0) >= 0 else ''}{q['qoq_pct']}%)" if q["qoq_pct"] is not None else ""
                    rows.append(f"  {q['period']}: <code>${q['revenue_b']}B{qoq_str}</code>")
                rev_table = "<b>Quarterly Revenue</b>\n" + "\n".join(rows) + "\n\n"

            header = (
                f"<b>Fundamentals — {name} ({ticker})</b>\n"
                f"Sector: <code>{sector}</code>  Mkt Cap: <code>{mcap_str}</code>\n"
                f"PE: <code>{pe}</code>  Fwd PE: <code>{fwd_pe}</code>  D/E: <code>{de}</code>\n"
                f"Rev Growth YoY: <code>{rev_yoy}%</code>  "
                f"Profit Margin: <code>{margin}%</code>  Gross Margin: <code>{gmargin}%</code>\n\n"
                + rev_table
            )

            return AgentResult(
                agent=self.name, version=self.version,
                output=header + analysis,
                confidence=0.85,
                metadata={"fundamentals": data},
            )

        except Exception as exc:
            logger.error("FundamentalsAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
