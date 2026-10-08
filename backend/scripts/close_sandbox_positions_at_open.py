#!/usr/bin/env python3
"""Market-Open Sandbox Position Closer for IBTastyAgent.

Closes option positions on the IBKR paper trading account at the exact moment
US markets open (09:30:00 US/Eastern), while strictly shielding any symbols
specified via --exclude / --exclude-list.

Supported input formats:
    --exclude QQQ
    --exlude QQQ
    --exclude QQQ,SMCI,NOW
    --exclude-list=[QQQ,SMCI,NOW]
    --exlude-list=[QQQ,SMCI,NOW]
    --exclude-list QQQ,SMCI,NOW
    --exclude QQQ --exclude SMCI

Usage:
    # Wait for market open, excluding QQQ:
    python scripts/close_sandbox_positions_at_open.py --exclude QQQ

    # Exclude multiple symbols:
    python scripts/close_sandbox_positions_at_open.py --exclude-list=[QQQ,SMCI,NOW]

    # Dry-run test (simulation without placing real orders):
    python scripts/close_sandbox_positions_at_open.py --exclude-list=[QQQ,SMCI,NOW] --dry-run

    # Execute immediately (bypassing open wait):
    python scripts/close_sandbox_positions_at_open.py --exclude QQQ --now
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import logging
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load backend .env
BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env", override=True)

from ib_async import IB, MarketOrder, Trade  # noqa: E402
from tastyagent.settings import load_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("close_at_open")

ET_TZ = ZoneInfo("US/Eastern")


def parse_symbol_tokens(raw_tokens: list[str]) -> set[str]:
    """Parse symbol strings into a normalized set of uppercase tickers.

    Handles bracketed lists ([QQQ,SMCI,NOW]), comma-separated, space-separated, etc.
    """
    symbols = set()
    for token in raw_tokens:
        if not token:
            continue
        cleaned = re.sub(r"[\[\]\(\)\"\'\s]", "", token)
        for part in cleaned.split(","):
            part = part.strip().upper()
            if part and part != "NONE":
                symbols.add(part)
    return symbols


def get_watchlist_symbols(db_path: Path) -> set[str]:
    """Query all watchlist symbols from SQLite database."""
    if not db_path.exists():
        alt = BACKEND_DIR / "tastyagent.db"
        if alt.exists():
            db_path = alt
        else:
            raise FileNotFoundError(f"Database not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    rows = cur.execute("SELECT symbol FROM watchlist;").fetchall()
    conn.close()

    all_symbols = {r[0].upper() for r in rows}
    # Ensure standard candidates are in set
    all_symbols.update({"BA", "BABA", "MARA"})
    return all_symbols


def calculate_wait_seconds(target_time_et: datetime | None = None) -> float:
    """Calculate seconds until 09:30:00 US/Eastern."""
    now_et = datetime.now(ET_TZ)
    if target_time_et is None:
        target_time_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

    delta = (target_time_et - now_et).total_seconds()
    return delta


def is_target_order(
    trade: Trade,
    target_symbols: set[str],
    target_con_ids: set[int],
    protected_symbols: set[str],
) -> bool:
    """Returns True if an open order belongs to target closing positions/symbols and is not protected."""
    if trade.isDone():
        return False
    contract = trade.contract
    sym = (contract.symbol or "").upper()

    # Strictly shield protected symbols (e.g. QQQ)
    if sym in protected_symbols:
        return False

    # 1. Match by contract conId
    if contract.conId and contract.conId in target_con_ids:
        return True

    # 2. Match by BAG combo legs
    if hasattr(contract, "comboLegs") and contract.comboLegs:
        for leg in contract.comboLegs:
            if leg.conId in target_con_ids:
                return True

    # 3. Match by target symbol (if contract is an OPT or BAG for our target symbols)
    if sym in target_symbols and contract.secType in ("OPT", "BAG"):
        return True

    return False


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Close sandbox positions at market open, with configurable symbol exclusion."
    )
    parser.add_argument(
        "--exclude",
        "--exlude",
        action="append",
        default=[],
        help="Single or comma-separated symbol(s) to exclude (e.g. --exclude QQQ).",
    )
    parser.add_argument(
        "--exclude-list",
        "--exlude-list",
        action="append",
        default=[],
        help="List of symbols to exclude (e.g. --exclude-list=[QQQ,SMCI,NOW] or QQQ,SMCI,NOW).",
    )
    parser.add_argument(
        "--watchlist-only",
        action="store_true",
        default=False,
        help="Only target symbols found in database watchlist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without placing any broker orders.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Execute immediately without waiting for market open.",
    )
    parser.add_argument(
        "--client-id",
        type=int,
        default=92,
        help="Dedicated client ID to avoid collision with backend daemon (default: 92).",
    )
    parser.add_argument(
        "--audit-delay",
        type=int,
        default=30,
        help="Seconds to wait after opening orders before running follow-up inspection (default: 30).",
    )
    args = parser.parse_args()

    # Parse excluded symbols from CLI inputs
    raw_excludes = args.exclude + args.exclude_list
    if not raw_excludes:
        # Default exclude if no flag specified
        protected_symbols = {"QQQ"}
        logger.info("No --exclude specified: Defaulting to exclude ['QQQ'].")
    else:
        protected_symbols = parse_symbol_tokens(raw_excludes)

    db_path = BACKEND_DIR / "data" / "tastyagent.db"
    watchlist_symbols = get_watchlist_symbols(db_path)

    print("=" * 80)
    print("   IBTastyAgent — Market-Open Sandbox Position Closer")
    print("=" * 80)
    print(f"Target DB: {db_path}")
    print(f"Protected / Excluded Symbols ({len(protected_symbols)}): {sorted(list(protected_symbols))}")
    print(f"Watchlist Scope: {'Strictly Watchlist-only' if args.watchlist_only else 'All Options on Account (excluding protected)'}")
    print("=" * 80)

    settings = load_settings()
    host = settings.ibkr_host
    port = settings.ibkr_port

    ib = IB()
    logger.info(f"Connecting to IBKR paper trading gateway at {host}:{port} (clientId={args.client_id})...")
    try:
        await ib.connectAsync(host, port, clientId=args.client_id, timeout=15)
    except Exception as e:
        logger.error(f"Failed to connect to IBKR: {e}")
        sys.exit(1)

    accounts = ib.managedAccounts()
    logger.info(f"Connected to IBKR! Managed accounts: {accounts}")

    # 1. Fetch all positions from gateway
    all_positions = await ib.reqPositionsAsync()
    logger.info(f"Retrieved {len(all_positions)} total open positions from gateway.")

    to_close = []
    protected = []
    unmanaged = []

    for p in all_positions:
        sym = p.contract.symbol.upper()
        sec_type = p.contract.secType.upper()

        if sec_type != "OPT":
            # Non-option instruments (e.g. index futures OMXS30)
            unmanaged.append(p)
        elif sym in protected_symbols:
            # Excluded by command-line flag
            protected.append(p)
        elif args.watchlist_only and sym not in watchlist_symbols:
            # Non-watchlist symbol when watchlist_only is active
            unmanaged.append(p)
        else:
            to_close.append(p)

    # Print breakdown
    print(f"\n[1] TARGET POSITIONS TO CLOSE ({len(to_close)} legs):")
    if not to_close:
        print("   No positions matching close criteria found! Everything is clean.")
    else:
        contracts_to_qualify = [p.contract for p in to_close]
        await ib.qualifyContractsAsync(*contracts_to_qualify)
        for p in to_close:
            c = p.contract
            action = "BUY (Close Short)" if p.position < 0 else "SELL (Close Long)"
            print(
                f"   • {c.symbol:<5} {c.right} strike={c.strike:<6} exp={c.lastTradeDateOrContractMonth} "
                f"pos={p.position:>3.0f} --> ACTION: {action} {abs(p.position):.0f} [conId={c.conId}]"
            )

    print(f"\n[2] PROTECTED POSITIONS (EXCLUDED & UNTOUCHED) ({len(protected)} legs):")
    if not protected:
        print("   No open positions matching the exclude list.")
    else:
        for p in protected:
            c = p.contract
            print(f"   [SHIELDED] {c.symbol:<5} {c.right} strike={c.strike:<6} pos={p.position:>3.0f} (Excluded via CLI argument)")

    if unmanaged:
        print(f"\n[3] UNMANAGED OTHER POSITIONS ({len(unmanaged)} legs):")
        for p in unmanaged:
            c = p.contract
            print(f"   [OTHER]    {c.symbol:<5} {c.secType:<4} strike={getattr(c, 'strike', 0):<6} pos={p.position:>3.0f}")

    print("-" * 80)

    if not to_close:
        logger.info("Nothing to close. Exiting.")
        ib.disconnect()
        return

    # Check timing
    now_et = datetime.now(ET_TZ)
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    wait_secs = calculate_wait_seconds(market_open_et)

    if args.now:
        logger.info("Flag --now specified: Proceeding immediately without waiting for market open.")
    elif wait_secs <= 0:
        logger.info(f"Current time ({now_et.strftime('%H:%M:%S')} ET) is already past market open (09:30:00 ET). Executing immediately.")
    else:
        hours = int(wait_secs // 3600)
        minutes = int((wait_secs % 3600) // 60)
        secs = int(wait_secs % 60)
        logger.info(
            f"Current time: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Waiting {hours}h {minutes}m {secs}s until market open at {market_open_et.strftime('%H:%M:%S %Z')}..."
        )

        if args.dry_run:
            logger.info("[DRY-RUN] Simulating countdown (skipping real wait).")
        else:
            try:
                while True:
                    remaining = calculate_wait_seconds(market_open_et)
                    if remaining <= 0:
                        break
                    if remaining > 600:
                        logger.info(f"Countdown: {remaining / 3600:.2f} hours until market open...")
                        await asyncio.sleep(min(300, remaining - 10))
                    elif remaining > 60:
                        logger.info(f"Countdown: {int(remaining // 60)}m {int(remaining % 60)}s until market open...")
                        await asyncio.sleep(min(30, remaining - 10))
                    elif remaining > 10:
                        logger.info(f"Countdown: {remaining:.1f}s until market open...")
                        await asyncio.sleep(5)
                    else:
                        logger.info(f"T-minus {remaining:.1f}s...")
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                logger.info("Wait cancelled by user.")
                ib.disconnect()
                return

    # 2. Check and cancel stale open orders on target contracts/combos BEFORE submitting market orders
    target_con_ids = {p.contract.conId for p in to_close}
    target_symbols = {p.contract.symbol.upper() for p in to_close}

    # Request latest open orders from IBKR gateway
    await ib.reqAllOpenOrdersAsync()
    open_trades = ib.openTrades()
    stale_orders = [t for t in open_trades if is_target_order(t, target_symbols, target_con_ids, protected_symbols)]
    if stale_orders:
        logger.info(f"Found {len(stale_orders)} stale open order(s) on target contracts/combos. Cancelling them first...")
        for ot in stale_orders:
            if not args.dry_run:
                ib.cancelOrder(ot.order)
                logger.info(f"Cancelled stale order #{ot.order.orderId} ({ot.contract.symbol} {ot.order.action} {ot.order.orderType})")
        if not args.dry_run:
            await asyncio.sleep(1.5)

    # 3. Submit Market Close Orders
    print("\n" + "=" * 80)
    print(f"[{datetime.now(ET_TZ).strftime('%H:%M:%S.%f')[:-3]} ET] TRANSMITTING MARKET CLOSE ORDERS...")
    print("=" * 80)

    placed_trades: list[Trade] = []
    for p in to_close:
        qty = abs(p.position)
        if qty <= 0:
            continue
        action = "BUY" if p.position < 0 else "SELL"
        order = MarketOrder(action, qty)
        order.orderRef = "TastyAgent_CloseAtOpen"

        if args.dry_run:
            logger.info(f"[DRY-RUN] Would submit {action} {qty} {p.contract.symbol} {p.contract.right} {p.contract.strike} (conId={p.contract.conId})")
        else:
            trade = ib.placeOrder(p.contract, order)
            placed_trades.append(trade)
            logger.info(
                f"SUBMITTED: Order #{trade.order.orderId} {action} {qty} {p.contract.symbol} "
                f"{p.contract.right} {p.contract.strike} exp={p.contract.lastTradeDateOrContractMonth}"
            )

    if args.dry_run:
        print("\n[DRY-RUN] All simulated orders logged. No real orders sent.")
        ib.disconnect()
        return

    # Wait 3 seconds for initial order status updates
    await asyncio.sleep(3.0)

    # 4. Wait for the 30-second follow-up inspection
    logger.info(f"Waiting {args.audit_delay} seconds for post-open execution and fill reconciliation...")
    for elapsed in range(1, args.audit_delay + 1):
        await asyncio.sleep(1.0)
        if elapsed % 10 == 0:
            logger.info(f"Audit progress: {elapsed}/{args.audit_delay}s elapsed...")

    # 5. Follow-up Inspection & Verification
    print("\n" + "=" * 80)
    print(f"[{datetime.now(ET_TZ).strftime('%H:%M:%S')} ET] RUNNING POST-OPEN FOLLOW-UP AUDIT (+{args.audit_delay}s)...")
    print("=" * 80)

    current_positions = await ib.reqPositionsAsync()
    remnants = []

    for p in current_positions:
        sym = p.contract.symbol.upper()
        sec_type = p.contract.secType.upper()
        if sec_type == "OPT" and sym not in protected_symbols:
            if not args.watchlist_only or sym in watchlist_symbols:
                if abs(p.position) > 0:
                    remnants.append(p)

    if remnants:
        logger.warning(f"⚠️ ATTENTION: Found {len(remnants)} remnant positions that were NOT fully closed:")
        for r in remnants:
            c = r.contract
            print(f"   • UNFILLED REMNANT: {c.symbol} {c.right} {c.strike} exp={c.lastTradeDateOrContractMonth} remaining pos={r.position}")
            action = "BUY" if r.position < 0 else "SELL"
            qty = abs(r.position)
            logger.info(f"   Retrying immediate sweep order: {action} {qty} on {c.symbol}...")
            sweep_order = MarketOrder(action, qty)
            sweep_order.orderRef = "TastyAgent_CloseSweep"
            ib.placeOrder(c, sweep_order)
        await asyncio.sleep(3.0)
    else:
        print("✅ POSITIONS AUDIT: All target positions have been completely flattened to 0.0!")

    # 6. Post-Audit Open Order Sweep: Ensure NO lingering/unfulfilled TP limit orders remain
    await ib.reqAllOpenOrdersAsync()
    post_open_trades = ib.openTrades()
    lingering_orders = [t for t in post_open_trades if is_target_order(t, target_symbols, target_con_ids, protected_symbols)]
    if lingering_orders:
        logger.warning(f"⚠️ Found {len(lingering_orders)} lingering open order(s) for target symbols. Cancelling all...")
        for lo in lingering_orders:
            ib.cancelOrder(lo.order)
            logger.info(f"   Cancelled lingering order #{lo.order.orderId} ({lo.contract.symbol} {lo.order.action} {lo.order.orderType})")
        await asyncio.sleep(1.0)
    else:
        print("✅ ORDERS AUDIT: Zero lingering open orders for target symbols (all TP/limit orders cleared)!")

    # Verify protected positions
    print(f"\n[VERIFICATION] Protected Symbols Check ({len(protected_symbols)} symbols: {sorted(list(protected_symbols))}):")
    for prot_sym in sorted(list(protected_symbols)):
        prot_positions = [p for p in current_positions if p.contract.symbol.upper() == prot_sym]
        if prot_positions:
            print(f"   • {prot_sym} ({len(prot_positions)} legs remaining):")
            for q in prot_positions:
                c = q.contract
                print(f"      ✅ {prot_sym} {c.right} strike={c.strike} pos={q.position} — COMPLETELY UNTOUCHED & SAFE.")
        else:
            print(f"   • {prot_sym}: 0 open positions on gateway (SAFE).")

    print("=" * 80)
    logger.info("Close-at-open routine completed. Disconnecting from IBKR.")
    ib.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
