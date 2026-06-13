import logging
from datetime import date, datetime
from typing import Literal

from agents.base_agent import AgentResult, BaseAgent
from tools.market_data import get_options_chain

logger = logging.getLogger(__name__)

Outlook = Literal["bullish", "bearish", "neutral"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dte(exp: str) -> int:
    try:
        return max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 0


def _mid(row: dict) -> float:
    b, a = row.get("bid") or 0.0, row.get("ask") or 0.0
    if b > 0 and a > 0:
        return round((b + a) / 2, 2)
    return round(row.get("lastPrice") or 0.0, 2)


def _fmt_exp(exp: str) -> str:
    try:
        return datetime.strptime(exp, "%Y-%m-%d").strftime("%b %d")
    except Exception:
        return exp


# ── Section 1: Expiration selector ───────────────────────────────────────────

def _exp_selector(expirations: list[str]) -> str:
    if not expirations:
        return ""
    show = expirations[:5]
    dtes = [_dte(e) for e in show]
    max_dte = max(dtes) if dtes else 1
    lines = []
    for exp, dte in zip(show, dtes):
        filled = round(dte / max_dte * 12) if max_dte else 0
        bar = "█" * filled + "░" * (12 - filled)
        if dte <= 7:
            tag = "  ← weekly"
        elif dte <= 21:
            tag = "  ← near-term"
        elif 25 <= dte <= 50:
            tag = "  ← theta sweet spot"
        else:
            tag = ""
        lines.append(f"{_fmt_exp(exp)}  {dte:>3}d  {bar}{tag}")
    return "<pre>" + "\n".join(lines) + "</pre>"


# ── Section 2: Chain table ────────────────────────────────────────────────────

def _chain_table(calls: list, puts: list, current_price: float) -> str:
    calls_by_s = {r["strike"]: r for r in calls if r.get("strike")}
    puts_by_s  = {r["strike"]: r for r in puts  if r.get("strike")}
    strikes = sorted(set(list(calls_by_s) + list(puts_by_s)))
    if not strikes:
        return ""

    atm = min(strikes, key=lambda s: abs(s - current_price))
    idx = strikes.index(atm)
    selected = strikes[max(0, idx - 2): idx + 3]

    lines = ["Strike │  Call    Put  │  IV"]
    lines.append("───────┼──────────────┼──────")
    for s in selected:
        c = calls_by_s.get(s, {})
        p = puts_by_s.get(s, {})
        cm = _mid(c)
        pm = _mid(p)
        iv = c.get("impliedVolatility") or p.get("impliedVolatility") or 0
        marker = "▶" if s == atm else " "
        tag    = " ATM" if s == atm else "    "
        c_str  = f"{cm:>5.2f}" if cm else "    —"
        p_str  = f"{pm:>5.2f}" if pm else "    —"
        iv_str = f"{iv * 100:>4.0f}%" if iv else "    —"
        lines.append(f"{marker}{s:>5.0f} │ {c_str}  {p_str} │ {iv_str}{tag}")
    return "<pre>" + "\n".join(lines) + "</pre>"


# ── Section 3: P&L charts ─────────────────────────────────────────────────────

def _chart_bull_call(buy: float, sell: float, debit: float) -> str:
    be = round(buy + debit, 2)
    mp = round((sell - buy - debit) * 100)
    ml = round(debit * 100)
    return (
        f"+${mp} ┤──────────────┌────\n"
        f"    $0 ┤────────────┬─┘\n"
        f"-${ml} ┤████████████│\n"
        f"       ${buy:.0f}  B/E:${be}  ${sell:.0f}"
    )


def _chart_bull_put(sell: float, buy: float, credit: float) -> str:
    be = round(sell - credit, 2)
    mp = round(credit * 100)
    ml = round((sell - buy - credit) * 100)
    return (
        f"+${mp} ┤─────────────┐\n"
        f"    $0 ┤──────────────┴────\n"
        f"-${ml} ┤                ████\n"
        f"       ${buy:.0f}  B/E:${be}  ${sell:.0f}"
    )


def _chart_bear_put(buy: float, sell: float, debit: float) -> str:
    be = round(buy - debit, 2)
    mp = round((buy - sell - debit) * 100)
    ml = round(debit * 100)
    return (
        f"+${mp} ┤────┐\n"
        f"    $0 ┤─────┴─────────────\n"
        f"-${ml} ┤          ██████████\n"
        f"       ${sell:.0f}  B/E:${be}  ${buy:.0f}"
    )


def _chart_bear_call(sell: float, buy: float, credit: float) -> str:
    be = round(sell + credit, 2)
    mp = round(credit * 100)
    ml = round((buy - sell - credit) * 100)
    return (
        f"+${mp} ┤──────────────────\n"
        f"    $0 ┤──────┬────────────\n"
        f"-${ml} ┤██████│\n"
        f"       ${sell:.0f}  B/E:${be}  ${buy:.0f}"
    )


# ── Strategy picker ───────────────────────────────────────────────────────────

def _pick_strategies(
    outlook: str, calls: list, puts: list, current_price: float, ticker: str, exp: str
) -> list[dict]:
    calls_s = sorted(calls, key=lambda r: r["strike"])
    puts_s  = sorted(puts,  key=lambda r: r["strike"])
    if not calls_s or not puts_s:
        return []

    atm_c = min(calls_s, key=lambda r: abs(r["strike"] - current_price))
    atm_p = min(puts_s,  key=lambda r: abs(r["strike"] - current_price))
    ci = calls_s.index(atm_c)
    pi = puts_s.index(atm_p)

    out = []

    if outlook in ("bullish", "neutral"):
        # ① Bull Call Spread (debit)
        if ci + 1 < len(calls_s):
            otm = calls_s[ci + 1]
            debit = round(_mid(atm_c) - _mid(otm), 2)
            if debit > 0:
                spread = otm["strike"] - atm_c["strike"]
                be = round(atm_c["strike"] + debit, 2)
                out.append({
                    "num": "①", "name": "Bull Call Spread",
                    "legs": f"Buy <code>${atm_c['strike']:.0f}C</code> @ ${_mid(atm_c):.2f}  ·  Sell <code>${otm['strike']:.0f}C</code> @ ${_mid(otm):.2f}",
                    "cost_label": "Net debit",
                    "cost": debit,
                    "max_profit": round((spread - debit) * 100),
                    "max_loss":   round(debit * 100),
                    "breakeven":  be, "exp": exp,
                    "chart": _chart_bull_call(atm_c["strike"], otm["strike"], debit),
                    "note": f"Profits above ${be}. Defined risk, limited reward.",
                })

        # ② Bull Put Spread (credit)
        if pi > 0:
            otm = puts_s[pi - 1]
            credit = round(_mid(atm_p) - _mid(otm), 2)
            if credit > 0:
                spread = atm_p["strike"] - otm["strike"]
                be = round(atm_p["strike"] - credit, 2)
                out.append({
                    "num": "②", "name": "Bull Put Spread",
                    "legs": f"Sell <code>${atm_p['strike']:.0f}P</code> @ ${_mid(atm_p):.2f}  ·  Buy <code>${otm['strike']:.0f}P</code> @ ${_mid(otm):.2f}",
                    "cost_label": "Net credit",
                    "cost": credit,
                    "max_profit": round(credit * 100),
                    "max_loss":   round((spread - credit) * 100),
                    "breakeven":  be, "exp": exp,
                    "chart": _chart_bull_put(atm_p["strike"], otm["strike"], credit),
                    "note": f"Keeps premium if stock stays above ${be}. Profits from time decay.",
                })

    elif outlook == "bearish":
        # ① Bear Put Spread (debit)
        if pi + 1 < len(puts_s):
            itm = puts_s[pi + 1]
            debit = round(_mid(itm) - _mid(atm_p), 2)
            if debit > 0:
                spread = itm["strike"] - atm_p["strike"]
                be = round(itm["strike"] - debit, 2)
                out.append({
                    "num": "①", "name": "Bear Put Spread",
                    "legs": f"Buy <code>${itm['strike']:.0f}P</code> @ ${_mid(itm):.2f}  ·  Sell <code>${atm_p['strike']:.0f}P</code> @ ${_mid(atm_p):.2f}",
                    "cost_label": "Net debit",
                    "cost": debit,
                    "max_profit": round((spread - debit) * 100),
                    "max_loss":   round(debit * 100),
                    "breakeven":  be, "exp": exp,
                    "chart": _chart_bear_put(itm["strike"], atm_p["strike"], debit),
                    "note": f"Profits below ${be}. Best if stock drops steadily.",
                })

        # ② Bear Call Spread (credit)
        if ci + 1 < len(calls_s):
            otm = calls_s[ci + 1]
            credit = round(_mid(atm_c) - _mid(otm), 2)
            if credit > 0:
                spread = otm["strike"] - atm_c["strike"]
                be = round(atm_c["strike"] + credit, 2)
                out.append({
                    "num": "②", "name": "Bear Call Spread",
                    "legs": f"Sell <code>${atm_c['strike']:.0f}C</code> @ ${_mid(atm_c):.2f}  ·  Buy <code>${otm['strike']:.0f}C</code> @ ${_mid(otm):.2f}",
                    "cost_label": "Net credit",
                    "cost": credit,
                    "max_profit": round(credit * 100),
                    "max_loss":   round((spread - credit) * 100),
                    "breakeven":  be, "exp": exp,
                    "chart": _chart_bear_call(atm_c["strike"], otm["strike"], credit),
                    "note": f"Keeps premium if stock stays below ${be}.",
                })

    return out[:2]


def _fmt_strategy(s: dict) -> str:
    cost_per = round(s["cost"] * 100)
    return (
        f"<b>{s['num']} {s['name']}</b>  <code>{s['exp']}</code>\n"
        f"{s['legs']}\n"
        f"<code>"
        f"{s['cost_label']:12}: ${s['cost']:.2f}  (${cost_per}/contract)\n"
        f"{'Max profit':12}: ${s['max_profit']}\n"
        f"{'Max loss':12}: ${s['max_loss']}\n"
        f"{'Break-even':12}: ${s['breakeven']}"
        f"</code>\n"
        f"<pre>{s['chart']}</pre>"
        f"<i>{s['note']}</i>"
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class OptionsAgent(BaseAgent):
    name = "options"
    version = "2.0.0"

    async def run(self, input: dict) -> AgentResult:
        ticker: str = input.get("ticker", "").strip().upper()
        outlook: str = input.get("outlook", "neutral").lower()
        if outlook not in ("bullish", "bearish", "neutral"):
            outlook = "neutral"
        if not ticker:
            return AgentResult(agent=self.name, version=self.version,
                               output="No ticker provided.", confidence=0.0,
                               metadata={"error": "missing ticker"})

        try:
            mkt = await get_options_chain(ticker)
            if "error" in mkt:
                return AgentResult(agent=self.name, version=self.version,
                                   output=f"No options data for {ticker}: {mkt['error']}",
                                   confidence=0.0, metadata=mkt)

            price = mkt.get("current_price") or 0.0
            expirations = mkt.get("available_expirations") or []
            chains = mkt.get("chains") or []
            name = mkt.get("company_name", ticker)

            outlook_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "↔️"}.get(outlook, "")

            parts: list[str] = []

            # Header
            parts.append(
                f"<b>Options — {ticker}</b>  <code>${price}</code>  "
                f"{outlook_emoji} <b>{outlook.capitalize()}</b>"
            )

            # Expiration selector
            if expirations:
                parts.append(f"\n<b>Expirations</b>\n{_exp_selector(expirations)}")

            # Chain table + strategies for nearest expiration with data
            chain = chains[0] if chains else None
            if chain:
                exp = chain["expiration"]
                dte = _dte(exp)
                calls = chain.get("calls") or []
                puts  = chain.get("puts")  or []

                parts.append(
                    f"\n<b>Chain — {_fmt_exp(exp)} ({dte}d)</b>\n"
                    + _chain_table(calls, puts, price)
                )

                strategies = _pick_strategies(outlook, calls, puts, price, ticker, exp)
                if strategies:
                    parts.append("")
                    for s in strategies:
                        parts.append(_fmt_strategy(s))
                else:
                    parts.append("<i>Could not compute spreads — insufficient chain data.</i>")
            else:
                parts.append("<i>No options chain data available.</i>")

            parts.append("\n<i>Educational only — not financial advice.</i>")

            output = "\n".join(parts)

            return AgentResult(
                agent=self.name, version=self.version,
                output=output, confidence=0.9,
                metadata={"market_data": mkt, "outlook": outlook},
            )

        except Exception as exc:
            logger.error("OptionsAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
