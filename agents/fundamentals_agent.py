import logging
import re

import config
from agents.base_agent import AgentResult, BaseAgent
from mcp_servers.llm import get_llm_client
from tools.charts import generate_fundamentals_charts
from tools.market_data import get_fundamentals

logger = logging.getLogger(__name__)

_llm = get_llm_client()

_SYSTEM = (
    "You are a senior fundamental equity analyst. Given detailed financial data, "
    "write a rigorous multi-section analysis. Be specific about the numbers — "
    "quote exact figures, compute ratios inline, explain what trends mean. "
    "No disclaimers, no generic filler. Write like you're presenting to a fund manager."
)


def _llm_output_usable(text: str) -> bool:
    if not text or len(text) < 80:
        return False
    words = text.split()
    repeats = sum(1 for a, b in zip(words, words[1:]) if a.lower() == b.lower())
    if repeats > 3:
        return False
    if text.count("?") / len(text) > 0.03:
        return False
    if re.search(r'[^\x00-\x7F]{4,}|[.,:;!?]{4,}', text):
        return False
    return True


class FundamentalsAgent(BaseAgent):
    name = "fundamentals"
    version = "3.0.0"

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
            eps       = data.get("eps_ttm", "N/A")
            margin    = data.get("profit_margin_pct", "N/A")
            gmargin   = data.get("gross_margin_pct", "N/A")
            rev_yoy   = data.get("revenue_growth_yoy_pct", "N/A")
            de        = data.get("debt_to_equity", "N/A")
            sector    = data.get("sector", "N/A")
            name      = data.get("company_name", ticker)
            mcap      = data.get("market_cap")
            mcap_str  = f"${mcap/1e9:.1f}B" if mcap else "N/A"
            qtrs      = data.get("quarterly_revenues", [])

            # Build quarterly revenue detail for the prompt
            qtr_lines = []
            for q in qtrs[-6:]:
                qoq = f" ({'+' if (q.get('qoq_pct') or 0) >= 0 else ''}{q.get('qoq_pct', 'N/A')}% QoQ)" if q.get("qoq_pct") is not None else ""
                qtr_lines.append(f"  {q['period']}: ${q['revenue_b']}B{qoq}")

            # Revenue acceleration/deceleration signal
            rev_trend = "N/A"
            if len(qtrs) >= 3:
                recent_qoq = [q["qoq_pct"] for q in qtrs[-3:] if q.get("qoq_pct") is not None]
                if recent_qoq:
                    avg_qoq = sum(recent_qoq) / len(recent_qoq)
                    rev_trend = "accelerating" if avg_qoq > 2 else ("decelerating" if avg_qoq < -2 else "stable")

            # PEG-like signal
            peg_note = "N/A"
            if pe != "N/A" and rev_yoy != "N/A":
                try:
                    peg = float(pe) / float(rev_yoy) if float(rev_yoy) > 0 else None
                    if peg is not None:
                        peg_note = f"{peg:.1f}x (PE/revenue growth ratio — <1 suggests undervalued relative to growth)"
                except (TypeError, ValueError):
                    pass

            prompt = f"""Fundamental analysis for {name} ({ticker}) in {sector}.
Market Cap: {mcap_str}  |  EPS TTM: ${eps}

VALUATION
P/E (TTM): {pe}  |  Forward P/E: {fwd_pe}
PE/Revenue Growth ratio: {peg_note}

REVENUE
YoY Growth: {rev_yoy}%  |  Trend: {rev_trend}
Quarterly Revenue (last 6 quarters):
{chr(10).join(qtr_lines) if qtr_lines else "  No quarterly data available"}

PROFITABILITY
Gross Margin: {gmargin}%  |  Profit Margin (net): {margin}%
(Gross margin measures pricing power; net margin measures operational efficiency)

BALANCE SHEET
Debt/Equity: {de}

Write a thorough analysis with EXACTLY these four sections. Use specific numbers throughout — do not round or generalize.

**VALUATION ASSESSMENT**
Is {name} cheap, fair, or expensive at {pe}x trailing and {fwd_pe}x forward P/E? Explain what the PE compression or expansion from trailing to forward implies about expected earnings growth. If PE/growth ratio is available, interpret it. Compare margins ({margin}% net, {gmargin}% gross) against typical software/tech/consumer/industrial benchmarks for {sector}.

**REVENUE QUALITY**
Analyse the revenue trajectory: is {rev_yoy}% YoY growth accelerating or decelerating quarter over quarter? Walk through the most recent 3 quarters and call out any inflection. Is {rev_trend} growth momentum a positive or negative signal given the current valuation?

**PROFITABILITY AND EFFICIENCY**
What does a {gmargin}% gross margin tell us about pricing power? How does the step-down from {gmargin}% gross to {margin}% net margin reflect cost structure? Is this a capital-light or capital-heavy business at a {de} debt/equity ratio?

**CATALYSTS AND RISKS**
Name 2 specific catalysts that could re-rate {name} higher given these metrics, and 2 specific risks. Be concrete — reference the actual numbers (e.g., what margin expansion is needed to justify the forward multiple, or what revenue deceleration would break the thesis)."""

            val_signal = "N/A"
            if pe != "N/A":
                try:
                    val_signal = "stretched" if float(pe) > 30 else ("fair" if float(pe) > 15 else "cheap")
                except (TypeError, ValueError):
                    pass
            analysis = (
                f"Valuation: P/E {pe} vs forward P/E {fwd_pe} — appears {val_signal}.\n"
                f"Revenue: {rev_yoy}% YoY growth, {rev_trend} trend, {margin}% net margin.\n"
                f"Balance sheet: D/E {de}."
            )

            # Build quarterly revenue table for HTML output
            rev_table = ""
            if qtrs:
                rows = []
                for q in qtrs[-4:]:
                    qoq_str = f" ({'+' if (q['qoq_pct'] or 0) >= 0 else ''}{q['qoq_pct']}%)" if q.get("qoq_pct") is not None else ""
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

            charts = generate_fundamentals_charts(data)

            return AgentResult(
                agent=self.name, version=self.version,
                output=header + analysis,
                confidence=0.85,
                metadata={"fundamentals": data, "charts": charts},
            )

        except Exception as exc:
            logger.error("FundamentalsAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
