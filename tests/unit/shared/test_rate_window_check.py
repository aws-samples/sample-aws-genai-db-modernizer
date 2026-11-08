# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The rate window's length, dates and divisor, and whether it is representative (#1991).

Since #1901 each statement's rate is its count over its own window. A 28-minute window
taken right after a statistics reset on a server up 198 days is a load test, not the
workload's traffic, and nothing said so. ``rate_window_facts`` now states the window and
checks it: short (under a day), short against the server's uptime, the statements first
seen together (a reset or a restart), and a measured rate several times the server's own
long-window statement rate. Any flag labels the costs and the rate-driven risks a
scenario at the measured rate, with a suggestion to collect over a longer window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.shared.assumptions import assumption_items, gate_assumption_lines
from src.shared.rate_window import (
    OBSERVED,
    SAMPLED,
    UPTIME,
    collection_rate_window_facts,
    rate_window_facts,
)

DAY = 86_400.0
RESET = datetime(2026, 11, 5, 14, 2, 0)


def _observed(i: int, start: datetime, seconds: float, calls: float) -> dict[str, Any]:
    return {
        "query_id": f"q{i}",
        "rate_window_basis": OBSERVED,
        "rate_window_seconds": seconds,
        "calls_per_second": calls,
        "first_seen": start.isoformat(sep=" "),
        "last_seen": (start + timedelta(seconds=seconds)).isoformat(sep=" "),
    }


def _stress() -> list[dict[str, Any]]:
    """The #1987 capture: every statement first seen within a minute of a reset, and
    counted over the 28 minutes of a load test."""
    return [
        _observed(i, RESET + timedelta(seconds=10 * i), 28 * 60 - 10 * i, 50.0) for i in range(4)
    ]


UP_198_DAYS = {"uptime_seconds": 198 * DAY, "read_at": "2026-11-05T14:31:00"}


def test_a_short_window_after_a_reset_is_flagged_and_labels_a_scenario() -> None:
    facts = rate_window_facts(_stress(), UP_198_DAYS)
    assert facts["representative"] is False
    assert facts["flags"] == ["short_window", "short_against_uptime", "after_reset"]
    assert facts["server_uptime_seconds"] == 198 * DAY
    assert facts["window_start"] == "2026-11-05 14:02"
    assert facts["window_end"] == "2026-11-05 14:30"
    assert facts["window_clock"] == "server"
    check = facts["check_statement"]
    assert check.startswith("not representative: ")
    assert "under a day" in check
    assert "on a server up 198 days" in check
    assert "4 of 4 statements were first seen within 5 minutes of 2026-11-05 14:02" in check
    label = facts["scenario_label"]
    assert label.startswith("Scenario at the measured rate:")
    assert "costs and the rate-driven risks" in label
    assert "longer window" in label and "without resetting the statement statistics" in label


def test_the_window_states_its_length_dates_and_the_divisor_reminder() -> None:
    facts = rate_window_facts(_stress(), UP_198_DAYS)
    assert (facts["shortest_window_seconds"], facts["longest_window_seconds"]) == (1650, 1680)
    window = facts["window_statement"]
    assert "from 27.5 minutes to 28 minutes" in window
    assert "between 2026-11-05 14:02 and 2026-11-05 14:30 (the server's clock)" in window
    assert "a short window right after a statistics reset overstates traffic" in window
    assert (
        "a fixed divisor (the server uptime or an assumed 24 hours) can bias either way" in window
    )
    # The divisor count sentence is unchanged (#1893).
    assert facts["statement"].startswith("execution counts are divided by")


def test_a_long_window_on_a_long_running_server_is_representative() -> None:
    start = datetime(2026, 10, 1)
    rows = [_observed(i, start + timedelta(hours=i), 20 * DAY, 5.0) for i in range(3)]
    facts = rate_window_facts(
        rows, {"uptime_seconds": 40 * DAY, "statements_since_start": int(40 * DAY * 20)}
    )
    assert facts["representative"] is True and facts["flags"] == []
    assert facts["scenario_label"] is None
    assert facts["check_statement"].startswith("no sign of an unrepresentative window")
    assert "proven" not in facts["check_statement"]


def test_the_measured_rate_several_times_the_servers_own_is_flagged() -> None:
    """A week of Questions over the uptime is the source's own long-window rate; a
    measured rate 3x or more above it is a burst, whatever the window's length."""
    start = datetime(2026, 10, 1)
    rows = [_observed(i, start + timedelta(hours=i), 2 * DAY, 100.0) for i in range(3)]
    counters = {"uptime_seconds": 10 * DAY, "statements_since_start": int(10 * DAY * 50)}
    facts = rate_window_facts(rows, counters)
    assert facts["flags"] == ["above_source_rate"]
    assert facts["measured_rate_per_second"] == 300.0
    assert facts["server_rate_per_second"] == 50.0
    assert (
        "6× the server's own 50 statements/s over its uptime of 10 days" in facts["check_statement"]
    )


def test_the_servers_own_rate_is_compared_only_over_a_week_or_more() -> None:
    start = datetime(2026, 10, 1)
    rows = [_observed(i, start + timedelta(hours=i), 2 * DAY, 100.0) for i in range(3)]
    facts = rate_window_facts(rows, {"uptime_seconds": 3 * DAY, "statements_since_start": 10})
    assert "above_source_rate" not in facts["flags"]
    assert facts["server_rate_per_second"] is None
    assert (
        "not checked: the server's own statement rate (its uptime, 3 days, is under 7 days"
        in facts["check_statement"]
    )


def test_a_collected_count_on_a_short_uptime_says_why_it_was_not_compared() -> None:
    """The WordPress sample (review of #2045): Questions was collected, but over a 39.8-hour
    uptime, so the check states the uptime, never "not collected"."""
    start = datetime(2026, 4, 21, 22, 21, 51)
    rows = [_observed(i, start + timedelta(hours=i), 22.9 * 3600, 80.0) for i in range(3)]
    counters = {"uptime_seconds": 143_402, "statements_since_start": 20_142_722}
    check = rate_window_facts(rows, counters)["check_statement"]
    assert "(not collected)" not in check
    assert "the server's own statement rate (its uptime, 39.8 hours, is under 7 days" in check


def test_no_count_or_no_uptime_says_which_was_not_collected() -> None:
    rows = [_observed(0, datetime(2026, 10, 1), 20 * DAY, 1.0)]
    assert "the server's own statement rate (not collected)" in str(
        rate_window_facts(rows, {"uptime_seconds": 30 * DAY})["check_statement"]
    )
    assert "the server's own statement rate (its uptime was not collected)" in str(
        rate_window_facts(rows, {"statements_since_start": 5})["check_statement"]
    )


def test_a_postgresql_reset_under_a_day_ago_is_a_reset() -> None:
    """PostgreSQL's statistics reset age names the reset even with no first-seen times."""
    rows = [
        {
            "query_id": "q1",
            "rate_window_basis": "stats_reset",
            "rate_window_seconds": 1680.0,
            "calls_per_second": 10.0,
        }
    ]
    facts = rate_window_facts(rows, {"uptime_seconds": 864_000, "stats_reset_seconds": 1680.0})
    assert facts["flags"] == ["short_window", "short_against_uptime", "after_reset"]
    assert "the statement statistics were reset 28 minutes before" in facts["check_statement"]
    long_ago = rate_window_facts(rows, {"stats_reset_seconds": 2 * DAY})
    assert "after_reset" not in long_ago["flags"]


def test_the_server_rate_reason_names_the_routine_caveat() -> None:
    start = datetime(2026, 10, 1)
    rows = [_observed(i, start + timedelta(hours=i), 2 * DAY, 100.0) for i in range(3)]
    counters = {"uptime_seconds": 10 * DAY, "statements_since_start": int(10 * DAY * 50)}
    check = rate_window_facts(rows, counters)["check_statement"]
    assert "counts a procedure call once" in check


def _sampled(calls: float) -> list[dict[str, Any]]:
    return [
        {
            "query_id": "q1",
            "rate_window_basis": SAMPLED,
            "rate_window_seconds": 300.0,
            "calls_per_second": calls,
            "rate_sample": {"status": "both_snapshots", "elapsed_seconds": 300.0},
        }
    ]


def test_a_sampled_interval_is_short_on_purpose_and_not_flagged_for_it() -> None:
    """#1908 samples five minutes of the load while the collection runs; its length is a
    measurement, not a reset, so only the server's own rate can flag it."""
    facts = rate_window_facts(
        _sampled(4.0), {"uptime_seconds": 30 * DAY, "read_at": "2026-11-05T14:31:00Z"}
    )
    # Nothing else to check it against here: neither representative nor not.
    assert facts["flags"] == [] and facts["representative"] is None
    assert facts["scenario_label"] is None
    assert facts["window_clock"] == "utc"
    assert (facts["window_start"], facts["window_end"]) == ("2026-11-05 14:26", "2026-11-05 14:31")
    assert "each window is 5 minutes" in facts["window_statement"]
    assert facts["check_statement"].startswith(
        "not checked: the window's length (a sampled interval, short on purpose)"
    )


def test_a_sampled_interval_during_a_stress_test_is_flagged_by_the_servers_rate() -> None:
    counters = {"uptime_seconds": 30 * DAY, "statements_since_start": int(30 * DAY * 10)}
    facts = rate_window_facts(_sampled(400.0), counters)
    assert facts["flags"] == ["above_source_rate"]
    assert facts["scenario_label"] is not None


def test_the_uptime_basis_tells_the_uptime_without_server_counters() -> None:
    """A collector output written before the server counters: a statement counted over
    the uptime still tells it."""
    rows = [
        _observed(0, RESET, 28 * 60, 10.0),
        {"query_id": "q9", "rate_window_basis": UPTIME, "rate_window_seconds": 198 * DAY},
    ]
    facts = rate_window_facts(rows)
    assert facts["server_uptime_seconds"] == 198 * DAY


def test_counts_started_together_on_a_long_window_are_not_flagged() -> None:
    """A restart thirty days ago starts every count together; thirty days is no burst."""
    rows = [_observed(i, RESET, 30 * DAY, 1.0) for i in range(3)]
    facts = rate_window_facts(rows, {"uptime_seconds": 30 * DAY})
    assert facts["flags"] == []


def test_no_statements_states_nothing() -> None:
    facts = rate_window_facts([], UP_198_DAYS)
    assert facts["representative"] is None
    assert facts["window_statement"] is None and facts["check_statement"] is None
    assert facts["scenario_label"] is None and facts["flags"] == []


def test_the_collection_carries_its_server_counters() -> None:
    collector = {
        "metadata": {"server_counters": UP_198_DAYS},
        "queries": {"query_patterns": _stress()},
    }
    assert collection_rate_window_facts(collector) == rate_window_facts(_stress(), UP_198_DAYS)
    assert collection_rate_window_facts(None) is None


def test_the_assumptions_block_states_the_window_and_its_check() -> None:
    facts = rate_window_facts(_stress(), UP_198_DAYS)
    items = assumption_items(
        None,
        growth=5.0,
        growth_origin="default",
        price_basis=None,
        rate_window_statement=None,
        rate_windows=facts,
    )
    by_key = {i["key"]: i for i in items}
    assert by_key["rate_window"]["origin"] == "collected"
    assert by_key["rate_window"]["line"] == (
        f"Rate window: {facts['statement']}; {facts['window_statement']} (collected)"
    )
    assert by_key["rate_window_check"]["line"] == (
        f"Rate window check: {facts['check_statement']} (collected)"
    )
    assert by_key["rate_window_check"]["setting"] == "not_representative"


def test_the_gate_states_the_check() -> None:
    collector = {
        "metadata": {"server_counters": UP_198_DAYS},
        "queries": {"query_patterns": _stress()},
    }
    lines = gate_assumption_lines(collector, None)
    assert any(line.startswith("Rate window check: not representative: ") for line in lines)
    assert any("between 2026-11-05 14:02 and 2026-11-05 14:30" in line for line in lines)


def test_an_assumed_24_hours_is_no_measured_window() -> None:
    """A count divided by an assumed 24 hours has no window to check or date: the facts
    say none was measured, and do not call the window representative."""
    rows = [{"query_id": "q1", "rate_window_basis": "assumed_24h", "rate_window_seconds": DAY}]
    facts = rate_window_facts(rows, {"uptime_seconds": 40 * DAY, "read_at": "2026-11-05T14:31:00Z"})
    assert facts["window_start"] is None and facts["typical_window_seconds"] is None
    assert facts["window_statement"].startswith("no window was measured")
    assert facts["check_statement"].startswith(
        "not checked: the window's length (no statement's window was measured)"
    )
    assert facts["flags"] == [] and facts["representative"] is None
