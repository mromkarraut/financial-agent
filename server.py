"""
FastAPI web UI — financial options research dashboard.

Changes from v1 (per UIResearcherAgent recommendations, 2026-06-13):
  - HTMX replaces ~80 lines of imperative fetch/DOM JS
  - Routes return HTML fragments (not JSON) → direct hx-swap targets
  - OOB swap refreshes sidebar + results in one round trip
  - Pre block CSS: line-height 1.45, letter-spacing 0, ligatures off, ui-monospace stack
  - Results fade-in animation on DOM insertion
  - /ui-research page served by UIResearcherAgent

Run:  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
Open: http://localhost:8000
"""

import asyncio
import logging

import aiosqlite
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
from agents.options_research_agent import OptionsResearchAgent
from agents.ui_researcher_agent import UIResearcherAgent
from agents.ui_testing_agent import UITestingAgent
from db.database import init_db

logger = logging.getLogger(__name__)
app       = FastAPI(title="Financial Research Agent")
_ui_agent = UIResearcherAgent()


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    await _ui_agent.ensure_seeded()
    # Python 3.14 compatibility: set the running loop in the policy so that
    # asyncio.get_event_loop() works from non-async contexts (ib_insync threads).
    import asyncio as _aio
    _loop = _aio.get_running_loop()
    _aio.set_event_loop(_loop)
    from eventkit.util import register_event_loop
    register_event_loop(_loop)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_history(limit: int = 40) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT id, ticker, outlook, price, recommended, timestamp, ivr "
            "FROM options_research_memory ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "ticker": r[1], "outlook": r[2], "price": r[3],
             "recommended": r[4], "timestamp": r[5], "ivr": r[6]} for r in rows]


async def _get_result_html(result_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT output_html FROM options_research_memory WHERE id=?", (result_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] else None


# ── Sidebar fragment ──────────────────────────────────────────────────────────

def _sidebar_items(history: list[dict], active_id: int | None = None) -> str:
    if not history:
        return '<p class="no-history">No searches yet</p>'
    rows = []
    for item in history:
        cls  = "bull" if item["outlook"] == "bullish" else "bear" if item["outlook"] == "bearish" else "neu"
        icon = "📈" if item["outlook"] == "bullish" else "📉" if item["outlook"] == "bearish" else "↔️"
        try:
            from datetime import datetime
            ts   = datetime.fromisoformat(item["timestamp"])
            date = ts.strftime("%b %d")
        except Exception:
            date = str(item["timestamp"])[:10]
        price = f"${item['price']:.2f}" if item.get("price") else "—"
        rec   = (item.get("recommended") or "").replace("2026-", "")[:26]
        active = " active" if item["id"] == active_id else ""
        rid = item["id"]
        rows.append(
            f'<div class="h-item{active}" data-id="{rid}"'
            f' hx-get="/api/result/{rid}"'
            f' hx-target="#results" hx-swap="innerHTML"'
            f' hx-on--after-request="setActive({rid})">'
            f'<div class="h-ticker">{item["ticker"]}'
            f' <span class="{cls}">{icon}</span></div>'
            f'<div class="h-meta">{price} · {date}<br>'
            f'<span class="h-rec">{rec}</span></div>'
            f'</div>'
        )
    return "\n".join(rows)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    history = await _get_history()
    return HTMLResponse(_page(history))


@app.post("/search", response_class=HTMLResponse)
async def search(
    ticker:     str = Form(...),
    outlook:    str = Form("neutral"),
    term:       str = Form("short"),
    dte_target: str = Form("30"),
) -> HTMLResponse:
    ticker = ticker.strip().upper()
    if not ticker:
        return HTMLResponse('<p class="error">Ticker is required.</p>')

    agent = OptionsResearchAgent()
    result, fund_card = await asyncio.gather(
        agent.run({"ticker": ticker, "outlook": outlook, "term": term,
                   "dte_target": dte_target, "chat_id": "web"}),
        _fundamentals_card(ticker),
    )
    history = await _get_history()
    new_id  = history[0]["id"] if history else None

    body = fund_card + result["output"]
    results_html = f'<div class="result-wrap">{body}</div>'
    sidebar_html = (f'<div id="history-list" hx-swap-oob="true">'
                    f'{_sidebar_items(history, active_id=new_id)}</div>')
    return HTMLResponse(results_html + sidebar_html)


@app.get("/api/result/{result_id}", response_class=HTMLResponse)
async def api_result(result_id: int) -> HTMLResponse:
    html = await _get_result_html(result_id)
    if html:
        return HTMLResponse(f'<div class="result-wrap">{html}</div>')
    return HTMLResponse(
        '<p class="error">Stored result not found. Run a new search.</p>',
        status_code=404,
    )


@app.get("/api/history", response_class=JSONResponse)
async def api_history() -> JSONResponse:
    return JSONResponse(await _get_history())


@app.get("/ui-research", response_class=HTMLResponse)
async def ui_research_page() -> HTMLResponse:
    report  = await _ui_agent.html_report()
    history = await _get_history()
    return HTMLResponse(_page(history, active_tab="research", body_override=report))


@app.post("/api/ui-research/implement/{finding_id}", response_class=HTMLResponse)
async def mark_implemented(finding_id: int) -> HTMLResponse:
    await _ui_agent.mark_implemented(finding_id)
    report = await _ui_agent.html_report()
    return HTMLResponse(report)


@app.get("/ibkr", response_class=HTMLResponse)
async def ibkr_page() -> HTMLResponse:
    from mcp_servers.ibkr_session.server import get_connection_status
    from mcp_servers.ibkr_orders.server import get_order_history
    history = await _get_history()
    status  = await get_connection_status()
    orders  = await get_order_history(limit=10)
    body    = (f'<div class="result-wrap" style="max-width:760px">'
               f'<pre>{status}</pre><br>{orders}</div>')
    return HTMLResponse(_page(history, active_tab="ibkr", body_override=body))


@app.post("/api/place-order", response_class=HTMLResponse)
async def api_place_order(
    ticker:       str   = Form(...),
    short_strike: float = Form(...),
    long_strike:  float = Form(...),
    right:        str   = Form("P"),
    expiry:       str   = Form(...),
    net_price:    float = Form(...),
    quantity:     int   = Form(1),
) -> HTMLResponse:
    from mcp_servers.ibkr_orders.server import place_spread
    result = await place_spread(
        ticker=ticker.upper(), short_strike=short_strike,
        long_strike=long_strike, right=right, expiry=expiry,
        net_price=net_price, quantity=quantity,
    )
    ok = "Not connected" not in result and "failed" not in result.lower() and "Error" not in result
    cls = "order-ok" if ok else "order-err"
    return HTMLResponse(f'<div class="order-confirmation {cls}"><pre>{result}</pre></div>')


@app.post("/api/cancel-order", response_class=HTMLResponse)
async def api_cancel_order(order_id: str = Form(...)) -> Response:
    from mcp_servers.ibkr_orders.server import cancel_open_order
    from db.database import delete_order_by_ibkr_id
    try:
        result = await cancel_open_order(int(order_id))
        if "not found" in result.lower():
            await delete_order_by_ibkr_id(order_id)
            return Response(
                content="",
                media_type="text/html",
                headers={
                    "HX-Reswap": "outerHTML",
                    "HX-Retarget": f"#order-row-{order_id}",
                },
            )
        ok  = "failed" not in result.lower()
        cls = "cancel-ok" if ok else "cancel-err"
    except Exception as exc:
        result = f"Error: {exc}"
        cls = "cancel-err"
    return HTMLResponse(f'<span class="cancel-result {cls}">{result}</span>')


@app.get("/positions", response_class=HTMLResponse)
async def positions_page() -> HTMLResponse:
    history = await _get_history()
    body = '<div id="positions-content" hx-get="/api/positions-fragment" hx-trigger="load, every 60s" hx-swap="innerHTML"><div class="placeholder"><div class="icon">⏳</div><p>Loading positions…</p></div></div>'
    return HTMLResponse(_page(history, active_tab="positions", body_override=body, show_search=False))


@app.get("/api/positions-fragment", response_class=HTMLResponse)
async def positions_fragment() -> HTMLResponse:
    return HTMLResponse(await _build_positions_html())


@app.get("/ibkr/positions", response_class=HTMLResponse)
async def ibkr_positions() -> HTMLResponse:
    from mcp_servers.ibkr_positions.server import get_portfolio_summary
    html = await get_portfolio_summary()
    return HTMLResponse(f'<div class="result-wrap"><pre>{html}</pre></div>')


@app.get("/test", response_class=HTMLResponse)
async def run_tests() -> HTMLResponse:
    agent  = UITestingAgent(base_url="http://localhost:8000")
    report = await agent.run_all()
    history = await _get_history()
    return HTMLResponse(_page(history, active_tab="test", body_override=
        f'<div class="result-wrap" style="max-width:700px">{report}</div>'
    ))


# ── HTML page ─────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* ── Design tokens ── */
:root {
  /* Dark theme (Robinhood-inspired) */
  --bg:       #000000;
  --bg2:      #111111;
  --bg3:      #1c1c1c;
  --border:   #2e2e2e;
  --text:     #ffffff;
  --dim:      #8a8a8a;
  --green:    #00c805;
  --red:      #ff5000;
  --yellow:   #f5c518;
  --blue:     #387dff;
  --blue-lt:  #5a99ff;
  --code-fg:  #7ec8e3;
  --mono:     ui-monospace, 'Cascadia Mono', 'Cascadia Code',
              'JetBrains Mono', 'Fira Code', Consolas,
              'Liberation Mono', monospace;
  --radius:   10px;
  --ease:     0.16s ease;
}

/* ── Light theme ── */
body.light {
  --bg:       #ffffff;
  --bg2:      #f7f7f7;
  --bg3:      #efefef;
  --border:   #e2e2e2;
  --text:     #0a0a0a;
  --dim:      #6b6b6b;
  --green:    #008a03;
  --red:      #e03000;
  --yellow:   #b87800;
  --blue:     #0050d0;
  --blue-lt:  #0066ee;
  --code-fg:  #0055aa;
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  font-size: 15px;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ── */
header {
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  z-index: 10;
}

.logo {
  display: flex; align-items: center; gap: 9px;
  text-decoration: none; color: var(--text); flex-shrink: 0;
}
.logo-mark {
  width: 30px; height: 30px;
  background: var(--green);
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 15px; font-weight: 800; color: #000; line-height: 1;
  flex-shrink: 0;
}
.logo-text { font-size: 16px; font-weight: 700; letter-spacing: -0.3px; }

.nav-links { margin-left: auto; display: flex; gap: 2px; }
.nav-link {
  color: var(--dim); font-size: 13px; font-weight: 500;
  text-decoration: none; padding: 5px 12px;
  border-radius: 8px; transition: background var(--ease), color var(--ease);
}
.nav-link:hover { background: var(--bg3); color: var(--text); }
.nav-link.active { color: var(--text); font-weight: 600; }

/* ── Theme toggle — pill ── */
.theme-toggle {
  display: flex; background: var(--bg3);
  border-radius: 20px; padding: 3px; gap: 1px; margin-left: 10px; flex-shrink: 0;
}
.theme-btn {
  background: none; border: none; border-radius: 18px;
  padding: 4px 11px; font-size: 13px; cursor: pointer;
  color: var(--dim); transition: all var(--ease); line-height: 1;
}
.theme-btn.active {
  background: var(--bg); color: var(--text);
  box-shadow: 0 1px 4px rgba(0,0,0,0.2);
}

/* Hamburger — mobile only */
.hamburger {
  display: none; background: none; border: none;
  color: var(--text); font-size: 22px; cursor: pointer;
  padding: 4px; flex-shrink: 0; line-height: 1;
}

/* ── App layout ── */
.app-layout { display: flex; flex: 1; overflow: hidden; min-height: 0; }

/* ── Sidebar ── */
aside {
  width: 230px; min-width: 230px;
  background: var(--bg); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
}
.sidebar-hdr {
  padding: 14px 16px 10px;
  font-size: 10px; font-weight: 700;
  color: var(--dim); text-transform: uppercase; letter-spacing: 0.08em;
}
#history-list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
#history-list::-webkit-scrollbar { width: 3px; }
#history-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.h-item {
  padding: 10px 10px; border-radius: 8px; cursor: pointer;
  transition: background var(--ease); margin-bottom: 2px;
}
.h-item:hover  { background: var(--bg3); }
.h-item.active { background: var(--bg3); }
.h-ticker {
  font-weight: 700; font-size: 14px;
  display: flex; align-items: center; justify-content: space-between;
}
.h-meta { font-size: 11px; color: var(--dim); margin-top: 3px; line-height: 1.4; }
.h-rec  { font-size: 10px; opacity: 0.7; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.no-history { padding: 20px 16px; color: var(--dim); font-size: 13px; }
.bull { color: var(--green); }
.bear { color: var(--red); }
.neu  { color: var(--yellow); }

/* ── Content area ── */
.content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

/* ── Search bar ── */
.search-bar {
  padding: 12px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.search-form {
  display: flex; align-items: center; gap: 10px; max-width: 640px;
}
.ticker-wrap { position: relative; flex: 0 0 140px; }
.ticker-wrap::before {
  content: '$'; position: absolute; left: 11px; top: 50%;
  transform: translateY(-50%); color: var(--dim);
  font-weight: 700; font-size: 15px; pointer-events: none;
}
.search-bar input[type="text"] {
  width: 100%; background: var(--bg2);
  border: 1.5px solid var(--border); border-radius: 8px;
  color: var(--text); font-size: 15px; font-weight: 700;
  padding: 8px 10px 8px 26px; outline: none;
  letter-spacing: 0.04em; text-transform: uppercase;
  transition: border-color var(--ease);
}
.search-bar input[type="text"]::placeholder {
  text-transform: none; font-weight: 400; color: var(--dim); letter-spacing: 0;
}
.search-bar input[type="text"]:focus { border-color: var(--green); }
.search-bar select {
  background: var(--bg2); border: 1.5px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 14px;
  padding: 8px 10px; outline: none; cursor: pointer;
  transition: border-color var(--ease);
}
.search-bar select:focus { border-color: var(--green); }

.btn {
  background: var(--green); border: none; border-radius: 8px;
  color: #000; font-size: 14px; font-weight: 700;
  padding: 8px 22px; cursor: pointer;
  transition: opacity var(--ease); white-space: nowrap;
}
.btn:hover  { opacity: 0.85; }
.btn:active { opacity: 0.7; }

/* HTMX spinner */
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator,
.htmx-request.htmx-indicator { display: flex !important; }
#spinner { align-items: center; gap: 7px; color: var(--dim); font-size: 12px; }
#spinner::before {
  content: ''; width: 14px; height: 14px; flex-shrink: 0;
  border: 2px solid var(--border); border-top-color: var(--green);
  border-radius: 50%; animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Results ── */
#results { flex: 1; overflow-y: auto; padding: 24px; }
#results::-webkit-scrollbar { width: 4px; }
#results::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

#results > * { animation: fadein 0.18s ease; }
@keyframes fadein {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
}

.placeholder {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; min-height: 55vh;
  color: var(--dim); text-align: center; gap: 10px;
}
.placeholder .icon { font-size: 42px; }
.placeholder h2    { font-size: 20px; font-weight: 700; color: var(--text); }
.placeholder p     { font-size: 14px; max-width: 280px; line-height: 1.6; }
.placeholder kbd {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 5px; padding: 1px 7px;
  font-size: 12px; font-family: var(--mono);
}
.error { color: var(--red); padding: 24px; text-align: center; }

/* ── Agent output ── */
.result-wrap { max-width: 820px; margin: 0 auto; line-height: 1.65; }
.result-wrap b { color: var(--text); font-weight: 600; }
.result-wrap i { color: var(--dim); font-style: italic; }
.result-wrap code {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 6px; padding: 3px 8px;
  font-family: var(--mono); font-size: 13px;
  color: var(--code-fg); white-space: pre-wrap;
}
.result-wrap pre {
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.45;
  letter-spacing: 0;
  font-variant-ligatures: none;
  font-feature-settings: "liga" 0, "calt" 0;
  -webkit-font-smoothing: auto;
  white-space: pre;
  overflow-x: auto;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 18px;
  margin: 6px 0 12px;
  color: var(--text);
  tab-size: 4;
}
.result-wrap pre::-webkit-scrollbar { height: 3px; }
.result-wrap pre::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

/* ── Color classes (added by enhanceOutput) ── */
.pos { color: var(--green) !important; font-weight: 600; }
.neg { color: var(--red)   !important; font-weight: 600; }
.pct { color: var(--yellow) !important; }

.star-row {
  display: block; width: 100%;
  background: rgba(245,197,24,0.1);
  border-left: 3px solid var(--yellow);
  padding-left: 4px; margin-left: -4px;
}
.atm-row {
  display: block; width: 100%;
  background: rgba(56,125,255,0.09);
  border-left: 3px solid var(--blue);
  padding-left: 4px; margin-left: -4px;
}
body.light .star-row { background: rgba(184,120,0,0.1); }
body.light .atm-row  { background: rgba(0,80,208,0.07); }

/* ── Term selector groups (Short / Long) ── */
.term-group {
  display: flex; align-items: center; gap: 6px;
  background: var(--bg2); border: 1.5px solid var(--green);
  border-radius: 8px; padding: 6px 10px;
  transition: border-color var(--ease), opacity var(--ease);
  white-space: nowrap; cursor: pointer;
}
.term-group.term-off { border-color: var(--border); opacity: 0.55; }
.term-group input[type="radio"] {
  accent-color: var(--green); width: 14px; height: 14px;
  cursor: pointer; flex-shrink: 0; margin: 0;
}
.term-group label { font-size: 13px; font-weight: 600; cursor: pointer; }
.term-group.term-off label { color: var(--dim); }
.term-sel {
  background: transparent; border: none; color: var(--text);
  font-size: 13px; font-weight: 500; padding: 0 2px;
  cursor: pointer; outline: none;
}
.term-group.term-off .term-sel { color: var(--dim); }

/* ── Fundamentals card ── */
.fund-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 18px;
  margin-bottom: 18px;
}
.fund-name {
  font-size: 15px; font-weight: 700; margin-bottom: 10px;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
}
.fund-sub  { font-size: 12px; color: var(--dim); font-weight: 400; }
.fund-metrics {
  display: flex; flex-wrap: wrap; gap: 6px 16px;
}
.fund-metric {
  display: flex; flex-direction: column; gap: 1px;
  font-size: 13px; font-weight: 700; font-family: var(--mono);
  min-width: 60px;
}
.fund-label {
  font-size: 10px; font-weight: 600; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.06em;
  font-family: inherit;
}
.fund-footer {
  margin-top: 10px; font-size: 11px; color: var(--dim);
  border-top: 1px solid var(--border); padding-top: 8px;
}
.fund-source {
  color: var(--dim); text-decoration: none;
  border-bottom: 1px dotted var(--dim);
  transition: color var(--ease);
}
.fund-source:hover { color: var(--text); }

/* ── Order panel ── */
.order-panel {
  margin: 16px 0 8px;
  padding: 16px 20px;
  background: var(--bg2);
  border: 1.5px solid var(--border);
  border-radius: var(--radius);
}
.order-panel-label {
  font-size: 12px; font-weight: 600; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 12px;
}
.order-row {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.qty-label {
  display: flex; align-items: center; gap: 7px;
  font-size: 13px; font-weight: 600; color: var(--dim);
}
.qty-input {
  width: 60px; background: var(--bg3);
  border: 1.5px solid var(--border); border-radius: 7px;
  color: var(--text); font-size: 14px; font-weight: 700;
  padding: 6px 8px; outline: none; text-align: center;
  transition: border-color var(--ease);
}
.qty-input:focus { border-color: var(--blue); }
.order-btn {
  background: var(--blue) !important; color: #fff !important;
  font-size: 14px; font-weight: 700; padding: 8px 20px;
}
.order-btn:hover { opacity: 0.85; }
.order-net {
  font-size: 12px; color: var(--dim); font-family: var(--mono);
}
.order-spinner {
  margin-top: 10px; font-size: 13px; color: var(--dim);
}
.order-confirmation {
  margin-top: 12px; padding: 14px 16px;
  border-radius: 8px; font-size: 13px;
}
.order-ok  { background: rgba(0,200,5,0.08);  border: 1px solid rgba(0,200,5,0.3); }
.order-err { background: rgba(255,80,0,0.08); border: 1px solid rgba(255,80,0,0.3); }
.order-confirmation pre {
  background: none; border: none; padding: 0; margin: 0; font-size: 12px;
}

/* ── Positions page ── */
.pos-page        { padding: 24px; max-width: 1100px; margin: 0 auto; }
.pos-live-wrap   { margin-bottom: 28px; }
.pos-strat-wrap  { }
.pos-section-hdr {
  font-size: 11px; font-weight: 700; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.08em;
  margin-bottom: 12px; display: flex; align-items: center; gap: 10px;
}
.pos-refresh { font-weight: 400; font-size: 10px; color: var(--dim); opacity: 0.6; }
.btn-pos-refresh {
  background: none; border: 1px solid var(--border); border-radius: 5px;
  color: var(--dim); font-size: 10px; font-weight: 600; padding: 2px 7px;
  cursor: pointer; transition: all var(--ease); margin-left: auto;
}
.btn-pos-refresh:hover { border-color: var(--blue); color: var(--blue); }
.htmx-request .btn-pos-refresh { opacity: 0.4; pointer-events: none; }
.pos-refresh-spin { font-size: 10px; color: var(--dim); }
.pos-pre     { font-size: 12px; margin-bottom: 10px; }
.pos-err     { color: var(--red); font-size: 13px; padding: 12px 0; }
.pos-empty   { color: var(--dim); font-size: 14px; padding: 20px 0; }

.pos-table-wrap { overflow-x: auto; }
.pos-table {
  width: 100%; border-collapse: collapse;
  font-size: 13px; white-space: nowrap;
}
.pos-table thead th {
  text-align: left; padding: 8px 14px;
  font-size: 11px; font-weight: 700; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky; top: 0;
}
.pos-table tbody tr {
  border-bottom: 1px solid var(--border);
  transition: background var(--ease);
}
.pos-table tbody tr:hover { background: var(--bg3); }
.pos-table td { padding: 10px 14px; vertical-align: top; }
.pos-ticker { font-weight: 700; font-size: 14px; }
.pos-mono   { font-family: var(--mono); font-size: 12px; }
.pos-profit { color: var(--green) !important; font-weight: 700; font-family: var(--mono); }
.pos-loss   { color: var(--red)   !important; font-weight: 700; font-family: var(--mono); }
.pos-date   { color: var(--dim); font-size: 11px; }
.pos-be     { color: var(--yellow); }
.pos-action { white-space: nowrap; }
.btn-cancel {
  background: none; border: 1px solid var(--red);
  border-radius: 6px; color: var(--red); font-size: 11px;
  font-weight: 600; padding: 3px 8px; cursor: pointer;
  transition: all var(--ease); white-space: nowrap;
}
.btn-cancel:hover { background: var(--red); color: #fff; }
/* grayed-out state while cancel request is in flight */
.htmx-request .btn-cancel {
  opacity: 0.45; pointer-events: none; cursor: default;
  border-color: var(--dim); color: var(--dim);
}
.cancel-busy { display: none; }
.htmx-request .cancel-busy { display: inline; }
.htmx-request .cancel-idle { display: none; }
.cancel-result   { font-size: 11px; font-weight: 600; }
.cancel-ok  { color: var(--green); }
.cancel-err { color: var(--red); }
.pos-toggle {
  margin-left: auto; display: flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 500; color: var(--dim);
  cursor: pointer; text-transform: none; letter-spacing: 0;
}
.pos-toggle input { accent-color: var(--blue); cursor: pointer; }

/* DTE badges */
.dte-badge {
  display: inline-block; padding: 2px 8px; border-radius: 20px;
  font-size: 11px; font-weight: 700; white-space: nowrap;
}
.dte-ok       { background: rgba(0,200,5,0.12);   color: var(--green); }
.dte-warn     { background: rgba(245,197,24,0.15); color: var(--yellow); }
.dte-urgent   { background: rgba(255,80,0,0.15);  color: var(--red); }
.dte-expired  { background: var(--bg3); color: var(--dim); }
.dte-dim      { color: var(--dim); }

/* Status pills */
.pos-status    { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 20px; }
.pos-filled    { background: rgba(0,200,5,0.12);   color: var(--green); }
.pos-pending   { background: rgba(56,125,255,0.12); color: var(--blue); }
.pos-cancelled { background: var(--bg3); color: var(--dim); }

/* ── Sidebar backdrop ── */
.sidebar-backdrop {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.5); z-index: 50;
}

/* ── Bottom nav (mobile only) ── */
.bottom-nav {
  display: none;
  position: fixed; bottom: 0; left: 0; right: 0;
  height: 58px; z-index: 40;
  background: var(--bg); border-top: 1px solid var(--border);
}
.bottom-nav a {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 3px; text-decoration: none;
  color: var(--dim); font-size: 10px; font-weight: 600;
  transition: color var(--ease); letter-spacing: 0.02em;
}
.bottom-nav a .bn-icon { font-size: 22px; line-height: 1; }
.bottom-nav a.active   { color: var(--green); }

/* ── Mobile ── */
@media (max-width: 660px) {
  .hamburger { display: flex; }
  .nav-links  { display: none; }
  .logo-text  { display: none; }

  aside {
    position: fixed; left: -250px; top: 0; height: 100%; width: 250px;
    z-index: 100; transition: left 0.25s cubic-bezier(.4,0,.2,1);
    box-shadow: 4px 0 32px rgba(0,0,0,0.4);
  }
  aside.open { left: 0; }
  .sidebar-backdrop.open { display: block; }

  header { padding: 0 12px; gap: 8px; }
  .theme-toggle { margin-left: auto; }

  .search-bar { padding: 10px 14px; }
  .search-form { flex-wrap: wrap; gap: 8px; width: 100%; }
  .ticker-wrap { flex: 1 1 100px; min-width: 0; }
  .search-bar select { flex: 1 1 120px; min-width: 0; }
  .term-group { flex: 1 1 auto; }
  .btn { flex: 0 0 auto; }

  #results { padding: 14px; padding-bottom: 72px; }
  .result-wrap { max-width: 100%; }
  .result-wrap pre  { font-size: 11.5px; line-height: 1.42; padding: 10px 12px; }
  .result-wrap code { display: block; overflow-x: auto; white-space: pre; font-size: 12px; }
  .placeholder { min-height: 48vh; }
  .placeholder .icon { font-size: 34px; }

  .bottom-nav { display: flex; }
}

/* ── UI Research page ── */
.research-report h2 { font-size: 20px; font-weight: 700; margin-bottom: 8px; }
.research-report h3 {
  font-size: 10px; font-weight: 700; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.08em; margin: 24px 0 12px;
}
.research-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px 18px; margin-bottom: 10px;
}
.research-card.done    { border-left: 3px solid var(--green); }
.research-card.pending { border-left: 3px solid var(--yellow); }
.rc-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.rc-topic  { font-weight: 700; font-size: 14px; flex: 1; }
.rc-status { font-size: 12px; color: var(--dim); }
.rc-score  { font-size: 12px; color: var(--dim); margin-bottom: 8px; }
.rc-summary, .rc-rec { font-size: 13px; line-height: 1.6; margin-bottom: 6px; }
.rc-rec { color: var(--code-fg); }
.rc-footer {
  font-size: 11px; color: var(--dim); margin-top: 10px;
  display: flex; align-items: center; gap: 10px;
}
.btn-impl {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); font-size: 11px;
  padding: 2px 8px; cursor: pointer;
}
.btn-impl:hover { background: var(--green); border-color: var(--green); color: #000; }
"""


async def _fundamentals_card(ticker: str) -> str:
    """Compact fundamentals summary card — fetched in parallel with options analysis."""
    try:
        from tools.market_data import get_fundamentals
        data = await asyncio.wait_for(get_fundamentals(ticker), timeout=12)
        if "error" in data:
            return ""

        def _f(v, fmt=".1f", suffix=""):
            try:
                return f"{float(v):{fmt}}{suffix}" if v is not None else "—"
            except (TypeError, ValueError):
                return "—"

        mcap   = data.get("market_cap")
        mcap_s = f"${mcap/1e9:.1f}B" if mcap and mcap >= 1e9 else (f"${mcap/1e6:.0f}M" if mcap else "—")
        pe     = _f(data.get("pe_ratio"),              ".1f")
        fpe    = _f(data.get("forward_pe"),             ".1f")
        rev    = _f(data.get("revenue_growth_yoy_pct"), ".1f", "%")
        margin = _f(data.get("profit_margin_pct"),      ".1f", "%")
        de     = _f(data.get("debt_to_equity"),         ".1f")
        roe    = _f(data.get("roe_pct"),                ".1f", "%")
        div    = data.get("dividend_yield_pct") or 0
        div_s  = _f(div, ".2f", "%") if div and float(div) > 0 else None
        sector = data.get("sector") or ""
        ind    = data.get("industry") or ""
        name   = data.get("company_name", ticker)

        metrics = [
            ("Mkt Cap",    mcap_s),
            ("P/E",        pe),
            ("Fwd P/E",    fpe),
            ("Rev YoY",    rev),
            ("Net Margin", margin),
            ("D/E",        de),
            ("ROE",        roe),
        ]
        if div_s:
            metrics.append(("Div Yield", div_s))

        chips = "".join(
            f'<span class="fund-metric"><span class="fund-label">{lbl}</span>{val}</span>'
            for lbl, val in metrics
        )
        sub    = " · ".join(filter(None, [sector, ind]))
        source = data.get("source", "Yahoo Finance")
        src_url = data.get("source_url", "")
        src_link = (f'<a href="{src_url}" target="_blank" class="fund-source">{source}</a>'
                    if src_url else f'<span class="fund-source">{source}</span>')
        return (
            f'<div class="fund-card">'
            f'<div class="fund-name">{name}'
            f'{"  <span class=\"fund-sub\">" + sub + "</span>" if sub else ""}'
            f'</div>'
            f'<div class="fund-metrics">{chips}</div>'
            f'<div class="fund-footer">Source: {src_link}</div>'
            f'</div>'
        )
    except Exception:
        return ""


async def _build_positions_html() -> str:
    import asyncio as _aio
    from datetime import date, datetime

    from db.database import order_history

    orders = await order_history(limit=100)
    today  = date.today()

    # ── Sync pending order statuses from IBKR ───────────────────────────────
    live_open_ids: set[str]      = set()
    live_statuses: dict[str, str] = {}
    ibkr_synced = False
    try:
        from mcp_servers.ibkr_orders.server import get_live_order_statuses
        from db.database import update_order_status
        live_open_ids, live_statuses = await _aio.wait_for(get_live_order_statuses(), timeout=15)
        ibkr_synced = True
        for o in orders:
            oid = str(o.get("ibkr_order_id") or "")
            if oid and "submit" in (o.get("status") or "").lower() and oid in live_statuses:
                new_s = live_statuses[oid]
                if any(t in new_s.lower() for t in ("fill", "cancel", "inactive")):
                    await update_order_status(oid, new_s)
                    o["status"] = new_s
    except Exception:
        pass

    # ── Live positions from ib_insync (best-effort) ──────────────────────────
    _refresh_btn = (
        f'<button class="btn-pos-refresh"'
        f' hx-get="/api/positions-fragment"'
        f' hx-target="#positions-content"'
        f' hx-swap="innerHTML"'
        f' hx-indicator=".pos-refresh-spin">'
        f'↻ Refresh</button>'
        f'<span class="pos-refresh-spin htmx-indicator">fetching…</span>'
    )
    try:
        from mcp_servers.ibkr_positions.server import get_live_pnl, get_open_positions
        pnl_text = await _aio.wait_for(get_live_pnl(),       timeout=20)
        pos_text = await _aio.wait_for(get_open_positions(), timeout=20)
        live_body = f'<pre class="pos-pre">{pnl_text}</pre><pre class="pos-pre">{pos_text}</pre>'
    except Exception as exc:
        err = str(exc) or type(exc).__name__
        live_body = f'<p class="pos-err">IB Gateway offline: {err}</p>'
    live_html = (
        f'<div class="pos-live-wrap">'
        f'<div class="pos-section-hdr">Live Account'
        f'  <span class="pos-refresh">auto-refresh 60s</span>'
        f'  {_refresh_btn}'
        f'</div>'
        f'{live_body}'
        f'</div>'
    )

    # ── Strategy table from order history ────────────────────────────────────
    if not orders:
        strat_html = '<p class="pos-empty">No orders in history yet.</p>'
    else:
        rows = []
        for o in orders:
            ibkr_oid = str(o.get("ibkr_order_id") or "")
            if ibkr_synced and ibkr_oid and ibkr_oid in live_statuses:
                o = {**o, "status": live_statuses[ibkr_oid]}

            expiry = o.get("expiry") or ""
            try:
                exp_date = date.fromisoformat(expiry)
                dte      = max(0, (exp_date - today).days)
                exp_disp = exp_date.strftime("%b %d '%y")
            except Exception:
                dte = None
                exp_disp = expiry

            short_s = float(o.get("short_strike") or 0)
            long_s  = float(o.get("long_strike")  or 0)
            net     = float(o.get("net_price")     or 0)
            qty     = int(o.get("quantity")         or 1)
            spread  = abs(short_s - long_s)
            right   = (o.get("option_type") or "P").upper()

            is_credit = net > 0
            if is_credit:
                max_p = round(abs(net) * 100 * qty)
                max_l = round((spread - abs(net)) * 100 * qty)
                breakeven = round(short_s - abs(net), 2) if right == "P" else round(short_s + abs(net), 2)
            else:
                max_l = round(abs(net) * 100 * qty)
                max_p = round((spread - abs(net)) * 100 * qty)
                breakeven = round(max(short_s, long_s) - abs(net), 2) if right == "P" else round(min(short_s, long_s) + abs(net), 2)

            # DTE badge
            if dte is None:
                dte_cell = '<span class="dte-badge dte-dim">—</span>'
            elif dte == 0:
                dte_cell = '<span class="dte-badge dte-expired">Expired</span>'
            elif dte <= 7:
                dte_cell = f'<span class="dte-badge dte-urgent">{dte}d ⚠</span>'
            elif dte <= 21:
                dte_cell = f'<span class="dte-badge dte-warn">{dte}d</span>'
            else:
                dte_cell = f'<span class="dte-badge dte-ok">{dte}d</span>'

            status = (o.get("status") or "—").lower()
            if "fill" in status:
                status_cell = '<span class="pos-status pos-filled">Filled</span>'
            elif "submit" in status:
                status_cell = '<span class="pos-status pos-pending">Pending</span>'
            elif "cancel" in status:
                status_cell = '<span class="pos-status pos-cancelled">Cancelled</span>'
            else:
                status_cell = f'<span class="pos-status">{status}</span>'

            ts = (o.get("timestamp") or "")[:10]
            strategy = o.get("strategy") or "—"
            ticker   = o.get("ticker") or "—"
            right_w  = "Put" if right == "P" else "Call"

            is_cancelled = "cancel" in status
            is_filled    = "fill" in status
            # Show cancel unless IBKR explicitly reports the order as closed.
            # If IBKR doesn't know the order (cross-session), still allow the
            # attempt — IBKR will return "not found" if it's already done.
            ibkr_live_status = live_statuses.get(ibkr_oid, "").lower() if ibkr_synced else ""
            ibkr_closed = bool(ibkr_live_status and any(
                t in ibkr_live_status for t in ("fill", "cancel", "inactive")
            ))
            can_cancel   = (
                not is_cancelled and not is_filled
                and ibkr_oid and ibkr_oid not in ("", "None", "0")
                and not ibkr_closed
            )

            if can_cancel:
                action_cell = (
                    f'<form hx-post="/api/cancel-order" hx-target="this" hx-swap="outerHTML">'
                    f'<input type="hidden" name="order_id" value="{ibkr_oid}">'
                    f'<button type="submit" class="btn-cancel">'
                    f'<span class="cancel-idle">✕ Cancel</span>'
                    f'<span class="cancel-busy">Cancelling…</span>'
                    f'</button>'
                    f'</form>'
                )
            else:
                action_cell = ""

            rows.append(
                f'<tr id="order-row-{ibkr_oid}"{" data-cancelled" if is_cancelled else ""}>'
                f'<td class="pos-ticker">{ticker}</td>'
                f'<td>{strategy}</td>'
                f'<td class="pos-mono">${short_s:.0f} / ${long_s:.0f} {right_w}</td>'
                f'<td>{exp_disp}<br>{dte_cell}</td>'
                f'<td class="pos-profit">+${max_p}</td>'
                f'<td class="pos-loss">-${max_l}</td>'
                f'<td class="pos-mono pos-be">${breakeven:.2f}</td>'
                f'<td class="pos-mono">{qty}</td>'
                f'<td class="pos-mono">{"+$" if is_credit else "-$"}{abs(net):.2f}</td>'
                f'<td>{status_cell}</td>'
                f'<td class="pos-date">{ts}</td>'
                f'<td class="pos-action">{action_cell}</td>'
                f'</tr>'
            )

        strat_html = (
            f'<div class="pos-section-hdr">Strategy History'
            f'<label class="pos-toggle">'
            f'<input type="checkbox" id="show-cancelled" onchange="toggleCancelled(this.checked)">'
            f'Show cancelled'
            f'</label>'
            f'</div>'
            f'<div class="pos-table-wrap">'
            f'<table class="pos-table" id="pos-table">'
            f'<thead><tr>'
            f'<th>Symbol</th><th>Strategy</th><th>Strikes</th>'
            f'<th>Expiry / DTE</th>'
            f'<th class="pos-profit">Max Profit</th>'
            f'<th class="pos-loss">Max Loss</th>'
            f'<th>Breakeven</th>'
            f'<th>Qty</th><th>Net</th><th>Status</th><th>Date</th><th></th>'
            f'</tr></thead>'
            f'<tbody>' + "\n".join(rows) + '</tbody>'
            f'</table>'
            f'</div>'
            f'<script>'
            f'(function(){{'
            f'  function toggleCancelled(show){{'
            f'    document.querySelectorAll("#pos-table tr[data-cancelled]")'
            f'      .forEach(function(r){{ r.style.display = show ? "" : "none"; }});'
            f'  }}'
            f'  window.toggleCancelled = toggleCancelled;'
            f'  toggleCancelled(false);'  # hide by default
            f'}})();'
            f'</script>'
        )

    return f'<div class="pos-page">{live_html}<div class="pos-strat-wrap">{strat_html}</div></div>'


def _page(history: list[dict], active_tab: str = "search",
          body_override: str | None = None, show_search: bool = True) -> str:
    sidebar = _sidebar_items(history)
    search_content = (body_override if body_override else
        '<div class="placeholder">'
        '<div class="icon">📊</div>'
        '<h2>Options Research</h2>'
        '<p>Enter a ticker and outlook to get a vertical spread analysis.</p>'
        '</div>')

    _tabs = [
        ("search",    "/",            "🔍", "Search"),
        ("positions", "/positions",   "📋", "Positions"),
        ("ibkr",      "/ibkr",        "⚡", "IBKR"),
        ("research",  "/ui-research", "🔬", "Research"),
        ("test",      "/test",        "✅", "Tests"),
    ]
    bottom_nav = "".join(
        f'<a href="{url}" class="{"active" if t == active_tab else ""}">'
        f'<span class="bn-icon">{icon}</span>{label}</a>'
        for t, url, icon, label in _tabs
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <title>FinAgent</title>
  <script src="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js" defer></script>
  <style>{_CSS}</style>
</head>
<body>
  <div class="sidebar-backdrop" id="backdrop" onclick="closeSidebar()"></div>

  <header>
    <button class="hamburger" onclick="toggleSidebar()" aria-label="Menu">☰</button>
    <a href="/" class="logo">
      <div class="logo-mark">F</div>
      <span class="logo-text">FinAgent</span>
    </a>
    <nav class="nav-links">
      <a href="/"            class="nav-link {'active' if active_tab=='search'    else ''}">Search</a>
      <a href="/positions"   class="nav-link {'active' if active_tab=='positions' else ''}">Positions</a>
      <a href="/ibkr"        class="nav-link {'active' if active_tab=='ibkr'      else ''}">IBKR</a>
      <a href="/ui-research" class="nav-link {'active' if active_tab=='research'  else ''}">Research</a>
      <a href="/test"        class="nav-link {'active' if active_tab=='test'       else ''}">Tests</a>
    </nav>
    <div class="theme-toggle">
      <button class="theme-btn" id="t-dark"  onclick="setTheme('dark')"  title="Dark">🌙</button>
      <button class="theme-btn" id="t-light" onclick="setTheme('light')" title="Light">☀️</button>
    </div>
  </header>

  <div class="app-layout">
    <aside id="sidebar">
      <div class="sidebar-hdr">Recent Searches</div>
      <div id="history-list">{sidebar}</div>
    </aside>

    <div class="content">
      {'<div class="search-bar"><form class="search-form" id="search-form" hx-post="/search" hx-target="#results" hx-swap="innerHTML" hx-indicator="#spinner"><div class="ticker-wrap"><input name="ticker" type="text" placeholder="AAPL" maxlength="6" autocomplete="off" autocapitalize="characters" autofocus /></div><select name="outlook"><option value="bullish">📈 Bullish</option><option value="bearish">📉 Bearish</option><option value="neutral">↔️ Neutral</option></select><div class="term-group" id="tg-short"><input type="radio" name="trm" id="tr-short" checked onchange="onTerm(\'short\')"><label for="tr-short">📅 Short-term</label><select class="term-sel" id="sel-short" onchange="onDte(\'short\')"><option value="7">7d</option><option value="14">14d</option><option value="21">21d</option><option value="30" selected>30d</option><option value="45">45d</option></select></div><div class="term-group term-off" id="tg-long"><input type="radio" name="trm" id="tr-long" onchange="onTerm(\'long\')"><label for="tr-long">📆 Long-term</label><select class="term-sel" id="sel-long" disabled onchange="onDte(\'long\')"><option value="60">60d</option><option value="90" selected>90d</option><option value="120">120d</option><option value="180">180d</option><option value="365">1yr</option></select></div><input type="hidden" name="term" id="term-val" value="short"><input type="hidden" name="dte_target" id="dte-val" value="30"><button class="btn" type="submit">Research</button><div id="spinner" class="htmx-indicator">Fetching…</div></form><script>function onTerm(t){var s=t===\'short\';document.getElementById(\'term-val\').value=t;document.getElementById(\'tg-short\').classList.toggle(\'term-off\',!s);document.getElementById(\'tg-long\').classList.toggle(\'term-off\',s);document.getElementById(\'sel-short\').disabled=!s;document.getElementById(\'sel-long\').disabled=s;onDte(t);}function onDte(t){var sel=document.getElementById(t===\'short\'?\'sel-short\':\'sel-long\');document.getElementById(\'dte-val\').value=sel.value;}</script></div>' if show_search else ''}

      <div id="results">{search_content}</div>
    </div>
  </div>

  <nav class="bottom-nav">{bottom_nav}</nav>

  <script>
    function toggleSidebar() {{
      document.getElementById('sidebar').classList.toggle('open');
      document.getElementById('backdrop').classList.toggle('open');
    }}
    function closeSidebar() {{
      document.getElementById('sidebar').classList.remove('open');
      document.getElementById('backdrop').classList.remove('open');
    }}
    document.body.addEventListener('htmx:afterRequest', e => {{
      if (e.detail.elt.classList.contains('h-item')) closeSidebar();
    }});

    function setTheme(t) {{
      document.body.classList.toggle('light', t === 'light');
      localStorage.setItem('theme', t);
      document.querySelectorAll('.theme-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('t-' + t).classList.add('active');
    }}
    setTheme(localStorage.getItem('theme') || 'dark');

    function setActive(id) {{
      document.querySelectorAll('.h-item')
        .forEach(el => el.classList.toggle('active', +el.dataset.id === id));
    }}
    document.body.addEventListener('htmx:beforeRequest', e => {{
      if (e.detail.elt.tagName === 'FORM')
        document.querySelectorAll('.h-item').forEach(el => el.classList.remove('active'));
    }});

    function enhanceOutput() {{
      document.querySelectorAll('.result-wrap pre').forEach(pre => {{
        pre.innerHTML = pre.innerHTML
          .split('\\n')
          .map(line => {{
            if (line.includes('⭐'))    return `<span class="star-row">${{line}}</span>`;
            if (line.includes('◀ATM')) return `<span class="atm-row">${{line}}</span>`;
            return line;
          }})
          .join('\\n')
          .replace(/(\\+\\$[\\d,.]+)/g, '<span class="pos">$1</span>')
          .replace(/(-\\$[\\d,.]+)/g,  '<span class="neg">$1</span>');
      }});
      document.querySelectorAll('.result-wrap code').forEach(code => {{
        if (!code.textContent.includes('POP')) return;
        code.innerHTML = code.innerHTML
          .replace(/(\\+\\$[\\d,.]+)/g, '<span class="pos">$1</span>')
          .replace(/(-\\$[\\d,.]+)/g,  '<span class="neg">$1</span>')
          .replace(/\\b(\\d{{1,3}}%)/g, '<span class="pct">$1</span>');
      }});
    }}

    document.body.addEventListener('htmx:afterSwap', enhanceOutput);
    document.addEventListener('DOMContentLoaded', enhanceOutput);
  </script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
