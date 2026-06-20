"""
Options Research MCP Server

Independent agent for options analysis: live chain data, Black-Scholes Greeks,
IVR, vertical spread ranking (tastytrade-style). Zero LLM dependency for core math;
Claude Sonnet provides optional market context when requested.

Tools:
  research_options(ticker, outlook)     → full spread research + ranked strategies
  get_options_chain(ticker)             → raw chain JSON (up to 24 expirations, 700d)
  calculate_iv_rank(ticker)             → IVR + ATM IV vs 52-week HV range
  recall_research(ticker, limit)        → history from agent memory

Memory: db/agents/options_research.db  (independent from main state.db)
LLM:    claude-sonnet-4-6  (used only for market-context commentary)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp_servers.llm import get_llm_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config  # noqa: E402
from mcp_servers.data_pull import get_options_chain  # noqa: E402
from tools.options_math import (  # noqa: E402
    bs_delta, bs_theta_daily, expected_move,
    ivr_rank, p50, pop_credit_spread, pop_debit_spread,
)

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "options_research.db")
SYSTEM = (
    "You are a professional options trader with deep expertise in volatility, Greeks, "
    "and spread mechanics. Given detailed market data and computed strategies, write a "
    "rigorous, specific analysis. Be direct — use exact numbers, no disclaimers, no filler."
)

_llm = get_llm_client()

_db_ready = False
NUMS = ["①", "②", "③", "④", "⑤"]


def _output_usable(text: str) -> bool:
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dte(exp: str) -> int:
    from datetime import date
    try:
        return max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
    except Exception:
        return 0


def _mid(row: dict) -> float:
    b, a = row.get("bid") or 0.0, row.get("ask") or 0.0
    if b > 0 and a > 0:
        return round((b + a) / 2.0, 2)
    return round(row.get("lastPrice") or 0.0, 2)


def _atm(strikes: list[float], price: float) -> float:
    return min(strikes, key=lambda s: abs(s - price))


def _sigma(row: dict) -> float:
    iv = (row or {}).get("impliedVolatility") or 0.0
    return iv if iv > 0.01 else 0.30


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS research_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                price       REAL,
                outlook     TEXT,
                ivr         REAL,
                recommended TEXT,
                strategies  TEXT,
                output      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rl_ticker ON research_log(ticker);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT NOT NULL,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save_research(ticker: str, price: float, outlook: str, ivr: float,
                          recommended: str, strategies: list, output: str) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO research_log (ticker, timestamp, price, outlook, ivr, "
            "recommended, strategies, output) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, _utcnow(), price, outlook, ivr, recommended,
             json.dumps(strategies), output),
        )
        await db.commit()


async def _log_call(tool: str, ticker: str, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, ticker, duration_ms),
        )
        await db.commit()


# ── Strategy engine (vertical spreads only) ───────────────────────────────────

MIN_SPREAD_WIDTH = 4.0  # minimum spread width in dollars — rejects near-worthless spreads


def _make_spread(kind: str, buy_s: float, sell_s: float,
                 buy_r: dict, sell_r: dict,
                 price: float, T: float, exp: str, dte: int) -> dict | None:
    bp, sp = _mid(buy_r), _mid(sell_r)
    if bp <= 0 or sp <= 0 or buy_s == sell_s:
        return None
    is_credit = sp > bp
    is_call   = "call" in kind
    leg_sigma = (_sigma(buy_r) + _sigma(sell_r)) / 2

    if is_credit:
        net    = round(sp - bp, 2)
        spread = abs(sell_s - buy_s)
        mx_p   = round(net * 100)
        mx_l   = round((spread - net) * 100)
        be     = round(sell_s - net, 2) if "put" in kind else round(sell_s + net, 2)
        pop    = pop_credit_spread(sell_s, price, T, leg_sigma, is_put="put" in kind)
    else:
        debit  = abs(round(bp - sp, 2))
        net    = -debit
        spread = abs(buy_s - sell_s)
        mx_p   = round((spread - debit) * 100)
        mx_l   = round(debit * 100)
        be     = round(min(buy_s, sell_s) + debit, 2) if is_call else round(max(buy_s, sell_s) - debit, 2)
        pop    = pop_debit_spread(be, price, T, leg_sigma, is_call=is_call)

    if mx_l <= 0:
        return None

    roc   = round(mx_p / mx_l * 100, 1)
    bd    = bs_delta(price, buy_s, T, leg_sigma, is_call)
    sd    = bs_delta(price, sell_s, T, leg_sigma, is_call)
    bt    = bs_theta_daily(price, buy_s, T, leg_sigma, is_call)
    st    = bs_theta_daily(price, sell_s, T, leg_sigma, is_call)

    return {
        "kind": kind, "exp": exp, "dte": dte,
        "buy_strike": buy_s, "sell_strike": sell_s,
        "buy_price": bp, "sell_price": sp,
        "net": net, "is_credit": is_credit,
        "max_profit": mx_p, "max_loss": mx_l,
        "breakeven": be,
        "pop": round(pop, 3), "p50": p50(pop),
        "roc": roc, "score": round(pop * (roc / 100), 4),
        "delta": round(bd - sd, 3),
        "theta": round((bt - st) * 100, 2),
        "spread": abs(sell_s - buy_s),
    }


def _generate_spreads(outlook: str, chains: list[dict], price: float) -> list[dict]:
    candidates: list[dict] = []
    for chain in chains:
        exp = chain["expiration"]
        dte = _dte(exp)
        if dte <= 4:
            continue
        T = dte / 365.0
        calls_l = sorted([r for r in chain.get("calls", []) if r.get("strike")], key=lambda r: r["strike"])
        puts_l  = sorted([r for r in chain.get("puts",  []) if r.get("strike")], key=lambda r: r["strike"])
        if not calls_l or not puts_l:
            continue
        all_s  = sorted({r["strike"] for r in calls_l} | {r["strike"] for r in puts_l})
        atm    = _atm(all_s, price)
        cm     = {r["strike"]: r for r in calls_l}
        pm     = {r["strike"]: r for r in puts_l}
        # Only strikes that are at least MIN_SPREAD_WIDTH away from ATM
        above  = [s for s in all_s if s > atm and (s - atm) >= MIN_SPREAD_WIDTH]
        below  = [s for s in all_s if s < atm and (atm - s) >= MIN_SPREAD_WIDTH]

        if outlook in ("bullish", "neutral"):
            for otm in above[:2]:
                s = _make_spread("bull_call", atm, otm, cm.get(atm, {}), cm.get(otm, {}), price, T, exp, dte)
                if s: candidates.append(s)
            for otm in below[-2:][::-1]:
                s = _make_spread("bull_put", otm, atm, pm.get(otm, {}), pm.get(atm, {}), price, T, exp, dte)
                if s: candidates.append(s)
        elif outlook == "bearish":
            for otm in above[:2]:
                s = _make_spread("bear_call", otm, atm, cm.get(otm, {}), cm.get(atm, {}), price, T, exp, dte)
                if s: candidates.append(s)
            for otm in below[-2:][::-1]:
                s = _make_spread("bear_put", atm, otm, pm.get(atm, {}), pm.get(otm, {}), price, T, exp, dte)
                if s: candidates.append(s)

    credits = sorted([c for c in candidates if     c["is_credit"]], key=lambda s: -s["pop"])
    debits  = sorted([c for c in candidates if not c["is_credit"]], key=lambda s: -s["pop"])
    top5    = (credits[:3] + debits[:2]) or credits[:5] or debits[:5]
    for i, s in enumerate(top5[:5]):
        s["num"] = NUMS[i]
    return top5[:5]


def _fmt_strategy_row(s: dict) -> str:
    exp_short = s["exp"][5:] if len(s["exp"]) > 5 else s["exp"]
    net_str   = f"+${s['max_profit']}" if s["is_credit"] else f"-${s['max_loss']}"
    return (
        f"{s['num']} {s['kind'].replace('_', ' ').title():<22} "
        f"{exp_short}  {s['pop']*100:>2.0f}%  {s['p50']*100:>2.0f}%  "
        f"{net_str:>6}  {s['roc']:>4.0f}%  Δ{s['delta']:+.2f}  θ+${s['theta']:.2f}/d"
    )


# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="options-research",
    instructions=(
        "Options research with Black-Scholes Greeks, IVR, and vertical spread ranking. "
        "Tastytrade-style: POP, P50, ROC, theta. Zero LLM for math — Claude Sonnet for context."
    ),
)


@mcp.tool()
async def research_options(ticker: str, outlook: str = "neutral") -> str:
    """
    Full options research for a ticker.
    Fetches live chain data, computes IVR, generates and ranks up to 5 vertical
    spread candidates (credit + debit), and returns a formatted report.
    outlook must be 'bullish', 'bearish', or 'neutral'.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker  = ticker.strip().upper()
    outlook = outlook.lower() if outlook.lower() in ("bullish", "bearish", "neutral") else "neutral"

    mkt = await get_options_chain(ticker)
    if "error" in mkt:
        return f"No options data for {ticker}: {mkt['error']}"

    price   = mkt.get("current_price") or 0.0
    chains  = mkt.get("chains") or []
    name    = mkt.get("company_name", ticker)
    hv_ser  = mkt.get("hv_series") or []
    hv_30d  = mkt.get("hv_30d")

    # ATM IV from first chain
    atm_iv = 0.0
    if chains:
        fc    = chains[0]
        all_s = sorted({r["strike"] for r in fc.get("calls", [])} | {r["strike"] for r in fc.get("puts", [])})
        if all_s:
            atm  = _atm(all_s, price)
            cm   = {r["strike"]: r for r in fc.get("calls", [])}
            pm   = {r["strike"]: r for r in fc.get("puts", [])}
            atm_iv = (cm.get(atm) or {}).get("impliedVolatility") or \
                     (pm.get(atm) or {}).get("impliedVolatility") or 0.0

    ivr     = ivr_rank(atm_iv, hv_ser)
    iv_str  = f"{atm_iv*100:.1f}%" if atm_iv else "—"
    ivr_tag = "🔴 Rich" if ivr >= 50 else "🟢 Cheap"

    strategies = _generate_spreads(outlook, chains, price)
    best       = strategies[0] if strategies else None

    # LLM context
    context_txt = ""
    if atm_iv and chains:
        # Build expiration expected-move summary
        exp_lines = []
        for chain in chains[:8]:
            exp = chain["expiration"]
            dte = _dte(exp)
            calls_l = [r for r in chain.get("calls", []) if r.get("strike")]
            puts_l  = [r for r in chain.get("puts",  []) if r.get("strike")]
            all_s   = sorted({r["strike"] for r in calls_l} | {r["strike"] for r in puts_l})
            em_str = ivx_str = ""
            if all_s:
                atm_r = _atm(all_s, price)
                cm    = {r["strike"]: r for r in calls_l}
                pm    = {r["strike"]: r for r in puts_l}
                iv    = (cm.get(atm_r) or {}).get("impliedVolatility") or (pm.get(atm_r) or {}).get("impliedVolatility")
                if iv:
                    ivx_str = f"IVx {iv*100:.0f}%"
                c_mid = _mid(cm.get(atm_r, {})); p_mid = _mid(pm.get(atm_r, {}))
                if c_mid and p_mid:
                    em_str = f"EM ±${expected_move(c_mid, p_mid):.2f}"
            exp_lines.append(f"  {exp} ({dte}d): {ivx_str}  {em_str}".rstrip())

        # Build strategy summary
        strat_lines = []
        for s in strategies:
            strat_lines.append(
                f"  {s['num']} {s['kind'].replace('_',' ').title()}"
                f"  {s['exp']} ({s['dte']}d)"
                f"  POP {s['pop']*100:.0f}%  P50 {s['p50']*100:.0f}%"
                f"  {'credit' if s['is_credit'] else 'debit'} ${abs(int(s['net']*100))}"
                f"  ROC {s['roc']:.0f}%  Δ{s['delta']:+.2f}  θ${s['theta']:+.2f}/d"
            )

        best_desc = ""
        if best:
            best_desc = (
                f"\nTop pick: {best['num']} {best['kind'].replace('_',' ').title()}\n"
                f"  Strikes: ${best['sell_strike']:.0f}/${best['buy_strike']:.0f}"
                f"  Expiry: {best['exp']} ({best['dte']}d)\n"
                f"  Net: ${abs(int(best['net']*100))}/contract  POP: {best['pop']*100:.0f}%  ROC: {best['roc']:.0f}%\n"
                f"  Breakeven: ${best['breakeven']}  Δ {best['delta']:+.2f}  θ ${best['theta']:+.2f}/day"
            )

        hv_line = f"HV30: {hv_30d*100:.1f}%" if hv_30d else "HV30: N/A"
        ivr_env = "elevated — buying premium is expensive" if ivr >= 50 else "depressed — buying premium is cheap"

        prompt = f"""Options analysis for {name} ({ticker}) at ${price:.2f}. Outlook: {outlook.upper()}.

VOLATILITY
ATM IV: {iv_str}  {hv_line}  IVR: {ivr:.0f}/100 ({ivr_env})
{'IV/HV ratio: ' + f'{atm_iv/hv_30d:.2f}x' if hv_30d and atm_iv else ''}

EXPIRATIONS
{chr(10).join(exp_lines)}

STRATEGIES RANKED BY POP
{chr(10).join(strat_lines)}
{best_desc}

Write a thorough analysis with EXACTLY these four sections:

**IV ENVIRONMENT**
Assess current IV vs HV, what IVR {ivr:.0f} means for option buyers, and how the expected moves across expirations look. Which expiration has the most attractive IV term structure for a {outlook} trade?

**TRADE THESIS**
Why is the top-ranked strategy the best fit for a {outlook} outlook right now? Explain why these specific strikes and expiration were chosen. Compare briefly to the other candidates and why they rank lower.

**KEY LEVELS TO WATCH**
What price levels matter most (breakeven, max profit trigger)? What does the delta position mean for P&L sensitivity? What would need to happen for the trade to reach maximum profit?

**RISK FACTORS**
Top 2-3 specific risks: theta decay rate, IV crush potential, directional failure. When should the position be closed early? Be specific with numbers."""

        try:
            raw = await _llm.complete(SYSTEM, prompt, max_tokens=800)
            if raw and _output_usable(raw):
                context_txt = raw
        except Exception as exc:
            logger.warning("LLM context call failed: %s", exc)

    # Build report
    parts = [
        f"{name} ({ticker}) — Options Research",
        f"Price: ${price}  |  Outlook: {outlook.capitalize()}",
        f"IVR: {ivr:.0f}  IVx: {iv_str}  {ivr_tag}",
    ]
    if hv_30d:
        parts[-1] += f"  HV30: {hv_30d*100:.1f}%"
    if context_txt:
        parts.append(f"\nContext:\n{context_txt}")

    if strategies:
        header = f"\n{'#':<2} {'Strategy':<22} {'Exp':>5}  POP  P50  {'Net':>6}  {'ROC':>4}  Delta    Theta"
        parts.append(f"\nStrategies Ranked by POP:\n{header}\n{'─'*len(header)}")
        for s in strategies:
            star = " ⭐" if s == best else ""
            parts.append(_fmt_strategy_row(s) + star)
    else:
        parts.append("\nCould not generate strategies — insufficient chain data.")

    if best:
        opt  = "Call" if "call" in best["kind"] else "Put"
        verb = "Sell" if best["is_credit"] else "Buy"
        parts.append(
            f"\nRecommended: {best.get('num','')} {best['kind'].replace('_',' ').title()}\n"
            f"  {verb} a {opt} at ${best['sell_strike']:.0f}  /  "
            f"{'Buy' if best['is_credit'] else 'Sell'} a {opt} at ${best['buy_strike']:.0f}\n"
            f"  Expiry: {best['exp']} ({best['dte']}d)  |  "
            f"Max {'credit' if best['is_credit'] else 'debit'}: ${abs(int(best['net']*100))}/contract\n"
            f"  POP: {best['pop']*100:.0f}%  |  P50: {best['p50']*100:.0f}%  |  ROC: {best['roc']:.0f}%"
        )

    parts.append("\nEducational only — not financial advice.")
    output = "\n".join(parts)

    if best:
        await _save_research(ticker, price, outlook, ivr,
                              f"{best['kind']} {best['exp']}", strategies, output)
    await _log_call("research_options", ticker, int((time.monotonic() - t0) * 1000))
    return output


@mcp.tool()
async def get_options_chain_data(ticker: str) -> str:
    """
    Raw options chain JSON for up to 24 expirations within 700 days (calls + puts).
    Strikes filtered to ±15% around current price. No formatting.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    data = await get_options_chain(ticker)
    await _log_call("get_options_chain_data", ticker, int((time.monotonic() - t0) * 1000))
    if "error" in data:
        return json.dumps({"error": data["error"]})
    return json.dumps({k: v for k, v in data.items() if k != "hv_series"}, indent=2)


@mcp.tool()
async def calculate_iv_rank(ticker: str) -> str:
    """
    Calculate IV Rank (IVR) using 52-week rolling HV as proxy for IV range.
    Returns IVR, current ATM IV, HV-30, and implied expected move for nearest expiry.
    """
    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    mkt = await get_options_chain(ticker)
    await _log_call("calculate_iv_rank", ticker, int((time.monotonic() - t0) * 1000))

    if "error" in mkt:
        return f"Error: {mkt['error']}"

    price  = mkt.get("current_price") or 0.0
    chains = mkt.get("chains") or []
    hv_ser = mkt.get("hv_series") or []
    hv_30d = mkt.get("hv_30d")

    atm_iv = 0.0
    em_str = "N/A"
    if chains:
        fc    = chains[0]
        all_s = sorted({r["strike"] for r in fc.get("calls", [])} | {r["strike"] for r in fc.get("puts", [])})
        if all_s:
            atm  = _atm(all_s, price)
            cm   = {r["strike"]: r for r in fc.get("calls", [])}
            pm   = {r["strike"]: r for r in fc.get("puts", [])}
            atm_iv = (cm.get(atm) or {}).get("impliedVolatility") or \
                     (pm.get(atm) or {}).get("impliedVolatility") or 0.0
            c_mid = _mid(cm.get(atm, {}))
            p_mid = _mid(pm.get(atm, {}))
            if c_mid and p_mid:
                em_str = f"±${expected_move(c_mid, p_mid):.2f}"

    ivr = ivr_rank(atm_iv, hv_ser)
    return (
        f"{ticker} IV Analysis\n"
        f"Current Price: ${price}\n"
        f"ATM IV:        {atm_iv*100:.1f}%\n"
        f"HV-30:         {hv_30d*100:.1f}%" + (f"  (ratio: {atm_iv/hv_30d:.2f}x)" if hv_30d else "") + "\n"
        f"IVR (52w):     {ivr:.1f}  {'🔴 Rich — credit spreads favored' if ivr >= 50 else '🟢 Cheap — debit spreads favored'}\n"
        f"Expected Move: {em_str}  (nearest expiry, 1σ)"
    )


@mcp.tool()
async def recall_research(ticker: str, limit: int = 5) -> str:
    """
    Retrieve past options research for a ticker from agent memory.
    Returns most recent `limit` entries.
    """
    await _ensure_db()
    ticker = ticker.strip().upper()
    async with aiosqlite.connect(AGENT_DB) as db:
        async with db.execute(
            "SELECT timestamp, price, outlook, ivr, recommended FROM research_log "
            "WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (ticker, max(1, min(limit, 20))),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return f"No previous options research found for {ticker}."
    lines = [f"Past research for {ticker} (newest first):"]
    for ts, price, outlook, ivr, rec in rows:
        lines.append(f"\n[{ts[:10]}] ${price}  IVR:{ivr:.0f}  {outlook}  → {rec}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
