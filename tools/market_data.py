"""
Market data wrapper — yfinance + Polygon.io (real-time) + TWS.

Options chain: TWS only (real-time via ib_insync).
Returns error if TWS is unavailable — no fallback for chains.

Price / HV / company metadata comes from yfinance (+ Polygon overlay if key set).

All public functions are async; blocking calls are offloaded to asyncio.to_thread.
"""

import asyncio
import logging
import os
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
    quarterly_revenues: list[dict] = []        # [{period, revenue_b, qoq_pct}]
    quarterly_profitability: list[dict] = []   # [{period, gross_margin_pct, op_margin_pct, net_margin_pct, roe_pct}]

    try:
        def _find_revenue_key(index) -> str | None:
            for want in ("Total Revenue", "Operating Revenue"):
                if want in index:
                    return want
            return None

        def _series(df, key: str):
            return df.loc[key].dropna() if df is not None and key in df.index else None

        # Annual revenue for YoY
        fin = stock.financials
        if fin is not None and not fin.empty:
            rev_key = _find_revenue_key(fin.index)
            if rev_key:
                revs = fin.loc[rev_key].dropna()
                if len(revs) >= 2:
                    r0, r1 = float(revs.iloc[0]), float(revs.iloc[1])
                    if r1 != 0:
                        revenue_yoy = round((r0 - r1) / abs(r1) * 100, 2)

        # Quarterly financials
        qfin = stock.quarterly_financials
        qbal = stock.quarterly_balance_sheet
        if qfin is not None and not qfin.empty:
            qrev_key = _find_revenue_key(qfin.index)
            if qrev_key:
                rev_s  = _series(qfin, qrev_key)
                gp_s   = _series(qfin, "Gross Profit")
                oi_s   = _series(qfin, "Operating Income")
                ni_s   = _series(qfin, "Net Income")
                eq_s   = _series(qbal, "Common Stock Equity")
                if eq_s is None:
                    eq_s = _series(qbal, "Stockholders Equity")

                qrevs = rev_s.sort_index()
                vals  = [(str(dt.date()), float(v)) for dt, v in qrevs.items()]
                vals  = vals[-6:]

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

                # Quarterly profitability metrics
                for period, rev in vals:
                    if rev == 0:
                        continue
                    import pandas as _pd
                    dt = _pd.Timestamp(period)
                    entry: dict = {"period": period}

                    def _margin(series, dt, rev):
                        if series is None or dt not in series.index:
                            return None
                        v = series[dt]
                        return round(float(v) / rev * 100, 2) if v and rev else None

                    entry["gross_margin_pct"]     = _margin(gp_s, dt, rev)
                    entry["operating_margin_pct"] = _margin(oi_s, dt, rev)
                    entry["net_margin_pct"]        = _margin(ni_s, dt, rev)

                    # ROE = annualised quarterly net income / equity
                    if eq_s is not None and ni_s is not None and dt in eq_s.index and dt in ni_s.index:
                        eq = float(eq_s[dt] or 0)
                        ni = float(ni_s[dt] or 0)
                        if eq and eq > 0:
                            entry["roe_pct"] = round(ni * 4 / eq * 100, 2)

                    if len(entry) > 1:
                        quarterly_profitability.append(entry)
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
        "quarterly_profitability": quarterly_profitability,
        # yfinance debtToEquity is already ×100 (e.g. 79.55 = 0.7955 ratio)
        "debt_to_equity": _safe((info.get("debtToEquity") or 0) / 100),
        "profit_margin_pct": _safe(
            (info.get("profitMargins") or 0) * 100
        ),
        "gross_margin_pct": _safe(
            (info.get("grossMargins") or 0) * 100
        ),
        "roe_pct": _safe((info.get("returnOnEquity") or 0) * 100),
        "market_cap": info.get("marketCap"),
        # yfinance `dividendYield` is unreliable; use trailingAnnualDividendYield (decimal → %)
        "dividend_yield_pct": _safe(
            (info.get("trailingAnnualDividendYield") or 0) * 100
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


def _fetch_fundamentals_edgar_sync(ticker: str) -> dict:
    """Fetch fundamentals from SEC EDGAR via edgartools (XBRL filings).

    Returns the same dict shape as _fetch_fundamentals_sync.  Falls back to
    yfinance only for market-derived metrics (price, PE, market cap, forward PE)
    since those aren't in SEC filings.
    """
    import warnings
    import edgar as _edgar  # edgartools package

    _edgar_identity = os.environ.get("EDGAR_IDENTITY", "Financial Agent research@example.com")
    _edgar.set_identity(_edgar_identity)

    company = _edgar.Company(ticker)
    if getattr(company, "not_found", False):
        return {"error": f"EDGAR: no company found for {ticker}"}

    facts = company.get_facts()
    if facts is None:
        return {"error": f"EDGAR: no XBRL facts for {ticker}"}

    _meta_cols = {"label", "depth", "is_abstract", "is_total", "section", "confidence"}

    def _period_cols(df):
        return [c for c in df.columns if c not in _meta_cols]

    def _val(df, concept, col):
        if concept not in df.index or col not in df.columns:
            return None
        try:
            f = float(df.loc[concept, col])
            return None if (np.isnan(f) or np.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        q_is  = facts.income_statement(periods=6, period="quarterly", as_dataframe=True)
        ann_is = facts.income_statement(periods=3, period="annual",   as_dataframe=True)
        q_bs  = facts.balance_sheet(   periods=1, period="quarterly", as_dataframe=True)

    q_cols   = _period_cols(q_is)    # newest → oldest
    ann_cols = _period_cols(ann_is)
    bs_cols  = _period_cols(q_bs)
    latest_bs = bs_cols[0] if bs_cols else None

    # Revenue concept varies by company (ASC 606 vs old GAAP)
    rev_concept = (
        "RevenueFromContractWithCustomerExcludingAssessedTax"
        if "RevenueFromContractWithCustomerExcludingAssessedTax" in q_is.index
        else "Revenues"
    )

    # Annual YoY revenue growth
    revenue_yoy = None
    if len(ann_cols) >= 2:
        r0 = _val(ann_is, rev_concept, ann_cols[0])
        r1 = _val(ann_is, rev_concept, ann_cols[1])
        if r0 and r1 and r1 != 0:
            revenue_yoy = round((r0 - r1) / abs(r1) * 100, 2)

    # Quarterly revenues oldest→newest for QoQ deltas
    rev_series = [(col, _val(q_is, rev_concept, col)) for col in reversed(q_cols)]
    quarterly_revenues: list[dict] = []
    for i, (period, rev) in enumerate(rev_series):
        if rev is None:
            continue
        qoq = None
        if i > 0 and rev_series[i - 1][1]:
            prev = rev_series[i - 1][1]
            if prev != 0:
                qoq = round((rev - prev) / abs(prev) * 100, 1)
        quarterly_revenues.append({"period": period, "revenue_b": round(rev / 1e9, 2), "qoq_pct": qoq})

    # Margins from most recent quarter
    latest_q = q_cols[0] if q_cols else None
    gross_margin_pct = net_margin_pct = None
    if latest_q:
        rev_q = _val(q_is, rev_concept, latest_q)
        gp_q  = _val(q_is, "GrossProfit",   latest_q)
        ni_q  = _val(q_is, "NetIncomeLoss",  latest_q)
        if rev_q and gp_q:
            gross_margin_pct = round(gp_q / rev_q * 100, 2)
        if rev_q and ni_q:
            net_margin_pct = round(ni_q / rev_q * 100, 2)

    # Debt/Equity from balance sheet
    de_ratio = None
    if latest_bs:
        ltd    = _val(q_bs, "LongTermDebtNoncurrent", latest_bs) or 0
        std    = _val(q_bs, "LongTermDebtCurrent",    latest_bs) or 0
        equity = _val(q_bs, "StockholdersEquity",     latest_bs)
        total_debt = ltd + std
        if equity and equity > 0:
            de_ratio = round(total_debt / equity, 2)

    # TTM EPS (sum of 4 most recent quarterly diluted EPS)
    eps_ttm = None
    if "EarningsPerShareDiluted" in q_is.index:
        eps_vals = [_val(q_is, "EarningsPerShareDiluted", col) for col in q_cols[:4]]
        if all(v is not None for v in eps_vals):
            eps_ttm = round(sum(eps_vals), 2)

    # Market-derived metrics still need yfinance (price, market cap, forward estimates)
    yf_info: dict = {}
    try:
        import yfinance as _yf
        yf_info = _yf.Ticker(ticker).info or {}
    except Exception:
        pass

    current_price = (
        yf_info.get("regularMarketPrice")
        or yf_info.get("currentPrice")
        or yf_info.get("previousClose")
    )
    pe_ratio = None
    if current_price and eps_ttm and eps_ttm > 0:
        pe_ratio = round(float(current_price) / float(eps_ttm), 2)
    else:
        pe_ratio = _safe(yf_info.get("trailingPE"))

    return {
        "ticker":                  ticker.upper(),
        "company_name":            company.name or ticker,
        "sector":                  yf_info.get("sector") or getattr(company, "industry", None) or "N/A",
        "industry":                yf_info.get("industry") or getattr(company, "industry", None),
        "pe_ratio":                pe_ratio,
        "forward_pe":              _safe(yf_info.get("forwardPE")),
        "eps_ttm":                 eps_ttm,
        "eps_forward":             _safe(yf_info.get("forwardEps")),
        "revenue_growth_yoy_pct":  revenue_yoy,
        "quarterly_revenues":      quarterly_revenues,
        "quarterly_profitability": [],
        "debt_to_equity":          de_ratio,
        "profit_margin_pct":       net_margin_pct,
        "gross_margin_pct":        gross_margin_pct,
        "roe_pct":                 _safe((yf_info.get("returnOnEquity") or 0) * 100),
        "market_cap":              yf_info.get("marketCap"),
        "dividend_yield_pct":      _safe((yf_info.get("trailingAnnualDividendYield") or 0) * 100),
        "source":                  "SEC EDGAR (edgartools)",
        "source_url":              f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={company.cik}",
    }


def _parse_ibkr_fundamentals_xml(xml: str, ticker: str) -> dict:
    """Parse Reuters/Refinitiv ReportSnapshot XML from TWS into our standard dict."""
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
        "source":                  "TWS (Reuters Refinitiv)",
        "source_url":              "",
    }


async def _get_fundamentals_ibkr(ticker: str) -> dict | None:
    """Try TWS reqFundamentalData. Returns None if unavailable."""
    try:
        from tools.ibkr_tws import connect_ib
        import config as _cfg
        ib  = await asyncio.wait_for(
            connect_ib(_cfg.IBKR_CLIENT_ID_MARKET_DATA, timeout=300), timeout=300
        )
        from ib_insync import Stock
        contract  = Stock(ticker.upper(), "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return None
        xml = await asyncio.wait_for(
            ib.reqFundamentalDataAsync(qualified[0], "ReportSnapshot"), timeout=300
        )
        if not xml:
            return None
        return _parse_ibkr_fundamentals_xml(xml, ticker)
    except Exception as exc:
        logger.debug("IBKR fundamentals unavailable for %s: %s", ticker, exc)
        return None


async def _get_fundamentals_edgar(ticker: str) -> dict | None:
    """Try SEC EDGAR via edgartools. Returns None if unavailable or not listed."""
    try:
        data = await asyncio.to_thread(_fetch_fundamentals_edgar_sync, ticker)
        if "error" in data:
            logger.debug("EDGAR fundamentals unavailable for %s: %s", ticker, data["error"])
            return None
        return data
    except Exception as exc:
        logger.debug("EDGAR fundamentals failed for %s: %s", ticker, exc)
        return None


async def get_fundamentals(ticker: str) -> dict:
    """Returns PE, forward PE, EPS, revenue growth, debt/equity, margins.

    Priority: TWS (Reuters Refinitiv) → SEC EDGAR (edgartools) → Yahoo Finance.

    IBKR and EDGAR are launched concurrently so the ~15s IBKR timeout doesn't
    block EDGAR (which typically responds in ~2s).  IBKR wins if it finishes
    within 2 seconds after EDGAR returns; otherwise EDGAR result is used.
    """
    ibkr_task  = asyncio.create_task(_get_fundamentals_ibkr(ticker))
    edgar_task = asyncio.create_task(_get_fundamentals_edgar(ticker))

    done, pending = await asyncio.wait(
        {ibkr_task, edgar_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # EDGAR finished first (typical — it's fast)
    if edgar_task in done and ibkr_task in pending:
        edgar = edgar_task.result()
        if edgar:
            # Give IBKR a short grace period in case it's about to succeed
            try:
                ibkr = await asyncio.wait_for(asyncio.shield(ibkr_task), timeout=2.0)
                if ibkr:
                    ibkr_task.cancel()
                    logger.info("get_fundamentals(%s): using TWS data", ticker)
                    return ibkr
            except (asyncio.TimeoutError, Exception):
                pass
            ibkr_task.cancel()
            logger.info("get_fundamentals(%s): using SEC EDGAR data", ticker)
            return edgar
        # EDGAR failed — wait for IBKR to finish
        ibkr = await ibkr_task
        if ibkr:
            logger.info("get_fundamentals(%s): using TWS data", ticker)
            return ibkr

    # IBKR finished first (TWS is running and fast)
    elif ibkr_task in done:
        ibkr = ibkr_task.result()
        if ibkr:
            edgar_task.cancel()
            logger.info("get_fundamentals(%s): using TWS data", ticker)
            return ibkr
        # IBKR failed — wait for EDGAR
        edgar = await edgar_task
        if edgar:
            logger.info("get_fundamentals(%s): using SEC EDGAR data", ticker)
            return edgar

    # Both finished simultaneously — prefer IBKR
    else:
        ibkr  = ibkr_task.result()
        edgar = edgar_task.result()
        if ibkr:
            logger.info("get_fundamentals(%s): using TWS data", ticker)
            return ibkr
        if edgar:
            logger.info("get_fundamentals(%s): using SEC EDGAR data", ticker)
            return edgar

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
            logger.debug("yfinance chain fetch failed %s@%s: %s", ticker, exp, exc)

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
        "source": "Yahoo Finance",
    }


async def get_options_chain(ticker: str) -> dict:
    """
    Returns current price, expirations, and options chain (calls+puts).
    Priority: TWS (real-time) → Yahoo Finance (delayed).
    """
    try:
        from tools.ibkr_options import get_options_chain_ibkr
        ibkr_data = await get_options_chain_ibkr(ticker)
        chains_ok = ibkr_data.get("chains") and any(
            (opt.get("bid") or 0) > 0 or (opt.get("ask") or 0) > 0
            for ch in ibkr_data["chains"]
            for opt in ch.get("calls", []) + ch.get("puts", [])
        )
        if "error" not in ibkr_data and chains_ok:
            logger.info("get_options_chain(%s): using TWS data", ticker)
            ibkr_data["source"] = "TWS"
            return ibkr_data
        reason = ibkr_data.get("error") or ("no live quotes" if ibkr_data.get("chains") else "empty chains")
        logger.info("get_options_chain(%s): IBKR unavailable (%s) — falling back to yfinance", ticker, reason)
    except Exception as exc:
        logger.debug("get_options_chain(%s): IBKR import/call failed (%s) — falling back to yfinance", ticker, exc)

    try:
        data = await asyncio.to_thread(_fetch_options_chain_sync, ticker)
        if data.get("chains"):
            logger.info("get_options_chain(%s): using Yahoo Finance data", ticker)
            return data
        return {"error": f"No options chain data available for {ticker} from TWS or Yahoo Finance."}
    except Exception as exc:
        logger.error("get_options_chain(%s) yfinance fallback failed: %s", ticker, exc)
        return {"error": str(exc)}
