"""
Tastytrade-style options research agent with per-ticker memory.

For each request it:
  1. Fetches live chain data for up to 4 expirations
  2. Computes BS delta, theta, POP, P50, ROC for up to 5 spread strategies
  3. Ranks them and highlights the best fit for the stated outlook
  4. Outputs a Tastytrade-style HTML card with chain table, comparison grid, P&L chart
  5. Remembers previous research per (chat, ticker) so it can show price/IV changes
"""

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Literal

import aiosqlite

import config
from agents.base_agent import AgentResult, BaseAgent
from tools.market_data import get_options_chain
from tools.options_math import (
    bs_delta, bs_theta_daily,
    expected_move, ivr_rank,
    p50, pop_credit_spread, pop_debit_spread,
)

logger = logging.getLogger(__name__)
Outlook = Literal["bullish", "bearish", "neutral"]

NUMS = ["①", "②", "③", "④", "⑤"]


# ── Date helpers ──────────────────────────────────────────────────────────────

def _dte(exp: str) -> int:
    try:
        return max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 0


def _fmt_exp(exp: str, short: bool = False) -> str:
    try:
        d = datetime.strptime(exp, "%Y-%m-%d")
        return d.strftime("%b%d") if short else d.strftime("%b %d")
    except Exception:
        return exp


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Chain helpers ─────────────────────────────────────────────────────────────

def _mid(row: dict) -> float:
    b    = row.get("bid") or 0.0
    a    = row.get("ask") or 0.0
    last = row.get("lastPrice") or 0.0
    if b > 0 and a > 0:
        return round((b + a) / 2.0, 2)
    if a > 0:
        return round(a, 2)   # use ask alone when market maker quotes ask but no bid
    return round(last, 2)


def _atm(strikes: list[float], price: float) -> float:
    return min(strikes, key=lambda s: abs(s - price))


def _chain_strikes(calls: list, puts: list) -> list[float]:
    cs = {r["strike"] for r in calls if r.get("strike")}
    ps = {r["strike"] for r in puts  if r.get("strike")}
    return sorted(cs | ps)


# ── Strategy builder ──────────────────────────────────────────────────────────

def _make_strategy(
    num: str, name: str, kind: str,
    buy_strike: float, sell_strike: float,
    buy_price: float, sell_price: float,
    S: float, T: float, sigma: float,
    exp: str, dte: int,
) -> dict:
    is_call = "call" in kind
    is_credit = sell_price > buy_price

    if is_credit:
        net = round(sell_price - buy_price, 2)
        max_profit = round(net * 100)
        spread = abs(sell_strike - buy_strike)
        max_loss = round((spread - net) * 100)
        short_strike = sell_strike
        if "bear_call" in kind:
            breakeven = round(sell_strike + net, 2)
            pop = pop_credit_spread(short_strike, S, T, sigma, is_put=False)
        else:  # bull_put
            breakeven = round(sell_strike - net, 2)
            pop = pop_credit_spread(short_strike, S, T, sigma, is_put=True)
    else:
        net = round(-(buy_price - sell_price), 2)  # negative = debit
        debit = abs(net)
        spread = abs(buy_strike - sell_strike)
        max_profit = round((spread - debit) * 100)
        max_loss = round(debit * 100)
        if "bull_call" in kind:
            breakeven = round(min(buy_strike, sell_strike) + debit, 2)
            pop = pop_debit_spread(breakeven, S, T, sigma, is_call=True)
        else:  # bear_put
            breakeven = round(max(buy_strike, sell_strike) - debit, 2)
            pop = pop_debit_spread(breakeven, S, T, sigma, is_call=False)

    if max_loss <= 0:
        return {}

    roc = round(max_profit / max_loss * 100, 1)
    score = round(pop * (roc / 100), 4)

    # Position-level delta and theta (buy leg + sell leg)
    buy_d  = bs_delta(S, buy_strike,  T, sigma, is_call)
    sell_d = bs_delta(S, sell_strike, T, sigma, is_call)
    buy_t  = bs_theta_daily(S, buy_strike,  T, sigma, is_call)
    sell_t = bs_theta_daily(S, sell_strike, T, sigma, is_call)

    pos_delta = round(buy_d - sell_d, 3)
    # buy_t/sell_t are long-option theta (always negative).
    # For the position: long leg pays theta (buy_t), short leg earns theta (-sell_t).
    pos_theta = round((buy_t - sell_t) * 100, 2)  # positive = earns theta (credit), negative = pays (debit)

    return {
        "num": num, "name": name, "kind": kind,
        "exp": exp, "dte": dte,
        "buy_strike": buy_strike, "sell_strike": sell_strike,
        "buy_price": buy_price, "sell_price": sell_price,
        "net": net, "is_credit": is_credit,
        "max_profit": max_profit, "max_loss": max_loss,
        "breakeven": breakeven,
        "pop": round(pop, 3), "p50": p50(pop),
        "roc": roc, "score": score,
        "pos_delta": pos_delta, "pos_theta": pos_theta,
        "spread": abs(sell_strike - buy_strike),
        "sigma": sigma,
    }


def _generate_strategies(outlook: str, chains: list[dict], price: float) -> list[dict]:
    candidates: list[dict] = []

    for chain in chains[:3]:
        exp = chain["expiration"]
        dte = _dte(exp)
        if dte <= 0:
            continue
        T = dte / 365.0
        calls_l = sorted([r for r in chain.get("calls", []) if r.get("strike")], key=lambda r: r["strike"])
        puts_l  = sorted([r for r in chain.get("puts",  []) if r.get("strike")], key=lambda r: r["strike"])
        if not calls_l or not puts_l:
            continue

        all_s = _chain_strikes(calls_l, puts_l)
        atm = _atm(all_s, price)

        calls_m = {r["strike"]: r for r in calls_l}
        puts_m  = {r["strike"]: r for r in puts_l}

        atm_c  = calls_m.get(atm)
        atm_p  = puts_m.get(atm)

        # Nearest strikes above and below ATM
        s_above = [s for s in all_s if s > atm]
        s_below = [s for s in all_s if s < atm]
        otm1_c = s_above[0] if s_above else None
        otm2_c = s_above[1] if len(s_above) > 1 else None
        otm1_p = s_below[-1] if s_below else None
        otm2_p = s_below[-2] if len(s_below) > 1 else None

        # ATM IV as representative sigma for this expiration
        sigma = (atm_c or {}).get("impliedVolatility") or (atm_p or {}).get("impliedVolatility") or 0.30

        def _add(name, kind, b_s, s_s, b_row, s_row):
            if not b_row or not s_row:
                return
            bp, sp = _mid(b_row), _mid(s_row)
            if bp <= 0 or sp <= 0:
                return
            s = _make_strategy(
                num="", name=name, kind=kind,
                buy_strike=b_s, sell_strike=s_s,
                buy_price=bp, sell_price=sp,
                S=price, T=T, sigma=sigma,
                exp=exp, dte=dte,
            )
            if s:
                candidates.append(s)

        if outlook in ("bullish", "neutral"):
            if atm_c and otm1_c:
                _add("Bull Call Spread", "bull_call", atm, otm1_c, atm_c, calls_m.get(otm1_c))
            if atm_p and otm1_p:
                _add("Bull Put Spread",  "bull_put",  otm1_p, atm, puts_m.get(otm1_p), atm_p)
            if atm_c and otm2_c:
                _add("Bull Call (Wide)", "bull_call", atm, otm2_c, atm_c, calls_m.get(otm2_c))

        elif outlook == "bearish":
            if atm_c and otm1_c:
                _add("Bear Call Spread", "bear_call", otm1_c, atm, calls_m.get(otm1_c), atm_c)
            if atm_p and otm1_p:
                _add("Bear Put Spread",  "bear_put",  atm, otm1_p, atm_p, puts_m.get(otm1_p))
            if atm_c and otm2_c:
                _add("Bear Call (Wide)", "bear_call", otm2_c, atm, calls_m.get(otm2_c), atm_c)

    # Rank: credit strategies first, then by score (POP × ROC)
    candidates.sort(key=lambda s: (-int(s["is_credit"]), -s["score"]))
    top5 = candidates[:5]
    for i, s in enumerate(top5):
        s["num"] = NUMS[i]
    return top5


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_exp_selector(chains: list[dict], price: float) -> str:
    rows = []
    dtes = [_dte(c["expiration"]) for c in chains[:5]]
    max_dte = max(dtes) if dtes else 1
    for chain, dte in zip(chains[:5], dtes):
        exp = chain["expiration"]
        # ATM IV and expected move for this expiration
        calls = chain.get("calls", [])
        puts  = chain.get("puts",  [])
        all_s = _chain_strikes(calls, puts)
        atm   = _atm(all_s, price) if all_s else None
        cm = {r["strike"]: r for r in calls}
        pm = {r["strike"]: r for r in puts}
        ivx = ""
        em  = ""
        if atm:
            ac = cm.get(atm, {}); ap = pm.get(atm, {})
            iv = ac.get("impliedVolatility") or ap.get("impliedVolatility")
            if iv:
                ivx = f"  IVx {iv*100:.0f}%"
            ac_mid = _mid(ac); ap_mid = _mid(ap)
            if ac_mid and ap_mid:
                em = f"  EM ±${expected_move(ac_mid, ap_mid):.2f}"
        filled = round(dte / max_dte * 10) if max_dte else 0
        bar = "█" * filled + "░" * (10 - filled)
        tag = "  ← weekly" if dte <= 7 else ("  ← sweet spot" if 21 <= dte <= 50 else "")
        rows.append(f"{_fmt_exp(exp)} {dte:>3}d  {bar}{ivx}{em}{tag}")
    return "<pre>" + "\n".join(rows) + "</pre>"


def _fmt_chain_table(chain: dict, price: float) -> str:
    exp = chain["expiration"]
    dte = _dte(exp)
    T   = dte / 365.0
    calls_l = sorted([r for r in chain.get("calls", []) if r.get("strike")], key=lambda r: r["strike"])
    puts_l  = sorted([r for r in chain.get("puts",  []) if r.get("strike")], key=lambda r: r["strike"])
    if not calls_l and not puts_l:
        return ""

    all_s = _chain_strikes(calls_l, puts_l)
    atm = _atm(all_s, price) if all_s else None
    cm = {r["strike"]: r for r in calls_l}
    pm = {r["strike"]: r for r in puts_l}

    # Expected move for this exp
    ac_mid = _mid(cm.get(atm, {})) if atm else 0
    ap_mid = _mid(pm.get(atm, {})) if atm else 0
    em_str = f"EM ±${expected_move(ac_mid, ap_mid):.2f}" if ac_mid and ap_mid else ""

    # 5 strikes around ATM
    if atm:
        idx = all_s.index(atm)
        sel = all_s[max(0, idx - 2): idx + 3]
    else:
        sel = all_s[:5]

    lines = [f"  Δ    Bid  Ask  STRIKE  Bid  Ask    Δ   "]
    lines.append("─────────────────────────────────────────")
    for s in sel:
        cr = cm.get(s, {})
        pr = pm.get(s, {})
        sigma = cr.get("impliedVolatility") or pr.get("impliedVolatility") or 0.30
        cd = bs_delta(price, s, T, sigma, is_call=True)
        pd = bs_delta(price, s, T, sigma, is_call=False)
        cm2 = _mid(cr); pm2 = _mid(pr)
        cb = cr.get("bid") or 0; ca = cr.get("ask") or 0
        pb = pr.get("bid") or 0; pa = pr.get("ask") or 0
        marker = "◀ATM" if s == atm else "    "
        lines.append(
            f"{cd:>5.2f} {cb:>4.2f} {ca:>4.2f}  {s:>6.0f}  {pb:>4.2f} {pa:>4.2f}  {pd:>5.2f} {marker}"
        )
    if em_str:
        pad = (41 - len(em_str)) // 2
        lines.append("░" * pad + em_str + "░" * (41 - pad - len(em_str)))
    return f"<b>Chain — {_fmt_exp(exp)} ({dte}d)</b>\n<pre>" + "\n".join(lines) + "</pre>"


def _fmt_comparison(strategies: list[dict], best_num: str) -> str:
    header = f"{'#':<2} {'Strategy':<18} {'Exp':>5}  {'POP':>3}  {'P50':>3}  {'Net':>5}  {'ROC':>4}"
    sep    = "━" * len(header)
    rows   = [header, sep]
    for s in strategies:
        net_str = f"+${s['max_profit']}" if s["is_credit"] else f"-${s['max_loss']}"
        star    = " ⭐" if s["num"] == best_num else ""
        rows.append(
            f"{s['num']:<2} {s['name']:<18} {_fmt_exp(s['exp'], short=True):>5}  "
            f"{s['pop']*100:>2.0f}%  {s['p50']*100:>2.0f}%  {net_str:>5}  {s['roc']:>3.0f}%{star}"
        )
    return "<pre>" + "\n".join(rows) + "</pre>"


def _fmt_detail_card(s: dict, price: float) -> str:
    is_call   = "call" in s["kind"]
    opt_word  = "Call" if is_call else "Put"
    net_label = "Net credit" if s["is_credit"] else "Net debit"
    net_sign  = "+" if s["is_credit"] else "-"
    per_share = f"${abs(s['net']):.2f}"
    per_cont  = f"${abs(int(s['net'] * 100))}"

    sell_leg = f"Sell a {opt_word} at <b>${s['sell_strike']:.0f}</b>"
    buy_leg  = f"Buy a {opt_word} at <b>${s['buy_strike']:.0f}</b>"

    # Narrative broken into 3 separate lines, one idea each
    if s["kind"] == "bear_call":
        n1 = f"You collect {per_share}/share ({per_cont}/contract) upfront by selling this spread."
        n2 = f"You keep the full premium if the stock stays <b>below ${s['sell_strike']:.0f}</b> at expiration."
        n3 = f"Losses start above ${s['sell_strike']:.0f} and are <b>capped at ${s['max_loss']}</b> above ${s['buy_strike']:.0f}."
    elif s["kind"] == "bull_put":
        n1 = f"You collect {per_share}/share ({per_cont}/contract) upfront by selling this spread."
        n2 = f"You keep the full premium if the stock stays <b>above ${s['sell_strike']:.0f}</b> at expiration."
        n3 = f"Losses start below ${s['sell_strike']:.0f} and are <b>capped at ${s['max_loss']}</b> below ${s['buy_strike']:.0f}."
    elif s["kind"] == "bear_put":
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a decline."
        n2 = f"The trade becomes profitable if the stock falls <b>below ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in below ${s['sell_strike']:.0f}."
    else:  # bull_call
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a rise."
        n2 = f"The trade becomes profitable if the stock rises <b>above ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in above ${s['sell_strike']:.0f}."

    theta_str = f"{'+' if s['pos_theta'] >= 0 else ''}${s['pos_theta']:.2f}"

    return (
        f"<b>The Legs :</b>  {sell_leg}\n"
        f"             {buy_leg} <i>(protection)</i>\n\n"

        f"<b>How it works</b>\n"
        f"  {n1}\n"
        f"  {n2}\n"
        f"  {n3}\n\n"

        f"<b>Key Numbers</b>\n"
        f"<code>"
        f"{net_label:<12} {net_sign}{per_share}  ({per_cont}/contract)\n"
        f"Break-even   ${s['breakeven']}\n"
        f"\n"
        f"Probability of profit    {s['pop']*100:.0f}%\n"
        f"Prob. of 50% profit      {s['p50']*100:.0f}%\n"
        f"\n"
        f"Max profit   ${s['max_profit']}  |  Max loss  -${s['max_loss']}\n"
        f"ROC          {s['roc']:.1f}%    |  Theta     {theta_str}/day\n"
        f"Delta        {s['pos_delta']:+.3f}   |  Spread    ${s['spread']:.0f}"
        f"</code>"
    )


def _pnl_chart(s: dict) -> str:
    mp = s["max_profit"]
    ml = s["max_loss"]
    be = s["breakeven"]
    lo = min(s["buy_strike"], s["sell_strike"])
    hi = max(s["buy_strike"], s["sell_strike"])
    kind = s["kind"]

    if kind == "bear_call":
        return (
            f"+${mp} ┤──────────────────────\n"
            f"    $0 ┤──────┬───────────────\n"
            f" -${ml} ┤██████│\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )
    elif kind == "bear_put":
        return (
            f"+${mp} ┤──────┐\n"
            f"    $0 ┤───────┴───────────────\n"
            f" -${ml} ┤              ██████████\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )
    elif kind == "bull_call":
        return (
            f"+${mp} ┤──────────────┌────────\n"
            f"    $0 ┤────────────┬─┘\n"
            f" -${ml} ┤████████████│\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )
    else:  # bull_put
        return (
            f"+${mp} ┤────────────────┐\n"
            f"    $0 ┤─────────────────┴─────\n"
            f" -${ml} ┤                   █████\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )


# ── Memory ─────────────────────────────────────────────────────────────────────

async def _load_memory(chat_id: str, ticker: str) -> dict | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT timestamp, price, outlook, ivr, recommended, strategies "
            "FROM options_research_memory "
            "WHERE chat_id=? AND ticker=? ORDER BY id DESC LIMIT 1",
            (chat_id, ticker),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "timestamp": row[0], "price": row[1], "outlook": row[2],
        "ivr": row[3], "recommended": row[4],
        "strategies": json.loads(row[5]) if row[5] else [],
    }


async def _save_memory(
    chat_id: str, ticker: str, price: float, outlook: str,
    ivr: float, recommended: str, strategies: list[dict], output_html: str = "",
) -> None:
    safe_strats = [{k: v for k, v in s.items() if k != "num"} for s in strategies]
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO options_research_memory "
            "(chat_id, ticker, timestamp, price, outlook, ivr, recommended, strategies, output_html) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, ticker, _utcnow(), price, outlook, ivr, recommended,
             json.dumps(safe_strats), output_html),
        )
        await db.commit()


def _fmt_memory_note(prev: dict, current_price: float) -> str:
    try:
        ts   = datetime.fromisoformat(prev["timestamp"])
        days = (datetime.now(timezone.utc) - ts).days
        ago  = f"{days}d ago" if days > 0 else "today"
        prev_price = prev.get("price") or 0
        delta = current_price - prev_price
        sign  = "+" if delta >= 0 else ""
        return (
            f"<i>📋 Last researched {ago} @ ${prev_price:.2f}  "
            f"({sign}${delta:.2f} since, was {prev.get('outlook','?')} — "
            f"recommended {prev.get('recommended','?')})</i>"
        )
    except Exception:
        return ""


# ── Agent ─────────────────────────────────────────────────────────────────────

class OptionsResearchAgent(BaseAgent):
    name = "options_research"
    version = "1.0.0"

    async def run(self, input: dict) -> AgentResult:
        ticker:  str = input.get("ticker", "").strip().upper()
        outlook: str = input.get("outlook", "neutral").lower()
        chat_id: str = str(input.get("chat_id", ""))
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

            price      = mkt.get("current_price") or 0.0
            chains     = mkt.get("chains") or []
            name       = mkt.get("company_name", ticker)
            hv_series  = mkt.get("hv_series") or []
            hv_30d     = mkt.get("hv_30d")

            # IVR — use ATM IV from first chain vs 52w HV range
            atm_iv = 0.0
            if chains:
                fc = chains[0]
                all_s = _chain_strikes(fc.get("calls", []), fc.get("puts", []))
                if all_s:
                    atm = _atm(all_s, price)
                    cm  = {r["strike"]: r for r in fc.get("calls", [])}
                    pm  = {r["strike"]: r for r in fc.get("puts", [])}
                    atm_iv = (cm.get(atm) or {}).get("impliedVolatility") or \
                             (pm.get(atm) or {}).get("impliedVolatility") or 0.0
            ivr = ivr_rank(atm_iv, hv_series)

            outlook_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "↔️"}.get(outlook, "")

            # Load previous research for this chat+ticker
            prev = await _load_memory(chat_id, ticker) if chat_id else None

            # Generate and rank strategies
            strategies = _generate_strategies(outlook, chains, price)
            best = strategies[0] if strategies else None

            # ── Build output ──────────────────────────────────────────────────
            parts: list[str] = []

            # Header
            iv_str  = f"{atm_iv*100:.1f}%" if atm_iv else "—"
            ivr_tag = ("🔴 Rich" if ivr >= 50 else "🟢 Cheap") if ivr != 50 else ""
            parts.append(
                f"<b>{name} ({ticker}) — Options Research</b>\n"
                f"<code>${price:.2f}</code>  {outlook_emoji} <b>{outlook.capitalize()}</b>  "
                f"│  IVR: <code>{ivr:.0f}</code>  IVx: <code>{iv_str}</code>  {ivr_tag}"
            )
            if hv_30d:
                parts[-1] += f"  HV30: <code>{hv_30d*100:.1f}%</code>"

            # Expiration selector
            if chains:
                parts.append(f"\n<b>Expirations</b>\n{_fmt_exp_selector(chains, price)}")

            # Chain table (first expiration)
            if chains:
                parts.append("\n" + _fmt_chain_table(chains[0], price))

            # Comparison table
            if strategies:
                best_num = best["num"] if best else ""
                parts.append(f"\n<b>5 Strategies Compared</b>\n{_fmt_comparison(strategies, best_num)}")

            # Recommendation + detail card
            if best:
                pop_pct   = f"{best['pop']*100:.0f}%"
                p50_pct   = f"{best['p50']*100:.0f}%"
                net_desc  = (f"collecting <b>${best['max_profit']}</b> credit"
                             if best["is_credit"] else f"paying <b>${best['max_loss']}</b> debit")
                risk_desc = (f"<b>${best['max_loss']}</b> maximum loss"
                             if best["is_credit"] else f"<b>${best['max_profit']}</b> maximum gain")
                parts.append(
                    f"\n🏆 <b>Recommended: {best['num']} {best['name']}</b>\n"
                    f"<b>{_fmt_exp(best['exp'])}  ·  {best['dte']} days to expiration</b>\n"
                    f"──────────────────────────────────\n\n"
                    f"📊 <b>{pop_pct}</b> probability of profit  (<b>{p50_pct}</b> chance of reaching 50% profit early)\n"
                    f"💰 {net_desc}  ·  {risk_desc}\n"
                    f"📈 Best risk-adjusted return for a <b>{outlook}</b> outlook  "
                    f"(ROC {best['roc']:.0f}%)"
                )
                parts.append("\n" + _fmt_detail_card(best, price))
                parts.append(f"<pre>{_pnl_chart(best)}</pre>")
            else:
                parts.append("<i>Could not compute strategies — insufficient chain data.</i>")

            # Memory note
            if prev:
                note = _fmt_memory_note(prev, price)
                if note:
                    parts.append("\n" + note)

            parts.append("\n<i>Educational only — not financial advice.</i>")
            if config.WEB_SERVER_URL:
                parts.append(
                    f'🌐 <a href="{config.WEB_SERVER_URL}">'
                    f"View full dashboard</a>"
                )

            output = "\n".join(parts)

            # Save this research to memory (including rendered HTML for web UI)
            if chat_id and best:
                await _save_memory(
                    chat_id=chat_id, ticker=ticker, price=price,
                    outlook=outlook, ivr=ivr,
                    recommended=f"{best['name']} {best['exp']}",
                    strategies=strategies,
                    output_html=output,
                )

            return AgentResult(
                agent=self.name, version=self.version,
                output=output, confidence=0.92,
                metadata={"market_data": mkt, "outlook": outlook, "strategies": len(strategies)},
            )

        except Exception as exc:
            logger.error("OptionsResearchAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
