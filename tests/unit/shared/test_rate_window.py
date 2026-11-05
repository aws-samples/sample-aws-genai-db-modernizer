# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement's call rate over the window its execution count covers (#1893)."""

import math
from datetime import datetime, timedelta

import pytest

from src.shared.rate_window import (
    ASSUMED_24H,
    ASSUMED_WINDOW_SECONDS,
    MIN_WINDOW_SECONDS,
    OBSERVED,
    STATS_RESET,
    UPTIME,
    ServerWindow,
    rate_window,
    rate_window_facts,
    server_window,
    statement_rate_fields,
)

FIRST = "2026-11-05 02:58:47.490"
LAST = "2026-11-05 14:34:34.873"
SPAN = (
    datetime(2026, 11, 5, 14, 34, 34, 873000) - datetime(2026, 11, 5, 2, 58, 47, 490000)
).total_seconds()
UP = ServerWindow(50_400, UPTIME)
THREE_DAYS = ServerWindow(3 * 86_400, UPTIME)


def test_the_observed_window_divides_the_count():
    fields = statement_rate_fields(91_099, FIRST, LAST, None)
    assert fields["rate_window_basis"] == OBSERVED
    assert fields["rate_window_seconds"] == pytest.approx(SPAN)
    assert fields["calls_per_second"] == pytest.approx(91_099 / SPAN)
    assert fields["frequency_per_hour"] == pytest.approx(91_099 / SPAN * 3600)
    # The DNN top statement: 2.2 calls/s over its 11.6-hour window, not 1.05.
    assert round(fields["calls_per_second"], 1) == 2.2


def test_the_observed_window_wins_over_the_server_window():
    assert rate_window(FIRST, LAST, UP).basis == OBSERVED


def test_no_window_keeps_todays_24_hour_figures_exactly():
    fields = statement_rate_fields(715_089, None, None, None)
    assert fields["rate_window_basis"] == ASSUMED_24H
    assert fields["rate_window_seconds"] == ASSUMED_WINDOW_SECONDS == 86_400
    # Bit for bit what the collectors computed before #1893.
    assert fields["calls_per_second"] == 715_089 / (24 * 3600)
    assert fields["frequency_per_hour"] == 715_089 / 24


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (FIRST, FIRST),  # first_seen equal to last_seen (one execution)
        (LAST, FIRST),  # clock skew: last before first
        (None, LAST),  # a missing field
        (FIRST, None),
        ("", ""),
        ("not a time", LAST),  # unreadable
        (FIRST, 12345),  # not a time at all
    ],
)
def test_no_usable_observed_window_falls_back_to_the_server_window(first, last):
    window = rate_window(first, last, UP)
    assert (window.basis, window.seconds) == (UPTIME, 50_400)


@pytest.mark.parametrize(("first", "last"), [(FIRST, FIRST), (LAST, FIRST), (None, None)])
def test_with_no_server_window_either_it_assumes_24_hours(first, last):
    window = rate_window(first, last, None)
    assert (window.basis, window.seconds) == (ASSUMED_24H, ASSUMED_WINDOW_SECONDS)


def test_the_minimum_window_is_inclusive():
    start = datetime(2026, 11, 5, 12, 0, 0)
    exact = rate_window(start, start + timedelta(seconds=MIN_WINDOW_SECONDS), None)
    assert (exact.basis, exact.seconds) == (OBSERVED, MIN_WINDOW_SECONDS)


def _calls_over(seconds: float, server: ServerWindow | None) -> dict:
    start = datetime(2026, 11, 5, 12, 0, 0)
    return statement_rate_fields(300, start, start + timedelta(seconds=seconds), server)


def test_59_seconds_and_61_seconds_give_nearly_the_same_rate():
    """The review's case: 300 calls over 59 s against 61 s, with a 3-day uptime. A plain
    fallback would give 0.0012 calls/s and 4.92 calls/s, 4,000 times apart."""
    short, long = _calls_over(59, THREE_DAYS), _calls_over(61, THREE_DAYS)
    assert long["rate_window_basis"] == OBSERVED
    assert long["calls_per_second"] == pytest.approx(300 / 61)
    # Under the minimum the rate blends toward the fallback and counts under its basis.
    assert short["rate_window_basis"] == UPTIME
    assert short["calls_per_second"] == pytest.approx(4.917, abs=0.001)
    assert abs(short["calls_per_second"] / long["calls_per_second"] - 1) < 0.001
    assert short["rate_window_seconds"] == pytest.approx(300 / short["calls_per_second"], abs=1e-3)


@pytest.mark.parametrize("server", [THREE_DAYS, UP, None])
def test_the_rate_is_continuous_and_monotone_across_the_minimum(server):
    """From 0 s (the fallback's rate) to 60 s (the minimum's) the rate moves without a
    step, and a longer window never gives a higher rate past the minimum."""
    fallback = 300 / (server.seconds if server else ASSUMED_WINDOW_SECONDS)
    assert _calls_over(0, server)["calls_per_second"] == pytest.approx(fallback)
    below = _calls_over(MIN_WINDOW_SECONDS - 1e-6, server)["calls_per_second"]
    assert below == pytest.approx(300 / MIN_WINDOW_SECONDS, rel=1e-6)
    assert _calls_over(30, server)["calls_per_second"] == pytest.approx(
        (300 / MIN_WINDOW_SECONDS + fallback) / 2
    )
    rates = [_calls_over(s, server)["calls_per_second"] for s in (60, 61, 120, 3600)]
    assert rates == sorted(rates, reverse=True)


def test_an_untrusted_server_window_is_not_used():
    assert rate_window(None, None, ServerWindow(MIN_WINDOW_SECONDS - 1, UPTIME)).basis == (
        ASSUMED_24H
    )
    assert rate_window(None, None, ServerWindow(MIN_WINDOW_SECONDS, UPTIME)).basis == UPTIME


def test_the_server_window_is_read_only_when_needed():
    calls: list[int] = []

    def read() -> ServerWindow:
        calls.append(1)
        return UP

    assert rate_window(FIRST, LAST, read).basis == OBSERVED and not calls
    assert rate_window(None, None, read).basis == UPTIME and calls == [1]


def test_postgres_17s_server_computed_window_is_observed():
    window = rate_window(None, None, None, observed_seconds=7_200.5)
    assert (window.basis, window.seconds) == (OBSERVED, 7_200.5)
    # It takes the place of first/last seen, and a short one blends the same way.
    assert rate_window(FIRST, LAST, None, observed_seconds=120).seconds == 120
    assert rate_window(None, None, UP, observed_seconds="not a number").basis == UPTIME


def test_one_execution_over_a_trusted_window_is_a_small_positive_rate():
    fields = statement_rate_fields(1, "2026-11-05 12:00:00", "2026-11-05 12:10:00", None)
    assert fields["calls_per_second"] == pytest.approx(1 / 600)


def test_a_very_old_statement_has_a_tiny_finite_rate():
    fields = statement_rate_fields(10, "1900-01-01 00:00:00.000", LAST, None)
    assert fields["rate_window_basis"] == OBSERVED
    assert 0 < fields["calls_per_second"] < 1e-8
    assert math.isfinite(fields["frequency_per_hour"])


@pytest.mark.parametrize("count", [-5, float("nan"), float("inf"), None, "x"])
def test_a_rate_is_never_negative_or_infinite(count):
    cases = [(FIRST, LAST, None), (None, None, UP), (None, None, None), (FIRST, FIRST, UP)]
    start = datetime(2026, 11, 5)
    cases.append((start, start + timedelta(seconds=10), None))
    for first, last, server in cases:
        fields = statement_rate_fields(count, first, last, server)
        for key in ("calls_per_second", "frequency_per_hour"):
            assert math.isfinite(fields[key]) and fields[key] >= 0


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-05 14:34:34.873",
        "2026-11-05T14:34:34.873",
        "2026-11-05T14:34:34.873Z",
        "2026-11-05/14:34:34.873",  # Oracle's FIRST_LOAD_TIME form
        "2026-11-0514:34:34.873",  # SQL Server FOR JSON's missing space
        datetime(2026, 11, 5, 14, 34, 34, 873000),
    ],
)
def test_every_collector_timestamp_form_is_read(value):
    window = rate_window("2026-11-05 14:00:00", value, None)
    assert window.basis == OBSERVED
    assert window.seconds == pytest.approx(34 * 60 + 34.873)


def test_a_zoned_time_is_converted_to_utc_and_a_zoneless_one_is_taken_as_written():
    """Documented, not corrected: a zone-less time compares with a zoned one only when
    it is UTC too, and a window across a daylight-saving change is off by that hour."""
    window = rate_window("2026-11-05T14:00:00+02:00", "2026-11-05 12:10:00", None)
    assert (window.basis, window.seconds) == (OBSERVED, 600)
    # 01:50 to 01:00 local across a fall-back is 70 real minutes, read as none here.
    assert rate_window("2026-11-01 01:50:00", "2026-11-01 01:00:00", UP).basis == UPTIME


def test_the_server_window_names_its_basis():
    assert server_window(uptime_seconds="144080") == ServerWindow(144_080, UPTIME)
    assert server_window(uptime_hours=14) == ServerWindow(50_400, UPTIME)
    assert server_window(uptime_seconds=100, uptime_hours=14) == ServerWindow(100, UPTIME)
    assert server_window(stats_reset_seconds="3600.5") == ServerWindow(3_600.5, STATS_RESET)


def test_postgres_30_days_of_counts_and_a_restart_10_minutes_ago():
    """The review's case: pg_stat_statements kept 30 days of counts at 1 call/s across a
    restart 10 minutes ago. The window is the 30 days since the reset; a PostgreSQL caller
    never passes the uptime (with it, the old helper gave 4320 calls/s)."""
    days = 30 * 86_400
    window = server_window(stats_reset_seconds=days)
    fields = statement_rate_fields(days, None, None, window)
    assert (fields["rate_window_basis"], fields["calls_per_second"]) == (STATS_RESET, 1.0)
    # The reset age wins over an uptime even if a caller passed both.
    assert server_window(uptime_seconds=600, stats_reset_seconds=days).basis == STATS_RESET


@pytest.mark.parametrize("bad", [None, "", "abc", -1, 0, float("nan"), float("inf"), True])
def test_an_unusable_server_figure_is_ignored(bad):
    assert server_window(uptime_seconds=bad, uptime_hours=bad, stats_reset_seconds=bad) is None
    assert server_window(uptime_seconds=bad, uptime_hours=2) == ServerWindow(7_200, UPTIME)


def _pattern(basis):
    return {"query_id": basis or "none", "rate_window_basis": basis}


def test_the_facts_count_each_basis_and_state_them_once():
    facts = rate_window_facts(
        [_pattern(OBSERVED), _pattern(OBSERVED), _pattern(UPTIME), _pattern(ASSUMED_24H)]
    )
    assert facts["statement_count"] == 4
    assert facts["observed_count"] == 2
    assert facts["uptime_count"] == 1
    assert facts["stats_reset_count"] == 0
    assert facts["assumed_24h_count"] == 1
    assert facts["unrecorded_count"] == 0
    assert facts["statement"] == (
        "execution counts are divided by each statement's own observed window "
        "(2 statements), the server uptime (1), or an assumed 24 hours (1); costs assume "
        "that rate runs continuously"
    )


def test_the_facts_state_the_bases_even_with_no_24_hour_rate():
    facts = rate_window_facts([_pattern(OBSERVED)] * 156 + [_pattern(UPTIME)] * 82)
    assert facts["statement"] == (
        "execution counts are divided by each statement's own observed window "
        "(156 statements), the server uptime (82), or an assumed 24 hours (0); costs "
        "assume that rate runs continuously"
    )


def test_the_facts_name_the_stats_reset_only_when_a_rate_used_it():
    facts = rate_window_facts([_pattern(STATS_RESET), _pattern(ASSUMED_24H)])
    assert facts["stats_reset_count"] == 1
    assert facts["statement"] == (
        "execution counts are divided by each statement's own observed window "
        "(0 statements), the server uptime (0), the time since the statement statistics "
        "were reset (1), or an assumed 24 hours (1); costs assume that rate runs "
        "continuously"
    )


def test_the_facts_state_the_statements_that_recorded_no_window():
    """A collector output from before #1893 records no basis: its rates assumed 24 hours,
    and the deliverables say so rather than nothing."""
    facts = rate_window_facts([_pattern(None), _pattern(None), "junk"])
    assert (facts["statement_count"], facts["unrecorded_count"]) == (2, 2)
    assert facts["statement"] == (
        "execution counts are divided by each statement's own observed window "
        "(0 statements), the server uptime (0), or an assumed 24 hours (0); 2 statements "
        "recorded no window (an older collector output, whose rates assumed 24 hours, or "
        "a Performance Insights rate); costs assume that rate runs continuously"
    )


def test_the_facts_of_no_statements():
    facts = rate_window_facts([])
    assert facts["statement_count"] == 0 and facts["statement"] is None
