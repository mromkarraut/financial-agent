"""
Quick TWS connection test. Run with:
  source /home/omkar/venvs/bin/activate && python test_tws_connection.py
"""
import asyncio
import socket
import os
from dotenv import load_dotenv

load_dotenv()

HOST = os.environ.get("IBKR_TWS_HOST", "127.0.0.1")
PORT = int(os.environ.get("IBKR_TWS_PORT", "7497"))
TIMEOUT = 60  # seconds — longer than the default 20s


def tcp_probe() -> str:
    try:
        with socket.create_connection((HOST, PORT), timeout=3):
            return "OPEN"
    except OSError as e:
        return f"FAIL ({e})"


async def main():
    print(f"Target: {HOST}:{PORT}  (timeout={TIMEOUT}s)\n")

    print(f"[1/3] TCP probe ... ", end="", flush=True)
    result = tcp_probe()
    print(result)
    if "FAIL" in result:
        print("\n  TWS is not reachable at TCP level.")
        print("  Check:")
        print("  • TWS is running and logged in on the Windows machine")
        print("  • File → Global Configuration → API → Settings → Enable socket, port 7497")
        print(f'  • "Allow connections from localhost only" is UNCHECKED')
        print(f"  • TrustedIPs includes this machine's IP in jts.ini")
        return

    print("[2/3] Raw API handshake (5s) ... ", end="", flush=True)
    try:
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.sendall(b"API\x00")
        s.settimeout(5)
        data = s.recv(1024)
        s.close()
        print(f"got {len(data)} bytes: {data[:40]!r}")
    except socket.timeout:
        print("no response — 'Allow localhost only' is probably still checked")
        return
    except Exception as e:
        print(f"error: {e}")
        return

    print(f"[3/3] ib_insync connect (timeout={TIMEOUT}s) ... ", end="", flush=True)
    from tools.ibkr_tws import connect_ib
    try:
        ib = await connect_ib(client_id=9, timeout=TIMEOUT)
        accts = ib.managedAccounts()
        ver = ib.client.serverVersion()
        ib.disconnect()
        print(f"OK\n\nConnected! accounts={accts}  serverVersion={ver}")
    except Exception as e:
        print(f"FAILED\n\n{e}")


asyncio.run(main())
