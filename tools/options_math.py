"""
Black-Scholes utilities for options Greeks, probabilities, and metrics.
All functions are pure math — no I/O, no yfinance.
"""

import math

_SQRT2 = math.sqrt(2)
_SQRT2PI = math.sqrt(2 * math.pi)
_RF = 0.045  # approximate US risk-free rate


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _d1d2(S: float, K: float, T: float, sigma: float, r: float = _RF):
    if T <= 0 or sigma <= 0 or K <= 0 or S <= 0:
        inf = float("inf") if S >= K else float("-inf")
        return inf, inf
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_delta(S: float, K: float, T: float, sigma: float, is_call: bool, r: float = _RF) -> float:
    if T <= 0 or sigma <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return _ncdf(d1) if is_call else _ncdf(d1) - 1.0


def bs_theta_daily(S: float, K: float, T: float, sigma: float, is_call: bool, r: float = _RF) -> float:
    """Theta per calendar day (negative = time decay cost for long, positive for short)."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, d2 = _d1d2(S, K, T, sigma, r)
    decay = -S * _npdf(d1) * sigma / (2.0 * math.sqrt(T))
    rate = (-r * K * math.exp(-r * T) * _ncdf(d2)) if is_call else (r * K * math.exp(-r * T) * _ncdf(-d2))
    return round((decay + rate) / 365.0, 4)


def pop_credit_spread(short_strike: float, S: float, T: float, sigma: float, is_put: bool, r: float = _RF) -> float:
    """P(max profit at expiry) for a short vertical = P(short leg expires worthless)."""
    if T <= 0:
        return 1.0 if (is_put and S > short_strike) or (not is_put and S < short_strike) else 0.0
    _, d2 = _d1d2(S, short_strike, T, sigma, r)
    return _ncdf(d2) if is_put else _ncdf(-d2)


def pop_debit_spread(breakeven: float, S: float, T: float, sigma: float, is_call: bool, r: float = _RF) -> float:
    """P(any profit at expiry) for a long vertical = P(S expires past breakeven)."""
    if T <= 0:
        return 1.0 if (is_call and S > breakeven) or (not is_call and S < breakeven) else 0.0
    _, d2 = _d1d2(S, breakeven, T, sigma, r)
    return _ncdf(d2) if is_call else _ncdf(-d2)


def p50(pop: float) -> float:
    """P50: probability of reaching 50% of max profit before expiry.
    Based on tastytrade research — empirically ~10-15% above POP."""
    return round(min(0.97, pop + (1.0 - pop) * 0.32), 3)


def expected_move(atm_call_mid: float, atm_put_mid: float) -> float:
    """Market-implied 1-sigma move = ATM straddle price."""
    return round(atm_call_mid + atm_put_mid, 2)


def ivr_rank(current_iv: float, hv_series: list[float]) -> float:
    """IV Rank using rolling HV as IV proxy (52-week range)."""
    if not hv_series or len(hv_series) < 10:
        return 50.0
    lo, hi = min(hv_series), max(hv_series)
    if hi <= lo:
        return 50.0
    return round(min(100.0, max(0.0, (current_iv - lo) / (hi - lo) * 100.0)), 1)
