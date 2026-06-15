"""
Market data wrapper — yfinance (primary) + Polygon.io (real-time, when key is set).

All public functions are async; blocking yfinance calls are offloaded to a thread
pool via asyncio.to_thread so the event loop is never blocked.
"""

import asyncio
import logging
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)

# Optional: TA-Lib for RSI / MA if installed
try:
    import talib as ta  # type: ignore
    _HAS_TALIB = True
except ImportError:
    _HAS_TALIB = False

# Optional: Polygon real-time quotes
try:
    from polygon import RESTClient as PolygonClient  # type: ignore
    _HAS_POLYGON = bool(config.POLYGON_API_KEY)
except ImportError:
    _HAS_POLYGON = False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _rsi(prices: pd.Series, period: int = 14) -> float:
    if _HAS_TALIB:
        vals = ta.RSI(prices.to_numpy(dtype=float), timeperiod=period)
        last = vals[-1]
        return float(last) if not np.isnan(last) else 50.0

    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def _safe(val: Any, decimals: int = 2) -> Any:
    if val is None:
        return None
    try:
        f = float(val)
        return None if (np.isnan(f) or np.isinf(f)) else round(f, decimals)
    except (TypeError, ValueError):
        return None


def _fetch_stock_sync(ticker: str) -> dict:
    stock = yf.Ticker(ticker)
    hist = stock.history(period="1y")
    if hist.empty:
        return {"error": f"No price history found for {ticker}"}

    close = hist["Close"].dropna()
    if len(close) < 2:
        return {"error": f"Insufficient price history for {ticker}"}

    current_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])

    ma20 = _safe(close.rolling(20).mean().iloc[-1])
    ma50 = _safe(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    rsi = _rsi(close)

    info = stock.info or {}
    return {
        "ticker": ticker.upper(),
        "current_price": round(current_price, 2),
        "prev_close": round(prev_close, 2),
        "price_change_pct": round((current_price - prev_close) / prev_close * 100, 2),
        "week52_high": _safe(close.max()),
        "week52_low": _safe(close.min()),
        "rsi_14": round(rsi, 2),
        "ma_20": ma20,
        "ma_50": ma50,
        "volume": int(hist["Volume"].iloc[-1]),
        "avg_volume_30d": int(hist["Volume"].tail(30).mean()),
        "company_name": info.get("longName") or info.get("shortName") or ticker,
        "sector": info.get("sector"),
        "currency": info.get("currency", "USD"),
    }


def _fetch_fundamentals_sync(ticker: str) -> dict:
    stock = yf.Ticker(ticker)
    info = stock.info or {}

    revenue_yoy: float | None = None
    quarterly_revenues: list[dict] = []   # [{period, revenue_b, qoq_pct}]

    try:
        # Annual revenue for YoY
        fin = stock.financials
        if fin is not None and not fin.empty:
            rev_key = next(
                (k for k in fin.index if "Total Revenue" in k or "Revenue" in k), None
            )
            if rev_key:
                revs = fin.loc[rev_key].dropna()
                if len(revs) >= 2:
                    r0, r1 = float(revs.iloc[0]), float(revs.iloc[1])
                    if r1 != 0:
                        revenue_yoy = round((r0 - r1) / abs(r1) * 100, 2)

        # Quarterly revenue — last 6 quarters
        qfin = stock.quarterly_financials
        if qfin is not None and not qfin.empty:
            qrev_key = next(
                (k for k in qfin.index if "Total Revenue" in k or "Revenue" in k), None
            )
            if qrev_key:
                qrevs = qfin.loc[qrev_key].dropna().sort_index()
                vals = [(str(dt.date()), float(v)) for dt, v in qrevs.items()]
                vals = vals[-6:]   # keep most recent 6
                for i, (period, rev) in enumerate(vals):
                    qoq = None
                    if i > 0:
                        prev = vals[i - 1][1]
                        if prev != 0:
                            qoq = round((rev - prev) / abs(prev) * 100, 1)
                    quarterly_revenues.append({
                        "period": period,
                        "revenue_b": round(rev / 1e9, 2),
                        "qoq_pct": qoq,
                    })
    except Exception:
        pass

    return {
        "ticker": ticker.upper(),
        "company_name": info.get("longName") or info.get("shortName") or ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "pe_ratio": _safe(info.get("trailingPE")),
        "forward_pe": _safe(info.get("forwardPE")),
        "eps_ttm": _safe(info.get("trailingEps")),
        "eps_forward": _safe(info.get("forwardEps")),
        "revenue_growth_yoy_pct": revenue_yoy,
        "quarterly_revenues": quarterly_revenues,
        "debt_to_equity": _safe(info.get("debtToEquity")),
        "profit_margin_pct": _safe(
            (info.get("profitMargins") or 0) * 100
        ),
        "gross_margin_pct": _safe(
            (info.get("grossMargins") or 0) * 100
        ),
        "roe_pct": _safe((info.get("returnOnEquity") or 0) * 100),
        "market_cap": info.get("marketCap"),
        "dividend_yield_pct": _safe(
            (info.get("dividendYield") or 0) * 100
        ),
        "source": "Yahoo Finance",
        "source_url": "https://finance.yahoo.com",
    }


def _fetch_options_context_sync(ticker: str) -> dict:
    stock = yf.Ticker(ticker)
    info = stock.info or {}

    current_price = (
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
    )

    expirations: list[str] = []
    try:
        expirations = list(stock.options[:6]) if stock.options else []
    except Exception:
        pass

    # Implied vol from yfinance info (not always available)
    iv = _safe(info.get("impliedVolatility"))

    return {
        "ticker": ticker.upper(),
        "current_price": _safe(current_price),
        "company_name": info.get("longName") or ticker,
        "available_expirations": expirations,
        "implied_volatility": iv,
        "beta": _safe(info.get("beta")),
        "avg_volume": info.get("averageVolume"),
    }


def _fetch_options_chain_sync(ticker: str) -> dict:
    import math as _math
    stock = yf.Ticker(ticker)
    info = stock.info or {}

    current_price = _safe(
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
    )

    expirations: list[str] = []
    try:
        expirations = list(stock.options) if stock.options else []
    except Exception:
        pass

    def _rows(df: "pd.DataFrame") -> list[dict]:
        if df is None or df.empty:
            return []
        want = [c for c in ["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"] if c in df.columns]
        rows = []
        for _, row in df[want].iterrows():
            r: dict = {}
            for k in want:
                r[k] = _safe(row[k], 4) if k == "impliedVolatility" else _safe(row[k])
            rows.append(r)
        return rows

    chains: list[dict] = []
    for exp in expirations[:4]:
        try:
            opt = stock.option_chain(exp)
            calls_df = opt.calls.copy()
            puts_df = opt.puts.copy()
            if current_price:
                lo, hi = current_price * 0.85, current_price * 1.15
                calls_df = calls_df[(calls_df["strike"] >= lo) & (calls_df["strike"] <= hi)]
                puts_df = puts_df[(puts_df["strike"] >= lo) & (puts_df["strike"] <= hi)]
            chains.append({
                "expiration": exp,
                "calls": _rows(calls_df),
                "puts": _rows(puts_df),
            })
        except Exception as exc:
            logger.debug("Chain fetch failed %s@%s: %s", ticker, exp, exc)

    # 52-week rolling 30d HV for IVR computation
    hv_series: list[float] = []
    hv_30d: float | None = None
    try:
        hist = stock.history(period="1y")
        close = hist["Close"].dropna()
        if len(close) >= 30:
            log_ret = close.pct_change().dropna()
            roll_hv = log_ret.rolling(30).std() * _math.sqrt(252)
            hv_series = [float(v) for v in roll_hv.dropna().tolist()]
            hv_30d = _safe(float(roll_hv.iloc[-1])) if not roll_hv.empty else None
    except Exception:
        pass

    return {
        "ticker": ticker.upper(),
        "current_price": current_price,
        "company_name": info.get("longName") or ticker,
        "available_expirations": expirations,
        "chains": chains,
        "implied_volatility": _safe(info.get("impliedVolatility")),
        "beta": _safe(info.get("beta")),
        "hv_30d": hv_30d,
        "hv_series": hv_series,
    }


# ── Polygon real-time quote (optional) ────────────────────────────────────────

async def _polygon_quote(ticker: str) -> dict | None:
    if not _HAS_POLYGON or not config.POLYGON_API_KEY:
        return None
    try:
        def _sync() -> dict | None:
            client = PolygonClient(config.POLYGON_API_KEY)
            snap = client.get_snapshot_ticker("stocks", ticker)
            if snap is None:
                return None
            day = getattr(snap, "day", None)
            prev = getattr(snap, "prev_day", None)
            return {
                "current_price": getattr(snap, "last_trade", {}).get("price") or (
                    getattr(day, "close", None) if day else None
                ),
                "open": getattr(day, "open", None) if day else None,
                "volume": getattr(day, "volume", None) if day else None,
                "prev_close": getattr(prev, "close", None) if prev else None,
            }
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        logger.debug("Polygon quote failed for %s: %s", ticker, exc)
        return None


# ── Public async API ──────────────────────────────────────────────────────────

async def get_stock_data(ticker: str) -> dict:
    """Returns price, 52w range, RSI-14, MA-20/50, volume."""
    try:
        data = await asyncio.to_thread(_fetch_stock_sync, ticker)
        if "error" in data:
            return data

        # Overlay Polygon real-time price if available
        poly = await _polygon_quote(ticker)
        if poly and poly.get("current_price"):
            data["current_price"] = round(float(poly["current_price"]), 2)
            if poly.get("volume"):
                data["volume"] = int(poly["volume"])

        return data
    except Exception as exc:
        logger.error("get_stock_data(%s) failed: %s", ticker, exc)
        return {"error": str(exc)}


def _parse_ibkr_fundamentals_xml(xml: str, ticker: str) -> dict:
    """Parse Reuters/Refinitiv ReportSnapshot XML from IB Gateway into our standard dict."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)

    def _ratio(field: str) -> float | None:
        for el in root.iter("Ratio"):
            if el.get("FieldName") == field:
                try:
                    return float(el.text)
                except (TypeError, ValueError):
                    return None
        return None

    def _text(tag: str) -> str | None:
        el = root.find(f".//{tag}")
        return el.text.strip() if el is not None and el.text else None

    mcap_m = _ratio("MKTCAP")  # in millions
    return {
        "ticker":                  ticker.upper(),
        "company_name":            _text("CoName") or ticker,
        "sector":                  _text("Industry"),
        "industry":                _text("Industry"),
        "pe_ratio":                _ratio("PEEXCLXOR"),
        "forward_pe":              _ratio("FWDPEEXCL"),
        "eps_ttm":                 _ratio("TTMEPSXCLX"),
        "eps_forward":             _ratio("EPSFWD"),
        "revenue_growth_yoy_pct":  _ratio("TTMREVCHG"),
        "quarterly_revenues":      [],
        "debt_to_equity":          _ratio("QTOTD2EQ"),
        "profit_margin_pct":       _ratio("TTMNPMGN"),
        "gross_margin_pct":        _ratio("TTMGROSMGN"),
        "roe_pct":                 _ratio("TTMROEPCT"),
        "market_cap":              mcap_m * 1e6 if mcap_m else None,
        "dividend_yield_pct":      _ratio("YIELD"),
        "source":                  "IB Gateway (Reuters Refinitiv)",
        "source_url":              "",
    }


async def _get_fundamentals_ibkr(ticker: str) -> dict | None:
    """Try IB Gateway reqFundamentalData. Returns None if unavailable."""
    try:
        from tools.ibkr_tws import connect_ib
        import config as _cfg
        ib  = await asyncio.wait_for(
            connect_ib(_cfg.IBKR_CLIENT_ID_MARKET_DATA, timeout=15), timeout=18
        )
        from ib_insync import Stock
        contract  = Stock(ticker.upper(), "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return None
        xml = await asyncio.wait_for(
            ib.reqFundamentalDataAsync(qualified[0], "ReportSnapshot"), timeout=12
        )
        if not xml:
            return None
        return _parse_ibkr_fundamentals_xml(xml, ticker)
    except Exception as exc:
        logger.debug("IBKR fundamentals unavailable for %s: %s", ticker, exc)
        return None


async def get_fundamentals(ticker: str) -> dict:
    """Returns PE, forward PE, EPS, revenue growth, debt/equity, margins.
    Tries IB Gateway (Reuters Refinitiv) first; falls back to Yahoo Finance."""
    ibkr = await _get_fundamentals_ibkr(ticker)
    if ibkr:
        logger.info("get_fundamentals(%s): using IB Gateway data", ticker)
        return ibkr
    logger.info("get_fundamentals(%s): falling back to Yahoo Finance", ticker)
    try:
        return await asyncio.to_thread(_fetch_fundamentals_sync, ticker)
    except Exception as exc:
        logger.error("get_fundamentals(%s) yfinance failed: %s", ticker, exc)
        return {"error": str(exc)}


async def get_options_data(ticker: str) -> dict:
    """Returns current price, available expirations, IV, beta."""
    try:
        return await asyncio.to_thread(_fetch_options_context_sync, ticker)
    except Exception as exc:
        logger.error("get_options_data(%s) failed: %s", ticker, exc)
        return {"error": str(exc)}


async def get_options_chain(ticker: str) -> dict:
    """Returns current price, expirations, and options chain (calls+puts) for first 4 expirations.
    Tries IB Gateway (ib_insync) first; falls back to yfinance if not connected or market closed."""
    try:
        from tools.ibkr_options import get_options_chain_ibkr
        ibkr_data = await get_options_chain_ibkr(ticker)
        if "error" not in ibkr_data:
            logger.info("get_options_chain(%s): using IB Gateway data", ticker)
            ibkr_data["source"] = "IB Gateway"
            return ibkr_data
        logger.info("IBKR options unavailable (%s) — falling back to yfinance", ibkr_data["error"])
    except Exception as exc:
        logger.debug("IBKR options import/call failed for %s: %s", ticker, exc)

    try:
        data = await asyncio.to_thread(_fetch_options_chain_sync, ticker)
        data["source"] = "Yahoo Finance"
        return data
    except Exception as exc:
        logger.error("get_options_chain(%s) failed: %s", ticker, exc)
        return {"error": str(exc)}
