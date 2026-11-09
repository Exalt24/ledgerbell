from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ledgerbell import cli, config, routines, standin
from ledgerbell.config import ConfigError
from ledgerbell.runlog import FIELDS, AirtableError, RunLog, RunRow

from .conftest import NOW
from .fakes import AIRTABLE_TOKEN, BASE_ID, TABLE_ID, TABLE_NAME

ROW = RunRow("open", datetime(2026, 11, 9, 14, 35, tzinfo=UTC), "done", 5, 2, 1, "paper", "5 proposals considered at the open")


def log(airtable, table=TABLE_NAME, token=AIRTABLE_TOKEN, sleeps=None):
    return RunLog(BASE_ID, table, token, airtable.url, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_a_row_is_appended_and_read_back_with_every_field(airtable):
    rid = log(airtable).append(ROW)
    got = log(airtable).read(rid)
    assert got == {
        "Routine": "open", "Run at": "2026-11-09T14:35:00Z", "Status": "done", "Orders suggested": 5, "Orders placed": 2,
        "Orders refused": 1, "Mode": "paper", "Summary": "5 proposals considered at the open",
    }


def test_the_row_carries_exactly_the_columns_the_setup_script_makes():
    assert set(ROW.fields()) == set(FIELDS)


def test_the_table_name_with_spaces_and_brackets_is_url_encoded(airtable):
    log(airtable).append(ROW)
    method, path = airtable.requests[0]
    assert method == "POST" and path == f"/v0/{BASE_ID}/{TABLE_NAME}"  # the fake decodes it: a raw space would never arrive intact


def test_a_table_name_with_a_slash_a_question_mark_and_a_hash_is_encoded_into_one_path_segment(airtable):
    odd = "Runs / 2026? #1"
    airtable.tables.add(odd)
    assert log(airtable, table=odd).append(ROW).startswith("rec")
    assert airtable.requests[0][1] == f"/v0/{BASE_ID}/{odd}"  # decoded by the fake: only one segment survived the trip


def test_a_table_id_works_in_place_of_the_name(airtable):
    assert log(airtable, table=TABLE_ID).append(ROW).startswith("rec")


def test_429_waits_the_documented_thirty_seconds_and_tries_once_more(airtable):
    airtable.rate_limit_next = 1
    waits: list[float] = []
    assert log(airtable, sleeps=waits).append(ROW).startswith("rec")
    assert waits == [30]


def test_a_second_429_is_an_error(airtable):
    airtable.rate_limit_next = 2
    with pytest.raises(AirtableError) as e:
        log(airtable).append(ROW)
    assert e.value.status == 429 and not airtable.records


def test_a_wrong_token_is_a_401_error(airtable):
    with pytest.raises(AirtableError) as e:
        log(airtable, token="nope").append(ROW)
    assert e.value.status == 401


def test_an_unknown_table_is_a_404_error(airtable):
    with pytest.raises(AirtableError) as e:
        log(airtable, table="nope").append(ROW)
    assert e.value.status == 404


def test_a_long_summary_is_cut_not_refused(airtable):
    rid = log(airtable).append(RunRow("eod", ROW.at, "done", 0, 0, 0, "paper", "x" * 5000))
    assert len(log(airtable).read(rid)["Summary"]) == 2000


# -- through the routines -----------------------------------------------------------------------------------------------


@pytest.fixture()
def logged(desk, airtable):
    desk.runlog = log(airtable)
    return desk


def test_each_routine_run_appends_one_summary_row(logged, airtable):
    routines.run("eod", logged, NOW, None)
    routines.run("weekly", logged, NOW, None)
    assert [r["fields"]["Routine"] for r in airtable.records.values()] == ["eod", "weekly"]
    assert all(r["fields"]["Status"] == "done" and r["fields"]["Mode"] == "standin" for r in airtable.records.values())
    assert [e["event"] for e in logged.ledger.read()].count("runlog_written") == 2


def test_the_open_row_counts_what_was_suggested_placed_and_refused(logged, airtable):
    res = routines.run("open", logged, NOW, None)
    row = next(iter(airtable.records.values()))["fields"]
    assert row["Routine"] == "open" and row["Orders suggested"] == res.suggested > 0
    assert row["Orders placed"] == res.placed and row["Orders refused"] == res.refused
    sent = [e for e in logged.ledger.read() if e["event"] == "order_submitted"]
    assert res.placed == len(sent) >= 1


def test_the_premarket_row_counts_the_models_suggestions(logged, airtable):
    from .test_strategy_agent import GOOD, Scripted

    res = routines.run("premarket", logged, NOW, Scripted(GOOD))
    row = next(iter(airtable.records.values()))["fields"]
    assert row["Orders suggested"] == res.suggested > 0 and row["Orders placed"] == 0


def test_a_skipped_run_is_logged_too(logged, airtable, server):
    _, book = server
    book.is_open = False
    routines.run("open", logged, NOW, None)
    assert next(iter(airtable.records.values()))["fields"]["Status"] == "skipped"


def test_a_kill_switch_skip_is_logged(logged, airtable):
    logged.engage_kill("test")
    routines.run("open", logged, NOW, None)
    assert [r["fields"]["Status"] for r in airtable.records.values()] == ["skipped"]


def test_a_crashed_routine_is_logged_and_still_raises(logged, airtable):
    def boom():
        raise RuntimeError("broker gone")

    logged.client.account = boom
    with pytest.raises(RuntimeError):
        routines.run("eod", logged, NOW, None)
    row = next(iter(airtable.records.values()))["fields"]
    assert row["Status"] == "crashed" and "broker gone" in row["Summary"]


def test_a_dead_log_never_stops_the_routine(logged, airtable):
    airtable.stop()
    res = routines.run("eod", logged, NOW, None)
    assert res.status == "done"
    assert any(e["event"] == "runlog_failed" for e in logged.ledger.read())


def test_no_log_configured_means_no_calls(desk, airtable):
    routines.run("eod", desk, NOW, None)
    assert airtable.requests == []


def test_enabling_the_log_without_a_base_is_refused_at_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[airtable]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="base_id"):
        config.load(p)


def test_the_token_comes_from_the_environment_only():
    assert config.secret_from_env("AIRTABLE_TOKEN", {"AIRTABLE_TOKEN": " pat1 "}) == "pat1"
    with pytest.raises(ConfigError, match="AIRTABLE_TOKEN"):
        config.secret_from_env("AIRTABLE_TOKEN", {})


def test_the_command_line_writes_a_row_end_to_end(cfg, server, airtable, tmp_path, monkeypatch):
    toml = tmp_path / "c.toml"
    toml.write_text(
        f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n'
        f'[airtable]\nenabled = true\nbase_id = "{BASE_ID}"\ntable = "{TABLE_NAME}"\napi_url = "{airtable.url}"\n'
        f'[paths]\nstate_dir = "{(tmp_path / "state").as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("APCA_API_KEY_ID", standin.KEY)
    monkeypatch.setenv("APCA_API_SECRET_KEY", standin.SECRET)
    monkeypatch.setenv("AIRTABLE_TOKEN", AIRTABLE_TOKEN)
    assert cli.main(["--config", str(toml), "run", "eod", "--no-model"]) == 0
    assert [r["fields"]["Routine"] for r in airtable.records.values()] == ["eod"]
