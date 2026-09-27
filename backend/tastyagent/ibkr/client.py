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
    forex_balances: dict[str, float] = field(default_factory=dict)


class IBKRClient:
    """Manages connections to Interactive Brokers gateway/TWS sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.trading_ib: IB = IB()
        self.data_ib: IB = IB()
        self._trading_account: Optional[str] = None
        self._connected = False
        self._last_cash_summary: Optional[AccountCashSummary] = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self.trading_ib.isConnected()

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

        if self._trading_account and self.trading_ib.isConnected():
            try:
                self.trading_ib.reqAccountUpdates(self._trading_account)
            except Exception as e:
                logger.debug("reqAccountUpdates failed: %s", e)

    def refresh_account(self) -> str:
        """Refresh and return the active trading account based on current mode."""
        managed = (
            self.trading_ib.managedAccounts() if self.trading_ib.isConnected() else []
        )
        self._resolve_account(managed)
        return self._trading_account or ""

    @property
    def account(self) -> str:
        """Active account number used for trading operations.

        IBKR_ACCOUNT is strictly reserved for real account live trading (LIVE_APPROVAL / LIVE_AUTO).
        In sandbox mode, IBKR_ACCOUNT is never used; the connected paper gateway account is used instead.
        """
        if not self.settings.mode.is_live:
            if self._trading_account:
                return self._trading_account
            accounts = self.trading_ib.managedAccounts()
            if accounts:
                return accounts[0]
            return ""

        # Real account live trading:
        if self._trading_account:
            return self._trading_account
        if self.settings.ibkr_account:
            return self.settings.ibkr_account
        accounts = self.trading_ib.managedAccounts()
        if accounts:
            return accounts[0]
        return ""

    async def connect(self, timeout: float = 10.0) -> None:
        """Establish connections to trading and data gateways."""
        logger.info(
            "Connecting to IBKR Trading Gateway at %s:%s (clientId=%s)...",
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            self.settings.ibkr_client_id,
        )

        # Connect trading session
        await self.trading_ib.connectAsync(
            host=self.settings.ibkr_host,
            port=self.settings.ibkr_port,
            clientId=self.settings.ibkr_client_id,
            timeout=timeout,
            readonly=False,
        )

        managed = self.trading_ib.managedAccounts()
        logger.info("Trading gateway connected. Managed accounts: %s", managed)

        self._resolve_account(managed)

        # Connect data session
        logger.info(
            "Connecting to IBKR Market Data Gateway at %s:%s (clientId=%s)...",
            self.settings.ibkr_data_host,
            self.settings.ibkr_data_port,
            self.settings.ibkr_data_client_id,
        )

        try:
            await self.data_ib.connectAsync(
                host=self.settings.ibkr_data_host,
                port=self.settings.ibkr_data_port,
                clientId=self.settings.ibkr_data_client_id,
                timeout=timeout,
                readonly=True,
            )
            logger.info("Market data gateway connected.")
        except Exception as e:
            logger.warning(
                "Could not connect to separate market data gateway (%s). Reusing trading session for data: %s",
                f"{self.settings.ibkr_data_host}:{self.settings.ibkr_data_port}",
                e,
            )
            self.data_ib = self.trading_ib

        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect all active IBKR sessions."""
        self._connected = False
        try:
            if self.data_ib is not self.trading_ib and self.data_ib.isConnected():
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
        base_currency = "USD"
        for item in av:
            if getattr(item, "tag", "") in ("Currency", "BaseCurrency") and getattr(
                item, "value", ""
            ):
                base_currency = str(item.value).upper()
                break

        forex_balances: dict[str, float] = {}
        exchange_rates: dict[str, float] = {}
        total_cash_base = 0.0
        total_cash_usd_direct: Optional[float] = None
        net_liq_base = 0.0
        net_liq_usd_direct: Optional[float] = None
        buying_power = 0.0

        for item in av:
            tag = getattr(item, "tag", "")
            curr = (getattr(item, "currency", "") or "").upper()
            try:
                val = float(item.value)
            except (ValueError, TypeError):
                continue

            if tag == "ExchangeRate" and curr:
                exchange_rates[curr] = val

            elif tag in ("TotalCashBalance", "TotalCashValue"):
                if curr == "BASE":
                    total_cash_base = val
                elif curr == base_currency and total_cash_base == 0.0:
                    total_cash_base = val
                elif curr == "USD":
                    total_cash_usd_direct = val
                if curr and curr != "BASE":
                    forex_balances[curr] = val

            elif tag == "NetLiquidation":
                if curr == "BASE":
                    net_liq_base = val
                elif curr == base_currency and net_liq_base == 0.0:
                    net_liq_base = val
                elif curr == "USD":
                    net_liq_usd_direct = val

            elif tag == "BuyingPower":
                if curr == "BASE":
                    buying_power = val
                elif curr == base_currency and buying_power == 0.0:
                    buying_power = val

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
        else:
            # Base currency is non-USD (e.g. EUR, HKD, GBP, CAD, AUD).
            # IBKR convention: 1 foreign unit * ExchangeRate = Base Currency units.
            # So 1 USD * exchange_rates["USD"] = Base Currency units.
            # Therefore: USD amount = Base amount / exchange_rates["USD"].
            usd_rate = exchange_rates.get("USD", 0.0)
            if usd_rate > 0.0:
                total_cash_usd = total_cash_base / usd_rate
                net_liq_usd = net_liq_base / usd_rate
            elif exchange_rates.get(base_currency, 0.0) > 0.0:
                base_to_usd = exchange_rates[base_currency]
                total_cash_usd = total_cash_base * base_to_usd
                net_liq_usd = net_liq_base * base_to_usd
            elif total_cash_usd_direct is not None:
                total_cash_usd = total_cash_usd_direct
                net_liq_usd = (
                    net_liq_usd_direct
                    if net_liq_usd_direct is not None
                    else total_cash_usd_direct
                )
            else:
                logger.warning(
                    "No exchange rate found to convert %s to USD; assuming 1:1 fallback.",
                    base_currency,
                )
                total_cash_usd = total_cash_base
                net_liq_usd = net_liq_base

        return AccountCashSummary(
            base_currency=base_currency,
            total_cash_base=total_cash_base,
            total_cash_usd=total_cash_usd,
            net_liq_usd=net_liq_usd,
            buying_power=buying_power,
            forex_balances=forex_balances,
        )

    def cached_cash_summary(self) -> Optional[AccountCashSummary]:
        """Return the most recent cached cash summary or quick in-memory parse."""
        if not self.trading_ib.isConnected():
            return self._last_cash_summary
        account = self.account
        av = self.trading_ib.accountValues(account)
        if av:
            self._last_cash_summary = self._parse_account_values(av)
        return self._last_cash_summary

    async def get_account_cash_summary(self) -> AccountCashSummary:
        """Fetch comprehensive cash balances, base currency, and forex breakdown.

        Handles:
        1. Base currency detection (e.g. USD, EUR, HKD, CAD, GBP, JPY).
        2. Aggregation of multi-currency forex cash balances into Total Cash.
        3. Conversion of Total Cash to USD at IBKR exchange rates if base currency is not USD.
        """
        if not self.trading_ib.isConnected():
            return AccountCashSummary()

        account = self.account
        av = self.trading_ib.accountValues(account)
        if not av:
            try:
                await self.trading_ib.reqAccountUpdatesAsync(account)
                av = self.trading_ib.accountValues(account)
            except Exception as e:
                logger.debug("reqAccountUpdatesAsync failed for %s: %s", account, e)

        if not av:
            try:
                summary = await self.trading_ib.accountSummaryAsync(account)
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
            "BuyingPower": cash.buying_power,
        }
