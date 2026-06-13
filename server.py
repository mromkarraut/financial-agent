"""
FastAPI web UI for the financial research agent.
Serves options research results as styled HTML in the browser.
History is persisted in SQLite and displayed in the left sidebar.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
Then open http://localhost:8000 in your browser.
"""

import logging

import aiosqlite
import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

import config
from agents.options_research_agent import OptionsResearchAgent
from db.database import init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="Financial Research Agent")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_history(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT id, ticker, outlook, price, recommended, timestamp, ivr "
            "FROM options_research_memory "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"id": r[0], "ticker": r[1], "outlook": r[2], "price": r[3],
         "recommended": r[4], "timestamp": r[5], "ivr": r[6]}
        for r in rows
    ]


async def _get_result_html(result_id: int) -> str | None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute(
            "SELECT output_html FROM options_research_memory WHERE id=?", (result_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row and row[0] else None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    history = await _get_history()
    return HTMLResponse(content=_render_page(history))


@app.post("/search")
async def search(
    ticker: str = Form(...),
    outlook: str = Form("neutral"),
) -> JSONResponse:
    ticker = ticker.strip().upper()
    if not ticker:
        return JSONResponse({"error": "Ticker is required"}, status_code=400)

    agent = OptionsResearchAgent()
    result = await agent.run({"ticker": ticker, "outlook": outlook, "chat_id": "web"})
    history = await _get_history()
    return JSONResponse({
        "html": result["output"],
        "ticker": ticker,
        "outlook": outlook,
        "history": history,
        "new_id": history[0]["id"] if history else None,
    })


@app.get("/api/history")
async def api_history() -> JSONResponse:
    return JSONResponse(await _get_history())


@app.get("/api/result/{result_id}")
async def api_result(result_id: int) -> JSONResponse:
    html = await _get_result_html(result_id)
    if html:
        return JSONResponse({"html": html})
    return JSONResponse({"error": "Not found"}, status_code=404)


# ── HTML page ─────────────────────────────────────────────────────────────────

def _render_page(history: list[dict]) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Financial Research Agent</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:        #0d1117;
      --bg2:       #161b22;
      --bg3:       #21262d;
      --border:    #30363d;
      --text:      #e6edf3;
      --text-dim:  #8b949e;
      --blue:      #1f6feb;
      --blue-lt:   #388bfd;
      --green:     #3fb950;
      --red:       #f85149;
      --yellow:    #d29922;
      --code-fg:   #79c0ff;
      --mono:      'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Courier New', monospace;
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    /* ── Header ── */
    header {{
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-shrink: 0;
    }}
    header h1 {{ font-size: 17px; font-weight: 600; color: var(--text); }}
    .badge {{
      background: var(--blue); color: #fff;
      font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 500;
    }}
    .header-right {{ margin-left: auto; color: var(--text-dim); font-size: 12px; }}

    /* ── Layout ── */
    .main {{ display: flex; flex: 1; overflow: hidden; }}

    /* ── Sidebar ── */
    aside {{
      width: 230px; min-width: 230px;
      background: var(--bg2);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column;
      overflow: hidden;
    }}
    .sidebar-hdr {{
      padding: 10px 14px;
      font-size: 11px; font-weight: 600;
      color: var(--text-dim); text-transform: uppercase; letter-spacing: .06em;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }}
    .history-list {{ flex: 1; overflow-y: auto; padding: 6px; }}
    .history-list::-webkit-scrollbar {{ width: 4px; }}
    .history-list::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 2px; }}

    .h-item {{
      padding: 9px 10px; border-radius: 6px;
      cursor: pointer; transition: background .12s;
      margin-bottom: 2px; border: 1px solid transparent;
    }}
    .h-item:hover {{ background: var(--bg3); }}
    .h-item.active {{ background: #1a2a40; border-color: var(--blue); }}
    .h-ticker {{ font-weight: 600; font-size: 13px; display: flex; align-items: center; gap: 6px; }}
    .h-meta {{ font-size: 11px; color: var(--text-dim); margin-top: 3px; line-height: 1.4; }}
    .bull {{ color: var(--green); }}
    .bear {{ color: var(--red); }}
    .neu  {{ color: var(--yellow); }}

    /* ── Content ── */
    .content {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}

    /* ── Search bar ── */
    .search-bar {{
      background: var(--bg2);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      display: flex; align-items: center; gap: 8px;
      flex-shrink: 0;
    }}
    .search-bar input, .search-bar select {{
      background: var(--bg); border: 1px solid var(--border);
      border-radius: 6px; color: var(--text);
      font-size: 14px; padding: 7px 11px; outline: none;
    }}
    .search-bar input {{ width: 110px; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }}
    .search-bar input::placeholder {{ text-transform: none; font-weight: 400; color: var(--text-dim); }}
    .search-bar input:focus, .search-bar select:focus {{ border-color: var(--blue); }}
    .search-bar select {{ cursor: pointer; }}
    .btn {{
      background: var(--blue); border: none; border-radius: 6px;
      color: #fff; font-size: 14px; font-weight: 500;
      padding: 7px 18px; cursor: pointer; transition: background .12s;
    }}
    .btn:hover {{ background: var(--blue-lt); }}
    .btn:disabled {{ background: var(--bg3); color: var(--text-dim); cursor: not-allowed; }}
    .spinner {{
      display: none; width: 16px; height: 16px;
      border: 2px solid var(--border); border-top-color: var(--blue);
      border-radius: 50%; animation: spin .65s linear infinite; flex-shrink: 0;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .status {{ font-size: 12px; color: var(--text-dim); margin-left: 4px; }}

    /* ── Results panel ── */
    .results {{ flex: 1; overflow-y: auto; padding: 28px 32px; }}
    .results::-webkit-scrollbar {{ width: 6px; }}
    .results::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

    .placeholder {{
      text-align: center; color: var(--text-dim); margin-top: 80px;
    }}
    .placeholder .icon {{ font-size: 40px; margin-bottom: 12px; }}
    .placeholder h2 {{ font-size: 20px; color: var(--text); margin-bottom: 6px; }}
    .placeholder p {{ font-size: 14px; }}
    .placeholder kbd {{
      background: var(--bg3); border: 1px solid var(--border);
      border-radius: 4px; padding: 1px 6px; font-size: 12px; font-family: var(--mono);
    }}

    /* ── Agent HTML output styling ── */
    .result-wrap {{ max-width: 780px; margin: 0 auto; line-height: 1.65; }}

    .result-wrap b {{ color: #f0f6fc; font-weight: 600; }}
    .result-wrap i {{ color: var(--text-dim); font-style: italic; }}

    .result-wrap code {{
      background: var(--bg2); border: 1px solid var(--border);
      border-radius: 5px; padding: 3px 7px;
      font-family: var(--mono); font-size: 13px; color: var(--code-fg);
      white-space: pre-wrap;
    }}

    .result-wrap pre {{
      background: var(--bg2); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px 16px;
      font-family: var(--mono); font-size: 13px;
      overflow-x: auto; white-space: pre;
      color: var(--text); line-height: 1.55;
      margin: 6px 0 10px;
    }}

    /* Colour P&L lines in pre blocks */
    .result-wrap pre {{ position: relative; }}

    .result-wrap p, .result-wrap div {{ margin-bottom: 4px; }}
  </style>
</head>
<body>
  <header>
    <h1>📊 Financial Research Agent</h1>
    <span class="badge">Options</span>
    <span class="header-right">Powered by yfinance · Black-Scholes</span>
  </header>

  <div class="main">
    <!-- Sidebar -->
    <aside>
      <div class="sidebar-hdr">Recent Searches</div>
      <div class="history-list" id="history-list">{_render_history_items(history)}</div>
    </aside>

    <!-- Main content -->
    <div class="content">
      <div class="search-bar">
        <input id="ticker" type="text" placeholder="AAPL" maxlength="6"
               onkeydown="if(event.key==='Enter') runSearch()" autofocus />
        <select id="outlook">
          <option value="bullish">📈  Bullish</option>
          <option value="bearish">📉  Bearish</option>
          <option value="neutral">↔️  Neutral</option>
        </select>
        <button class="btn" id="search-btn" onclick="runSearch()">Research</button>
        <div class="spinner" id="spinner"></div>
        <span class="status" id="status"></span>
      </div>

      <div class="results" id="results">
        <div class="placeholder">
          <div class="icon">🔭</div>
          <h2>Options Research</h2>
          <p>Type a ticker and press <kbd>Enter</kbd> to analyse.</p>
        </div>
      </div>
    </div>
  </div>

  <script>
    let activeId = null;

    async function runSearch() {{
      const ticker  = document.getElementById('ticker').value.trim().toUpperCase();
      const outlook = document.getElementById('outlook').value;
      if (!ticker) {{ document.getElementById('ticker').focus(); return; }}

      setLoading(true, `Fetching ${{ticker}}…`);
      try {{
        const form = new FormData();
        form.append('ticker', ticker);
        form.append('outlook', outlook);
        const res  = await fetch('/search', {{ method: 'POST', body: form }});
        const data = await res.json();
        if (data.error) {{ showError(data.error); return; }}
        showResult(data.html);
        activeId = data.new_id;
        updateHistory(data.history);
      }} catch(e) {{
        showError(e.message);
      }} finally {{
        setLoading(false);
      }}
    }}

    async function loadResult(id, ticker, outlook) {{
      activeId = id;
      document.getElementById('ticker').value  = ticker;
      document.getElementById('outlook').value = outlook;
      highlightActive();
      setLoading(true, `Loading ${{ticker}}…`);
      try {{
        const res  = await fetch(`/api/result/${{id}}`);
        const data = await res.json();
        if (data.error) {{
          // Stored HTML not available — re-run live
          await runSearch();
        }} else {{
          showResult(data.html);
        }}
      }} catch(e) {{
        showError(e.message);
      }} finally {{
        setLoading(false);
      }}
    }}

    function showResult(html) {{
      document.getElementById('results').innerHTML =
        '<div class="result-wrap">' + html + '</div>';
    }}

    function showError(msg) {{
      document.getElementById('results').innerHTML =
        `<div class="result-wrap" style="color:var(--red);padding:20px">⚠ ${{msg}}</div>`;
    }}

    function setLoading(on, msg) {{
      document.getElementById('search-btn').disabled = on;
      document.getElementById('spinner').style.display = on ? 'block' : 'none';
      document.getElementById('status').textContent = on ? (msg || '') : '';
    }}

    function updateHistory(items) {{
      document.getElementById('history-list').innerHTML = renderHistoryItems(items);
    }}

    function highlightActive() {{
      document.querySelectorAll('.h-item').forEach(el => {{
        el.classList.toggle('active', parseInt(el.dataset.id) === activeId);
      }});
    }}

    function renderHistoryItems(items) {{
      if (!items.length) return '<div style="padding:14px;color:var(--text-dim);font-size:13px">No searches yet</div>';
      return items.map(item => {{
        const cls   = item.outlook === 'bullish' ? 'bull' : item.outlook === 'bearish' ? 'bear' : 'neu';
        const icon  = item.outlook === 'bullish' ? '📈' : item.outlook === 'bearish' ? '📉' : '↔️';
        const date  = new Date(item.timestamp).toLocaleDateString(undefined, {{month:'short', day:'numeric'}});
        const price = item.price ? `$${{item.price.toFixed(2)}}` : '—';
        const rec   = (item.recommended || '').replace('2026-', '').substring(0, 28);
        const active = item.id === activeId ? ' active' : '';
        return `<div class="h-item${{active}}" data-id="${{item.id}}"
                     onclick="loadResult(${{item.id}}, '${{item.ticker}}', '${{item.outlook}}')">
          <div class="h-ticker">
            ${{item.ticker}}
            <span class="${{cls}}">${{icon}}</span>
          </div>
          <div class="h-meta">
            ${{price}} · ${{date}}<br>
            <span style="font-size:10px;opacity:.8">${{rec}}</span>
          </div>
        </div>`;
      }}).join('');
    }}
  </script>
</body>
</html>"""


def _render_history_items(history: list[dict]) -> str:
    if not history:
        return '<div style="padding:14px;color:var(--text-dim);font-size:13px">No searches yet</div>'
    items = []
    for item in history:
        cls  = "bull" if item["outlook"] == "bullish" else "bear" if item["outlook"] == "bearish" else "neu"
        icon = "📈" if item["outlook"] == "bullish" else "📉" if item["outlook"] == "bearish" else "↔️"
        try:
            from datetime import datetime
            ts   = datetime.fromisoformat(item["timestamp"])
            date = ts.strftime("%b %d")
        except Exception:
            date = item["timestamp"][:10]
        price = f"${item['price']:.2f}" if item.get("price") else "—"
        rec   = (item.get("recommended") or "").replace("2026-", "")[:28]
        items.append(
            f'<div class="h-item" data-id="{item["id"]}"'
            f' onclick="loadResult({item["id"]}, \'{item["ticker"]}\', \'{item["outlook"]}\')">'
            f'<div class="h-ticker">{item["ticker"]} <span class="{cls}">{icon}</span></div>'
            f'<div class="h-meta">{price} · {date}<br>'
            f'<span style="font-size:10px;opacity:.8">{rec}</span></div>'
            f"</div>"
        )
    return "\n".join(items)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
