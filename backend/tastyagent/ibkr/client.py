"""IBKR connection manager wrapping ib_async.

Supports dual-gateway configurations:
1. Trading Session: connected to IBKR_HOST:IBKR_PORT (places orders, manages positions).
2. Market Data Session: connected to IBKR_DATA_HOST:IBKR_DATA_PORT (streams real-time Greeks,
   runs market scanners, and pulls historical IV).

Includes monkey-patching for ib_async wrapper to prevent KeyError on late contract details.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Optional

from ib_async import IB
import ib_async.wrapper

from ..settings import Settings

logger = logging.getLogger(__name__)

# Monkey-patch ib_async wrapper to ignore contract details when reqId is already cleared
# (prevents KeyError on delayed responses after timeouts/disconnects).
_orig_contractDetails = ib_async.wrapper.Wrapper.contractDetails


def _patched_contractDetails(self, reqId: int, contractDetails):
    if reqId not in self._results:
        return
    _orig_contractDetails(self, reqId, contractDetails)


ib_async.wrapper.Wrapper.contractDetails = _patched_contractDetails


@dataclass
class AccountCashSummary:
    """Summary of account cash balances, base currency, and USD equivalent."""

    base_currency: str = "USD"
    total_cash_base: float = 0.0
    total_cash_usd: float = 0.0
    net_liq_usd: float = 0.0
    buying_power: float = 0.0
    buying_power_usd: float = 0.0
    cushion: Optional[float] = None  # Excess Liquidity / Net Liquidation Value (e.g. 0.35 = 35%)
    forex_balances: dict[str, float] = field(default_factory=dict)


@dataclass
class ConnectionStatus:
    """Status summary of dual-gateway IBKR connections."""

    real_connected: bool
    paper_connected: bool
    status: str  # "connected" | "warning" | "disconnected"
    detail: str


class IBKRClient:
    """Manages connections to Interactive Brokers gateway/TWS sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.trading_ib: IB = IB()  # Paper trading gateway (e.g. port 4002)
        self.data_ib: IB = (
            IB()
        )  # Real account / Live market data gateway (e.g. port 4001)
        self._trading_account: Optional[str] = None
        self._connected = False
        self._last_cash_summary: Optional[AccountCashSummary] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_running: bool = False

    @property
    def active_trading_ib(self) -> IB:
        """Active IB session used for trading orders and account queries in current mode.

        In live modes (LIVE_APPROVAL, LIVE_AUTO):
        - Strictly uses data_ib (real gateway, e.g. port 4001).
        - If data_ib is not connected but trading_ib is connected (e.g. in mock tests),
          falls back to trading_ib.
        In sandbox / backtest:
        - Uses trading_ib (paper gateway, e.g. port 4002).
        """
        if self.settings.mode.is_live:
            if getattr(self, "data_ib", None) and (
                self.data_ib.isConnected()
                or not getattr(self.trading_ib, "isConnected", lambda: False)()
            ):
                return self.data_ib
            if getattr(self, "trading_ib", None) and self.trading_ib.isConnected():
                return self.trading_ib
            return self.data_ib
        return self.trading_ib

    @property
    def connection_status(self) -> ConnectionStatus:
        """Compute connection status according to current trading mode.

        Rules:
        1. live_approval / live_auto:
           - Only uses real account / data client (data_ib).
           - If real client is connected -> "connected" (green live flashing), regardless of paper client.
           - If real client is disconnected -> "disconnected" (red).
        2. sandbox:
           - Real client supplies market data; paper client places orders.
           - If real client is disconnected -> "disconnected" (red).
           - If real client is connected but paper client is disconnected -> "warning" (yellow).
           - If both are connected -> "connected" (green live flashing).
        """
        real_ok = bool(self.data_ib and self.data_ib.isConnected())
        paper_ok = bool(self.trading_ib and self.trading_ib.isConnected())
        mode = self.settings.mode

        if mode.is_live:
            if real_ok:
                status = "connected"
                detail = f"Live gateway connected ({self.account or 'real'})"
            else:
                status = "disconnected"
                detail = "Live trading gateway disconnected"
        else:
            if not real_ok:
                status = "disconnected"
                detail = "Market data / Real gateway disconnected"
            elif not paper_ok:
                status = "warning"
                detail = "Paper trading gateway offline"
            else:
                status = "connected"
                detail = "All gateways online (Paper + Live Data)"

        return ConnectionStatus(
            real_connected=real_ok,
            paper_connected=paper_ok,
            status=status,
            detail=detail,
        )

    @property
    def is_connected(self) -> bool:
        return self.connection_status.status in ("connected", "warning")

    def _resolve_account(self, managed: list[str]) -> None:
        """Resolve active trading account based on current mode and connected gateway accounts.

        IBKR_ACCOUNT is strictly reserved for real account live trading (LIVE_APPROVAL / LIVE_AUTO).
        In sandbox mode, IBKR_ACCOUNT is ignored and the connected paper gateway account is used.
        """
        if not self.settings.mode.is_live:
            # Sandbox / non-live mode: strictly use the paper trading gateway account, ignore IBKR_ACCOUNT
            self._trading_account = managed[0] if managed else ""
            if self.settings.ibkr_account:
                logger.info(
                    "Sandbox mode active: ignoring configured IBKR_ACCOUNT '%s'. "
                    "Using gateway paper account: '%s'.",
                    self.settings.ibkr_account,
                    self._trading_account or "none",
                )
            else:
                logger.info(
                    "Sandbox mode active: using gateway paper account: '%s'.",
                    self._trading_account or "none",
                )
        else:
            # Real account live trading: apply IBKR_ACCOUNT
            if self.settings.ibkr_account:
                if managed and self.settings.ibkr_account in managed:
                    self._trading_account = self.settings.ibkr_account
                    logger.info(
                        "Live mode active: using configured real IBKR account: %s",
                        self._trading_account,
                    )
                else:
                    fallback = managed[0] if managed else self.settings.ibkr_account
                    logger.warning(
                        "Live mode active: configured IBKR_ACCOUNT '%s' not found in managed accounts %s. "
                        "Defaulting to '%s'.",
                        self.settings.ibkr_account,
                        managed,
                        fallback,
                    )
                    self._trading_account = fallback
            elif managed:
                self._trading_account = managed[0]
                logger.info(
                    "Live mode active: no IBKR_ACCOUNT specified; using primary account: %s",
                    self._trading_account,
                )
            else:
                self._trading_account = ""

        active = self.active_trading_ib
        if self._trading_account and active.isConnected():
            try:
                if hasattr(active, "client") and hasattr(active.client, "reqAccountUpdates"):
                    active.client.reqAccountUpdates(True, self._trading_account)
                else:
                    active.reqAccountUpdates(self._trading_account)
            except Exception as e:
                logger.debug("reqAccountUpdates failed: %s", e)

    def refresh_account(self) -> str:
        """Refresh and return the active trading account based on current mode."""
        active = self.active_trading_ib
        managed = active.managedAccounts() if active.isConnected() else []
        self._resolve_account(managed)
        return self._trading_account or ""

    @property
    def account(self) -> str:
        """Active account number used for trading operations.

        IBKR_ACCOUNT is strictly reserved for real account live trading (LIVE_APPROVAL / LIVE_AUTO).
        In sandbox mode, IBKR_ACCOUNT is never used; the connected paper gateway account is used instead.
        """
        active = self.active_trading_ib
        if not self.settings.mode.is_live:
            if self._trading_account:
                return self._trading_account
            accounts = active.managedAccounts() if active.isConnected() else []
            if accounts:
                return accounts[0]
            return ""

        # Real account live trading:
        if self._trading_account:
            return self._trading_account
        if self.settings.ibkr_account:
            return self.settings.ibkr_account
        accounts = active.managedAccounts() if active.isConnected() else []
        if accounts:
            return accounts[0]
        return ""

    async def _try_connect_data(self, timeout: float = 5.0) -> bool:
        if self.data_ib.isConnected():
            return True
        try:
            logger.debug(
                "Connecting Live/Data Gateway at %s:%s (clientId=%s)...",
                self.settings.ibkr_live_host,
                self.settings.ibkr_live_port,
                self.settings.ibkr_live_client_id,
            )
            await self.data_ib.connectAsync(
                host=self.settings.ibkr_live_host,
                port=self.settings.ibkr_live_port,
                clientId=self.settings.ibkr_live_client_id,
                timeout=timeout,
                readonly=False,  # Can place orders when live trading
            )
            logger.info(
                "Live/Data Gateway connected at %s:%s",
                self.settings.ibkr_live_host,
                self.settings.ibkr_live_port,
            )
            try:
                self.data_ib.reqMarketDataType(3)
            except Exception:
                pass
            if self.settings.mode.is_live:
                self.refresh_account()
            return True
        except Exception as e:
            logger.debug(
                "Live/Data Gateway connection attempt failed (%s:%s): %s",
                self.settings.ibkr_live_host,
                self.settings.ibkr_live_port,
                e,
            )
            return False

    async def _try_connect_trading(self, timeout: float = 5.0) -> bool:
        if self.trading_ib.isConnected():
            return True
        try:
            logger.debug(
                "Connecting Sandbox Gateway at %s:%s (clientId=%s)...",
                self.settings.ibkr_sandbox_host,
                self.settings.ibkr_sandbox_port,
                self.settings.ibkr_sandbox_client_id,
            )
            await self.trading_ib.connectAsync(
                host=self.settings.ibkr_sandbox_host,
                port=self.settings.ibkr_sandbox_port,
                clientId=self.settings.ibkr_sandbox_client_id,
                timeout=timeout,
                readonly=False,
            )
            logger.info(
                "Sandbox Gateway connected at %s:%s",
                self.settings.ibkr_sandbox_host,
                self.settings.ibkr_sandbox_port,
            )
            if not self.settings.mode.is_live:
                self.refresh_account()
            return True
        except Exception as e:
            logger.debug(
                "Sandbox Gateway connection attempt failed (%s:%s): %s",
                self.settings.ibkr_sandbox_host,
                self.settings.ibkr_sandbox_port,
                e,
            )
            return False

    async def connect(self, timeout: float = 10.0) -> None:
        """Establish connections to trading and data gateways."""
        await asyncio.gather(
            self._try_connect_data(timeout=timeout),
            self._try_connect_trading(timeout=timeout),
            return_exceptions=True,
        )
        self._connected = True
        self.refresh_account()

    async def connect_loop(self, interval: float = 5.0) -> None:
        """Background loop continuously keeping both paper and real/data clients connected.

        Similar to ibkr_portfolio's connect_loop:
        - Reconnects real/data client if disconnected.
        - Reconnects paper client if disconnected.
        - Automatically refreshes account subscriptions on reconnection.
        """
        while self._monitor_running:
            try:
                # 1. Keep Real/Data Gateway online
                if not self.data_ib.isConnected():
                    await self._try_connect_data(timeout=5.0)

                # 2. Keep Paper Trading Gateway online
                if not self.trading_ib.isConnected():
                    await self._try_connect_trading(timeout=5.0)

            except Exception as e:
                logger.debug("Error in IBKR connection monitor loop: %s", e)

            await asyncio.sleep(interval)

    def start_connection_monitor(self) -> None:
        """Start the background keep-alive monitoring loop."""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_running = True
        try:
            loop = asyncio.get_running_loop()
            self._monitor_task = loop.create_task(self.connect_loop())
            logger.info("IBKR connection keep-alive monitor started.")
        except RuntimeError:
            logger.debug(
                "No running event loop; connection monitor deferred until loop is available."
            )

    def stop_connection_monitor(self) -> None:
        """Stop the background keep-alive monitoring loop."""
        self._monitor_running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
            logger.info("IBKR connection keep-alive monitor stopped.")

    async def disconnect(self) -> None:
        """Disconnect all active IBKR sessions."""
        self.stop_connection_monitor()
        self._connected = False
        try:
            if self.data_ib.isConnected():
                self.data_ib.disconnect()
        except Exception as e:
            logger.debug("Error disconnecting data session: %s", e)

        try:
            if self.trading_ib.isConnected():
                self.trading_ib.disconnect()
        except Exception as e:
            logger.debug("Error disconnecting trading session: %s", e)

        logger.info("Disconnected from IBKR.")

    async def __aenter__(self) -> IBKRClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()

    def _parse_account_values(self, av: list[Any]) -> AccountCashSummary:
        """Parse account values list into an AccountCashSummary."""
        # 1. Detect base currency (e.g. SEK, EUR, USD, CAD, GBP)
        base_currency = "USD"
        for item in av:
            tag = getattr(item, "tag", "").replace("$LEDGER-", "")
            curr = (getattr(item, "currency", "") or "").upper()
            val_str = str(getattr(item, "value", "")).strip().upper()
            if tag in ("NetLiquidation", "TotalCashValue", "BuyingPower") and curr and curr != "BASE":
                base_currency = curr
                break
            if tag in ("Currency", "BaseCurrency") and val_str and val_str != "BASE":
                base_currency = val_str
                break
            if tag == "ExchangeRate" and curr and curr != "BASE":
                try:
                    if float(item.value) == 1.0:
                        base_currency = curr
                        break
                except (ValueError, TypeError):
                    pass

        forex_balances: dict[str, float] = {}
        exchange_rates: dict[str, float] = {}
        total_cash_base = 0.0
        total_cash_usd_direct: Optional[float] = None
        net_liq_base = 0.0
        net_liq_usd_direct: Optional[float] = None
        buying_power_base = 0.0
        cushion_direct: Optional[float] = None
        excess_liquidity_base: Optional[float] = None

        for item in av:
            raw_tag = getattr(item, "tag", "")
            tag = raw_tag.replace("$LEDGER-", "")
            curr = (getattr(item, "currency", "") or "").upper()
            try:
                val = float(item.value)
            except (ValueError, TypeError):
                continue

            if tag == "ExchangeRate" and curr:
                exchange_rates[curr] = val

            elif tag in ("TotalCashBalance", "TotalCashValue", "CashBalance"):
                if curr == "BASE":
                    total_cash_base = val
                elif curr == base_currency and total_cash_base == 0.0:
                    total_cash_base = val
                elif curr == "USD":
                    total_cash_usd_direct = val
                if curr and curr != "BASE":
                    forex_balances[curr] = val

            elif tag in ("NetLiquidation", "NetLiquidationByCurrency"):
                if curr == "BASE":
                    net_liq_base = val
                elif curr == base_currency and net_liq_base == 0.0:
                    net_liq_base = val
                elif curr == "USD":
                    net_liq_usd_direct = val

            elif tag == "BuyingPower":
                if curr == "BASE":
                    buying_power_base = val
                elif curr == base_currency and buying_power_base == 0.0:
                    buying_power_base = val

            elif tag == "Cushion":
                cushion_direct = val / 100.0 if val > 1.0 else val

            elif tag in ("ExcessLiquidity", "ExcessLiquidity-S", "ExcessLiquidity-C"):
                if curr == "BASE" or excess_liquidity_base is None:
                    excess_liquidity_base = val

        # Calculate USD equivalent
        if base_currency == "USD":
            total_cash_usd = (
                total_cash_base
                if total_cash_base != 0.0 or total_cash_usd_direct is None
                else total_cash_usd_direct
            )
            net_liq_usd = (
                net_liq_base
                if net_liq_base != 0.0 or net_liq_usd_direct is None
                else net_liq_usd_direct
            )
            buying_power_usd = buying_power_base
        else:
            # Base currency is non-USD (e.g. SEK, EUR, HKD, GBP, CAD, AUD).
            # IBKR quote convention: 1 foreign unit * ExchangeRate = Base Currency units.
            # So 1 USD * exchange_rates["USD"] = Base Currency units.
            # Therefore: USD amount = Base amount / exchange_rates["USD"].
            usd_rate = exchange_rates.get("USD", 0.0)
            if usd_rate > 0.0:
                total_cash_usd = total_cash_base / usd_rate
                net_liq_usd = net_liq_base / usd_rate
                buying_power_usd = buying_power_base / usd_rate
            elif exchange_rates.get(base_currency, 0.0) > 0.0:
                base_to_usd = exchange_rates[base_currency]
                total_cash_usd = total_cash_base * base_to_usd
                net_liq_usd = net_liq_base * base_to_usd
                buying_power_usd = buying_power_base * base_to_usd
            elif total_cash_usd_direct is not None:
                total_cash_usd = total_cash_usd_direct
                net_liq_usd = (
                    net_liq_usd_direct
                    if net_liq_usd_direct is not None
                    else total_cash_usd_direct
                )
                buying_power_usd = buying_power_base
            else:
                logger.warning(
                    "No exchange rate found to convert %s to USD; assuming 1:1 fallback.",
                    base_currency,
                )
                total_cash_usd = total_cash_base
                net_liq_usd = net_liq_base
                buying_power_usd = buying_power_base

        # Determine cushion
        cushion: Optional[float] = cushion_direct
        if cushion is None and excess_liquidity_base is not None and net_liq_base > 0:
            cushion = max(0.0, excess_liquidity_base / net_liq_base)

        return AccountCashSummary(
            base_currency=base_currency,
            total_cash_base=total_cash_base,
            total_cash_usd=total_cash_usd,
            net_liq_usd=net_liq_usd,
            buying_power=buying_power_base,
            buying_power_usd=buying_power_usd,
            cushion=cushion,
            forex_balances=forex_balances,
        )

    def cached_cash_summary(self) -> Optional[AccountCashSummary]:
        """Return the most recent cached cash summary or quick in-memory parse."""
        active = self.active_trading_ib
        if not active.isConnected():
            return self._last_cash_summary
        account = self.account
        av = active.accountValues(account)
        if not av and hasattr(active, "wrapper") and getattr(active.wrapper, "acctSummary", None):
            av = list(active.wrapper.acctSummary.values())
        if av:
            self._last_cash_summary = self._parse_account_values(av)
        return self._last_cash_summary

    async def get_account_cash_summary(self) -> AccountCashSummary:
        """Fetch comprehensive cash balances, base currency, and forex breakdown.

        Handles:
        1. Base currency detection (e.g. USD, EUR, SEK, HKD, CAD, GBP, JPY).
        2. Aggregation of multi-currency forex cash balances into Total Cash.
        3. Conversion of Total Cash, Net Liquidation, and Buying Power to USD at IBKR exchange rates if base currency is not USD.
        """
        active = self.active_trading_ib
        if not active.isConnected():
            return AccountCashSummary()

        account = self.account
        av = active.accountValues(account)
        if not av:
            try:
                await asyncio.wait_for(active.reqAccountUpdatesAsync(account), timeout=2.5)
                av = active.accountValues(account)
            except Exception as e:
                logger.debug("reqAccountUpdatesAsync failed for %s: %s", account, e)

        if not av:
            try:
                summary = await active.accountSummaryAsync()
                av = summary
            except Exception as e:
                logger.debug("accountSummaryAsync failed for %s: %s", account, e)

        summary_obj = self._parse_account_values(av)
        self._last_cash_summary = summary_obj
        return summary_obj

    async def get_account_summary(self) -> dict[str, float]:
        """Fetch NetLiquidation, TotalCashValue, and BuyingPower for the active account."""
        cash = await self.get_account_cash_summary()
        return {
            "TotalCashValue": cash.total_cash_usd,
            "NetLiquidation": cash.net_liq_usd,
            "BuyingPower": cash.buying_power_usd,
        }

    async def get_account_cushion(self) -> Optional[float]:
        """Fetch the current account cushion (Excess Liquidity / Net Liquidation Value)."""
        cash = await self.get_account_cash_summary()
        return cash.cushion
