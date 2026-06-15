"""
Plotly chart generators for fundamental analysis.

All functions return a self-contained HTML string (div + inline script).
Requires plotly.js to be loaded separately in the host page.
Charts use transparent backgrounds so they adapt to light/dark themes.
"""

from __future__ import annotations

import plotly.graph_objects as go

# ── Common layout defaults ─────────────────────────────────────────────────────

_MONO = "ui-monospace, 'Cascadia Mono', 'JetBrains Mono', Consolas, monospace"

_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family=_MONO, size=11, color="#8a8a8a"),
    margin=dict(l=48, r=16, t=36, b=40),
    showlegend=False,
    hoverlabel=dict(bgcolor="#1c1c1c", font_color="#ffffff", bordercolor="#2e2e2e"),
)

_GRID = dict(gridcolor="#2e2e2e", zeroline=False)
_GREEN = "#00c805"
_RED   = "#ff5000"
_BLUE  = "#387dff"
_DIM   = "#8a8a8a"


def _to_div(fig: go.Figure) -> str:
    return fig.to_html(
        include_plotlyjs=False,
        full_html=False,
        config={"responsive": True, "displayModeBar": False},
    )


# ── Chart functions ────────────────────────────────────────────────────────────

def revenue_chart(quarterly_revenues: list[dict]) -> str:
    """
    Vertical bar chart of quarterly revenue (in $B).
    Bars coloured green/red by QoQ direction; QoQ % shown as bar text.
    """
    if not quarterly_revenues:
        return ""

    periods  = [q["period"][:7] for q in quarterly_revenues]
    revenues = [q["revenue_b"] for q in quarterly_revenues]
    qoq_vals = [q.get("qoq_pct") for q in quarterly_revenues]

    bar_colors = [
        (_GREEN if (v or 0) >= 0 else _RED) for v in qoq_vals
    ]
    bar_text = [f"${r:.1f}B" for r in revenues]
    hover = [
        f"<b>{p}</b><br>${r:.2f}B"
        + (f"<br>QoQ: {'+' if (v or 0) >= 0 else ''}{v:.1f}%" if v is not None else "")
        for p, r, v in zip(periods, revenues, qoq_vals)
    ]

    fig = go.Figure(go.Bar(
        x=periods, y=revenues,
        marker_color=bar_colors,
        text=bar_text, textposition="outside",
        customdata=qoq_vals,
        hovertext=hover, hoverinfo="text",
    ))

    # QoQ % annotation inside each bar
    for period, rev, qoq in zip(periods, revenues, qoq_vals):
        if qoq is not None and rev > 0:
            label = f"{'+' if qoq >= 0 else ''}{qoq:.1f}%"
            fig.add_annotation(
                x=period, y=rev * 0.45,
                text=label, showarrow=False,
                font=dict(size=10, color="#ffffff", family=_MONO),
            )

    fig.update_layout(
        **_LAYOUT,
        title=dict(text="Quarterly Revenue", font=dict(size=12, color=_DIM), x=0, pad=dict(b=8)),
        height=260,
        yaxis=dict(title="$B", **_GRID),
        xaxis=dict(**_GRID),
        bargap=0.3,
    )
    return _to_div(fig)


def margins_chart(gross_margin: float | None,
                  net_margin:   float | None,
                  roe:          float | None) -> str:
    """
    Horizontal bar chart: Gross Margin / Net Margin / ROE.
    Negative values rendered in red.
    """
    items = [
        ("Gross Margin", gross_margin),
        ("Net Margin",   net_margin),
        ("ROE",          roe),
    ]
    items = [(lbl, v) for lbl, v in items if v is not None]
    if not items:
        return ""

    labels = [l for l, _ in items]
    values = [v for _, v in items]
    colors = [_GREEN if v >= 0 else _RED for v in values]

    fig = go.Figure(go.Bar(
        y=labels, x=values, orientation="h",
        marker_color=colors,
        text=[f"{v:.1f}%" for v in values],
        textposition="outside",
        hovertemplate="%{y}: %{x:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        **_LAYOUT,
        title=dict(text="Profitability & Returns (%)", font=dict(size=12, color=_DIM), x=0),
        height=200,
        xaxis=dict(title="%", gridcolor="#2e2e2e", zeroline=True, zerolinecolor="#2e2e2e"),
        yaxis=dict(**_GRID),
    )
    return _to_div(fig)


def valuation_chart(pe:         float | None,
                    forward_pe: float | None,
                    eps_ttm:    float | None,
                    eps_fwd:    float | None) -> str:
    """
    Grouped bar chart: P/E TTM, P/E Fwd, EPS TTM, EPS Fwd.
    PE and EPS shown on separate y-axes so scale differences don't hide detail.
    """
    pe_items  = [("P/E TTM", pe, _BLUE), ("P/E Fwd", forward_pe, "#5a99ff")]
    eps_items = [("EPS TTM", eps_ttm, _GREEN), ("EPS Fwd", eps_fwd, "#33d433")]

    traces = []
    for lbl, val, col in pe_items + eps_items:
        if val is not None:
            yaxis = "y" if "P/E" in lbl else "y2"
            traces.append(go.Bar(
                name=lbl, x=[lbl], y=[val],
                marker_color=col,
                text=[f"{val:.1f}"], textposition="outside",
                yaxis=yaxis,
                hovertemplate=f"{lbl}: %{{y:.1f}}<extra></extra>",
            ))

    if not traces:
        return ""

    fig = go.Figure(traces)
    fig.update_layout(
        **_LAYOUT,
        title=dict(text="Valuation & Earnings", font=dict(size=12, color=_DIM), x=0),
        height=220,
        barmode="group",
        bargap=0.3,
        yaxis=dict(title="P/E", **_GRID),
        yaxis2=dict(
            title="EPS ($)", overlaying="y", side="right",
            gridcolor="rgba(0,0,0,0)", zeroline=False,
        ),
    )
    return _to_div(fig)


def profitability_history_chart(quarterly_profitability: list[dict]) -> str:
    """
    Multi-line chart: Gross Margin, Operating Margin, Net Margin, ROE
    plotted quarterly. Margins on left y-axis; ROE on right y-axis when
    its scale differs significantly from margin values.
    """
    if not quarterly_profitability:
        return ""

    periods = [q["period"][:7] for q in quarterly_profitability]

    metric_cfg = [
        ("gross_margin_pct",     "Gross Margin",     _BLUE,    "y"),
        ("operating_margin_pct", "Operating Margin", "#f5c518","y"),
        ("net_margin_pct",       "Net Margin",       _GREEN,   "y"),
        ("roe_pct",              "ROE (ann.)",        "#ff8c00","y2"),
    ]

    traces = []
    has_y2 = False
    for key, name, color, yaxis in metric_cfg:
        vals = [q.get(key) for q in quarterly_profitability]
        if not any(v is not None for v in vals):
            continue
        traces.append(go.Scatter(
            x=periods, y=vals,
            name=name, mode="lines+markers",
            line=dict(color=color, width=2),
            marker=dict(size=5, color=color),
            connectgaps=False,
            yaxis=yaxis,
            hovertemplate=f"<b>{name}</b>: %{{y:.1f}}%<extra></extra>",
        ))
        if yaxis == "y2":
            has_y2 = True

    if not traces:
        return ""

    layout_extra = {}
    if has_y2:
        layout_extra["yaxis2"] = dict(
            title="ROE (%)", overlaying="y", side="right",
            gridcolor="rgba(0,0,0,0)", zeroline=False,
            tickfont=dict(color="#ff8c00"),
        )

    fig = go.Figure(traces)
    layout = {**_LAYOUT, "showlegend": True}
    fig.update_layout(
        **layout,
        title=dict(
            text="Profitability & Returns — Quarterly",
            font=dict(size=12, color=_DIM), x=0,
        ),
        height=300,
        legend=dict(
            orientation="h", y=1.18, x=0,
            font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        yaxis=dict(title="%", **_GRID),
        xaxis=dict(**_GRID),
        hovermode="x unified",
        **layout_extra,
    )
    return _to_div(fig)


def generate_fundamentals_charts(data: dict) -> dict[str, str]:
    """
    Generate all fundamental charts for a ticker.
    Returns a dict mapping chart name → HTML string.
    Call this from FundamentalsAgent or the web page builder.
    """
    qtrs  = data.get("quarterly_revenues") or []
    qprof = data.get("quarterly_profitability") or []
    charts: dict[str, str] = {}

    rev = revenue_chart(qtrs)
    if rev:
        charts["revenue"] = rev

    prof_line = profitability_history_chart(qprof)
    if prof_line:
        charts["profitability_history"] = prof_line

    marg = margins_chart(
        gross_margin=data.get("gross_margin_pct"),
        net_margin=data.get("profit_margin_pct"),
        roe=data.get("roe_pct"),
    )
    if marg:
        charts["margins"] = marg

    val = valuation_chart(
        pe=data.get("pe_ratio"),
        forward_pe=data.get("forward_pe"),
        eps_ttm=data.get("eps_ttm"),
        eps_fwd=data.get("eps_forward"),
    )
    if val:
        charts["valuation"] = val

    return charts
