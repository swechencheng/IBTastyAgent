"""Verify IBKR connectivity, account details, and existing positions.

Run:
    python scripts/check_ibkr.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from tastyagent.ibkr.client import IBKRClient  # noqa: E402
from tastyagent.settings import load_settings  # noqa: E402


async def main() -> None:
    settings = load_settings()
    print("Connecting to IBKR Gateway / TWS...")
    print(
        f"  Sandbox (paper) session: {settings.ibkr_sandbox_host}:{settings.ibkr_sandbox_port} (clientId={settings.ibkr_sandbox_client_id})"
    )
    if settings.ibkr_live_port:
        print(
            f"  Live (real / market data) session: {settings.ibkr_live_host}:{settings.ibkr_live_port} (clientId={settings.ibkr_live_client_id})"
        )

    client = IBKRClient(settings)
    try:
        await client.connect()
        print("  Connection successful!\n")

        print(f"Target trading account: {client.account}")
        managed = client.trading_ib.managedAccounts()
        print(f"Managed accounts on gateway: {managed}")

        # Account Summary / Balances
        print("\n--- Account Balances ---")
        av = client.trading_ib.accountValues(client.account)
        summary_tags = {
            "NetLiquidation",
            "TotalCashValue",
            "BuyingPower",
            "FullInitMarginReq",
            "FullMaintMarginReq",
        }
        for item in av:
            if item.tag in summary_tags:
                print(f"  {item.tag:<22} ({item.currency}): {item.value}")

        # Positions
        positions = client.trading_ib.positions(client.account)
        print(f"\n--- Open Positions ({len(positions)}) ---")
        for p in positions:
            print(
                f"  {p.contract.symbol:<6} {p.contract.secType:<5} {p.position:>6} @ {p.avgCost:>.2f}"
            )

        # Open Orders
        open_trades = client.trading_ib.openTrades()
        print(f"\n--- Open Orders ({len(open_trades)}) ---")
        for t in open_trades:
            ref = getattr(t.order, "orderRef", "")
            is_ta = ref.startswith("TastyAgent_")
            tag = "[IBTastyAgent]" if is_ta else "[External]"
            print(
                f"  {tag} ID={t.order.orderId} {t.contract.symbol} {t.order.action} {t.order.totalQuantity} "
                f"{t.order.orderType} status={t.orderStatus.status} ref={ref!r}"
            )

    finally:
        await client.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
