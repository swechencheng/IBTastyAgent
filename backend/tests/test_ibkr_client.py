from unittest.mock import MagicMock
import pytest

from tastyagent.config import TradingMode
from tastyagent.ibkr.client import IBKRClient
from tastyagent.settings import Settings


def test_ibkr_client_initialization():
    settings = Settings(
        ibkr_host="127.0.0.1",
        ibkr_port=4002,
        ibkr_client_id=77,
        ibkr_account="U999999",
        ibkr_data_host="127.0.0.1",
        ibkr_data_port=4001,
        ibkr_data_client_id=78,
    )
    client = IBKRClient(settings)
    assert client.settings.ibkr_host == "127.0.0.1"
    assert client.settings.ibkr_port == 4002
    assert client.settings.ibkr_client_id == 77
    assert client.settings.ibkr_account == "U999999"
    assert not client.is_connected


def test_sandbox_ignores_ibkr_account():
    # In sandbox mode, configured IBKR_ACCOUNT must be ignored in favor of gateway paper account
    settings = Settings(mode=TradingMode.SANDBOX, ibkr_account="U999999")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = ["DUP12345"]
    assert client.account == "DUP12345"


def test_sandbox_does_not_fallback_to_ibkr_account_when_no_managed():
    # In sandbox mode, if no managed accounts are reported, it must NOT fall back to real IBKR_ACCOUNT
    settings = Settings(mode=TradingMode.SANDBOX, ibkr_account="U999999")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = []
    assert client.account == ""


def test_sandbox_resolve_account_ignores_ibkr_account():
    settings = Settings(mode=TradingMode.SANDBOX, ibkr_account="U999999")
    client = IBKRClient(settings)
    client._resolve_account(["DUP12345"])
    assert client.account == "DUP12345"


def test_live_mode_uses_configured_ibkr_account():
    settings = Settings(mode=TradingMode.LIVE_AUTO, ibkr_account="U999999")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = ["U999999", "U888888"]
    client._resolve_account(["U999999", "U888888"])
    assert client.account == "U999999"


def test_live_mode_warns_and_defaults_when_account_missing():
    settings = Settings(mode=TradingMode.LIVE_APPROVAL, ibkr_account="U999999")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = ["U111111"]
    client._resolve_account(["U111111"])
    assert client.account == "U111111"


def test_live_mode_fallback_when_no_ibkr_account_specified():
    settings = Settings(mode=TradingMode.LIVE_APPROVAL, ibkr_account="")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = ["U111111"]
    client._resolve_account(["U111111"])
    assert client.account == "U111111"


def test_mode_switch_refreshes_account():
    settings = Settings(mode=TradingMode.SANDBOX, ibkr_account="U999999")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True
    client.trading_ib.managedAccounts.return_value = ["DUP12345", "U999999"]

    # Initial sandbox resolution -> DUP12345
    assert client.refresh_account() == "DUP12345"

    # Switch to live -> U999999
    client.settings.mode = TradingMode.LIVE_APPROVAL
    assert client.refresh_account() == "U999999"

    # Switch back to sandbox -> DUP12345
    client.settings.mode = TradingMode.SANDBOX
    assert client.refresh_account() == "DUP12345"


def test_ibkr_client_contract_details_patch():
    # Verify monkey patch is applied to ib_async.wrapper.Wrapper.contractDetails
    import ib_async.wrapper

    wrapper = ib_async.wrapper.Wrapper(ib=MagicMock())
    # If reqId not in results, it should silently return without error
    wrapper._results = {}
    wrapper.contractDetails(reqId=99999, contractDetails=None)


async def test_get_account_cash_summary_usd_base():
    from ib_async import AccountValue

    settings = Settings(ibkr_account="U123456")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True
    client.trading_ib.managedAccounts.return_value = ["U123456"]
    client.settings.mode = TradingMode.LIVE_AUTO
    client.refresh_account()

    client.trading_ib.accountValues.return_value = [
        AccountValue(
            account="U123456",
            tag="Currency",
            value="USD",
            currency="BASE",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="50000.0",
            currency="BASE",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="50000.0",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="NetLiquidation",
            value="65000.0",
            currency="BASE",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="BuyingPower",
            value="120000.0",
            currency="BASE",
            modelCode="",
        ),
    ]

    summary = await client.get_account_cash_summary()
    assert summary.base_currency == "USD"
    assert summary.total_cash_base == 50000.0
    assert summary.total_cash_usd == 50000.0
    assert summary.net_liq_usd == 65000.0
    assert summary.buying_power == 120000.0
    assert summary.forex_balances == {"USD": 50000.0}


async def test_get_account_cash_summary_eur_base_with_forex_conversion():
    from ib_async import AccountValue

    settings = Settings(ibkr_account="U123456")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True
    client.trading_ib.managedAccounts.return_value = ["U123456"]
    client.settings.mode = TradingMode.LIVE_AUTO
    client.refresh_account()

    client.trading_ib.accountValues.return_value = [
        AccountValue(
            account="U123456",
            tag="Currency",
            value="EUR",
            currency="BASE",
            modelCode="",
        ),
        # Total cash in Base is 9200 EUR across EUR and USD
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="9200.0",
            currency="BASE",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="7000.0",
            currency="EUR",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="2391.30",
            currency="USD",
            modelCode="",
        ),
        # 1 USD = 0.92 EUR
        AccountValue(
            account="U123456",
            tag="ExchangeRate",
            value="0.92",
            currency="USD",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="NetLiquidation",
            value="18400.0",
            currency="BASE",
            modelCode="",
        ),
    ]

    summary = await client.get_account_cash_summary()
    assert summary.base_currency == "EUR"
    assert summary.total_cash_base == 9200.0
    # 9200.0 / 0.92 == 10000.0 USD
    assert pytest.approx(summary.total_cash_usd, 0.01) == 10000.0
    assert pytest.approx(summary.net_liq_usd, 0.01) == 20000.0
    assert summary.forex_balances == {"EUR": 7000.0, "USD": 2391.30}


async def test_get_account_cash_summary_negative_cash():
    from ib_async import AccountValue

    settings = Settings(ibkr_account="U123456")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True
    client.trading_ib.managedAccounts.return_value = ["U123456"]
    client.settings.mode = TradingMode.LIVE_AUTO
    client.refresh_account()

    client.trading_ib.accountValues.return_value = [
        AccountValue(
            account="U123456",
            tag="Currency",
            value="USD",
            currency="BASE",
            modelCode="",
        ),
        AccountValue(
            account="U123456",
            tag="TotalCashBalance",
            value="-1500.0",
            currency="BASE",
            modelCode="",
        ),
    ]

    summary = await client.get_account_cash_summary()
    assert summary.total_cash_usd == -1500.0
    assert summary.total_cash_base == -1500.0


def test_connection_status_real_disconnected():
    settings = Settings(mode=TradingMode.SANDBOX)
    client = IBKRClient(settings)
    client.data_ib = MagicMock()
    client.data_ib.isConnected.return_value = False
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True

    status = client.connection_status
    assert status.status == "disconnected"
    assert not status.real_connected
    assert status.paper_connected
    assert not client.is_connected


def test_connection_status_sandbox_paper_disconnected_is_warning():
    settings = Settings(mode=TradingMode.SANDBOX)
    client = IBKRClient(settings)
    client.data_ib = MagicMock()
    client.data_ib.isConnected.return_value = True
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = False

    status = client.connection_status
    assert status.status == "warning"
    assert status.real_connected
    assert not status.paper_connected
    assert client.is_connected


def test_connection_status_sandbox_both_connected_is_connected():
    settings = Settings(mode=TradingMode.SANDBOX)
    client = IBKRClient(settings)
    client.data_ib = MagicMock()
    client.data_ib.isConnected.return_value = True
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True

    status = client.connection_status
    assert status.status == "connected"
    assert status.real_connected
    assert status.paper_connected
    assert client.is_connected


def test_connection_status_live_mode_real_connected_paper_offline_is_connected():
    for live_mode in (TradingMode.LIVE_APPROVAL, TradingMode.LIVE_AUTO):
        settings = Settings(mode=live_mode, ibkr_account="U123456")
        client = IBKRClient(settings)
        client.data_ib = MagicMock()
        client.data_ib.isConnected.return_value = True
        client.trading_ib = MagicMock()
        client.trading_ib.isConnected.return_value = False

        status = client.connection_status
        assert status.status == "connected"
        assert status.real_connected
        assert not status.paper_connected
        assert client.is_connected


def test_connection_status_live_mode_real_offline_is_disconnected():
    for live_mode in (TradingMode.LIVE_APPROVAL, TradingMode.LIVE_AUTO):
        settings = Settings(mode=live_mode, ibkr_account="U123456")
        client = IBKRClient(settings)
        client.data_ib = MagicMock()
        client.data_ib.isConnected.return_value = False
        client.trading_ib = MagicMock()
        client.trading_ib.isConnected.return_value = True

        status = client.connection_status
        assert status.status == "disconnected"
        assert not client.is_connected


def test_active_trading_ib_routing():
    client = IBKRClient(Settings(mode=TradingMode.SANDBOX))
    assert client.active_trading_ib == client.trading_ib

    client_live = IBKRClient(Settings(mode=TradingMode.LIVE_AUTO))
    client_live.data_ib = MagicMock()
    client_live.data_ib.isConnected.return_value = True
    assert client_live.active_trading_ib == client_live.data_ib


async def test_start_stop_connection_monitor():
    settings = Settings(mode=TradingMode.SANDBOX)
    client = IBKRClient(settings)
    assert client._monitor_task is None
    client.start_connection_monitor()
    assert client._monitor_running is True
    assert client._monitor_task is not None

    client.stop_connection_monitor()
    assert client._monitor_running is False
    assert client._monitor_task is None
