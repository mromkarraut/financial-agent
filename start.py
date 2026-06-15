#!/usr/bin/env python3
"""
start.py — launch the full financial agent stack

  python start.py                # all services + IBKR gateway (paper trading)
  python start.py --no-lms      # skip LM Studio (already running)
  python start.py --no-gateway  # skip IBKR gateway (already running or not needed)
  python start.py --live        # use LIVE trading account (USE WITH CAUTION)
  python start.py --browser     # also start browser-tools-server on port 3025
  python start.py --web-only    # web dashboard only, no Telegram bot

Logs → logs/web.log, logs/gateway.log, logs/browser.log
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT    = os.path.dirname(os.path.abspath(__file__))
VENV    = "/home/omkar/venvs/bin"
PYTHON  = f"{VENV}/python"
UVICORN = f"{VENV}/uvicorn"
LMS     = "/mnt/c/Users/User/AppData/Local/Programs/LM Studio/resources/app/.webpack/lms.exe"
JAVA    = "/mnt/c/Program Files/Java/jre1.8.0_461/bin/java.exe"
GW_DIR  = os.path.join(ROOT, "ibkr_gateway")
DB_PATH = os.path.join(ROOT, "db", "state.db")
LOG_DIR = os.path.join(ROOT, "logs")

# ── ANSI ───────────────────────────────────────────────────────────────────────
G   = "\033[32m"
Y   = "\033[33m"
R   = "\033[31m"
B   = "\033[34m"
M   = "\033[35m"   # magenta — used for paper trading warnings
DIM = "\033[2m"
RST = "\033[0m"

def ok(msg):    print(f"  {G}✓{RST}  {msg}")
def warn(msg):  print(f"  {Y}⚠{RST}  {msg}")
def err(msg):   print(f"  {R}✗{RST}  {msg}")
def step(msg):  print(f"\n{B}▶{RST}  {msg}")
def paper(msg): print(f"  {M}📄{RST}  {msg}")

# ── Process registry ───────────────────────────────────────────────────────────
_procs: list[tuple[str, subprocess.Popen]] = []


def _launch(name: str, cmd: list[str], cwd: str = ROOT,
            log_file: str | None = None, **kwargs) -> subprocess.Popen:
    if log_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = open(log_file, "a")
        p  = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=fh, **kwargs)
        ok(f"{name}  {DIM}(logs → {os.path.relpath(log_file)}){RST}")
    else:
        p = subprocess.Popen(cmd, cwd=cwd, **kwargs)
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


# ── Gateway helpers ────────────────────────────────────────────────────────────

def _port_reachable(port: int) -> bool:
    """Quick TCP check — is something listening on localhost:port?"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: int = 45) -> bool:
    """Poll until the given TCP port is open or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_reachable(port):
            print()
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print()
    return False


def _open_browser(url: str) -> None:
    """Open URL in Windows Chrome via WSL interop."""
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # non-critical


# ── Steps ──────────────────────────────────────────────────────────────────────

def start_lm_studio() -> None:
    step("LM Studio API server")
    if not os.path.exists(LMS):
        warn(f"lms.exe not found — skipping")
        return
    result = subprocess.run(
        [LMS, "server", "start"], capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0 or "already" in (result.stdout + result.stderr).lower():
        ok("LM Studio API on http://localhost:1234")
    else:
        warn(f"lms returned {result.returncode}: {result.stderr.strip()[:80]}")


def init_db() -> None:
    step("Database")
    if os.path.exists(DB_PATH):
        ok(f"state.db exists ({os.path.getsize(DB_PATH) // 1024} KB)")
        return
    subprocess.run(
        [PYTHON, "-c",
         "import asyncio; from db.database import init_db; asyncio.run(init_db())"],
        cwd=ROOT, check=True,
    )
    ok("state.db created")


def start_tws_gateway(paper: bool = True) -> bool:
    """
    Launch IB Gateway desktop app (TWS socket API, port 4002/4001).
    Required for ib_insync MCP servers and IBKR options chain data.
    Waits up to 120s for the user to log in via the GUI window.
    """
    port = 4002 if paper else 4001
    step(f"IB Gateway (TWS socket, port {port})  [{'📄 PAPER' if paper else R+'⚠ LIVE'+RST}]")

    if _port_reachable(port):
        ok(f"IB Gateway already running on port {port}")
        return True

    # Resolve Windows exe path
    gw_exe_win = os.environ.get("IBKR_GATEWAY_EXE", r"C:\Jts\ibgateway\1039\ibgateway.exe")
    gw_exe_wsl = gw_exe_win.replace("C:\\", "/mnt/c/").replace("\\", "/")

    if not os.path.exists(gw_exe_wsl):
        warn(f"IB Gateway not found at {gw_exe_win}")
        warn("Install from: https://www.interactivebrokers.com/en/trading/ibgateway-stable.php")
        warn("Options research will fall back to yfinance until IB Gateway is available.")
        return False

    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", gw_exe_win],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ok(f"IB Gateway launched")

    mode_label = "PAPER" if paper else "LIVE"
    acct_hint  = "account starts with DU" if paper else "LIVE account — REAL MONEY"
    print(f"\n  {M}{'━'*56}{RST}")
    print(f"  {M}  IB Gateway login required ({mode_label}){RST}")
    print(f"  {M}  1. Log in via the IB Gateway window that just opened{RST}")
    print(f"  {M}  2. Use your {('paper trading' if paper else 'LIVE')} credentials ({acct_hint}){RST}")
    print(f"  {M}  3. Enable API: Configure → Settings → API → Socket port {port}{RST}")
    if not paper:
        print(f"  {R}  ⚠  LIVE ACCOUNT — orders will use real money{RST}")
    print(f"  {M}{'━'*56}{RST}\n")

    print(f"  {DIM}Waiting for IB Gateway login (up to 120s).", end="", flush=True)
    ready = _wait_for_port(port, timeout=120)

    if ready:
        ok(f"IB Gateway ready — ib_insync will connect on port {port}")
        return True
    else:
        warn(f"IB Gateway did not open port {port} in time.")
        warn("Log in when you're ready — options research will connect automatically.")
        return False


def start_cp_gateway(paper: bool = True) -> bool:
    """
    Start the IBKR CP Gateway JAR (REST API, port 5000).
    Used by the legacy ibkr MCP server and agents/ibkr_agent.py.
    """
    step(f"IBKR CP Gateway (REST, port 5000)  [{'📄 PAPER' if paper else R+'⚠ LIVE'+RST}]")

    if _port_reachable(5000):
        ok(f"CP Gateway already running on https://localhost:5000")
        return True

    if not os.path.isdir(GW_DIR):
        warn(f"ibkr_gateway/ not found — run start.py from the project root")
        return False

    # The gateway lives on the C: drive so cmd.exe can use normal Windows paths.
    # C:\ibkr_gateway is a copy of ibkr_gateway/ — update it when files change.
    WIN_GW = r"C:\ibkr_gateway"
    if not os.path.exists("/mnt/c/ibkr_gateway/start_gateway.bat"):
        warn("C:\\ibkr_gateway not found — copying gateway files...")
        subprocess.run(["cp", "-r", GW_DIR, "/mnt/c/ibkr_gateway"], check=True)
        ok("Gateway copied to C:\\ibkr_gateway")

    cmd = ["cmd.exe", "/c", f'cd /d "{WIN_GW}" && start_gateway.bat']
    os.makedirs(LOG_DIR, exist_ok=True)
    log_fh = open(os.path.join(LOG_DIR, "gateway.log"), "a")
    p = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, cwd="/mnt/c/Windows")
    _procs.append((f"CP Gateway ({'paper' if paper else 'LIVE'})", p))
    ok(f"CP Gateway starting  {DIM}(logs → logs/gateway.log){RST}")

    print(f"  {DIM}Waiting for CP Gateway to start", end="", flush=True)
    ready = _wait_for_port(5000, timeout=45)

    if ready:
        ok(f"CP Gateway ready at https://localhost:5000")
        mode_label = "PAPER" if paper else "LIVE"
        print(f"\n  {M}{'━'*54}{RST}")
        print(f"  {M}  CP Gateway {mode_label} — browser login required{RST}")
        print(f"  {M}  Open Chrome → https://localhost:5000{RST}")
        if paper:
            print(f"  {M}  Use your PAPER TRADING credentials (account starts DU){RST}")
        else:
            print(f"  {R}  ⚠ LIVE ACCOUNT — real money at risk{RST}")
        print(f"  {M}{'━'*54}{RST}\n")
        _open_browser("https://localhost:5000")
        time.sleep(3)
        return True
    else:
        warn("CP Gateway did not start in time — check logs/gateway.log")
        warn("You can still log in manually once it's ready: https://localhost:5000")
        return False


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


# ── Banner / status ────────────────────────────────────────────────────────────

def _banner(paper: bool) -> None:
    mode = f"{M}📄 PAPER TRADING{RST}" if paper else f"{R}⚠ LIVE TRADING{RST}"
    print(f"""
{B}╔══════════════════════════════════════╗
║      Financial Agent — starting      ║
╚══════════════════════════════════════╝{RST}
  IBKR mode: {mode}
  (set IBKR_PAPER_TRADING=false in .env to switch to live)
""")


def _status_line(paper: bool) -> None:
    mode    = f"{M}PAPER{RST}" if paper else f"{R}LIVE ⚠{RST}"
    tws_port = 4002 if paper else 4001
    tws_up  = _port_reachable(tws_port)
    tws_str = f"port {tws_port}  [{mode}]  {'✅' if tws_up else '❌ not connected'}"
    print(f"""
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
  Web dashboard  →  http://localhost:8000
  IB Gateway     →  {tws_str}
  MCP servers    →  auto-started by Claude Code
  Logs           →  logs/
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
""")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Start the financial agent stack")
    ap.add_argument("--no-lms",     action="store_true", help="Skip LM Studio (already running)")
    ap.add_argument("--no-gateway", action="store_true", help="Skip IBKR gateway (already running)")
    ap.add_argument("--live",       action="store_true", help="Use LIVE trading account (real money!)")
    ap.add_argument("--browser",    action="store_true", help="Also start browser-tools-server on :3025")
    ap.add_argument("--web-only",   action="store_true", help="Web dashboard only, no Telegram bot")
    args = ap.parse_args()

    # Paper trading is the default; --live overrides
    paper = not args.live

    _banner(paper)

    if not args.no_lms:
        start_lm_studio()

    init_db()

    if not args.no_gateway:
        start_tws_gateway(paper=paper)

    start_web()

    if args.browser:
        start_browser_tools()

    time.sleep(1.5)

    if not args.web_only:
        _status_line(paper)
        bot = start_bot()
        bot.wait()
    else:
        _status_line(paper)
        print(f"{DIM}Web-only mode. Press Ctrl+C to stop.{RST}\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    _stop_all()


if __name__ == "__main__":
    main()
