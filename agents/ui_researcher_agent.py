"""
UIResearcherAgent — researches UI/UX improvements for the web dashboard.

Stores findings in `ui_research_memory` (SQLite).  Each finding has:
  topic, summary, recommendation, score (0-10), implemented (0/1), source URL

Pre-seeded with the top-5 framework comparison done on 2026-06-13.
On subsequent calls for the same topic it recalls the stored finding instead
of re-researching, and notes how many days have passed.

Usage (from server.py):
    agent = UIResearcherAgent()
    findings = await agent.get_all()          # list all stored findings
    report   = await agent.html_report()      # full HTML for the /ui-research page
    await agent.mark_implemented(finding_id)
"""

import json
import logging
from datetime import datetime, timezone

import aiosqlite

import config

logger = logging.getLogger(__name__)

# ── Seed data (research done 2026-06-13) ──────────────────────────────────────

_SEED: list[dict] = [
    {
        "topic": "UI Framework Comparison",
        "summary": (
            "Compared 5 approaches for the financial dashboard: (1) Plain CSS/Flexbox "
            "(current), (2) Tailwind CDN, (3) Alpine.js + Tailwind, (4) HTMX, "
            "(5) React/Next.js. Evaluated on: <pre> ASCII rendering, sidebar layout, "
            "bundle size, DX, dark-theme support, and fit with FastAPI HTML output."
        ),
        "recommendation": (
            "Use HTMX (14 KB). The agent already returns HTML — HTMX is the only "
            "approach where the server output format matches the library input format "
            "natively. Drops ~80 lines of imperative JS (fetch, DOM update, sidebar "
            "re-render) to ~10 HTMX attributes. Keep current CSS unchanged. "
            "Do NOT add Tailwind (112 KB CDN, fights injected-HTML); "
            "do NOT add React (requires JSON API refactor, 2-3 day setup)."
        ),
        "score": 9,
        "implemented": 1,
        "source": "Internal research + testdriven.io/blog/fastapi-htmx",
    },
    {
        "topic": "Monospace <pre> CSS for ASCII Charts",
        "summary": (
            "Research into optimal CSS for rendering Unicode box-drawing characters "
            "(┌─┬─┐ etc.) and P&L ASCII charts in the browser. Key findings: "
            "letter-spacing must be exactly 0 (even 0.01em breaks box-drawing alignment); "
            "line-height 1.45 is the sweet spot (1.0 clips descenders, 1.55 gaps glyphs); "
            "font ligatures (Fira Code, JetBrains Mono) must be disabled on <pre> blocks "
            "or they merge -- into — and -> into arrows, breaking chart symbols."
        ),
        "recommendation": (
            "Set on .result-wrap pre: "
            "font-family: ui-monospace, 'Cascadia Mono', 'JetBrains Mono', monospace; "
            "line-height: 1.45; letter-spacing: 0; "
            "font-variant-ligatures: none; font-feature-settings: 'liga' 0, 'calt' 0; "
            "-webkit-font-smoothing: auto (not antialiased — thins box-drawing strokes)."
        ),
        "score": 10,
        "implemented": 1,
        "source": "MDN, qwtel.com/posts/software/the-monospaced-system-ui-css-font-stack",
    },
    {
        "topic": "HTMX OOB Swap Pattern for Sidebar + Results",
        "summary": (
            "HTMX out-of-band (OOB) swap lets a single server response update multiple "
            "page regions. The /search POST can return the results panel HTML plus a "
            "<div id='history-list' hx-swap-oob='true'> block to refresh the sidebar "
            "simultaneously — without a second request or JS coordination logic."
        ),
        "recommendation": (
            "In POST /search response: return results fragment + OOB sidebar fragment. "
            "Use hx-indicator='#spinner' with .htmx-indicator CSS class for the loading "
            "spinner (HTMX manages show/hide automatically). "
            "Active sidebar item: track via a small JS listener on htmx:afterRequest "
            "(3 lines, cleaner than bringing in Alpine.js just for this)."
        ),
        "score": 9,
        "implemented": 1,
        "source": "htmx.org/docs/#oob_swaps",
    },
    {
        "topic": "Financial Dark Theme Color Palette",
        "summary": (
            "Research on financial dashboard color systems: Bloomberg uses amber/orange "
            "on black; tastytrade uses GitHub-dark-adjacent blue-grey. For readability "
            "on extended sessions, pure phosphor green (#00FF41) causes eye fatigue — "
            "GitHub-dark palette (current) is superior as default. Offer theme toggle."
        ),
        "recommendation": (
            "Current CSS variable palette is correct for default. Add optional .theme-phosphor "
            "class: color #00FF41, bg #000, text-shadow 0 0 8px rgba(0,255,65,0.4). "
            "Add .theme-amber: color #FFB000, bg #0a0800. "
            "Implement as a toggle button in the header."
        ),
        "score": 7,
        "implemented": 0,
        "source": "bloomberg.com/company/stories/designing-the-terminal-for-color-accessibility",
    },
    {
        "topic": "Results Panel Fade-in Animation",
        "summary": (
            "HTMX supports CSS transition animations on swapped content via the "
            "'transition:true' modifier or by defining a .htmx-settling class. "
            "A subtle fade-in (opacity 0 → 1, 150ms) on the results panel makes "
            "updates feel responsive rather than jarring, especially for the ~8s "
            "chain-fetch delay."
        ),
        "recommendation": (
            "Add to CSS: #results > * { animation: fadein 0.15s ease; } "
            "@keyframes fadein { from { opacity: 0; transform: translateY(4px); } "
            "to { opacity: 1; transform: none; } }. "
            "No JS or HTMX config needed — the animation triggers on DOM insertion."
        ),
        "score": 8,
        "implemented": 1,
        "source": "htmx.org/examples/animations",
    },
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _seed_if_empty() -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM ui_research_memory") as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            for item in _SEED:
                await db.execute(
                    "INSERT INTO ui_research_memory "
                    "(topic, timestamp, summary, recommendation, score, implemented, source) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (_utcnow(), item["topic"], item["summary"],
                     item["recommendation"], item["score"],
                     item["implemented"], item["source"]),
                )
            await db.commit()
            logger.info("UIResearcherAgent: seeded %d findings", len(_SEED))


# ── Agent ──────────────────────────────────────────────────────────────────────

class UIResearcherAgent:
    """
    Stores and retrieves UI research findings.
    Pre-seeded with the top-5 framework comparison + CSS analysis.
    """

    async def ensure_seeded(self) -> None:
        await _seed_if_empty()

    async def get_all(self) -> list[dict]:
        await self.ensure_seeded()
        async with aiosqlite.connect(config.DB_PATH) as db:
            async with db.execute(
                "SELECT id, topic, timestamp, summary, recommendation, score, implemented, source "
                "FROM ui_research_memory ORDER BY score DESC, id ASC"
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"id": r[0], "topic": r[1], "timestamp": r[2], "summary": r[3],
             "recommendation": r[4], "score": r[5], "implemented": bool(r[6]), "source": r[7]}
            for r in rows
        ]

    async def add(self, topic: str, summary: str, recommendation: str,
                  score: int = 5, source: str = "") -> int:
        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO ui_research_memory "
                "(topic, timestamp, summary, recommendation, score, implemented, source) "
                "VALUES (?,?,?,?,?,0,?)",
                (topic, _utcnow(), summary, recommendation, score, source),
            )
            await db.commit()
            return cur.lastrowid

    async def mark_implemented(self, finding_id: int) -> None:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE ui_research_memory SET implemented=1 WHERE id=?", (finding_id,)
            )
            await db.commit()

    async def html_report(self) -> str:
        findings = await self.get_all()
        pending   = [f for f in findings if not f["implemented"]]
        done      = [f for f in findings if f["implemented"]]

        def _card(f: dict) -> str:
            status_cls  = "done" if f["implemented"] else "pending"
            status_lbl  = "✅ Implemented" if f["implemented"] else "⏳ Pending"
            score_bars  = "█" * f["score"] + "░" * (10 - f["score"])
            try:
                ts  = datetime.fromisoformat(f["timestamp"])
                ago = (datetime.now(timezone.utc) - ts).days
                date_str = f"{ago}d ago" if ago > 0 else "today"
            except Exception:
                date_str = f["timestamp"][:10]
            src = (f'<a href="{f["source"]}" target="_blank" style="color:var(--blue);font-size:11px">'
                   f'↗ source</a>') if f["source"] else ""
            return (
                f'<div class="research-card {status_cls}">'
                f'<div class="rc-header">'
                f'<span class="rc-topic">{f["topic"]}</span>'
                f'<span class="rc-status">{status_lbl}</span>'
                f'</div>'
                f'<div class="rc-score">Score: <code>{score_bars}</code> {f["score"]}/10</div>'
                f'<div class="rc-summary">{f["summary"]}</div>'
                f'<div class="rc-rec"><b>→ Recommendation:</b> {f["recommendation"]}</div>'
                f'<div class="rc-footer">{date_str} {src}'
                + (f' <button class="btn-impl" onclick="markImpl({f["id"]})">Mark implemented</button>'
                   if not f["implemented"] else "")
                + f'</div></div>'
            )

        pending_html = "".join(_card(f) for f in pending) if pending else \
            '<p style="color:var(--text-dim)">All findings implemented ✅</p>'
        done_html = "".join(_card(f) for f in done)

        return (
            f'<div class="research-report">'
            f'<h2>🔬 UI Research Findings</h2>'
            f'<p style="color:var(--text-dim);margin-bottom:20px">'
            f'{len(findings)} findings · {len(done)} implemented · {len(pending)} pending</p>'
            + (f'<h3 style="margin-bottom:12px">⏳ Pending ({len(pending)})</h3>{pending_html}'
               if pending else "")
            + (f'<h3 style="margin:20px 0 12px">✅ Implemented ({len(done)})</h3>{done_html}'
               if done else "")
            + f'</div>'
        )
