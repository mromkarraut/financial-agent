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

import logging

import aiosqlite
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
from agents.options_research_agent import OptionsResearchAgent
from agents.ui_researcher_agent import UIResearcherAgent
from db.database import init_db

logger = logging.getLogger(__name__)
app    = FastAPI(title="Financial Research Agent")
_ui_agent = UIResearcherAgent()


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    await _ui_agent.ensure_seeded()


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
    ticker: str  = Form(...),
    outlook: str = Form("neutral"),
) -> HTMLResponse:
    ticker = ticker.strip().upper()
    if not ticker:
        return HTMLResponse('<p class="error">Ticker is required.</p>')

    agent  = OptionsResearchAgent()
    result = await agent.run({"ticker": ticker, "outlook": outlook, "chat_id": "web"})
    history = await _get_history()
    new_id  = history[0]["id"] if history else None

    # Results panel + OOB sidebar update in one response
    results_html = f'<div class="result-wrap">{result["output"]}</div>'
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


# ── HTML page ─────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:       #0d1117;
  --bg2:      #161b22;
  --bg3:      #21262d;
  --border:   #30363d;
  --text:     #e6edf3;
  --dim:      #8b949e;
  --blue:     #1f6feb;
  --blue-lt:  #388bfd;
  --green:    #3fb950;
  --red:      #f85149;
  --yellow:   #d29922;
  --code-fg:  #79c0ff;
  --mono:     ui-monospace, 'Cascadia Mono', 'Cascadia Code',
              'JetBrains Mono', 'Fira Code', Consolas,
              'Liberation Mono', monospace;
}

body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}

/* ── Header ── */
header {
  background: var(--bg2); border-bottom: 1px solid var(--border);
  padding: 10px 18px; display: flex; align-items: center; gap: 10px;
  flex-shrink: 0; z-index: 10;
}
header h1 { font-size: 16px; font-weight: 600; }
.badge {
  background: var(--blue); color: #fff;
  font-size: 11px; padding: 1px 7px; border-radius: 10px;
}
.nav-links { margin-left: auto; display: flex; gap: 4px; }
.nav-link {
  color: var(--dim); font-size: 13px; text-decoration: none;
  padding: 4px 10px; border-radius: 5px; transition: background .12s;
}
.nav-link:hover { background: var(--bg3); color: var(--text); }
.nav-link.active { background: var(--bg3); color: var(--text); }

/* ── Layout ── */
.main { display: flex; flex: 1; overflow: hidden; }

/* ── Sidebar ── */
aside {
  width: 220px; min-width: 220px;
  background: var(--bg2); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
}
.sidebar-hdr {
  padding: 9px 13px; font-size: 11px; font-weight: 600;
  color: var(--dim); text-transform: uppercase; letter-spacing: .06em;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
#history-list {
  flex: 1; overflow-y: auto; padding: 5px;
}
#history-list::-webkit-scrollbar { width: 3px; }
#history-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.h-item {
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
  transition: background .1s; margin-bottom: 2px;
  border: 1px solid transparent;
}
.h-item:hover  { background: var(--bg3); }
.h-item.active { background: #1a2a40; border-color: var(--blue); }
.h-ticker { font-weight: 600; font-size: 13px; }
.h-meta   { font-size: 11px; color: var(--dim); margin-top: 2px; line-height: 1.4; }
.h-rec    { font-size: 10px; opacity: .75; }
.no-history { padding: 14px; color: var(--dim); font-size: 13px; }
.bull { color: var(--green); }
.bear { color: var(--red); }
.neu  { color: var(--yellow); }

/* ── Content ── */
.content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

/* ── Search bar ── */
.search-bar {
  background: var(--bg2); border-bottom: 1px solid var(--border);
  padding: 10px 18px; display: flex; align-items: center; gap: 8px;
  flex-shrink: 0;
}
.search-bar input, .search-bar select {
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 6px; color: var(--text); font-size: 14px;
  padding: 6px 10px; outline: none;
}
.search-bar input {
  width: 105px; text-transform: uppercase;
  letter-spacing: .04em; font-weight: 600;
}
.search-bar input::placeholder { text-transform: none; font-weight: 400; color: var(--dim); }
.search-bar input:focus, .search-bar select:focus { border-color: var(--blue); }
.search-bar select { cursor: pointer; }
.btn {
  background: var(--blue); border: none; border-radius: 6px;
  color: #fff; font-size: 14px; font-weight: 500; padding: 6px 16px;
  cursor: pointer; transition: background .12s;
}
.btn:hover { background: var(--blue-lt); }

/* HTMX loading indicator (auto-shown during hx-request) */
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator,
.htmx-request.htmx-indicator { display: flex !important; }
#spinner {
  align-items: center; gap: 6px;
  color: var(--dim); font-size: 12px;
}
#spinner::before {
  content: '';
  width: 14px; height: 14px; flex-shrink: 0;
  border: 2px solid var(--border); border-top-color: var(--blue);
  border-radius: 50%; animation: spin .6s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Results panel ── */
#results {
  flex: 1; overflow-y: auto; padding: 24px 28px;
}
#results::-webkit-scrollbar { width: 5px; }
#results::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

/* Fade-in on DOM insertion (fires on HTMX swap) */
#results > * {
  animation: fadein .15s ease;
}
@keyframes fadein {
  from { opacity: 0; transform: translateY(5px); }
  to   { opacity: 1; transform: none; }
}

.placeholder {
  text-align: center; color: var(--dim); margin-top: 72px;
}
.placeholder .icon { font-size: 36px; margin-bottom: 10px; }
.placeholder h2 { font-size: 19px; color: var(--text); margin-bottom: 5px; }
.placeholder p  { font-size: 13px; }
.placeholder kbd {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 6px;
  font-size: 12px; font-family: var(--mono);
}
.error { color: var(--red); padding: 16px; }

/* ── Agent HTML output ── */
.result-wrap {
  max-width: 800px; margin: 0 auto; line-height: 1.65;
}
.result-wrap b  { color: #f0f6fc; font-weight: 600; }
.result-wrap i  { color: var(--dim); font-style: italic; }

.result-wrap code {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 5px; padding: 3px 7px;
  font-family: var(--mono); font-size: 13px;
  color: var(--code-fg); white-space: pre-wrap;
}

/* Critical <pre> settings for ASCII box-drawing and P&L charts */
.result-wrap pre {
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.45;          /* 1.45 not 1.55 — glyphs connect across lines */
  letter-spacing: 0;          /* Explicit 0 — any gap breaks box-drawing alignment */
  font-variant-ligatures: none;               /* Disable -- → — and -> → arrow */
  font-feature-settings: "liga" 0, "calt" 0; /* Belt-and-suspenders ligature off */
  -webkit-font-smoothing: auto;               /* Not antialiased — thins box chars */
  white-space: pre;
  overflow-x: auto;
  background: #0a0e14;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin: 6px 0 12px;
  color: #c9d1d9;
  tab-size: 4;
}
.result-wrap pre::-webkit-scrollbar { height: 3px; }
.result-wrap pre::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

/* ── UI Research page ── */
.research-report h2 {
  font-size: 18px; margin-bottom: 6px;
}
.research-report h3 {
  font-size: 14px; color: var(--dim); margin: 18px 0 10px;
  text-transform: uppercase; letter-spacing: .05em;
}
.research-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 18px; margin-bottom: 12px;
}
.research-card.done   { border-left: 3px solid var(--green); }
.research-card.pending { border-left: 3px solid var(--yellow); }
.rc-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.rc-topic  { font-weight: 600; font-size: 14px; flex: 1; }
.rc-status { font-size: 12px; color: var(--dim); }
.rc-score  { font-size: 12px; color: var(--dim); margin-bottom: 8px; }
.rc-summary, .rc-rec { font-size: 13px; line-height: 1.55; margin-bottom: 6px; }
.rc-rec    { color: var(--code-fg); }
.rc-footer { font-size: 11px; color: var(--dim); margin-top: 10px;
             display: flex; align-items: center; gap: 10px; }
.btn-impl {
  background: var(--bg3); border: 1px solid var(--border);
  border-radius: 4px; color: var(--text); font-size: 11px;
  padding: 2px 8px; cursor: pointer;
}
.btn-impl:hover { background: var(--blue); border-color: var(--blue); }
"""


def _page(history: list[dict], active_tab: str = "search",
          body_override: str | None = None) -> str:
    sidebar = _sidebar_items(history)
    search_content = (body_override if body_override else
        '<div class="placeholder">'
        '<div class="icon">🔭</div>'
        '<h2>Options Research</h2>'
        '<p>Type a ticker and press <kbd>Enter</kbd> to analyse.</p>'
        '</div>')
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Financial Research Agent</title>
  <script src="https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js" defer></script>
  <style>{_CSS}</style>
</head>
<body>
  <header>
    <h1>📊 Financial Research</h1>
    <span class="badge">Options</span>
    <nav class="nav-links">
      <a href="/"            class="nav-link {'active' if active_tab=='search'   else ''}">Search</a>
      <a href="/ui-research" class="nav-link {'active' if active_tab=='research' else ''}">UI Research</a>
    </nav>
  </header>

  <div class="main">
    <aside>
      <div class="sidebar-hdr">Recent Searches</div>
      <div id="history-list">{sidebar}</div>
    </aside>

    <div class="content">
      <!-- Search bar — HTMX form -->
      <div class="search-bar">
        <form hx-post="/search"
              hx-target="#results"
              hx-swap="innerHTML"
              hx-indicator="#spinner"
              style="display:flex;gap:8px;align-items:center">
          <input name="ticker" type="text" placeholder="AAPL" maxlength="6"
                 autocomplete="off" autocapitalize="characters" autofocus />
          <select name="outlook">
            <option value="bullish">📈 Bullish</option>
            <option value="bearish">📉 Bearish</option>
            <option value="neutral">↔️ Neutral</option>
          </select>
          <button class="btn" type="submit">Research</button>
          <div id="spinner" class="htmx-indicator">Fetching…</div>
        </form>
      </div>

      <div id="results">
        {search_content}
      </div>
    </div>
  </div>

  <script>
    // Track active sidebar item — 3 lines, no Alpine needed
    function setActive(id) {{
      document.querySelectorAll('.h-item')
        .forEach(el => el.classList.toggle('active', +el.dataset.id === id));
    }}

    // Clear active state when a new search is submitted
    document.body.addEventListener('htmx:beforeRequest', e => {{
      if (e.detail.elt.tagName === 'FORM')
        document.querySelectorAll('.h-item').forEach(el => el.classList.remove('active'));
    }});
  </script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
