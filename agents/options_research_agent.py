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
from tools import html_components as hc
from tools.market_data import get_options_chain
from tools.options_math import (
    bs_delta, bs_theta_daily,
    expected_move, ivr_rank,
    p50, pop_credit_spread, pop_debit_spread,
)

logger = logging.getLogger(__name__)
Outlook = Literal["bullish", "bearish", "neutral"]

NUMS = ["①", "②", "③", "④", "⑤"]

MIN_SPREAD_WIDTH = 4.0  # hard minimum spread width in dollars — no spread narrower than this is ever suggested


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


MIN_OI  = 100    # minimum open interest per leg
MAX_BA_PCT = 0.50  # max bid-ask spread as fraction of mid (50%)


def _is_liquid(row: dict) -> bool:
    """True if this leg has an active two-sided market and sufficient open interest."""
    bid = row.get("bid") or 0.0
    ask = row.get("ask") or 0.0
    if bid <= 0 or ask <= 0:          # must have a two-sided market
        return False
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    if (ask - bid) / mid > MAX_BA_PCT:
        return False
    oi = row.get("openInterest")      # may be absent depending on data source
    if oi is not None and float(oi) < MIN_OI:
        return False
    return True


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

    # Guaranteed-loss debit spread: paid more than spread can ever be worth
    if not is_credit and max_profit <= 0:
        return {}

    # Guaranteed-loss credit spread: both legs already ITM
    # (stock below protection put for bull_put, or above protection call for bear_call)
    # → spread expires at max loss regardless of movement
    if is_credit:
        if "bull_put" in kind and S < buy_strike:
            return {}
        if "bear_call" in kind and S > buy_strike:
            return {}

    # Negligible credit — IBKR flags near-zero credits as riskless/worthless
    if is_credit and net < 0.10:
        return {}

    # Debit spread not worth placing if max profit < $5/contract after commission
    if not is_credit and max_profit < 5:
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


def _is_vertical(buy_strike: float, sell_strike: float, buy_row: dict,
                  sell_row: dict, exp: str) -> bool:
    """Guard: both legs must be same type, same expiration, meaningfully different strikes."""
    return (
        abs(buy_strike - sell_strike) >= 0.5   # reject same-strike or rounding artifacts
        and bool(buy_row) and bool(sell_row)
        and _mid(buy_row) > 0 and _mid(sell_row) > 0
    )


def _dte_rank_key(s: dict, dte_target: int) -> tuple:
    """Sort key that prefers strategies within tolerance of dte_target, then by POP."""
    tol = max(10, dte_target // 3)
    dist = abs(s.get("dte", 0) - dte_target)
    return (dist > tol, dist, -s["pop"])   # (out-of-tolerance last, closer first, higher POP first)


def _make_single_leg(
    num: str, name: str, kind: str,
    strike: float, price: float,
    S: float, T: float, sigma: float,
    exp: str, dte: int,
) -> dict:
    """Build a Long Call or Long Put strategy dict."""
    if price <= 0:
        return {}
    is_call   = kind == "long_call"
    max_loss  = round(price * 100)
    delta     = bs_delta(S, strike, T, sigma, is_call)
    pop       = round(abs(delta), 3)   # delta ≈ P(expires ITM)
    if is_call:
        breakeven  = round(strike + price, 2)
        max_profit = round(price * 3 * 100)    # display cap: 3× premium (realistic profit target)
    else:
        breakeven  = round(strike - price, 2)
        max_profit = round(price * 3 * 100)    # display cap: 3× premium
    if max_profit <= 0 or max_loss <= 0:
        return {}
    roc       = round(max_profit / max_loss * 100, 1)
    score     = round(pop * (roc / 100), 4)
    pos_theta = round(bs_theta_daily(S, strike, T, sigma, is_call) * 100, 2)
    return {
        "num": num, "name": name, "kind": kind,
        "exp": exp, "dte": dte,
        "buy_strike": strike, "sell_strike": 0.0,
        "buy_price": price,   "sell_price": 0.0,
        "net": -round(price, 2), "is_credit": False,
        "max_profit": max_profit, "max_loss": max_loss,
        "breakeven": breakeven,
        "pop": pop, "p50": p50(pop),
        "roc": roc, "score": score,
        "pos_delta": round(delta, 3), "pos_theta": pos_theta,
        "spread": 0.0, "sigma": sigma,
    }


def _make_straddle(
    num: str, name: str, kind: str,
    call_strike: float, put_strike: float,
    call_price: float, put_price: float,
    S: float, T: float, sigma: float,
    exp: str, dte: int,
) -> dict:
    """Build a Long Straddle or Long Strangle strategy dict."""
    total = round(call_price + put_price, 2)
    if total <= 0:
        return {}
    max_loss  = round(total * 100)
    upper_be  = round(call_strike + total, 2)
    lower_be  = round(put_strike  - total, 2)
    # POP = P(stock > upper_be) + P(stock < lower_be)
    pop_up    = bs_delta(S, upper_be, T, sigma, is_call=True)
    pop_dn    = 1.0 - bs_delta(S, lower_be, T, sigma, is_call=True)
    pop       = round(min(0.99, pop_up + pop_dn), 3)
    # Max profit display estimate (for strangle: distance between strikes + total debit)
    spread_w  = abs(call_strike - put_strike)
    max_profit = round((spread_w + total) * 100) if spread_w > 0 else round(S * 0.15 * 100)
    if max_profit <= 0:
        return {}
    roc       = round(max_profit / max_loss * 100, 1)
    score     = round(pop * (roc / 100), 4)
    call_d    = bs_delta(S, call_strike, T, sigma, True)
    put_d     = bs_delta(S, put_strike,  T, sigma, False)
    call_t    = bs_theta_daily(S, call_strike, T, sigma, True)
    put_t     = bs_theta_daily(S, put_strike,  T, sigma, False)
    pos_theta = round((call_t + put_t) * 100, 2)
    return {
        "num": num, "name": name, "kind": kind,
        "exp": exp, "dte": dte,
        "buy_strike": call_strike, "sell_strike": put_strike,
        "buy_price": call_price,   "sell_price": put_price,
        "net": -total, "is_credit": False,
        "max_profit": max_profit, "max_loss": max_loss,
        "breakeven": upper_be, "breakeven_lower": lower_be,
        "pop": pop, "p50": p50(pop),
        "roc": roc, "score": score,
        "pos_delta": round(call_d + put_d, 3), "pos_theta": pos_theta,
        "spread": round(call_strike - put_strike, 2), "sigma": sigma,
        "call_strike": call_strike, "put_strike": put_strike,
        "total_debit": total,
    }


def _generate_strategies(outlook: str, chains: list[dict], price: float,
                          dte_target: int = 0) -> list[dict]:
    """
    Generate debit-only strategy candidates from IBKR's approved list:
    Long Call, Long Put, Long Call Spread, Long Put Spread,
    Long Straddle, Long Strangle.
    All are debit (you pay to enter). No credit spreads.
    """
    candidates: list[dict] = []

    for chain in chains[:3]:
        exp = chain["expiration"]
        dte = _dte(exp)
        if dte <= 4:
            continue
        T = dte / 365.0
        calls_l = sorted([r for r in chain.get("calls", []) if r.get("strike")], key=lambda r: r["strike"])
        puts_l  = sorted([r for r in chain.get("puts",  []) if r.get("strike")], key=lambda r: r["strike"])
        if not calls_l or not puts_l:
            continue

        all_s   = _chain_strikes(calls_l, puts_l)
        atm     = _atm(all_s, price)
        calls_m = {r["strike"]: r for r in calls_l}
        puts_m  = {r["strike"]: r for r in puts_l}

        s_above = [s for s in all_s if s > atm]
        s_below = [s for s in all_s if s < atm]

        def _sigma(row: dict) -> float:
            iv = (row or {}).get("impliedVolatility") or 0.0
            return iv if iv > 0.01 else 0.30

        def _add_vertical(name: str, kind: str,
                          b_strike: float, s_strike: float,
                          b_row: dict, s_row: dict) -> None:
            if not _is_vertical(b_strike, s_strike, b_row, s_row, exp):
                return
            if abs(b_strike - s_strike) < MIN_SPREAD_WIDTH:
                return
            if not _is_liquid(b_row) or not _is_liquid(s_row):
                return
            leg_sigma = (_sigma(b_row) + _sigma(s_row)) / 2
            s = _make_strategy(
                num="", name=name, kind=kind,
                buy_strike=b_strike, sell_strike=s_strike,
                buy_price=_mid(b_row), sell_price=_mid(s_row),
                S=price, T=T, sigma=leg_sigma,
                exp=exp, dte=dte,
            )
            if s:
                candidates.append(s)

        atm_call_row = calls_m.get(atm, {})
        atm_put_row  = puts_m.get(atm, {})

        # ── Long Call Spread — debit (bullish / neutral) ────────────────────────
        if outlook in ("bullish", "neutral"):
            valid_above = [s for s in s_above if (s - atm) >= MIN_SPREAD_WIDTH]
            for i, otm in enumerate(valid_above[:2]):
                width = "Narrow" if i == 0 else "Wide"
                _add_vertical(f"Long Call Spread ({width})", "bull_call",
                              atm, otm, atm_call_row, calls_m.get(otm, {}))

        # ── Long Put Spread — debit (bearish / neutral) ─────────────────────────
        if outlook in ("bearish", "neutral"):
            valid_below = [s for s in s_below if (atm - s) >= MIN_SPREAD_WIDTH]
            for i, otm in enumerate(valid_below[-2:][::-1]):
                width = "Narrow" if i == 0 else "Wide"
                _add_vertical(f"Long Put Spread ({width})", "bear_put",
                              atm, otm, atm_put_row, puts_m.get(otm, {}))

    if dte_target > 0:
        rank = lambda s: _dte_rank_key(s, dte_target)   # noqa: E731
    else:
        rank = lambda s: -s["pop"]                        # noqa: E731

    top5 = sorted(candidates, key=rank)[:5]

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
    # Column widths
    W = {"num": 2, "name": 22, "strikes": 12, "exp": 5, "pop": 4, "p50": 4, "net": 6, "roc": 5}

    def _cell(text: str, w: int, align: str = "<") -> str:
        return f"{text:{align}{w}}"

    def _row(cells: list[str]) -> str:
        return "│" + "│".join(f" {c} " for c in cells) + "│"

    def _divider(left: str, mid: str, right: str) -> str:
        segs = ["─" * (w + 2) for w in W.values()]
        return left + mid.join(segs) + right

    header_cells = [
        _cell("#",        W["num"]),
        _cell("Strategy", W["name"]),
        _cell("Strikes",  W["strikes"]),
        _cell("Exp",      W["exp"],  ">"),
        _cell("POP",      W["pop"],  ">"),
        _cell("P50",      W["p50"],  ">"),
        _cell("Net",      W["net"],  ">"),
        _cell("ROC",      W["roc"],  ">"),
    ]

    lines = [
        _divider("┌", "┬", "┐"),
        _row(header_cells),
        _divider("├", "┼", "┤"),
    ]

    for s in strategies:
        kind = s["kind"]
        if kind == "long_call":
            strikes = f"${s['buy_strike']:.0f}C"
        elif kind == "long_put":
            strikes = f"${s['buy_strike']:.0f}P"
        elif kind == "long_straddle":
            strikes = f"${s['buy_strike']:.0f}C+P"
        elif kind == "long_strangle":
            strikes = f"${s['buy_strike']:.0f}C/${s['sell_strike']:.0f}P"
        elif "call" in kind:
            strikes = f"${s['sell_strike']:.0f}/${s['buy_strike']:.0f}C"
        else:
            strikes = f"${s['buy_strike']:.0f}/${s['sell_strike']:.0f}P"
        net_str = f"-${s['max_loss']}"   # all strategies are debit
        star    = "⭐" if s["num"] == best_num else " "
        roc_str = f"{s['roc']:>3.0f}%{star}"

        lines.append(_row([
            _cell(s["num"],                           W["num"]),
            _cell(s["name"][:W["name"]],              W["name"]),
            _cell(strikes[:W["strikes"]],             W["strikes"]),
            _cell(_fmt_exp(s["exp"], short=True),     W["exp"],  ">"),
            _cell(f"{s['pop']*100:.0f}%",             W["pop"],  ">"),
            _cell(f"{s['p50']*100:.0f}%",             W["p50"],  ">"),
            _cell(net_str,                            W["net"],  ">"),
            _cell(roc_str,                            W["roc"],  ">"),
        ]))

    lines.append(_divider("└", "┴", "┘"))
    return "<pre>" + "\n".join(lines) + "</pre>"


def _fmt_detail_card(s: dict, price: float) -> str:
    kind      = s["kind"]
    col       = 16

    def row(label: str, value: str) -> str:
        return f"{label:<{col}}│  {value}\n"

    def divider() -> str:
        return f"{'─' * col}┼{'─' * 20}\n"

    theta_str = f"{'+' if s['pos_theta'] >= 0 else ''}${s['pos_theta']:.2f}"
    per_share = f"${abs(s['net']):.2f}"
    per_cont  = f"${abs(int(s['net'] * 100))}"

    # ── Single-leg: Long Call / Long Put ──────────────────────────────────────
    if kind in ("long_call", "long_put"):
        is_call  = kind == "long_call"
        opt_word = "Call" if is_call else "Put"
        strike   = s["buy_strike"]
        be       = s["breakeven"]
        ml       = s["max_loss"]
        mp       = s["max_profit"]

        legs_html = f"Buy a {opt_word} at <b>${strike:.0f}</b>"
        if is_call:
            n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to buy at ${strike:.0f}."
            n2 = f"Profitable above <b>${be}</b> at expiration. Upside is <b>unlimited</b>."
            n3 = f"Maximum loss is <b>${ml}</b> (the premium paid) if the stock closes ≤ ${strike:.0f}."
        else:
            n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to sell at ${strike:.0f}."
            n2 = f"Profitable below <b>${be}</b> at expiration."
            n3 = f"Maximum loss is <b>${ml}</b> (the premium paid) if the stock closes ≥ ${strike:.0f}."

        table = (
            f"{'Metric':<{col}}│  Value\n"
            f"{'─' * col}┼{'─' * 20}\n"
            + row(f"Buy {opt_word}", f"${strike:.0f}  @  ${s['buy_price']:.2f}/share")
            + divider()
            + row("Net debit",   f"-{per_share}  ({per_cont}/contract)")
            + row("Break-even",  f"${be}")
            + divider()
            + row("POP",         f"{s['pop']*100:.0f}%")
            + row("P50",         f"{s['p50']*100:.0f}%")
            + divider()
            + row("Max profit",  f"+${mp} est." if is_call else f"+${mp}")
            + row("Max loss",    f"-${ml}")
            + row("ROC",         f"{s['roc']:.1f}% est.")
            + row("Theta",       f"{theta_str}/day")
            + row("Delta",       f"{s['pos_delta']:+.3f}")
        ).rstrip("\n")

        def _pnl_at_single(px: float) -> int:
            if is_call:
                return int(round((max(0, px - strike) - abs(s["net"])) * 100))
            else:
                return int(round((max(0, strike - px) - abs(s["net"])) * 100))

        step   = round(abs(s["net"]) * 2, 2) or round(strike * 0.03, 2)
        prices = sorted({round(be - step, 2), round(be, 2), round(be + step, 2),
                         round(strike, 2), round(strike + step * 2, 2)})
        scen_lines = [f"{'Stock Price':>12}  {'P&L (1 contract)':>18}  {'Outcome':<15}"]
        scen_lines.append("─" * 52)
        for px in prices:
            pnl  = _pnl_at_single(px)
            sign = "+" if pnl >= 0 else ""
            outcome = ("Max loss ❌" if pnl <= -ml else
                       "Break-even ~" if abs(pnl) <= 2 else
                       "Profit ✅" if pnl > 0 else "Loss")
            scen_lines.append(f"${px:>11.2f}  {sign}${abs(pnl):>16}  {outcome}")
        scenarios = "<pre>" + "\n".join(scen_lines) + "</pre>"

        return (
            f'<div class="hc-hide-web"><b>The Legs</b><br>\n  {legs_html}<br>\n</div>\n'
            f"<b>How it works</b><br>\n  {n1}<br>\n  {n2}<br>\n  {n3}<br>\n<br>\n"
            f'<div class="hc-hide-web"><b>Key Numbers</b>\n<pre>{table}</pre>'
            f"<b>Payoff at Expiration</b>  [{_fmt_exp(s['exp'])}]\n{scenarios}</div>"
        )

    # ── Two-leg: Long Straddle / Long Strangle ────────────────────────────────
    if kind in ("long_straddle", "long_strangle"):
        call_s   = s["buy_strike"]
        put_s    = s["sell_strike"]
        total    = s.get("total_debit", abs(s["net"]))
        upper_be = s["breakeven"]
        lower_be = s.get("breakeven_lower", round(put_s - total, 2))
        ml       = s["max_loss"]
        mp       = s["max_profit"]
        label    = "Straddle" if kind == "long_straddle" else "Strangle"

        n1 = f"You pay {per_share}/share ({per_cont}/contract) total for both options."
        n2 = (f"Profitable if the stock moves <b>above ${upper_be}</b> or <b>below ${lower_be}</b>."
              f" Maximum loss occurs if the stock pins near ${call_s:.0f} at expiration.")
        n3 = f"This is a <b>volatility play</b> — direction doesn't matter, magnitude does."

        table = (
            f"{'Metric':<{col}}│  Value\n"
            f"{'─' * col}┼{'─' * 20}\n"
            + row("Buy Call", f"${call_s:.0f}  @  ${s['buy_price']:.2f}/share")
            + row("Buy Put",  f"${put_s:.0f}  @  ${s['sell_price']:.2f}/share")
            + divider()
            + row("Net debit",     f"-{per_share}  ({per_cont}/contract)")
            + row("Upper B/E",     f"${upper_be}")
            + row("Lower B/E",     f"${lower_be}")
            + divider()
            + row("POP",           f"{s['pop']*100:.0f}%")
            + row("P50",           f"{s['p50']*100:.0f}%")
            + divider()
            + row("Max profit",    f"+${mp} est.")
            + row("Max loss",      f"-${ml}")
            + row("ROC",           f"{s['roc']:.1f}% est.")
            + row("Theta",         f"{theta_str}/day")
            + row("Net delta",     f"{s['pos_delta']:+.3f}")
        ).rstrip("\n")

        def _pnl_at_straddle(px: float) -> int:
            call_pnl = max(0, px - call_s) - s["buy_price"]
            put_pnl  = max(0, put_s  - px) - s["sell_price"]
            return int(round((call_pnl + put_pnl) * 100))

        step   = round(total * 1.5, 2) or round(call_s * 0.05, 2)
        prices = sorted({round(lower_be - step/2, 2), round(lower_be, 2),
                         round((call_s + put_s) / 2, 2),
                         round(upper_be, 2), round(upper_be + step/2, 2)})
        scen_lines = [f"{'Stock Price':>12}  {'P&L (1 contract)':>18}  {'Outcome':<15}"]
        scen_lines.append("─" * 52)
        for px in prices:
            pnl  = _pnl_at_straddle(px)
            sign = "+" if pnl >= 0 else ""
            outcome = ("Max loss ❌" if pnl <= -ml else
                       "Break-even ~" if abs(pnl) <= 2 else
                       "Profit ✅" if pnl > 0 else "Loss")
            scen_lines.append(f"${px:>11.2f}  {sign}${abs(pnl):>16}  {outcome}")
        scenarios = "<pre>" + "\n".join(scen_lines) + "</pre>"

        return (
            f'<div class="hc-hide-web"><b>The Legs</b><br>\n'
            f'  Buy a Call at <b>${call_s:.0f}</b><br>\n'
            f'  Buy a Put at <b>${put_s:.0f}</b><br>\n</div>\n'
            f"<b>How it works</b><br>\n  {n1}<br>\n  {n2}<br>\n  {n3}<br>\n<br>\n"
            f'<div class="hc-hide-web"><b>Key Numbers</b>\n<pre>{table}</pre>'
            f"<b>Payoff at Expiration</b>  [{_fmt_exp(s['exp'])}]\n{scenarios}</div>"
        )

    # ── Vertical debit spread: Long Call Spread / Long Put Spread ─────────────
    is_call   = "call" in kind
    opt_word  = "Call" if is_call else "Put"
    sell_leg  = f"Sell a {opt_word} at <b>${s['sell_strike']:.0f}</b>"
    buy_leg   = f"Buy a {opt_word} at <b>${s['buy_strike']:.0f}</b>"

    if kind == "bear_put":
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a decline."
        n2 = f"The trade becomes profitable if the stock falls <b>below ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in below ${s['sell_strike']:.0f}."
        prot = "protection"
    else:  # bull_call
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a rise."
        n2 = f"The trade becomes profitable if the stock rises <b>above ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in above ${s['sell_strike']:.0f}."
        prot = "cap"

    table = (
        f"{'Metric':<{col}}│  Value\n"
        f"{'─' * col}┼{'─' * 20}\n"
        + row(f"Buy {opt_word}",  f"${s['buy_strike']:.0f}  @  ${s['buy_price']:.2f}/share")
        + row(f"Sell {opt_word}", f"${s['sell_strike']:.0f}  @  ${s['sell_price']:.2f}/share")
        + divider()
        + row("Net debit",   f"-{per_share}  ({per_cont}/contract)")
        + row("Break-even",  f"${s['breakeven']}")
        + divider()
        + row("POP",         f"{s['pop']*100:.0f}%")
        + row("P50",         f"{s['p50']*100:.0f}%")
        + divider()
        + row("Max profit",  f"+${s['max_profit']}")
        + row("Max loss",    f"-${s['max_loss']}")
        + row("ROC",         f"{s['roc']:.1f}%")
        + row("Theta",       f"{theta_str}/day")
        + row("Delta",       f"{s['pos_delta']:+.3f}")
        + row("Spread",      f"${s['spread']:.0f}")
    ).rstrip("\n")

    lo   = min(s["buy_strike"], s["sell_strike"])
    hi   = max(s["buy_strike"], s["sell_strike"])
    be   = s["breakeven"]
    mp   = s["max_profit"]
    ml   = s["max_loss"]
    net  = abs(s["net"])
    step = round((hi - lo) / 4, 2)

    def _pnl_at_vertical(stock_price: float) -> int:
        K_buy  = s["buy_strike"]
        K_sell = s["sell_strike"]
        if is_call:
            pnl = (max(0, stock_price - K_buy) - max(0, stock_price - K_sell) - net) * 100
        else:
            pnl = (max(0, K_buy - stock_price) - max(0, K_sell - stock_price) - net) * 100
        return int(round(pnl))

    prices = sorted({round(lo - step, 2), lo, be, hi, round(hi + step, 2)})
    scen_lines = [f"{'Stock Price':>12}  {'P&L (1 contract)':>18}  {'Outcome':<20}"]
    scen_lines.append("─" * 56)
    for px in prices:
        pnl  = _pnl_at_vertical(px)
        sign = "+" if pnl >= 0 else ""
        outcome = ("Max profit ✅" if pnl >= mp else
                   "Max loss ❌"   if pnl <= -ml else
                   "Break-even ~"  if abs(pnl) <= 2 else
                   "Profit"        if pnl > 0 else "Loss")
        scen_lines.append(f"${px:>11.2f}  {sign}${abs(pnl):>16}  {outcome}")
    scenarios = "<pre>" + "\n".join(scen_lines) + "</pre>"

    return (
        f'<div class="hc-hide-web"><b>The Legs</b><br>\n'
        f'  {buy_leg}<br>\n  {sell_leg} <i>({prot})</i><br>\n</div>\n'
        f"<b>How it works</b><br>\n  {n1}<br>\n  {n2}<br>\n  {n3}<br>\n<br>\n"
        f'<div class="hc-hide-web"><b>Key Numbers</b>\n<pre>{table}</pre>'
        f"<b>Payoff at Expiration</b>  [{_fmt_exp(s['exp'])}]\n{scenarios}</div>"
    )


def _fmt_profit_table(s: dict, price: float) -> str:
    """
    Dense price-by-price P&L table for the recommended vertical spread.
    Works for bull_call and bear_put debit spreads.
    Shows ~12 rows from below lower strike to above upper strike,
    plus the current price, with % of max profit and outcome markers.
    """
    kind    = s["kind"]
    is_call = kind == "bull_call"
    lo      = min(s["buy_strike"], s["sell_strike"])
    hi      = max(s["buy_strike"], s["sell_strike"])
    width   = hi - lo
    mp      = s["max_profit"]
    ml      = s["max_loss"]
    net     = abs(s["net"])
    be      = s["breakeven"]

    def _pnl(px: float) -> int:
        if is_call:
            v = (max(0, px - s["buy_strike"]) - max(0, px - s["sell_strike"]) - net) * 100
        else:
            v = (max(0, s["buy_strike"] - px) - max(0, s["sell_strike"] - px) - net) * 100
        return int(round(v))

    # Build ~12 evenly-spaced levels spanning lo-0.5*width … hi+0.5*width
    pad   = width * 0.5
    start = lo - pad
    end   = hi + pad
    step  = (end - start) / 11
    levels = [round(start + i * step, 2) for i in range(12)]

    # Force-add key prices and sort / deduplicate
    for extra in (price, be, lo, hi):
        closest = min(levels, key=lambda x: abs(x - extra))
        if abs(closest - extra) > step * 0.25:
            levels.append(round(extra, 2))
    levels = sorted(set(round(x, 2) for x in levels))

    hdr = f"{'Stock':>8}  {'P&L':>8}  {'% Max':>6}  {'':4}  Note"
    sep = "─" * 52
    rows = [hdr, sep]
    for px in levels:
        pnl     = _pnl(px)
        pct_max = round(pnl / mp * 100) if mp else 0
        pct_max = max(-99, min(100, pct_max))

        if pnl >= mp:
            note    = "max profit ✅"
            pct_str = "100%"
        elif pnl <= -ml:
            note    = "max loss ❌"
            pct_str = "   —"
        elif abs(pnl) <= 2:
            note    = "break-even"
            pct_str = "  0%"
        elif pnl > 0:
            note    = "profit"
            pct_str = f"{pct_max:3d}%"
        else:
            note    = "loss"
            pct_str = "   —"

        sign    = "+" if pnl >= 0 else "-"
        pnl_str = f"{sign}${abs(pnl)}"

        # Row markers
        marker = "  "
        if abs(px - price) < step * 0.15:
            marker = "◀"          # current price
        elif abs(px - be) < step * 0.15:
            marker = "⬡"          # breakeven

        rows.append(f"${px:>7.2f}  {pnl_str:>8}  {pct_str:>6}  {marker}    {note}")

    return "<b>Profit Table</b> (per contract at expiration)\n<pre>" + "\n".join(rows) + "</pre>"


def _fmt_order_button(s: dict, ticker: str) -> str:
    if s["kind"] not in ("bull_call", "bear_put"):
        return ""   # order placement only implemented for 2-leg vertical spreads
    right       = "C" if "call" in s["kind"] else "P"
    net_display = f"-${s['max_loss']} debit"
    suffix      = f"· {ticker} {right} {s['sell_strike']:.0f}/{s['buy_strike']:.0f} {s['exp']}"
    return (
        f'<div class="order-panel">'
        f'<div class="order-panel-label">📋 Recommended trade — place via IB Gateway</div>'
        f'<form hx-post="/api/place-order" hx-target="#order-result" hx-swap="innerHTML" hx-indicator="#order-spinner">'
        f'<input type="hidden" name="ticker"       value="{ticker}">'
        f'<input type="hidden" name="short_strike" value="{s["sell_strike"]}">'
        f'<input type="hidden" name="long_strike"  value="{s["buy_strike"]}">'
        f'<input type="hidden" name="right"        value="{right}">'
        f'<input type="hidden" name="expiry"       value="{s["exp"]}">'
        f'<input type="hidden" name="net_price" class="calc-order-net-price" value="{s["net"]}">'
        f'<div class="order-row">'
        f'<label class="qty-label">Qty'
        f'<input type="number" name="quantity" value="1" min="1" max="100" class="qty-input">'
        f'</label>'
        f'<button type="submit" class="btn order-btn">📋 Stage in TWS — {s["name"]}</button>'
        f'<span class="order-net calc-order-net-display" data-suffix="{suffix}">'
        f'{net_display} {suffix}</span>'
        f'</div>'
        f'</form>'
        f'<div id="order-spinner" class="htmx-indicator order-spinner">⏳ Placing order…</div>'
        f'<div id="order-result"></div>'
        f'</div>'
    )


def _pnl_chart(s: dict) -> str:
    mp   = s["max_profit"]
    ml   = s["max_loss"]
    be   = s["breakeven"]
    kind = s["kind"]

    if kind == "long_call":
        k = s["buy_strike"]
        return (
            f"   ∞  ┤                    /\n"
            f"  $0  ┤───────────────────/\n"
            f" -${ml} ┤███████████████████\n"
            f"       ${k:.0f}  B/E:${be}"
        )
    elif kind == "long_put":
        k = s["buy_strike"]
        return (
            f"+${mp} ┤\\\n"
            f"   $0  ┤────────────────────\n"
            f" -${ml} ┤                ████\n"
            f"        ${k:.0f}  B/E:${be}"
        )
    elif kind in ("long_straddle", "long_strangle"):
        lower_be = s.get("breakeven_lower", "?")
        mid      = s.get("call_strike", s["buy_strike"])
        return (
            f"   ∞  ┤\\                  /\n"
            f"  $0  ┤─\\────────────────/─\n"
            f" -${ml} ┤  ████████████████\n"
            f"       ${lower_be}  ${mid:.0f}  ${be}"
        )
    elif kind == "bear_put":
        lo = min(s["buy_strike"], s["sell_strike"])
        hi = max(s["buy_strike"], s["sell_strike"])
        return (
            f"+${mp} ┤──────┐\n"
            f"    $0 ┤───────┴───────────────\n"
            f" -${ml} ┤              ██████████\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )
    else:  # bull_call
        lo = min(s["buy_strike"], s["sell_strike"])
        hi = max(s["buy_strike"], s["sell_strike"])
        return (
            f"+${mp} ┤──────────────┌────────\n"
            f"    $0 ┤────────────┬─┘\n"
            f" -${ml} ┤████████████│\n"
            f"       ${lo:.0f}  B/E:${be}  ${hi:.0f}"
        )


def _plotly_pnl_chart(s: dict, current_price: float) -> str:
    """Return a Plotly HTML div for the strategy P&L at expiration (for web UI only)."""
    try:
        import numpy as np
        import plotly.graph_objects as go
    except ImportError:
        return ""

    kind = s["kind"]

    # ── Price axis: tight range around the relevant strikes ──────────────────
    strikes = [current_price]
    for k in ("buy_strike", "sell_strike", "call_strike", "put_strike"):
        v = s.get(k)
        if v:
            strikes.append(v)
    lo = min(strikes) * 0.88
    hi = max(strikes) * 1.12
    prices = np.linspace(lo, hi, 500)

    # ── P&L at expiration (per share, multiply by 100 for per-contract $) ───
    if kind == "long_call":
        pnl = np.maximum(prices - s["buy_strike"], 0) + s["net"]   # net < 0
    elif kind == "long_put":
        pnl = np.maximum(s["buy_strike"] - prices, 0) + s["net"]
    elif kind == "bull_call":
        debit = abs(s["net"])
        pnl = (np.maximum(prices - s["buy_strike"], 0)
               - np.maximum(prices - s["sell_strike"], 0)
               - debit)
    elif kind == "bear_put":
        debit = abs(s["net"])
        pnl = (np.maximum(s["buy_strike"] - prices, 0)
               - np.maximum(s["sell_strike"] - prices, 0)
               - debit)
    elif kind in ("long_straddle", "long_strangle"):
        call_s  = s.get("call_strike", s["buy_strike"])
        put_s   = s.get("put_strike",  s["sell_strike"])
        total   = s.get("total_debit", abs(s["net"]))
        pnl = (np.maximum(prices - call_s, 0)
               + np.maximum(put_s - prices, 0)
               - total)
    else:
        return ""

    pnl_dollars = pnl * 100   # per-contract P&L in $

    fig = go.Figure()

    # Shaded profit / loss regions
    fig.add_trace(go.Scatter(
        x=prices, y=np.maximum(pnl_dollars, 0),
        fill="tozeroy", fillcolor="rgba(38,166,154,0.18)",
        line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=prices, y=np.minimum(pnl_dollars, 0),
        fill="tozeroy", fillcolor="rgba(239,83,80,0.18)",
        line=dict(color="rgba(0,0,0,0)"), showlegend=False, hoverinfo="skip",
    ))

    # Main P&L line
    fig.add_trace(go.Scatter(
        x=prices, y=pnl_dollars,
        mode="lines", line=dict(color="#2196F3", width=2.5),
        name="P&L at expiry",
        hovertemplate="Stock $%{x:.2f} → %{y:+$.0f}<extra></extra>",
    ))

    # Zero line
    fig.add_hline(y=0, line=dict(color="rgba(255,255,255,0.35)", width=1, dash="dot"))

    # Current price
    fig.add_vline(
        x=current_price,
        line=dict(color="rgba(255,255,255,0.5)", width=1, dash="dot"),
        annotation_text=f"Now ${current_price:.2f}",
        annotation_position="top left",
        annotation_font_color="rgba(255,255,255,0.6)",
    )

    # Strike lines
    STRIKE_COLOR = "#FFD700"
    if kind in ("long_call", "long_put"):
        fig.add_vline(x=s["buy_strike"], line=dict(color=STRIKE_COLOR, width=1.5, dash="dash"),
                      annotation_text=f"Strike ${s['buy_strike']:.0f}",
                      annotation_position="bottom right",
                      annotation_font_color=STRIKE_COLOR)
    elif kind in ("long_straddle", "long_strangle"):
        call_s = s.get("call_strike", s["buy_strike"])
        put_s  = s.get("put_strike",  s["sell_strike"])
        for sv, lbl in [(call_s, f"Call ${call_s:.0f}"), (put_s, f"Put ${put_s:.0f}")]:
            fig.add_vline(x=sv, line=dict(color=STRIKE_COLOR, width=1.5, dash="dash"),
                          annotation_text=lbl, annotation_position="bottom right",
                          annotation_font_color=STRIKE_COLOR)
    else:
        for sv, lbl in [
            (s["buy_strike"],  f"Long ${s['buy_strike']:.0f}"),
            (s["sell_strike"], f"Short ${s['sell_strike']:.0f}"),
        ]:
            fig.add_vline(x=sv, line=dict(color=STRIKE_COLOR, width=1.5, dash="dash"),
                          annotation_text=lbl, annotation_position="bottom right",
                          annotation_font_color=STRIKE_COLOR)

    # Breakeven line(s)
    BE_COLOR = "#4CAF50"
    fig.add_vline(x=s["breakeven"], line=dict(color=BE_COLOR, width=1.5, dash="dash"),
                  annotation_text=f"B/E ${s['breakeven']:.2f}",
                  annotation_position="top right",
                  annotation_font_color=BE_COLOR)
    if s.get("breakeven_lower"):
        fig.add_vline(x=s["breakeven_lower"], line=dict(color=BE_COLOR, width=1.5, dash="dash"),
                      annotation_text=f"B/E ${s['breakeven_lower']:.2f}",
                      annotation_position="top left",
                      annotation_font_color=BE_COLOR)

    # Max profit / max loss annotations on y-axis
    mp, ml = s["max_profit"], s["max_loss"]
    fig.update_layout(
        title=dict(
            text=f"P&L at Expiration — {s['name']} | {_fmt_exp(s['exp'])} | Max +${mp} / −${ml}",
            font=dict(size=13),
        ),
        xaxis_title="Underlying Price at Expiration ($)",
        yaxis_title="P&L per Contract ($)",
        template="plotly_dark",
        height=360,
        margin=dict(l=50, r=30, t=55, b=50),
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        showlegend=False,
        xaxis=dict(gridcolor="#2a2a4a"),
        yaxis=dict(gridcolor="#2a2a4a", tickprefix="$"),
    )

    # Emit a data-attribute div; enhanceOutput() in server.py calls Plotly.newPlot on it.
    # Inline <script> tags inside HTMX innerHTML swaps don't execute in browsers,
    # so we pass the spec as JSON and let the already-loaded Plotly in <head> render it.
    import html as _html
    spec = json.dumps({"data": fig.to_dict()["data"], "layout": fig.to_dict()["layout"]})
    return (
        '<div class="plotly-pnl-chart" data-spec="' + _html.escape(spec, quote=True) + '"></div>'
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


# ── Web-only styled output (hc-* design system) ──────────────────────────────

def _web_header(
    ticker: str, name: str, price: float, outlook: str,
    atm_iv: float, ivr: float, hv_30d: float | None,
    term_label: str, source_label: str,
) -> str:
    import html as _h
    outlook_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "↔️"}.get(outlook, "")
    iv_str = f"{atm_iv*100:.1f}%" if atm_iv else "—"
    metrics = [
        {"label": "Price",   "value": f"${price:.2f}"},
        {"label": "Outlook", "value": f"{outlook_emoji} {outlook.capitalize()}",
         "color": "pos" if outlook == "bullish" else ("neg" if outlook == "bearish" else "yellow")},
        {"label": "IVR",     "value": f"{ivr:.0f}",
         "color": "neg" if ivr >= 50 else "pos"},
        {"label": "IVx",     "value": iv_str},
    ]
    if hv_30d:
        metrics.append({"label": "HV30", "value": f"{hv_30d*100:.1f}%"})
    title  = f"{_h.escape(name)} ({_h.escape(ticker)}) — {_h.escape(term_label)}"
    footer = f'<div class="hc-card-footer">Data: {_h.escape(source_label)}</div>'
    return (
        f'<div class="hc-section" style="margin-bottom:14px">'
        f'<div class="hc-section-header">{title}</div>'
        f'<div class="hc-section-body">{hc.metric_grid(metrics)}</div>'
        f'{footer}'
        f'</div>'
    )


def _web_exp_selector(chains: list[dict], price: float) -> str:
    headers = ["Expiry", "DTE", "IVx", "Exp Move", ""]
    rows: list[list] = []; row_cls: list[str] = []
    for chain in chains[:5]:
        exp   = chain["expiration"]
        dte   = _dte(exp)
        calls = chain.get("calls", [])
        puts  = chain.get("puts",  [])
        all_s = _chain_strikes(calls, puts)
        atm   = _atm(all_s, price) if all_s else None
        cm    = {r["strike"]: r for r in calls}
        pm    = {r["strike"]: r for r in puts}
        iv_str = "—"; em_str = "—"
        if atm:
            ac = cm.get(atm, {}); ap = pm.get(atm, {})
            iv = ac.get("impliedVolatility") or ap.get("impliedVolatility")
            if iv:
                iv_str = f"{iv*100:.0f}%"
            ac_mid = _mid(ac); ap_mid = _mid(ap)
            if ac_mid and ap_mid:
                em_str = f"±${expected_move(ac_mid, ap_mid):.2f}"
        if dte <= 7:
            tag = hc.badge("weekly", "red")
        elif 21 <= dte <= 50:
            tag = hc.badge("sweet spot", "green")
        else:
            tag = ""
        rows.append([_fmt_exp(exp), f"{dte}d", iv_str, em_str, tag])
        row_cls.append("hc-row-current" if 21 <= dte <= 50 else "")
    return hc.section_card("Expirations", hc.data_table(headers, rows, row_cls), "📅")


def _web_chain_table(chain: dict, price: float) -> str:
    exp     = chain["expiration"]
    dte     = _dte(exp)
    T       = dte / 365.0
    calls_l = sorted([r for r in chain.get("calls", []) if r.get("strike")], key=lambda r: r["strike"])
    puts_l  = sorted([r for r in chain.get("puts",  []) if r.get("strike")], key=lambda r: r["strike"])
    if not calls_l and not puts_l:
        return ""
    all_s = _chain_strikes(calls_l, puts_l)
    atm   = _atm(all_s, price) if all_s else None
    cm    = {r["strike"]: r for r in calls_l}
    pm    = {r["strike"]: r for r in puts_l}
    if atm:
        idx = all_s.index(atm)
        sel = all_s[max(0, idx - 2): idx + 3]
    else:
        sel = all_s[:5]

    headers = ["C Δ", "C Bid", "C Ask", "Strike", "P Bid", "P Ask", "P Δ"]
    rows: list[list] = []; row_cls: list[str] = []
    for s in sel:
        cr    = cm.get(s, {}); pr = pm.get(s, {})
        sigma = cr.get("impliedVolatility") or pr.get("impliedVolatility") or 0.30
        cd    = bs_delta(price, s, T, sigma, is_call=True)
        pd_v  = bs_delta(price, s, T, sigma, is_call=False)
        cb    = cr.get("bid") or 0; ca = cr.get("ask") or 0
        pb    = pr.get("bid") or 0; pa = pr.get("ask") or 0
        is_atm = s == atm
        strike_cell = f"<b>${s:.0f} ◀ATM</b>" if is_atm else f"${s:.0f}"
        rows.append([f"{cd:.2f}", f"{cb:.2f}", f"{ca:.2f}",
                     strike_cell, f"{pb:.2f}", f"{pa:.2f}", f"{pd_v:.2f}"])
        row_cls.append("hc-row-current" if is_atm else "")
    return hc.section_card(
        f"Chain — {_fmt_exp(exp)} ({dte}d)",
        hc.data_table(headers, rows, row_cls), "🔗"
    )


def _web_comparison(strategies: list[dict], best_num: str) -> str:
    headers = ["#", "Strategy", "Strikes", "Exp", "POP", "P50", "Net", "ROC"]
    rows: list[list] = []; row_cls: list[str] = []
    for s in strategies:
        kind = s["kind"]
        if "call" in kind:
            strikes = f"${s['sell_strike']:.0f}/${s['buy_strike']:.0f}C"
        else:
            strikes = f"${s['buy_strike']:.0f}/${s['sell_strike']:.0f}P"
        is_best   = s["num"] == best_num
        name_cell = f"<b>{s['name']}</b>" if is_best else s["name"]
        roc_cell  = f"{s['roc']:.0f}%"   + (" ⭐" if is_best else "")
        net_str   = f"-${s['max_loss']}"
        pop_cls   = "pos" if s["pop"] >= 0.5 else "neg"
        pop_cell  = f'<span class="{pop_cls}">{s["pop"]*100:.0f}%</span>'
        rows.append([s["num"], name_cell, strikes,
                     _fmt_exp(s["exp"], short=True), pop_cell,
                     f"{s['p50']*100:.0f}%", net_str, roc_cell])
        row_cls.append("hc-row-max" if is_best else "")
    return hc.section_card("5 Strategies Compared", hc.data_table(headers, rows, row_cls), "📊")


def _web_recommendation_header(best: dict, outlook: str) -> str:
    pop_pct = f"{best['pop']*100:.0f}%"
    p50_pct = f"{best['p50']*100:.0f}%"
    metrics = [
        {"label": "Expiry",     "value": _fmt_exp(best["exp"])},
        {"label": "DTE",        "value": f"{best['dte']}d"},
        {"label": "POP",        "value": pop_pct,
         "color": "pos" if best["pop"] >= 0.5 else "neg"},
        {"label": "P50",        "value": p50_pct},
        {"label": "Max Profit", "value": f"+${best['max_profit']}", "color": "pos"},
        {"label": "Max Loss",   "value": f"-${best['max_loss']}",   "color": "neg"},
        {"label": "ROC",        "value": f"{best['roc']:.0f}%"},
        {"label": "Theta/day",  "value": f"{'+' if best['pos_theta'] >= 0 else ''}${best['pos_theta']:.2f}"},
        {"label": "Delta",      "value": f"{best['pos_delta']:+.3f}"},
    ]
    return hc.section_card(
        f"🏆 Recommended: {best['num']} {best['name']}",
        hc.metric_grid(metrics),
    )


def _web_how_it_works(s: dict) -> str:
    kind      = s["kind"]
    per_share = f"${abs(s['net']):.2f}"
    per_cont  = f"${abs(int(s['net'] * 100))}"
    if kind == "bull_call":
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a rise."
        n2 = f"The trade becomes profitable if the stock rises <b>above ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in above ${s['sell_strike']:.0f}."
    elif kind == "bear_put":
        n1 = f"You pay {per_share}/share ({per_cont}/contract) for the right to profit from a decline."
        n2 = f"The trade becomes profitable if the stock falls <b>below ${s['breakeven']}</b> at expiration."
        n3 = f"Maximum gain of <b>${s['max_profit']}</b> is locked in below ${s['sell_strike']:.0f}."
    else:
        return ""
    body = (
        f'<div style="padding:12px 16px;font-size:13px;line-height:1.8">'
        f'{n1}<br><br>{n2}<br><br>{n3}'
        f'</div>'
    )
    return hc.section_card("How It Works", body, "💡")


def _web_debit_calculator(s: dict, price: float) -> str:
    """Interactive debit-price calculator for a vertical spread (web only)."""
    kind = s.get("kind", "")
    if kind not in ("bull_call", "bear_put"):
        return ""
    spread = round(abs(s["sell_strike"] - s["buy_strike"]), 2)
    debit  = abs(s["net"])
    max_db = round(spread - 0.01, 2)
    return (
        f'<div class="hc-section calc-panel"'
        f' data-kind="{kind}"'
        f' data-buy="{s["buy_strike"]}"'
        f' data-sell="{s["sell_strike"]}"'
        f' data-spread="{spread}"'
        f' data-debit="{debit:.2f}"'
        f' data-buy-price="{s["buy_price"]:.2f}"'
        f' data-sell-price="{s["sell_price"]:.2f}"'
        f' data-pop="{s["pop"]:.4f}"'
        f' data-p50="{s["p50"]:.4f}"'
        f' data-delta="{s["pos_delta"]:.4f}"'
        f' data-theta="{s["pos_theta"]:.4f}"'
        f' data-price="{price:.2f}"'
        f' data-exp="{s["exp"]}">'
        f'<div class="hc-section-header">💲 Debit Calculator — adjust entry price</div>'
        f'<div class="hc-section-body">'
        f'<div class="calc-controls">'
        f'<span class="calc-label">Net Debit / share</span>'
        f'<input type="range" class="calc-slider"'
        f' min="0.05" max="{max_db}" step="0.05" value="{debit:.2f}">'
        f'<input type="number" class="calc-input"'
        f' min="0.05" max="{max_db}" step="0.05" value="{debit:.2f}">'
        f'<span class="calc-orig">market mid: ${debit:.2f}</span>'
        f'</div>'
        f'<div class="calc-metrics hc-metric-grid"></div>'
        f'</div>'
        f'</div>'
    )


def _build_web_output(
    ticker: str, name: str, price: float, outlook: str,
    atm_iv: float, ivr: float, hv_30d: float | None,
    term_label: str, source_label: str,
    chains: list[dict], strategies: list[dict], best: dict | None,
    dte_note: str | None = None, prev: dict | None = None,
) -> str:
    """Fully-styled HTML for web using hc-* design system. Never sent to Telegram."""
    parts: list[str] = []

    parts.append(_web_header(
        ticker, name, price, outlook, atm_iv, ivr, hv_30d, term_label, source_label,
    ))

    if dte_note:
        level = "warning" if "⚠" in dte_note else "info"
        parts.append(hc.alert(dte_note, level))

    if chains:
        parts.append(_web_exp_selector(chains, price))
        ct = _web_chain_table(chains[0], price)
        if ct:
            parts.append(ct)

    if strategies:
        best_num = best["num"] if best else ""
        parts.append(_web_comparison(strategies, best_num))

    if best:
        parts.append(_web_recommendation_header(best, outlook))

        calc = _web_debit_calculator(best, price)
        if calc:
            parts.append(calc)

        legs = hc.strategy_legs_card(best)
        if legs:
            parts.append(legs)

        how = _web_how_it_works(best)
        if how:
            parts.append(how)

        key_nums = hc.key_numbers_table(best)
        if key_nums:
            parts.append(f'<div class="calc-kn-wrap">{key_nums}</div>')

        profit = hc.profit_table(best, price)
        if profit:
            parts.append(f'<div class="calc-pt-wrap">{profit}</div>')

        plotly_div = _plotly_pnl_chart(best, price)
        if plotly_div:
            parts.append(hc.section_card("P&L Chart — interactive", plotly_div, "📈"))

        order_btn = _fmt_order_button(best, ticker)
        if order_btn:
            parts.append(order_btn)
    else:
        parts.append(hc.alert("Could not compute strategies — insufficient chain data.", "error"))

    if prev:
        note = _fmt_memory_note(prev, price)
        if note:
            parts.append(f'<div style="margin-top:8px;font-size:13px">{note}</div>')

    parts.append(
        '<div style="margin-top:12px;font-size:12px;color:var(--dim)">'
        '<i>Educational only — not financial advice.</i>'
        '</div>'
    )
    return "\n".join(filter(None, parts))


# ── Agent ─────────────────────────────────────────────────────────────────────

class OptionsResearchAgent(BaseAgent):
    name = "options_research"
    version = "1.0.0"

    async def run(self, input: dict) -> AgentResult:
        ticker:  str = input.get("ticker", "").strip().upper()
        outlook: str = input.get("outlook", "neutral").lower()
        term:    str = input.get("term", "short").lower()
        chat_id: str = str(input.get("chat_id", ""))
        if outlook not in ("bullish", "bearish", "neutral"):
            outlook = "neutral"
        if term not in ("short", "long"):
            term = "short"
        try:
            dte_target = int(input.get("dte_target") or 0)
        except (ValueError, TypeError):
            dte_target = 0
        # Infer term from dte_target when provided
        if dte_target > 45:
            term = "long"
        elif dte_target > 0:
            term = "short"

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

            # Filter chains by term horizon — always exclude ≤4 DTE (too close to expiry)
            viable = [c for c in chains if _dte(c["expiration"]) > 4]
            if dte_target > 0:
                # Pick up to 3 expirations closest to the requested DTE target
                chains = sorted(viable, key=lambda c: abs(_dte(c["expiration"]) - dte_target))[:3]
            elif term == "short":
                short_chains = [c for c in viable if _dte(c["expiration"]) <= 45]
                chains = (short_chains or viable or chains)[:3]
            else:
                long_chains = [c for c in viable if _dte(c["expiration"]) > 21]
                chains = (long_chains or viable or chains)[-3:]

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

            # Generate and rank strategies (guardrail: DTE target preference applied inside)
            strategies = _generate_strategies(outlook, chains, price, dte_target=dte_target)
            best = strategies[0] if strategies else None

            # ── DTE alignment guardrail ───────────────────────────────────────
            dte_note: str | None = None
            if dte_target > 0 and best:
                tol       = max(10, dte_target // 3)
                actual    = best.get("dte", 0)
                diff      = abs(actual - dte_target)
                exp_label = f"{actual}d ({_fmt_exp(best.get('exp', ''))})"
                if diff > tol:
                    dte_note = (
                        f'⚠️ <b>DTE mismatch</b> — no expiration within {tol}d of your '
                        f'<b>{dte_target}d</b> target. '
                        f'Showing closest available: <b>{exp_label}</b>.'
                    )
                elif diff > 3:
                    dte_note = (
                        f'ℹ️ Closest expiration to your <b>{dte_target}d</b> target: '
                        f'<b>{exp_label}</b>.'
                    )

            # ── Build output ──────────────────────────────────────────────────
            parts: list[str] = []

            # Header
            iv_str  = f"{atm_iv*100:.1f}%" if atm_iv else "—"
            ivr_tag = ("🔴 Rich" if ivr >= 50 else "🟢 Cheap") if ivr != 50 else ""
            if dte_target > 0:
                term_label = f"📅 {dte_target}d" if term == "short" else f"📆 {dte_target}d"
            else:
                term_label = "📅 Short Term" if term == "short" else "📆 Long Term"
            source_label = mkt.get("source", "Yahoo Finance")
            parts.append(
                f"<b>{name} ({ticker}) — Options Research  {term_label}</b>\n"
                f"<code>${price:.2f}</code>  {outlook_emoji} <b>{outlook.capitalize()}</b>  "
                f"│  IVR: <code>{ivr:.0f}</code>  IVx: <code>{iv_str}</code>  {ivr_tag}\n"
                f"<i>Data: {source_label}</i>"
            )
            if hv_30d:
                parts[-1] += f"  HV30: <code>{hv_30d*100:.1f}%</code>"

            # DTE guardrail note (shown between header and expiration selector)
            if dte_note:
                parts.append(dte_note)

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
                net_desc  = f"paying <b>${best['max_loss']}</b> debit"
                risk_desc = f"<b>${best['max_profit']}</b> maximum gain"
                parts.append(
                    f"\n🏆 <b>Recommended: {best['num']} {best['name']}</b>\n"
                    f"<b>{_fmt_exp(best['exp'])}  ·  {best['dte']} days to expiration</b>\n"
                    f"──────────────────────────────────<br>\n"
                    f"📊 <b>{pop_pct}</b> probability of profit  (<b>{p50_pct}</b> chance of reaching 50% profit early)<br>\n"
                    f"💰 {net_desc}  ·  {risk_desc}<br>\n"
                    f"📈 Best risk-adjusted return for a <b>{outlook}</b> outlook  "
                    f"(ROC {best['roc']:.0f}%)"
                )
                parts.append(f"\n<b>Trade Structure</b><br>")
                parts.append(_fmt_detail_card(best, price))
                parts.append(f'<div class="hc-hide-web"><pre>{_pnl_chart(best)}</pre></div>')
                parts.append(_fmt_order_button(best, ticker))
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

            # ── Fully-styled web output using hc-* design system ─────────────
            web_output = _build_web_output(
                ticker=ticker, name=name, price=price, outlook=outlook,
                atm_iv=atm_iv, ivr=ivr, hv_30d=hv_30d,
                term_label=term_label, source_label=source_label,
                chains=chains, strategies=strategies, best=best,
                dte_note=dte_note, prev=prev,
            )

            # Save this research to memory
            if chat_id and best:
                await _save_memory(
                    chat_id=chat_id, ticker=ticker, price=price,
                    outlook=outlook, ivr=ivr,
                    recommended=f"{best['name']} {best['exp']}",
                    strategies=strategies,
                    output_html=web_output,
                )

            return AgentResult(
                agent=self.name, version=self.version,
                output=output, confidence=0.92,
                metadata={
                    "market_data": mkt, "outlook": outlook,
                    "strategies": len(strategies),
                    "web_output": web_output,
                },
            )

        except Exception as exc:
            logger.error("OptionsResearchAgent(%s) failed: %s", ticker, exc)
            return self._error_result(exc)
