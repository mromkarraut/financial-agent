"""
Charting MCP Server

Interactive Plotly charts for financial data analysis.

Tools:
  plot_price_history(ticker, period, chart_type)    → candlestick/line + volume
  plot_fundamentals(ticker)                          → revenue + margin dashboard
  plot_options_payoff(...)                           → spread P&L at expiration
  plot_comparison(tickers_csv, metric)               → multi-ticker comparison
  plot_custom(title, chart_type, data_json)          → ad-hoc chart from JSON
  recall_charts(ticker, limit)                       → chart history from memory

Memory: db/agents/charting.db
Output: JSON {"html": "<plotly-div>", "description": "...", "ticker": "...", "chart_type": "..."}
        The html field is a Plotly div embed (include_plotlyjs='cdn') — drop it into any page.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_DB = os.path.join(_ROOT, "db", "agents", "charting.db")

_db_ready = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    os.makedirs(os.path.dirname(AGENT_DB), exist_ok=True)
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS charts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                ticker      TEXT,
                chart_type  TEXT NOT NULL,
                title       TEXT,
                html        TEXT NOT NULL,
                description TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chart_ticker ON charts(ticker);
            CREATE INDEX IF NOT EXISTS idx_chart_type   ON charts(chart_type);

            CREATE TABLE IF NOT EXISTS call_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                tool        TEXT NOT NULL,
                ticker      TEXT,
                duration_ms INTEGER
            );
        """)
        await db.commit()
    _db_ready = True


async def _save_chart(
    ticker: str | None, chart_type: str, title: str, html: str, description: str
) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO charts (timestamp, ticker, chart_type, title, html, description) "
            "VALUES (?,?,?,?,?,?)",
            (_utcnow(), ticker, chart_type, title, html, description),
        )
        await db.commit()


async def _log_call(tool: str, ticker: str | None, duration_ms: int) -> None:
    async with aiosqlite.connect(AGENT_DB) as db:
        await db.execute(
            "INSERT INTO call_log (timestamp, tool, ticker, duration_ms) VALUES (?,?,?,?)",
            (_utcnow(), tool, ticker or "", duration_ms),
        )
        await db.commit()


def _result(html: str, description: str, ticker: str | None = None, chart_type: str = "") -> str:
    return json.dumps({
        "html": html,
        "description": description,
        "ticker": ticker,
        "chart_type": chart_type,
    })


# Dark theme shared across all charts
_LAYOUT_BASE = dict(
    template="plotly_dark",
    paper_bgcolor="#1a1a2e",
    plot_bgcolor="#16213e",
    margin=dict(l=40, r=40, t=70, b=60),
)
_GRID = dict(gridcolor="#2a2a4a")

mcp = FastMCP(
    name="charting",
    instructions=(
        "Interactive Plotly charting agent for financial data. Generates candlestick/line "
        "price charts with volume, fundamentals dashboards, options spread payoff diagrams, "
        "multi-ticker comparisons, and custom ad-hoc charts. Returns embed-ready Plotly HTML "
        "divs in a JSON envelope {html, description, ticker, chart_type}."
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def plot_price_history(
    ticker: str,
    period: str = "3mo",
    chart_type: str = "candlestick",
) -> str:
    """
    Interactive price history chart with volume subplot.

    Args:
        ticker:     Stock symbol (e.g. AAPL)
        period:     yfinance period: 1d 5d 1mo 3mo 6mo 1y 2y 5y  (default: 3mo)
        chart_type: 'candlestick' (OHLC) or 'line' (close only)  (default: candlestick)

    Returns JSON {html, description, ticker, chart_type}.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import yfinance as yf

    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    try:
        data = await asyncio.to_thread(
            yf.download, ticker, period=period, interval="1d", progress=False, auto_adjust=True
        )
        if data.empty:
            return json.dumps({"error": f"No price data found for {ticker}"})

        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            row_heights=[0.75, 0.25],
        )

        close = data["Close"].squeeze()
        open_ = data["Open"].squeeze()

        if chart_type == "candlestick":
            fig.add_trace(
                go.Candlestick(
                    x=data.index,
                    open=open_,
                    high=data["High"].squeeze(),
                    low=data["Low"].squeeze(),
                    close=close,
                    name=ticker,
                    increasing_line_color="#26A69A",
                    decreasing_line_color="#EF5350",
                ),
                row=1, col=1,
            )
        else:
            fig.add_trace(
                go.Scatter(
                    x=data.index, y=close,
                    mode="lines", name=ticker,
                    line=dict(color="#2196F3", width=2),
                ),
                row=1, col=1,
            )

        bar_colors = [
            "#26A69A" if c >= o else "#EF5350"
            for c, o in zip(close, open_)
        ]
        fig.add_trace(
            go.Bar(
                x=data.index, y=data["Volume"].squeeze(),
                name="Volume", marker_color=bar_colors, opacity=0.7,
            ),
            row=2, col=1,
        )

        first, last = float(close.iloc[0]), float(close.iloc[-1])
        pct = (last - first) / first * 100
        arrow = "▲" if pct >= 0 else "▼"

        fig.update_layout(
            title=dict(text=f"{ticker} — {period}  {arrow} {pct:+.2f}%", font=dict(size=16)),
            xaxis_rangeslider_visible=False,
            height=500,
            showlegend=False,
            **_LAYOUT_BASE,
        )
        fig.update_yaxes(title_text="Price ($)", row=1, col=1, **_GRID)
        fig.update_yaxes(title_text="Volume",    row=2, col=1, **_GRID)
        fig.update_xaxes(**_GRID)

        html = fig.to_html(include_plotlyjs="cdn", full_html=False, config={"responsive": True})
        desc = f"{ticker} price history ({period}) — ${last:.2f} ({pct:+.2f}%)"

        await asyncio.gather(
            _save_chart(ticker, "price_history", f"{ticker} {period}", html, desc),
            _log_call("plot_price_history", ticker, int((time.monotonic() - t0) * 1000)),
        )
        return _result(html, desc, ticker, "price_history")

    except Exception as exc:
        logger.exception("plot_price_history failed for %s", ticker)
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def plot_fundamentals(ticker: str) -> str:
    """
    Fundamentals dashboard: quarterly revenue bars + gross/net margin lines.

    Args:
        ticker: Stock symbol (e.g. AAPL)

    Returns JSON {html, description, ticker, chart_type}.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from tools.market_data import get_fundamentals

    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()

    try:
        data = await get_fundamentals(ticker)
        if "error" in data:
            return json.dumps({"error": data["error"]})

        qtrs = data.get("quarterly_revenues", [])
        if not qtrs:
            return json.dumps({"error": f"No quarterly revenue data available for {ticker}"})

        periods  = [q["period"] for q in qtrs]
        revenues = [q["revenue_b"] for q in qtrs]
        qoq_pcts = [q.get("qoq_pct") or 0 for q in qtrs]
        bar_colors = ["#26A69A" if v >= 0 else "#EF5350" for v in qoq_pcts]

        fig = make_subplots(
            rows=2, cols=1,
            subplot_titles=("Quarterly Revenue ($B)", "Margins (%)"),
            vertical_spacing=0.18,
            row_heights=[0.55, 0.45],
        )

        fig.add_trace(
            go.Bar(x=periods, y=revenues, name="Revenue ($B)", marker_color=bar_colors),
            row=1, col=1,
        )

        for key, label, color in [
            ("gross_margin_pct",  "Gross Margin",  "#2196F3"),
            ("profit_margin_pct", "Net Margin",    "#4CAF50"),
        ]:
            val = data.get(key)
            if val is not None:
                fig.add_trace(
                    go.Scatter(
                        x=periods, y=[float(val)] * len(periods),
                        mode="lines+markers", name=label,
                        line=dict(color=color, width=2),
                    ),
                    row=2, col=1,
                )

        name   = data.get("company_name", ticker)
        pe     = data.get("pe_ratio", "N/A")
        mcap   = data.get("market_cap")
        mcap_s = f"${mcap/1e9:.1f}B" if mcap else "N/A"

        fig.update_layout(
            title=dict(
                text=f"{name} ({ticker}) — P/E: {pe}  |  Mkt Cap: {mcap_s}",
                font=dict(size=15),
            ),
            height=560,
            legend=dict(orientation="h", y=-0.12),
            **_LAYOUT_BASE,
        )
        fig.update_yaxes(**_GRID)
        fig.update_xaxes(**_GRID)

        html = fig.to_html(include_plotlyjs="cdn", full_html=False, config={"responsive": True})
        desc = (
            f"{name} ({ticker}) fundamentals — "
            f"P/E: {pe}, Gross margin: {data.get('gross_margin_pct')}%, "
            f"Net margin: {data.get('profit_margin_pct')}%"
        )

        await asyncio.gather(
            _save_chart(ticker, "fundamentals", f"{ticker} fundamentals", html, desc),
            _log_call("plot_fundamentals", ticker, int((time.monotonic() - t0) * 1000)),
        )
        return _result(html, desc, ticker, "fundamentals")

    except Exception as exc:
        logger.exception("plot_fundamentals failed for %s", ticker)
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def plot_options_payoff(
    ticker: str,
    short_strike: float,
    long_strike: float,
    right: str,
    expiry: str,
    net_price: float,
    spread_type: str = "credit",
) -> str:
    """
    Options spread payoff diagram at expiration (per-share P&L, multiply by 100 for contract).

    Args:
        ticker:       Underlying symbol
        short_strike: Strike you're selling
        long_strike:  Strike you're buying (hedge leg)
        right:        'C' for calls, 'P' for puts
        expiry:       Expiration date YYYY-MM-DD
        net_price:    Premium received (>0 credit) or paid (<0 debit)
        spread_type:  'credit' or 'debit'  (default: credit)

    Returns JSON {html, description, ticker, chart_type}.
    """
    import numpy as np
    import plotly.graph_objects as go

    await _ensure_db()
    t0 = time.monotonic()
    ticker = ticker.strip().upper()
    right = right.upper()

    try:
        is_call   = right == "C"
        is_credit = spread_type.lower() == "credit" or float(net_price) > 0
        net       = abs(float(net_price))

        mid = (short_strike + long_strike) / 2
        prices = np.linspace(mid * 0.75, mid * 1.25, 400)

        def _leg(strike: float, is_long: bool) -> np.ndarray:
            raw = np.maximum(prices - strike, 0) if is_call else np.maximum(strike - prices, 0)
            return raw if is_long else -raw

        if is_credit:
            pnl = _leg(short_strike, False) + _leg(long_strike, True) + net
        else:
            pnl = _leg(long_strike, True) + _leg(short_strike, False) - net

        max_profit = float(np.max(pnl))
        max_loss   = float(np.min(pnl))

        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=prices, y=np.maximum(pnl, 0),
            fill="tozeroy", fillcolor="rgba(38,166,154,0.15)",
            line=dict(color="rgba(0,0,0,0)"), showlegend=False,
        ))
        fig.add_trace(go.Scatter(
            x=prices, y=np.minimum(pnl, 0),
            fill="tozeroy", fillcolor="rgba(239,83,80,0.15)",
            line=dict(color="rgba(0,0,0,0)"), showlegend=False,
        ))
        fig.add_trace(go.Scatter(
            x=prices, y=pnl, mode="lines",
            line=dict(color="#2196F3", width=2.5),
            name="P&L at expiry",
        ))

        fig.add_hline(y=0, line=dict(color="white", width=1, dash="dot"), opacity=0.5)
        for s, lbl in [(short_strike, "Short"), (long_strike, "Long")]:
            fig.add_vline(
                x=s, line=dict(color="#FFD700", width=1.5, dash="dash"),
                annotation_text=f"{lbl} {s}",
                annotation_position="top right" if lbl == "Short" else "top left",
            )

        bull_put  = is_credit and not is_call
        bull_call = not is_credit and is_call
        spread_name = (
            ("Bull Put Credit" if bull_put else
             "Bear Call Credit" if is_credit else
             "Bull Call Debit" if bull_call else
             "Bear Put Debit") + " Spread"
        )

        fig.update_layout(
            title=dict(
                text=(
                    f"{ticker} — {spread_name} | Exp: {expiry} | "
                    f"Max profit: ${max_profit*100:.0f} | Max loss: ${abs(max_loss)*100:.0f}"
                ),
                font=dict(size=13),
            ),
            xaxis_title="Underlying Price at Expiration ($)",
            yaxis_title="P&L per Share ($)",
            height=430,
            **_LAYOUT_BASE,
        )
        fig.update_xaxes(**_GRID)
        fig.update_yaxes(**_GRID)

        html = fig.to_html(include_plotlyjs="cdn", full_html=False, config={"responsive": True})
        desc = (
            f"{ticker} {spread_name} — Short: {short_strike}, Long: {long_strike}, "
            f"Exp: {expiry}, Net: ${net:.2f} | "
            f"Max profit: ${max_profit*100:.0f}, Max loss: ${abs(max_loss)*100:.0f}"
        )

        await asyncio.gather(
            _save_chart(ticker, "options_payoff", f"{ticker} {spread_name}", html, desc),
            _log_call("plot_options_payoff", ticker, int((time.monotonic() - t0) * 1000)),
        )
        return _result(html, desc, ticker, "options_payoff")

    except Exception as exc:
        logger.exception("plot_options_payoff failed for %s", ticker)
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def plot_comparison(tickers_csv: str, metric: str = "price_return") -> str:
    """
    Compare multiple tickers on a single interactive chart.

    Args:
        tickers_csv: Comma-separated symbols e.g. 'AAPL,MSFT,GOOGL' (max 6)
        metric:      'price_return'   — normalised return over 1y (base 100)
                     'pe_ratio'       — trailing P/E bar chart
                     'revenue_growth' — revenue YoY growth % bar chart
                     'profit_margin'  — net profit margin % bar chart

    Returns JSON {html, description, chart_type}.
    """
    import plotly.graph_objects as go
    import yfinance as yf

    await _ensure_db()
    t0 = time.monotonic()
    tickers = [t.strip().upper() for t in tickers_csv.split(",") if t.strip()][:6]
    if len(tickers) < 2:
        return json.dumps({"error": "Provide at least 2 tickers"})

    COLORS = ["#2196F3", "#4CAF50", "#FF9800", "#E91E63", "#9C27B0", "#00BCD4"]

    try:
        fig = go.Figure()

        if metric == "price_return":
            def _dl(t: str):
                return t, yf.download(t, period="1y", interval="1d", progress=False, auto_adjust=True)

            datasets = await asyncio.gather(*[asyncio.to_thread(_dl, t) for t in tickers])

            for (t, d), color in zip(datasets, COLORS):
                if d.empty:
                    continue
                close = d["Close"].squeeze()
                normalised = close / close.iloc[0] * 100
                fig.add_trace(go.Scatter(
                    x=d.index, y=normalised, mode="lines",
                    name=t, line=dict(color=color, width=2),
                ))
            fig.update_layout(
                title="Price Return Comparison (base 100) — 1 Year",
                yaxis_title="Normalised Return (base 100)",
            )

        else:
            from tools.market_data import get_fundamentals
            fund_results = await asyncio.gather(
                *[get_fundamentals(t) for t in tickers], return_exceptions=True
            )

            key_map = {
                "pe_ratio":       ("pe_ratio",               "Trailing P/E"),
                "revenue_growth": ("revenue_growth_yoy_pct", "Revenue Growth YoY (%)"),
                "profit_margin":  ("profit_margin_pct",      "Net Profit Margin (%)"),
            }
            data_key, y_title = key_map.get(metric, ("pe_ratio", "P/E"))

            xs, ys, colors = [], [], []
            for t, res in zip(tickers, fund_results):
                if isinstance(res, Exception) or "error" in (res or {}):
                    continue
                val = res.get(data_key)
                if val is not None:
                    xs.append(t)
                    ys.append(float(val))
                    colors.append(COLORS[len(xs) - 1])

            if not xs:
                return json.dumps({"error": "No fundamental data returned for any ticker"})

            fig.add_trace(go.Bar(
                x=xs, y=ys, marker_color=colors,
                text=[f"{v:.1f}" for v in ys], textposition="outside",
            ))
            fig.update_layout(title=f"{y_title} — {', '.join(tickers)}", yaxis_title=y_title)

        fig.update_layout(
            height=460,
            legend=dict(orientation="h", y=-0.18),
            **_LAYOUT_BASE,
        )
        fig.update_xaxes(**_GRID)
        fig.update_yaxes(**_GRID)

        html = fig.to_html(include_plotlyjs="cdn", full_html=False, config={"responsive": True})
        desc = f"{metric} comparison: {', '.join(tickers)}"

        await asyncio.gather(
            _save_chart(None, "comparison", f"Comparison {tickers_csv}", html, desc),
            _log_call("plot_comparison", tickers_csv, int((time.monotonic() - t0) * 1000)),
        )
        return _result(html, desc, chart_type="comparison")

    except Exception as exc:
        logger.exception("plot_comparison failed")
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def plot_custom(title: str, chart_type: str, data_json: str) -> str:
    """
    Generate a custom Plotly chart from arbitrary JSON data.

    Args:
        title:      Chart title
        chart_type: 'line', 'bar', 'scatter', 'area', or 'pie'
        data_json:  JSON array of series objects.
                    For line/bar/scatter/area: [{"name": "S1", "x": [...], "y": [...]}, ...]
                    For pie:                   [{"labels": [...], "values": [...]}]

    Returns JSON {html, description, chart_type}.
    """
    import plotly.graph_objects as go

    await _ensure_db()
    t0 = time.monotonic()

    try:
        series = json.loads(data_json)
        if not isinstance(series, list):
            return json.dumps({"error": "data_json must be a JSON array of series objects"})

        COLORS = ["#2196F3", "#4CAF50", "#FF9800", "#E91E63", "#9C27B0", "#00BCD4", "#FF5722"]
        fig = go.Figure()

        for i, s in enumerate(series):
            color = COLORS[i % len(COLORS)]
            name  = s.get("name", f"Series {i+1}")

            if chart_type == "pie":
                fig.add_trace(go.Pie(labels=s.get("labels", []), values=s.get("values", []), name=name))
            elif chart_type == "bar":
                fig.add_trace(go.Bar(x=s["x"], y=s["y"], name=name, marker_color=color,
                                     text=[str(v) for v in s["y"]], textposition="outside"))
            elif chart_type == "scatter":
                fig.add_trace(go.Scatter(x=s["x"], y=s["y"], mode="markers", name=name,
                                         marker=dict(color=color, size=8)))
            elif chart_type == "area":
                r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
                fig.add_trace(go.Scatter(
                    x=s["x"], y=s["y"], mode="lines", name=name,
                    line=dict(color=color, width=2),
                    fill="tozeroy", fillcolor=f"rgba({r},{g},{b},0.15)",
                ))
            else:  # line
                fig.add_trace(go.Scatter(x=s["x"], y=s["y"], mode="lines+markers", name=name,
                                         line=dict(color=color, width=2)))

        fig.update_layout(
            title=dict(text=title, font=dict(size=15)),
            height=430,
            **_LAYOUT_BASE,
        )
        if chart_type != "pie":
            fig.update_xaxes(**_GRID)
            fig.update_yaxes(**_GRID)

        html = fig.to_html(include_plotlyjs="cdn", full_html=False, config={"responsive": True})
        desc = f"Custom {chart_type} chart: {title}"

        await asyncio.gather(
            _save_chart(None, f"custom_{chart_type}", title, html, desc),
            _log_call("plot_custom", None, int((time.monotonic() - t0) * 1000)),
        )
        return _result(html, desc, chart_type=f"custom_{chart_type}")

    except Exception as exc:
        logger.exception("plot_custom failed")
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def recall_charts(ticker: str = "", limit: int = 5) -> str:
    """
    Retrieve previously generated charts from agent memory.

    Args:
        ticker: Filter by symbol (empty = all charts)
        limit:  Number of recent entries to return (default 5, max 20)

    Returns a list of past chart metadata. Full HTML not returned here — call
    the original tool again to regenerate if needed.
    """
    await _ensure_db()
    limit  = max(1, min(limit, 20))
    ticker = ticker.strip().upper()

    async with aiosqlite.connect(AGENT_DB) as db:
        if ticker:
            async with db.execute(
                "SELECT id, timestamp, ticker, chart_type, title, description "
                "FROM charts WHERE ticker=? ORDER BY id DESC LIMIT ?",
                (ticker, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, timestamp, ticker, chart_type, title, description "
                "FROM charts ORDER BY id DESC LIMIT ?",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()

    if not rows:
        label = f" for {ticker}" if ticker else ""
        return f"No charts found{label}. Generate one with plot_price_history, plot_fundamentals, etc."

    lines = [f"Past charts{f' for {ticker}' if ticker else ''} (newest first):"]
    for row_id, ts, t, ct, title, desc in rows:
        t_str = f"  [{t}]" if t else ""
        lines.append(f"\n[{ts[:10]}] #{row_id}  {ct}{t_str}  {title or ''}\n  {desc or ''}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
