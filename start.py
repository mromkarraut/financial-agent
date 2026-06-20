#!/usr/bin/env python3
"""
start.py — launch the full financial agent stack

  python start.py            # all services (paper trading)
  python start.py --no-lms  # skip LM Studio (already running)
  python start.py --live    # use LIVE trading account (USE WITH CAUTION)
  python start.py --browser # also start browser-tools-server on port 3025
  python start.py --web-only # web dashboard only, no Telegram bot

Logs → logs/web.log, logs/browser.log
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT    = os.path.dirname(os.path.abspath(__file__))
VENV    = "/home/omkar/venvs/bin"
PYTHON  = f"{VENV}/python"
UVICORN = f"{VENV}/uvicorn"
LMS     = "/home/omkar/.lmstudio/bin/lms"
JAVA    = "/mnt/c/Program Files/Java/jre1.8.0_461/bin/java.exe"
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


# ── TWS helpers ───────────────────────────────────────────────────────────────

def _ibkr_host() -> str:
    """Return IBKR_TWS_HOST from env, falling back to 127.0.0.1."""
    return os.environ.get("IBKR_TWS_HOST", "127.0.0.1")


def _local_ip() -> str:
    """Best-guess LAN IP of this machine (for printing TrustedIPs instructions)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "???"


def _port_reachable(port: int, host: str | None = None) -> bool:
    """Quick TCP check — is something listening on host:port?"""
    try:
        with socket.create_connection((host or _ibkr_host(), port), timeout=1.5):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: int = 45, host: str | None = None) -> bool:
    """Poll until the given TCP port is open or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_reachable(port, host=host):
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


def _is_wsl() -> bool:
    """True when running inside WSL (Windows Subsystem for Linux)."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _ibkr_port(paper: bool) -> int:
    """Read IBKR_TWS_PORT from env. TWS defaults: paper=7497, live=7496."""
    default = 7497 if paper else 7496
    return int(os.environ.get("IBKR_TWS_PORT", default))


def start_tws_gateway(paper: bool = True) -> bool:
    """
    Connect to TWS (socket API).

    WSL mode  — launches the Windows TWS exe via cmd.exe, then waits for the
                user to log in through the GUI window.
    Native Linux mode — TWS must be started manually on the Windows machine;
                this function checks connectivity and prints setup instructions.
    """
    host = _ibkr_host()
    port = _ibkr_port(paper)
    step(f"TWS (socket, {host}:{port})  [{'📄 PAPER' if paper else R+'⚠ LIVE'+RST}]")

    if _port_reachable(port):
        ok(f"TWS reachable at {host}:{port}")
        return True

    # ── Native Linux (non-WSL): remote Windows machine ────────────────────────
    if not _is_wsl():
        local_ip  = _local_ip()
        acct_hint = "paper trading credentials (account starts DU)" if paper else "LIVE credentials — REAL MONEY"
        print(f"\n  {M}{'━'*62}{RST}")
        print(f"  {M}  TWS is not reachable at {host}:{port}{RST}")
        print(f"  {M}  Start TWS on the Windows machine, then:{RST}")
        print(f"  {M}  1. Log in with your {acct_hint}{RST}")
        print(f"  {M}  2. File → Global Configuration → API → Settings{RST}")
        print(f"  {M}     ✓ Enable ActiveX and Socket Clients{RST}")
        print(f"  {M}     Socket port: {port}{RST}")
        print(f"  {M}     ✗ Uncheck \"Allow connections from localhost only\"{RST}")
        print(f"  {M}  3. Add this server's IP to jts.ini TrustedIPs:{RST}")
        print(f"  {M}     C:\\Jts\\<ver>\\jts.ini{RST}")
        print(f"  {M}     TrustedIPs={local_ip}{RST}")
        print(f"  {M}  4. In .env:  IBKR_TWS_HOST=<windows-ip>  IBKR_TWS_PORT={port}{RST}")
        if not paper:
            print(f"  {R}  ⚠  LIVE ACCOUNT — orders will use real money{RST}")
        print(f"  {M}{'━'*62}{RST}\n")

        print(f"  {DIM}Waiting for TWS to become reachable at {host}:{port} (up to 120s).", end="", flush=True)
        ready = _wait_for_port(port, timeout=120)
        if ready:
            ok(f"TWS reachable — ib_insync will connect on {host}:{port}")
            return True
        else:
            warn(f"TWS not reachable at {host}:{port} after 120s.")
            warn("Start TWS on the Windows machine — options research will connect automatically.")
            return False

    # ── WSL mode: launch the Windows TWS exe via cmd.exe ─────────────────────
    tws_exe_win = os.environ.get("IBKR_TWS_EXE", r"C:\Jts\tws.exe")
    tws_exe_wsl = tws_exe_win.replace("C:\\", "/mnt/c/").replace("\\", "/")

    if not os.path.exists(tws_exe_wsl):
        warn(f"TWS not found at {tws_exe_win}")
        warn("Set IBKR_TWS_EXE in .env to the correct path (e.g. C:\\Jts\\tws.exe)")
        warn("Options research will fall back to yfinance until TWS is available.")
        return False

    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", tws_exe_win],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ok("TWS launched")

    mode_label = "PAPER" if paper else "LIVE"
    acct_hint  = "account starts with DU" if paper else "LIVE account — REAL MONEY"
    print(f"\n  {M}{'━'*56}{RST}")
    print(f"  {M}  TWS login required ({mode_label}){RST}")
    print(f"  {M}  1. Log in via the TWS window that just opened{RST}")
    print(f"  {M}  2. Use your {('paper trading' if paper else 'LIVE')} credentials ({acct_hint}){RST}")
    print(f"  {M}  3. Enable API: File → Global Configuration → API → Socket port {port}{RST}")
    if not paper:
        print(f"  {R}  ⚠  LIVE ACCOUNT — orders will use real money{RST}")
    print(f"  {M}{'━'*56}{RST}\n")

    print(f"  {DIM}Waiting for TWS login (up to 120s).", end="", flush=True)
    ready = _wait_for_port(port, timeout=120)

    if ready:
        ok(f"TWS ready — ib_insync will connect on port {port}")
        return True
    else:
        warn(f"TWS did not open port {port} in time.")
        warn("Log in when you're ready — options research will connect automatically.")
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
    mode     = f"{M}PAPER{RST}" if paper else f"{R}LIVE ⚠{RST}"
    tws_port = _ibkr_port(paper)
    tws_host = _ibkr_host()
    tws_up   = _port_reachable(tws_port)
    tws_str  = f"{tws_host}:{tws_port}  [{mode}]  {'✅' if tws_up else '❌ not connected'}"
    print(f"""
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
  Web dashboard  →  http://localhost:8000
  TWS            →  {tws_str}
  MCP servers    →  auto-started by Claude Code
  Logs           →  logs/
{G}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RST}
""")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Start the financial agent stack")
    ap.add_argument("--no-lms",  action="store_true", help="Skip LM Studio (already running)")
    ap.add_argument("--live",    action="store_true", help="Use LIVE trading account (real money!)")
    ap.add_argument("--browser",    action="store_true", help="Also start browser-tools-server on :3025")
    ap.add_argument("--web-only",   action="store_true", help="Web dashboard only, no Telegram bot")
    args = ap.parse_args()

    # Paper trading is the default; --live overrides
    paper = not args.live

    _banner(paper)

    if not args.no_lms:
        start_lm_studio()

    init_db()

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
