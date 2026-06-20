"""
IBKR options chain fetch via ib_insync TWS socket.

Primary data source for options research agents. Falls back gracefully
(returns {"error": ...}) if IB Gateway is not connected, allowing the
caller to fall through to yfinance.

Data flow:
  1. Connect to IB Gateway (client ID from config.IBKR_CLIENT_ID_OPTIONS_RESEARCH)
  2. Qualify underlying stock → get conId + company name
  3. reqMktData(underlying) → poll until price arrives (up to 5 min)
  4. reqSecDefOptParams → available expirations + strikes
  5. Build Option contracts for ±15% strikes × up to 12 expirations (180d window)
  6. reqMktData for all contracts (genericTickList="106" for IV)
  7. Poll every 0.5s until fills plateau (no new bid/ask for 4s) or 5-min timeout
  8. reqHistoricalData(TRADES, 1Y, 1day) → compute rolling 30d HV for IVR

All IBKR I/O is event-driven: no blind sleeps, proceeds as soon as data arrives.
Overall timeout: 300s (5 minutes) for each blocking step.
"""

import asyncio
import logging
import math
from datetime import date, timedelta

from ib_insync import Option, Stock

import config
from tools.ibkr_tws import connect_ib

logger = logging.getLogger(__name__)

CLIENT_ID  = config.IBKR_CLIENT_ID_OPTIONS_RESEARCH
IBKR_TIMEOUT = 300.0  # 5 minutes — applied to every blocking step


# ── Async polling helpers ──────────────────────────────────────────────────────

async def _wait_for_price(und_ticker, timeout: float = IBKR_TIMEOUT) -> float:
    """Poll until the underlying ticker has any valid price, or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for val in (und_ticker.last, und_ticker.close, und_ticker.bid, und_ticker.ask):
            try:
                if val and float(val) > 0:
                    return float(val)
            except (TypeError, ValueError):
                pass
        await asyncio.sleep(0.5)
    return 0.0


async def _wait_for_fills(
    ticker_map: dict,
    timeout: float = IBKR_TIMEOUT,
    plateau_secs: float = 4.0,
) -> int:
    """
    Poll option tickers until the number of contracts with a bid or ask
    stops increasing for plateau_secs consecutive seconds, or timeout.

    Returns the final fill count.
    """
    loop      = asyncio.get_event_loop()
    deadline  = loop.time() + timeout
    last_count   = -1
    plateau_start = loop.time()

    while loop.time() < deadline:
        filled = sum(
            1 for _, t in ticker_map.values()
            if (t.bid and t.bid > 0) or (t.ask and t.ask > 0)
        )
        now = loop.time()
        if filled != last_count:
            last_count    = filled
            plateau_start = now
        elif now - plateau_start >= plateau_secs:
            break  # data has stopped arriving
        await asyncio.sleep(0.5)

    logger.info("_wait_for_fills: %d/%d contracts filled", max(last_count, 0), len(ticker_map))
    return max(last_count, 0)


# ── Main fetch ─────────────────────────────────────────────────────────────────

async def get_options_chain_ibkr(ticker: str) -> dict:
    """
    Fetch options chain from IB Gateway via ib_insync.

    Returns the same structure as tools.market_data.get_options_chain():
      {ticker, current_price, company_name, available_expirations,
       chains: [{expiration, calls, puts}], hv_30d, hv_series}

    Returns {"error": "<reason>"} on any failure so the caller can
    fall back to yfinance without raising.
    """
    ticker = ticker.strip().upper()

    try:
        ib = await connect_ib(CLIENT_ID, timeout=IBKR_TIMEOUT)
    except ConnectionError as exc:
        return {"error": str(exc)}

    try:
        # ── 1. Qualify underlying ─────────────────────────────────────
        und = Stock(ticker, "SMART", "USD")
        qualified = await asyncio.wait_for(
            ib.qualifyContractsAsync(und), timeout=IBKR_TIMEOUT
        )
        if not qualified:
            return {"error": f"Cannot qualify {ticker}"}
        und_contract = qualified[0]

        # ── 2. Company name ───────────────────────────────────────────
        company_name = ticker
        try:
            details = await asyncio.wait_for(
                ib.reqContractDetailsAsync(und_contract), timeout=IBKR_TIMEOUT
            )
            if details:
                company_name = details[0].longName or ticker
        except Exception:
            pass

        # ── 3. Current price (poll, no blind sleep) ───────────────────
        price = 0.0
        for mkt_type in (1, 2, 3):   # live → frozen → delayed
            ib.reqMarketDataType(mkt_type)
            und_ticker = ib.reqMktData(und_contract, "221", False, False)
            price = await _wait_for_price(und_ticker, timeout=30.0)
            ib.cancelMktData(und_contract)
            if price:
                logger.info("get_options_chain_ibkr(%s): price=%.2f (mkt_type=%d)", ticker, price, mkt_type)
                break

        # Last resort: most recent close from historical data
        if not price:
            try:
                bars = await asyncio.wait_for(
                    ib.reqHistoricalDataAsync(
                        und_contract, endDateTime="", durationStr="5 D",
                        barSizeSetting="1 day", whatToShow="TRADES",
                        useRTH=True, formatDate=1, keepUpToDate=False,
                    ),
                    timeout=IBKR_TIMEOUT,
                )
                closes = [float(b.close) for b in bars if b.close and b.close > 0]
                if closes:
                    price = closes[-1]
            except Exception:
                pass

        if not price:
            return {"error": f"No price data for {ticker} — market may be closed"}

        # ── 4. Option chain params (expirations + strikes) ────────────
        chains_params = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync(
                und_contract.symbol, "", und_contract.secType, und_contract.conId
            ),
            timeout=IBKR_TIMEOUT,
        )

        # Prefer SMART exchange; fall back to IBKR or first available
        chain_p = (
            next((c for c in chains_params if c.exchange == "SMART"), None)
            or next((c for c in chains_params if c.exchange == "IBKR"), None)
            or (chains_params[0] if chains_params else None)
        )
        if not chain_p:
            return {"error": f"No option chain params for {ticker}"}

        # ── 5. Select expirations (180-day window, max 12) and strikes ─
        today  = date.today().strftime("%Y%m%d")
        cutoff = (date.today() + timedelta(days=700)).strftime("%Y%m%d")
        future_exps = sorted(e for e in chain_p.expirations if today <= e <= cutoff)[:24]
        if not future_exps:
            return {"error": f"No upcoming expirations found for {ticker}"}

        # ±15% around current price, ≤17 strikes centred on ATM
        lo, hi = price * 0.85, price * 1.15
        all_strikes = sorted(chain_p.strikes)
        filtered = [s for s in all_strikes if lo <= s <= hi]
        if not filtered:
            lo, hi = price * 0.75, price * 1.25
            filtered = [s for s in all_strikes if lo <= s <= hi]
        if not filtered:
            return {"error": f"No strikes within 25% of {ticker} price ${price:.2f}"}

        atm     = min(filtered, key=lambda s: abs(s - price))
        atm_idx = filtered.index(atm)
        filtered = filtered[max(0, atm_idx - 8): atm_idx + 9]

        # ── 6. Build Option contracts ─────────────────────────────────
        opt_contracts: list[Option] = []
        for exp in future_exps:
            for right in ("C", "P"):
                for strike in filtered:
                    opt_contracts.append(
                        Option(ticker, exp, strike, right, "SMART", currency="USD")
                    )

        # ── 7. Request market data + poll until fills plateau ──────────
        ticker_map: dict[tuple, tuple] = {}
        for c in opt_contracts:
            key = (c.lastTradeDateOrContractMonth, c.right, c.strike)
            t   = ib.reqMktData(c, "106", False, False)
            ticker_map[key] = (c, t)

        logger.info(
            "get_options_chain_ibkr(%s): requested %d contracts (%d exps × %d strikes × 2), polling...",
            ticker, len(opt_contracts), len(future_exps), len(filtered),
        )
        await _wait_for_fills(ticker_map, timeout=IBKR_TIMEOUT, plateau_secs=4.0)

        # Cancel all subscriptions
        for c, _ in ticker_map.values():
            try:
                ib.cancelMktData(c)
            except Exception:
                pass

        # ── 8. Build chains output ─────────────────────────────────────
        exp_rows: dict[str, dict] = {}
        for (exp_raw, right, strike), (c, t) in ticker_map.items():
            exp_fmt = f"{exp_raw[:4]}-{exp_raw[4:6]}-{exp_raw[6:]}"

            # IV: prefer modelGreeks, fall back to askGreeks / bidGreeks
            iv: float | None = None
            for greeks in (t.modelGreeks, t.askGreeks, t.bidGreeks):
                if greeks and getattr(greeks, "impliedVol", None):
                    try:
                        v = float(greeks.impliedVol)
                        if 0.001 < v < 20:
                            iv = round(v, 4)
                            break
                    except (TypeError, ValueError):
                        pass

            bid  = float(t.bid)   if (t.bid   and t.bid   > 0) else 0.0
            ask  = float(t.ask)   if (t.ask   and t.ask   > 0) else 0.0
            last = float(t.last)  if (t.last  and t.last  > 0) else (
                   float(t.close) if (t.close and t.close > 0) else 0.0)
            vol  = int(t.volume)  if (t.volume and t.volume > 0) else 0

            row = {
                "strike":            float(strike),
                "bid":               round(bid,  2),
                "ask":               round(ask,  2),
                "lastPrice":         round(last, 2),
                "volume":            vol,
                "openInterest":      None,  # not available in streaming data
                "impliedVolatility": iv,
            }

            if exp_fmt not in exp_rows:
                exp_rows[exp_fmt] = {"calls": [], "puts": []}
            if right == "C":
                exp_rows[exp_fmt]["calls"].append(row)
            else:
                exp_rows[exp_fmt]["puts"].append(row)

        available_exps = [f"{e[:4]}-{e[4:6]}-{e[6:]}" for e in future_exps]
        chains_out = []
        for exp_fmt in available_exps:
            rows = exp_rows.get(exp_fmt)
            if not rows:
                continue
            chains_out.append({
                "expiration": exp_fmt,
                "calls": sorted(rows["calls"], key=lambda r: r["strike"]),
                "puts":  sorted(rows["puts"],  key=lambda r: r["strike"]),
            })

        if not chains_out:
            return {"error": f"No options data received for {ticker} — check market data subscription"}

        # ── 9. Historical volatility (rolling 30d from 1Y daily closes) ──
        hv_series: list[float] = []
        hv_30d: float | None = None
        try:
            bars = await asyncio.wait_for(
                ib.reqHistoricalDataAsync(
                    und_contract,
                    endDateTime="",
                    durationStr="1 Y",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                    keepUpToDate=False,
                ),
                timeout=IBKR_TIMEOUT,
            )
            closes = [float(b.close) for b in bars if b.close and b.close > 0]
            if len(closes) >= 31:
                log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                window = 30
                for i in range(window - 1, len(log_rets)):
                    w    = log_rets[i - window + 1: i + 1]
                    mean = sum(w) / window
                    var  = sum((x - mean) ** 2 for x in w) / (window - 1)
                    hv_series.append(round(math.sqrt(var) * math.sqrt(252), 4))
                hv_30d = hv_series[-1] if hv_series else None
        except Exception as exc:
            logger.warning("HV fetch failed for %s: %s", ticker, exc)

        return {
            "ticker":                ticker,
            "current_price":         round(price, 2),
            "company_name":          company_name,
            "available_expirations": available_exps,
            "chains":                chains_out,
            "hv_30d":                hv_30d,
            "hv_series":             hv_series,
        }

    except asyncio.TimeoutError:
        return {"error": f"IBKR request timed out for {ticker}"}
    except Exception as exc:
        logger.error("get_options_chain_ibkr(%s) failed: %s", ticker, exc, exc_info=True)
        return {"error": f"IBKR options fetch failed: {exc}"}
