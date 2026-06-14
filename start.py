#!/usr/bin/env python3
"""
start.py — launch the full financial agent stack

  python start.py              # all services
  python start.py --no-lms    # skip LM Studio (already running)
  python start.py --browser   # also start browser-tools-server on port 3025
  python start.py --web-only  # web dashboard only (no Telegram bot)

Logs → logs/web.log, logs/browser.log  (bot runs in foreground so output is live)
"""

import argparse
import os
import signal
import subprocess
import sys
import time

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT         = os.path.dirname(os.path.abspath(__file__))
VENV         = "/home/omkar/venvs/bin"
PYTHON       = f"{VENV}/python"
UVICORN      = f"{VENV}/uvicorn"
LMS          = "/mnt/c/Users/User/AppData/Local/Programs/LM Studio/resources/app/.webpack/lms.exe"
DB_PATH      = os.path.join(ROOT, "db", "state.db")
LOG_DIR      = os.path.join(ROOT, "logs")

# ── ANSI helpers ───────────────────────────────────────────────────────────────
G  = "\033[32m"   # green
Y  = "\033[33m"   # yellow
R  = "\033[31m"   # red
B  = "\033[34m"   # blue
DIM= "\033[2m"
RST= "\033[0m"

def ok(msg):   print(f"  {G}✓{RST}  {msg}")
def warn(msg): print(f"  {Y}⚠{RST}  {msg}")
def err(msg):  print(f"  {R}✗{RST}  {msg}")
def step(msg): print(f"\n{B}▶{RST}  {msg}")

# ── Process registry ───────────────────────────────────────────────────────────
_procs: list[tuple[str, subprocess.Popen]] = []


def _launch(name: str, cmd: list[str], log_file: str | None = None, **kwargs) -> subprocess.Popen:
    if log_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = open(log_file, "a")
        p  = subprocess.Popen(cmd, cwd=ROOT, stdout=fh, stderr=fh, **kwargs)
        ok(f"{name}  {DIM}(logs → {os.path.relpath(log_file)}){RST}")
    else:
        p = subprocess.Popen(cmd, cwd=ROOT, **kwargs)
        ok(name)
    _procs.append((name, p))
    return p


def _stop_all(sig=None, frame=None) -> None:
    print(f"\n\n{Y}Shutting down all services…{RST}")
    for name, p in reversed(_procs):
        try:
            p.terminate()
            print(f"  {DIM}stopped {name}{RST}")
        except Exception:
            pass
    for _, p in _procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGINT,  _stop_all)
signal.signal(signal.SIGTERM, _stop_all)


# ── Steps ──────────────────────────────────────────────────────────────────────

def start_lm_studio() -> None:
    step("LM Studio API server")
    if not os.path.exists(LMS):
        warn(f"lms.exe not found at {LMS} — skipping")
        return
    result = subprocess.run(
        [LMS, "server", "start"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0 or "already" in (result.stdout + result.stderr).lower():
        ok("LM Studio API running on http://localhost:1234")
    else:
        warn(f"lms server start returned {result.returncode}: {result.stderr.strip()[:80]}")


def init_db() -> None:
    step("Database")
    if os.path.exists(DB_PATH):
        ok(f"state.db exists ({os.path.getsize(DB_PATH) // 1024} KB)")
        return
    ok("Initialising state.db…")
    subprocess.run(
        [PYTHON, "-c",
         "import asyncio; from db.database import init_db; asyncio.run(init_db())"],
        cwd=ROOT, check=True,
    )
    ok("state.db created")


def start_web() -> subprocess.Popen:
    step("Web dashboard  →  http://localhost:8000")
    return _launch(
        "uvicorn (port 8000)",
        [UVICORN, "server:app", "--host", "0.0.0.0", "--port", "8000"],
        log_file=os.path.join(LOG_DIR, "web.log"),
    )


def start_browser_tools() -> None:
    step("browser-tools-server  →  http://localhost:3025")
    npx = subprocess.run(["which", "npx"], capture_output=True, text=True).stdout.strip()
    if not npx:
        warn("npx not found — skipping browser-tools-server")
        return
    _launch(
        "browser-tools-server (port 3025)",
        ["npx", "@agentdeskai/browser-tools-server@latest"],
        log_file=os.path.join(LOG_DIR, "browser.log"),
    )


def start_bot() -> subprocess.Popen:
    step("Telegram bot  (foreground — Ctrl+C to stop everything)")
    print()
    p = subprocess.Popen([PYTHON, "main.py"], cwd=ROOT)
    _procs.append(("telegram bot", p))
    return p


# ── Banner ─────────────────────────────────────────────────────────────────────

def _banner() -> None:
    print(f"""
{B}╔══════════════════════════════════════╗
║      Financial Agent — starting      ║
╚══════════════════════════════════════╝{RST}""")


def _status_line() -> None:
    print(f"""
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
  Web dashboard  →  http://localhost:8000
  MCP servers    →  auto-started by Claude Code (.claude/settings.json)
  Logs           →  logs/web.log
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
""")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Start the financial agent stack")
    ap.add_argument("--no-lms",   action="store_true", help="Skip LM Studio (already running)")
    ap.add_argument("--browser",  action="store_true", help="Also start browser-tools-server on port 3025")
    ap.add_argument("--web-only", action="store_true", help="Start web dashboard only, no Telegram bot")
    args = ap.parse_args()

    _banner()

    if not args.no_lms:
        start_lm_studio()

    init_db()
    start_web()

    if args.browser:
        start_browser_tools()

    # Brief pause so uvicorn is ready before the bot starts
    time.sleep(1.5)

    if not args.web_only:
        _status_line()
        bot = start_bot()
        bot.wait()
    else:
        _status_line()
        print(f"{DIM}Web-only mode. Press Ctrl+C to stop.{RST}\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    _stop_all()


if __name__ == "__main__":
    main()
