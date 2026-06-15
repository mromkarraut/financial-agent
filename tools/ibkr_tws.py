"""
IB Gateway / TWS socket connection helpers using ib_insync.

Each MCP server calls connect_ib(client_id) to get its own IB instance.
Connections are cached per client_id for the lifetime of the process.

Paper trading port: 4002 (IB Gateway) / 7497 (TWS)
Live trading port:  4001 (IB Gateway) / 7496 (TWS)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from ib_insync import IB, Contract, ComboLeg, LimitOrder, Stock, Option

logger = logging.getLogger(__name__)

_connections: dict[tuple[int, int], IB] = {}  # (client_id, loop_id) → IB


async def connect_ib(client_id: int, timeout: float = 20.0) -> IB:
    """Return a connected IB instance for this client_id. Reconnects if dropped.
    Keyed by (client_id, event_loop_id) so uvicorn and test scripts never share connections."""
    cur_loop = asyncio.get_running_loop()
    key = (client_id, id(cur_loop))

    if key in _connections:
        ib = _connections[key]
        if ib.isConnected():
            return ib
        _connections.pop(key)

    ib = IB()
    try:
        await ib.connectAsync(
            config.IBKR_TWS_HOST,
            config.IBKR_TWS_PORT,
            clientId=client_id,
            timeout=timeout,
        )
        _connections[key] = ib
        logger.info("IB connected clientId=%d port=%d", client_id, config.IBKR_TWS_PORT)
    except Exception as exc:
        raise ConnectionError(
            f"Cannot connect to IB Gateway at {config.IBKR_TWS_HOST}:{config.IBKR_TWS_PORT} "
            f"(clientId={client_id}). "
            f"Start IB Gateway and log in first. Error: {exc}"
        )
    return ib


def is_connected(client_id: int) -> bool:
    import asyncio as _aio
    try:
        loop = _aio.get_running_loop()
        ib = _connections.get((client_id, id(loop)))
    except RuntimeError:
        ib = None
    return ib is not None and ib.isConnected()


def paper_label() -> str:
    return "📄 PAPER TRADING" if config.IBKR_PAPER_TRADING else "⚠ LIVE TRADING"


def is_paper_account(account_id: str) -> bool:
    return account_id.upper().startswith("DU")


async def get_account_id(ib: IB) -> str:
    """Return the first managed account."""
    accounts = ib.managedAccounts()
    return accounts[0] if accounts else ""


def make_option_contract(
    symbol: str,
    expiry: str,       # YYYYMMDD
    right: str,        # "P" or "C"
    strike: float,
    exchange: str = "SMART",
    currency: str = "USD",
) -> Contract:
    c = Option(symbol, expiry.replace("-", ""), strike, right, exchange, currency=currency)
    return c


def make_vertical_spread(
    symbol: str,
    short_conid: int,
    long_conid: int,
    is_credit: bool,
    exchange: str = "SMART",
) -> Contract:
    """
    Build a BAG (combo) contract for a vertical spread.
    Credit spread: sell short_conid, buy long_conid.
    Debit spread:  buy short_conid, sell long_conid.
    """
    combo = Contract()
    combo.symbol   = symbol
    combo.secType  = "BAG"
    combo.currency = "USD"
    combo.exchange = exchange

    short_leg         = ComboLeg()
    short_leg.conId   = short_conid
    short_leg.ratio   = 1
    short_leg.action  = "SELL" if is_credit else "BUY"
    short_leg.exchange = exchange

    long_leg          = ComboLeg()
    long_leg.conId    = long_conid
    long_leg.ratio    = 1
    long_leg.action   = "BUY" if is_credit else "SELL"
    long_leg.exchange = exchange

    combo.comboLegs = [short_leg, long_leg]
    return combo
