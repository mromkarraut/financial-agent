"""
UITestingAgent — verifies that web UI changes are correctly implemented.

Each test makes a real HTTP request to the local server and asserts that
specific content is present in the response.  Results are stored in
`ui_test_results` (SQLite) so trends can be tracked over time.

Usage:
    agent = UITestingAgent(base_url="http://localhost:8000")
    report = await agent.run_all()
    print(report)

Or trigger via the /test route in server.py.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiosqlite
import httpx

import config

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Individual test definitions ───────────────────────────────────────────────

class UITest:
    def __init__(self, name: str, desc: str):
        self.name = name
        self.desc = desc

    async def run(self, client: httpx.AsyncClient) -> tuple[bool, str]:
        raise NotImplementedError


class TestHomepageLoads(UITest):
    def __init__(self):
        super().__init__("homepage_loads", "GET / returns 200 with HTMX script tag")

    async def run(self, client):
        r = await client.get("/")
        if r.status_code != 200:
            return False, f"Status {r.status_code}"
        if "htmx.org" not in r.text:
            return False, "HTMX script tag missing"
        return True, "OK"


class TestHamburgerPresent(UITest):
    def __init__(self):
        super().__init__("hamburger_mobile", "☰ hamburger button present for mobile sidebar")

    async def run(self, client):
        r = await client.get("/")
        if "hamburger" not in r.text:
            return False, "No .hamburger CSS class found"
        if "toggleSidebar" not in r.text:
            return False, "toggleSidebar() JS function missing"
        if "sidebar-backdrop" not in r.text:
            return False, "sidebar-backdrop element missing"
        return True, "OK"


class TestThemeToggle(UITest):
    def __init__(self):
        super().__init__("theme_toggle", "3 theme buttons and phosphor/amber CSS present")

    async def run(self, client):
        r = await client.get("/")
        for theme in ("phosphor", "amber", "setTheme"):
            if theme not in r.text:
                return False, f"'{theme}' not found in page"
        for btn_id in ("t-default", "t-phosphor", "t-amber"):
            if btn_id not in r.text:
                return False, f"Theme button #{btn_id} missing"
        return True, "OK"


class TestMobileCSS(UITest):
    def __init__(self):
        super().__init__("mobile_css", "Mobile breakpoint @media (max-width: 660px) present")

    async def run(self, client):
        r = await client.get("/")
        if "max-width: 660px" not in r.text:
            return False, "Mobile breakpoint missing"
        if "overflow-x: auto" not in r.text:
            return False, "overflow-x:auto missing (pre blocks won't scroll on mobile)"
        return True, "OK"


class TestSearchEndpoint(UITest):
    def __init__(self):
        super().__init__("search_returns_html", "POST /search returns HTML fragment with options output")

    async def run(self, client):
        r = await client.post("/search", data={"ticker": "AAPL", "outlook": "bullish"}, timeout=90)
        if r.status_code != 200:
            return False, f"Status {r.status_code}"
        html = r.text
        checks = [
            ("result-wrap",        "result-wrap div missing"),
            ("Options Research",   "Agent header missing"),
            ("The Legs",           "'The Legs' section missing"),
            ("How it works",       "'How it works' section missing"),
            ("Key Numbers",        "'Key Numbers' section missing"),
            ("SELL",               "SELL leg in Key Numbers missing"),
            ("BUY",                "BUY leg in Key Numbers missing"),
            ("Probability",        "Probability rows missing"),
            ("history-list",       "OOB sidebar swap missing"),
        ]
        for token, msg in checks:
            if token not in html:
                return False, msg
        return True, "OK"


class TestLegsInKeyNumbers(UITest):
    def __init__(self):
        super().__init__("legs_in_key_numbers", "SELL/BUY lines appear inside Key Numbers <code> block")

    async def run(self, client):
        r = await client.post("/search", data={"ticker": "AAPL", "outlook": "bullish"}, timeout=90)
        html = r.text
        # Find the <code> block after Key Numbers
        kn_idx = html.find("Key Numbers")
        if kn_idx == -1:
            return False, "Key Numbers section not found"
        code_start = html.find("<code>", kn_idx)
        code_end   = html.find("</code>", code_start)
        if code_start == -1 or code_end == -1:
            return False, "<code> block not found after Key Numbers"
        code_block = html[code_start:code_end]
        if "SELL" not in code_block:
            return False, "SELL leg not inside <code> block"
        if "BUY" not in code_block:
            return False, "BUY leg not inside <code> block"
        if "@" not in code_block:
            return False, "Price per share (@) missing from legs"
        return True, "OK"


class TestRecommendationStructure(UITest):
    def __init__(self):
        super().__init__("recommendation_structure", "Recommendation has divider, emoji stats, spaced sections")

    async def run(self, client):
        r = await client.post("/search", data={"ticker": "AAPL", "outlook": "bullish"}, timeout=90)
        html = r.text
        checks = [
            ("🏆",                 "Trophy emoji missing"),
            ("──────",             "Divider line missing"),
            ("📊",                 "POP stat emoji missing"),
            ("💰",                 "Credit/risk emoji missing"),
            ("📈",                 "ROC emoji missing"),
            ("The Legs :",         "'The Legs :' label format missing"),
            ("protection",         "(protection) label on buy leg missing"),
        ]
        for token, msg in checks:
            if token not in html:
                return False, msg
        return True, "OK"


class TestResultHistoryEndpoint(UITest):
    def __init__(self):
        super().__init__("result_history", "GET /api/history returns JSON list")

    async def run(self, client):
        r = await client.get("/api/history")
        if r.status_code != 200:
            return False, f"Status {r.status_code}"
        try:
            data = r.json()
            if not isinstance(data, list):
                return False, f"Expected list, got {type(data)}"
        except Exception as e:
            return False, f"Invalid JSON: {e}"
        return True, f"OK — {len(data)} history items"


class TestUIResearchPage(UITest):
    def __init__(self):
        super().__init__("ui_research_page", "GET /ui-research shows all 5 findings implemented")

    async def run(self, client):
        r = await client.get("/ui-research")
        if r.status_code != 200:
            return False, f"Status {r.status_code}"
        if "research-card" not in r.text:
            return False, "No research cards found"
        if "✅ Implemented" not in r.text:
            return False, "No implemented findings shown"
        if "⏳ Pending" in r.text:
            return False, "Pending findings still showing — not all implemented"
        return True, "OK — all findings implemented"


class TestColorizeJS(UITest):
    def __init__(self):
        super().__init__("colorize_js", "enhanceOutput() JS present and colorizes +/- amounts")

    async def run(self, client):
        r = await client.get("/")
        if "enhanceOutput" not in r.text:
            return False, "enhanceOutput() function missing"
        if "star-row" not in r.text:
            return False, ".star-row CSS class missing"
        if "atm-row" not in r.text:
            return False, ".atm-row CSS class missing"
        if 'class="pos"' not in r.text and '"pos"' not in r.text:
            return False, ".pos color class missing"
        return True, "OK"


# ── Agent ─────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    TestHomepageLoads(),
    TestHamburgerPresent(),
    TestThemeToggle(),
    TestMobileCSS(),
    TestColorizeJS(),
    TestUIResearchPage(),
    TestResultHistoryEndpoint(),
    TestSearchEndpoint(),
    TestLegsInKeyNumbers(),
    TestRecommendationStructure(),
]


class UITestingAgent:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")

    async def run_all(self) -> str:
        await self._ensure_table()
        results: list[dict] = []

        async with httpx.AsyncClient(base_url=self.base_url) as client:
            for test in ALL_TESTS:
                t0 = time.monotonic()
                try:
                    passed, detail = await test.run(client)
                except Exception as exc:
                    passed, detail = False, f"Exception: {exc}"
                ms = int((time.monotonic() - t0) * 1000)
                results.append({
                    "name": test.name, "desc": test.desc,
                    "passed": passed, "detail": detail, "ms": ms,
                })
                await self._save(test.name, passed, detail, ms)

        return self._fmt_report(results)

    async def _ensure_table(self) -> None:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ui_test_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    test_name   TEXT    NOT NULL,
                    passed      INTEGER NOT NULL,
                    detail      TEXT    DEFAULT '',
                    duration_ms INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def _save(self, name: str, passed: bool, detail: str, ms: int) -> None:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "INSERT INTO ui_test_results (timestamp, test_name, passed, detail, duration_ms) "
                "VALUES (?,?,?,?,?)",
                (_utcnow(), name, int(passed), detail, ms),
            )
            await db.commit()

    def _fmt_report(self, results: list[dict]) -> str:
        passed = sum(1 for r in results if r["passed"])
        total  = len(results)
        lines  = [
            f"<b>UI Test Report</b>  {passed}/{total} passed\n"
        ]
        for r in results:
            icon = "✅" if r["passed"] else "❌"
            lines.append(
                f"{icon} <b>{r['desc']}</b>\n"
                f"   <i>{r['detail']}  ({r['ms']}ms)</i>"
            )
        if passed == total:
            lines.append("\n✅ <b>All tests passed.</b>")
        else:
            failed = [r for r in results if not r["passed"]]
            lines.append(f"\n⚠️ <b>{len(failed)} test(s) failed.</b>")
        return "\n".join(lines)
