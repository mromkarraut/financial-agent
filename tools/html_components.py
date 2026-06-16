"""
HTML component library for the financial agent web UI.

All components use CSS variables defined in server.py's _CSS (hc-* classes).
Functions return HTML strings safe to embed inside .result-wrap.
Used directly by agents (Python import) and exposed via the html_css MCP server.
"""

from __future__ import annotations

import html as _html


# ── Primitives ─────────────────────────────────────────────────────────────────

def badge(text: str, color: str = "dim") -> str:
    """Inline pill badge.  color: green | red | yellow | blue | dim"""
    return f'<span class="hc-badge hc-badge-{color}">{_html.escape(str(text))}</span>'


def alert(message: str, level: str = "info") -> str:
    """Alert strip.  level: info | warning | success | error"""
    return f'<div class="hc-alert hc-alert-{level}">{message}</div>'


# ── Layout shells ──────────────────────────────────────────────────────────────

def section_card(title: str, body_html: str, icon: str = "") -> str:
    """Card with a header bar + body."""
    prefix = f"{icon} " if icon else ""
    return (
        f'<div class="hc-section">'
        f'<div class="hc-section-header">{prefix}{_html.escape(title)}</div>'
        f'<div class="hc-section-body">{body_html}</div>'
        f'</div>'
    )


# ── Metric grid ────────────────────────────────────────────────────────────────

def metric_grid(metrics: list[dict]) -> str:
    """
    Responsive grid of labelled metric values.

    Each entry:  {"label": str, "value": str, "color": "pos|neg|dim|yellow|blue"  (opt)}
    """
    items = []
    for m in metrics:
        color_cls = f' {m["color"]}' if m.get("color") else ""
        items.append(
            f'<div class="hc-metric">'
            f'<div class="hc-metric-label">{_html.escape(m["label"])}</div>'
            f'<div class="hc-metric-value{color_cls}">{_html.escape(str(m["value"]))}</div>'
            f'</div>'
        )
    return f'<div class="hc-metric-grid">{"".join(items)}</div>'


# ── Option legs ────────────────────────────────────────────────────────────────

def legs_list(legs: list[dict]) -> str:
    """
    Horizontal pill-row for each option leg.

    Each entry:  {"action": "BUY"|"SELL", "type": "Call"|"Put",
                  "strike": float, "price": float, "note": str (opt)}
    """
    items = []
    for leg in legs:
        action = str(leg.get("action", "BUY")).upper()
        cls    = "buy" if action == "BUY" else "sell"
        note   = (f'<span class="hc-leg-note">{_html.escape(leg["note"])}</span>'
                  if leg.get("note") else "")
        items.append(
            f'<div class="hc-leg">'
            f'<span class="hc-leg-action {cls}">{action}</span>'
            f'<span class="hc-leg-strike">${float(leg["strike"]):.0f} {_html.escape(str(leg["type"]))}</span>'
            f'<span class="hc-leg-price">@ ${float(leg["price"]):.2f}</span>'
            f'{note}'
            f'</div>'
        )
    return f'<div class="hc-legs">{"".join(items)}</div>'


# ── Data table ─────────────────────────────────────────────────────────────────

def data_table(
    headers: list[str],
    rows: list[list],
    row_classes: list[str] | None = None,
) -> str:
    """
    Styled HTML table.

    row_classes:  list of CSS class strings per row.
                  Standard classes: hc-row-profit  hc-row-loss  hc-row-current
                                    hc-row-be      hc-row-max
    """
    th_html = "".join(f"<th>{_html.escape(str(h))}</th>" for h in headers)
    tr_rows = []
    for i, row in enumerate(rows):
        cls  = f' class="{row_classes[i]}"' if (row_classes and i < len(row_classes) and row_classes[i]) else ""
        tds  = "".join(f"<td>{cell}</td>" for cell in row)
        tr_rows.append(f"<tr{cls}>{tds}</tr>")
    return (
        f'<div class="hc-table-wrap">'
        f'<table class="hc-table">'
        f"<thead><tr>{th_html}</tr></thead>"
        f'<tbody>{"".join(tr_rows)}</tbody>'
        f"</table>"
        f"</div>"
    )


# ── Options-specific components ────────────────────────────────────────────────

def profit_table(s: dict, current_price: float) -> str:
    """
    Full styled profit-at-expiration table for a vertical spread.
    Returns empty string for unsupported strategy kinds.
    """
    kind    = s.get("kind", "")
    is_call = kind == "bull_call"
    if kind not in ("bull_call", "bear_put"):
        return ""

    lo    = min(s["buy_strike"], s["sell_strike"])
    hi    = max(s["buy_strike"], s["sell_strike"])
    width = hi - lo
    mp    = s["max_profit"]
    ml    = s["max_loss"]
    net   = abs(s["net"])
    be    = s["breakeven"]

    def _pnl(px: float) -> int:
        if is_call:
            v = (max(0, px - s["buy_strike"]) - max(0, px - s["sell_strike"]) - net) * 100
        else:
            v = (max(0, s["buy_strike"] - px) - max(0, s["sell_strike"] - px) - net) * 100
        return int(round(v))

    pad    = width * 0.5
    step   = (hi + pad - (lo - pad)) / 11
    levels = [round(lo - pad + i * step, 2) for i in range(12)]
    for extra in (current_price, be, lo, hi):
        if min(abs(x - extra) for x in levels) > step * 0.25:
            levels.append(round(extra, 2))
    levels = sorted(set(round(x, 2) for x in levels))

    headers = ["Price", "P&L / contract", "% of Max", ""]
    rows, row_classes = [], []

    for px in levels:
        pnl      = _pnl(px)
        pct_max  = max(-99, min(100, round(pnl / mp * 100) if mp else 0))
        is_curr  = abs(px - current_price) < step * 0.15
        is_be    = abs(px - be)            < step * 0.15

        if pnl >= mp:
            label, pct_str, cls = "max profit ✅", "100%", "hc-row-max hc-row-profit"
        elif pnl <= -ml:
            label, pct_str, cls = "max loss ❌",   "—",    "hc-row-loss"
        elif abs(pnl) <= 2:
            label, pct_str, cls = "break-even",    "0%",   "hc-row-be"
        elif pnl > 0:
            label, pct_str, cls = "profit",        f"{pct_max}%", "hc-row-profit"
        else:
            label, pct_str, cls = "loss",          "—",    "hc-row-loss"

        if is_curr:
            cls  += " hc-row-current"
            label = f"◀ now  {label}"
        elif is_be:
            cls  += " hc-row-be"

        sign    = "+" if pnl >= 0 else ""
        pnl_str = f"{sign}${abs(pnl)}"

        rows.append([f"${px:.2f}", pnl_str, pct_str, label])
        row_classes.append(cls.strip())

    table_html = data_table(headers, rows, row_classes)
    return section_card("Profit at Expiration — per contract", table_html, "📊")


def strategy_metrics(s: dict) -> str:
    """
    Metric grid for the recommended strategy's key numbers.
    """
    metrics = [
        {"label": "Max Profit",  "value": f"+${s['max_profit']}",  "color": "pos"},
        {"label": "Max Loss",    "value": f"−${s['max_loss']}",    "color": "neg"},
        {"label": "Break-even",  "value": f"${s['breakeven']}"},
        {"label": "POP",         "value": f"{s['pop']*100:.0f}%",
         "color": "pos" if s["pop"] >= 0.5 else "neg"},
        {"label": "P50",         "value": f"{s['p50']*100:.0f}%"},
        {"label": "ROC",         "value": f"{s['roc']:.0f}%"},
        {"label": "Delta",       "value": f"{s['pos_delta']:+.3f}"},
        {"label": "θ / day",     "value": f"{s['pos_theta']:+.2f}"},
        {"label": "DTE",         "value": str(s["dte"])},
    ]
    return section_card("Strategy Metrics", metric_grid(metrics), "📐")


def strategy_legs_card(s: dict) -> str:
    """BUY/SELL leg display for a vertical spread."""
    kind    = s.get("kind", "")
    is_call = "call" in kind

    if kind not in ("bull_call", "bear_put"):
        return ""

    opt_type  = "Call" if is_call else "Put"
    prot_lbl  = "cap" if kind == "bull_call" else "protection"
    legs = [
        {"action": "BUY",  "type": opt_type, "strike": s["buy_strike"],
         "price": s["buy_price"],  "note": "long leg"},
        {"action": "SELL", "type": opt_type, "strike": s["sell_strike"],
         "price": s["sell_price"], "note": prot_lbl},
    ]
    net    = abs(s["net"])
    debit  = f'<span class="neg">−${net:.2f}/share  (−${s["max_loss"]}/contract)</span>'
    footer = f'<div class="hc-legs-footer">Net debit: {debit}</div>'
    return section_card("Trade Legs", legs_list(legs) + footer, "⚖️")


def key_numbers_table(s: dict) -> str:
    """
    Styled HTML table equivalent of the 'Key Numbers' <pre> block in _fmt_detail_card.
    Handles both vertical spreads and single-leg options.
    """
    kind    = s.get("kind", "")
    is_call = "call" in kind
    opt     = "Call" if is_call else "Put"
    net     = abs(s.get("net", 0))
    per_s   = f"${net:.2f}"
    per_c   = f"${abs(int(net * 100))}"
    theta   = s.get("pos_theta", 0)
    theta_s = f"{'+' if theta >= 0 else ''}${theta:.2f}"
    pop_pct = f"{s['pop']*100:.0f}%"
    p50_pct = f"{s['p50']*100:.0f}%"

    def _color_pnl(v: str) -> str:
        if v.startswith("+"):
            return f'<span class="pos">{v}</span>'
        if v.startswith("-"):
            return f'<span class="neg">{v}</span>'
        return v

    # Row definitions: (label, value_html, row_class)
    rows: list[tuple[str, str, str]] = []

    if kind in ("bull_call", "bear_put"):
        rows += [
            (f"Buy {opt}",   f"${s['buy_strike']:.0f} &nbsp;<span class='hc-leg-price'>@ ${s['buy_price']:.2f}/sh</span>",  ""),
            (f"Sell {opt}",  f"${s['sell_strike']:.0f} &nbsp;<span class='hc-leg-price'>@ ${s['sell_price']:.2f}/sh</span>", ""),
            ("Net debit",    _color_pnl(f"-{per_s}") + f" &nbsp;<span class='hc-leg-price'>({per_c}/contract)</span>",       "hc-row-loss"),
            ("Break-even",   f"${s['breakeven']}",   "hc-row-be"),
            ("POP",          f'<span class="{"pos" if s["pop"]>=0.5 else "neg"}">{pop_pct}</span>', ""),
            ("P50",          p50_pct,                 ""),
            ("Max profit",   _color_pnl(f"+${s['max_profit']}"),  "hc-row-profit"),
            ("Max loss",     _color_pnl(f"-${s['max_loss']}"),    "hc-row-loss"),
            ("ROC",          f"{s['roc']:.1f}%",      ""),
            ("Theta",        _color_pnl(f"{theta_s}/day"), ""),
            ("Delta",        f"{s['pos_delta']:+.3f}", ""),
            ("Spread",       f"${s['spread']:.0f}",   ""),
        ]
    elif kind in ("long_call", "long_put"):
        mp_label = f"+${s['max_profit']} est." if is_call else f"+${s['max_profit']}"
        rows += [
            (f"Buy {opt}",  f"${s['buy_strike']:.0f} &nbsp;<span class='hc-leg-price'>@ ${s['buy_price']:.2f}/sh</span>", ""),
            ("Net debit",   _color_pnl(f"-{per_s}") + f" &nbsp;<span class='hc-leg-price'>({per_c}/contract)</span>",     "hc-row-loss"),
            ("Break-even",  f"${s['breakeven']}",   "hc-row-be"),
            ("POP",         f'<span class="{"pos" if s["pop"]>=0.5 else "neg"}">{pop_pct}</span>', ""),
            ("P50",         p50_pct,                ""),
            ("Max profit",  _color_pnl(mp_label),  "hc-row-profit"),
            ("Max loss",    _color_pnl(f"-${s['max_loss']}"), "hc-row-loss"),
            ("ROC",         f"{s['roc']:.1f}%",    ""),
            ("Theta",       _color_pnl(f"{theta_s}/day"), ""),
            ("Delta",       f"{s['pos_delta']:+.3f}", ""),
        ]
    else:
        return ""

    # Build table rows — td[0] is label (left), td[1] is value (right via CSS)
    tr_rows  = []
    row_cls  = []
    for label, val, cls in rows:
        tr_rows.append([label, val])
        row_cls.append(cls)

    table_html = data_table(["Metric", "Value"], tr_rows, row_cls)
    return section_card("Key Numbers", table_html, "🔑")
