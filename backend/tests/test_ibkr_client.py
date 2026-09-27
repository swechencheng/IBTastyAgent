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
