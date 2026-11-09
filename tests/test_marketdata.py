from __future__ import annotations

import pytest

from ledgerbell import cli, config, routines
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.config import Config, ConfigError, MarketDataSettings
from ledgerbell.desk import Desk
from ledgerbell.marketdata import AlpacaData, FinnhubData, ProviderError, build_provider
from ledgerbell import standin

from .conftest import NOW
from .fakes import FINNHUB_KEY


def provider(finnhub, key=FINNHUB_KEY, sleeps=None):
    return FinnhubData(finnhub.url, key, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_the_alpaca_provider_quotes_the_latest_trade(cfg, server):
    client = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    q = AlpacaData(client).quote("AAPL")
    assert q.symbol == "AAPL" and q.price == 190.0 and q.source == "alpaca" and q.at is not None


def test_the_alpaca_provider_returns_the_same_bars_as_the_client(cfg, server):
    client = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    assert AlpacaData(client).daily_bars(["AAPL"], limit=30) == client.daily_bars(["AAPL"], limit=30)


def test_finnhub_quote_is_parsed_from_the_documented_fields(finnhub):
    q = provider(finnhub).quote("AAPL")
    assert (q.price, q.prev_close, q.high, q.low, q.open, q.source) == (191.25, 190.0, 192.0, 189.9, 190.1, "finnhub")
    assert q.change_pct == 0.66 and q.at.year == 2025


def test_finnhub_sends_the_key_in_the_documented_header_and_never_in_the_url(finnhub):
    provider(finnhub).quote("AAPL")
    assert finnhub.calls == ["/api/v1/quote"]
    with pytest.raises(ProviderError) as e:
        provider(finnhub, key="wrong").quote("AAPL")
    assert e.value.status == 401


def test_finnhub_answers_an_unknown_symbol_with_zeros_and_that_is_an_error_not_a_price(finnhub):
    with pytest.raises(ProviderError) as e:
        provider(finnhub).quote("NOPE")
    assert e.value.status == 404 and "no quote" in str(e.value)


def test_finnhub_429_is_retried_after_the_wait_it_names(finnhub):
    finnhub.rate_limit_next = 1
    waits: list[float] = []
    assert provider(finnhub, sleeps=waits).quote("MSFT").price == 409.5
    assert waits == [0.0] and finnhub.calls.count("/api/v1/quote") == 2


def test_finnhub_gives_up_after_its_retries(finnhub):
    finnhub.rate_limit_next = 10
    with pytest.raises(ProviderError) as e:
        provider(finnhub).quote("AAPL")
    assert e.value.status == 429 and finnhub.calls.count("/api/v1/quote") == 3


def test_finnhub_daily_candles_are_premium_and_say_so(finnhub):
    with pytest.raises(ProviderError) as e:
        provider(finnhub).daily_bars(["AAPL"], limit=30)
    assert e.value.status == 403 and "access" in str(e.value)


def test_the_provider_comes_from_the_config_and_the_key_from_the_environment(cfg, server):
    client = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    assert build_provider(cfg, client).name == "alpaca"
    fin = Config(**{**cfg.__dict__, "market_data": MarketDataSettings(provider="finnhub", finnhub_url="http://127.0.0.1:9")})
    assert build_provider(fin, client, env={"FINNHUB_API_KEY": "k"}).name == "finnhub"
    with pytest.raises(ConfigError, match="FINNHUB_API_KEY"):
        build_provider(fin, client, env={})


def test_an_unknown_provider_name_is_refused_at_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[market_data]\nprovider = "bloomberg"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load(p)


def test_the_open_routine_prices_from_finnhub_and_falls_back_to_alpaca_bars(cfg, server, capture, finnhub):
    from ledgerbell.alerts import Notifier
    from ledgerbell.ledger import Ledger

    client = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    desk = Desk(cfg, client, Ledger(cfg.ledger_file), Notifier(cfg.alerts, smtp_factory=capture.smtp, http_post=capture.post), data=provider(finnhub))
    res = routines.run("open", desk, NOW, None)
    names = [(e["event"], e["data"]) for e in desk.ledger.read()]
    assert any(n == "provider_fallback" and "403" in d["error"] for n, d in names)  # no candles on the free plan: Alpaca's bars are used
    assert sum(1 for n, d in names if n == "quote_failed") == 3  # Finnhub's fake only knows AAPL and MSFT
    sent = next(d for n, d in names if n == "order_submitted" and d["symbol"] == "AAPL")
    assert sent["price"] == 191.25 and sent["stop_price"] == round(191.25 * 0.97, 2)  # sized and stopped from Finnhub's quote, not the bar close
    assert res.placed >= 1


def test_the_quote_command_prints_the_configured_providers_price(cfg, tmp_path, monkeypatch, capsys, finnhub):
    toml = tmp_path / "c.toml"
    toml.write_text(
        f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n'
        f'[market_data]\nprovider = "finnhub"\nfinnhub_url = "{finnhub.url}"\n', encoding="utf-8")
    monkeypatch.setenv("APCA_API_KEY_ID", standin.KEY)
    monkeypatch.setenv("APCA_API_SECRET_KEY", standin.SECRET)
    monkeypatch.setenv("FINNHUB_API_KEY", FINNHUB_KEY)
    assert cli.main(["--config", str(toml), "quote", "aapl"]) == 0
    assert "AAPL 191.25 from finnhub" in capsys.readouterr().out


def test_the_options_command_lists_contracts_with_greeks(cfg, server, tmp_path, monkeypatch, capsys):
    toml = tmp_path / "c.toml"  # this server builds its chain around the real today, which is the day the command asks about
    toml.write_text(f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n', encoding="utf-8")
    monkeypatch.setenv("APCA_API_KEY_ID", standin.KEY)
    monkeypatch.setenv("APCA_API_SECRET_KEY", standin.SECRET)
    assert cli.main(["--config", str(toml), "options", "AAPL", "--right", "call", "--rows", "2"]) == 0
    out = capsys.readouterr().out
    assert "AAPL last 190.00; 10 tradable contracts" in out and out.count("iv 0.310") == 2 and "delta +" in out


def test_a_provider_error_is_a_clean_exit_code_not_a_traceback(cfg, tmp_path, monkeypatch, capsys, finnhub):
    toml = tmp_path / "c.toml"
    toml.write_text(
        f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n'
        f'[market_data]\nprovider = "finnhub"\nfinnhub_url = "{finnhub.url}"\n', encoding="utf-8")
    monkeypatch.setenv("APCA_API_KEY_ID", standin.KEY)
    monkeypatch.setenv("APCA_API_SECRET_KEY", standin.SECRET)
    monkeypatch.setenv("FINNHUB_API_KEY", "wrong")
    assert cli.main(["--config", str(toml), "quote", "AAPL"]) == 1
    assert "error: finnhub 401" in capsys.readouterr().err
