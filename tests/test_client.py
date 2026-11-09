from __future__ import annotations

import pytest

from ledgerbell import standin
from ledgerbell.alpaca import AlpacaClient, AlpacaError, check_hosts
from ledgerbell.config import Config, ConfigError, credentials


def test_paper_mode_refuses_the_live_host():
    with pytest.raises(ConfigError, match="paper"):
        check_hosts(Config(mode="paper", base_url="https://api.alpaca.markets"))


def test_paper_mode_accepts_the_paper_host():
    check_hosts(Config(mode="paper"))


def test_live_mode_needs_the_owner_to_confirm_in_the_config():
    live = Config(mode="live", base_url="https://api.alpaca.markets")
    with pytest.raises(ConfigError, match="live_confirmed"):
        check_hosts(live)
    check_hosts(Config(mode="live", live_confirmed=True, base_url="https://api.alpaca.markets"))


def test_standin_mode_only_talks_to_localhost():
    with pytest.raises(ConfigError, match="localhost"):
        check_hosts(Config(mode="standin", base_url="https://paper-api.alpaca.markets", data_url="http://127.0.0.1:1"))


def test_market_data_host_is_checked_too():
    with pytest.raises(ConfigError, match="market data"):
        check_hosts(Config(mode="paper", data_url="https://example.com"))


def test_credentials_come_from_the_environment_and_are_required():
    assert credentials({"APCA_API_KEY_ID": "a", "APCA_API_SECRET_KEY": "b"}) == ("a", "b")
    with pytest.raises(ConfigError):
        credentials({"APCA_API_KEY_ID": "a"})


def test_wrong_keys_get_a_401_as_an_error(cfg):
    with pytest.raises(AlpacaError) as e:
        AlpacaClient(cfg, "nope", "nope").account()
    assert e.value.status == 401


def test_account_positions_and_clock_parse(cfg, server):
    c = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    a = c.account()
    assert a.equity == 100_000.0 and a.last_equity == 100_000.0 and not a.trading_blocked
    assert c.positions() == []
    assert c.clock()["is_open"] is True


def test_entry_order_fills_and_leaves_a_server_side_stop(cfg, server):
    _, book = server
    c = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    order = c.submit_entry("AAPL", 10, 184.0, "t-1")
    assert order["status"] == "filled" and order["order_class"] == "oto"
    stops = [o for o in c.open_orders() if o["type"] == "stop"]
    assert len(stops) == 1 and stops[0]["stop_price"] == "184.00" and stops[0]["side"] == "sell"
    assert c.positions()[0].qty == 10 and book.cash < 100_000


def test_duplicate_client_order_id_is_rejected_so_a_rerun_cannot_double_up(cfg):
    c = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    c.submit_entry("AAPL", 1, 180.0, "same")
    with pytest.raises(AlpacaError) as e:
        c.submit_entry("AAPL", 1, 180.0, "same")
    assert e.value.status == 422


def test_insufficient_buying_power_is_a_403(cfg):
    with pytest.raises(AlpacaError) as e:
        AlpacaClient(cfg, standin.KEY, standin.SECRET).submit_entry("SPY", 10_000, 500.0, "big")
    assert e.value.status == 403


def test_bars_follow_next_page_token_until_every_symbol_is_complete():
    srv, _ = standin.start(page_cap=50)  # 5 symbols of 60 bars each come back in several pages
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        c = AlpacaClient(Config(mode="standin", base_url=url, data_url=url), standin.KEY, standin.SECRET)
        symbols = ["AAPL", "MSFT", "SPY", "QQQ", "NVDA"]
        bars = c.daily_bars(symbols, limit=60)
        assert {s: len(b) for s, b in bars.items()} == {s: 60 for s in symbols}
    finally:
        srv.shutdown()
