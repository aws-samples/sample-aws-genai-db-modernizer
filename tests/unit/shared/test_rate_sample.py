# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Call rates over the load period, from two snapshots of the counters (#1908).

A collection reads the cumulative counters, waits the sampling interval, then reads
them again. A statement's rate is the change in its count over the time between the two
reads by the database's own clock (basis ``sampled``). A statement that ran no more, or
whose count went down, or whose server reset its counters between the reads, keeps the
single-snapshot rate (#1893's window order) with the reason recorded. An interval of 0
is today's single snapshot, bit for bit.
"""

from __future__ import annotations

import math

import pytest

from src.contracts.collector_input import DEFAULT_RATE_SAMPLE_SECONDS, CollectionOptions
from src.contracts.collector_output import QueryPattern
from src.shared.rate_window import (
    ASSUMED_24H,
    OBSERVED,
    SAMPLED,
    UPTIME,
    RateSample,
    ServerWindow,
    rate_sample,
    rate_window_facts,
    statement_rate_fields,
    take_live_sample,
)

FIRST, LAST = "2026-11-05 02:00:00", "2026-11-05 04:00:00"  # two hours observed
ON = rate_sample(300, 301.5, ServerWindow(86_400, UPTIME))


def _fields(count, first_count, *, sample=ON, second_count=None):
    return statement_rate_fields(
        count, FIRST, LAST, None, sample=sample, first_count=first_count, second_count=second_count
    )


def test_the_default_interval_is_300_seconds():
    assert DEFAULT_RATE_SAMPLE_SECONDS == 300
    assert CollectionOptions().rate_sample_seconds == 300
    assert CollectionOptions(rate_sample_seconds=0).rate_sample_seconds == 0


@pytest.mark.parametrize("bad", [-1, 1.5, "300", True])
def test_the_interval_is_a_whole_number_of_seconds_0_or_more(bad):
    with pytest.raises(ValueError):
        CollectionOptions(rate_sample_seconds=bad)


def test_the_rate_is_the_change_in_the_count_over_the_elapsed_time():
    fields = _fields(81_045, 72_000)
    assert fields["rate_window_basis"] == SAMPLED
    assert fields["rate_window_seconds"] == 301.5
    assert fields["calls_per_second"] == pytest.approx(9_045 / 301.5) == 30.0
    assert fields["frequency_per_hour"] == pytest.approx(9_045 / 301.5 * 3600)
    assert fields["rate_sample"] == {
        "status": "both_snapshots",
        "first_count": 72_000,
        "second_count": 81_045,
        "elapsed_seconds": 301.5,
    }
    # The same count over its two-hour observed window reads far lower: idle time.
    single = statement_rate_fields(81_045, FIRST, LAST, None)
    assert single["calls_per_second"] == pytest.approx(11.26, abs=0.01)


def test_a_second_count_from_its_own_snapshot_wins_over_the_execution_count():
    """A live collector reads both snapshots apart from its statement query."""
    fields = _fields(80_000, 72_000, second_count=72_300)
    assert fields["calls_per_second"] == pytest.approx(300 / 301.5)
    assert fields["rate_sample"]["second_count"] == 72_300


def test_a_statement_only_in_the_second_snapshot_ran_all_its_calls_in_the_interval():
    fields = _fields(603, None)
    assert fields["rate_window_basis"] == SAMPLED
    assert fields["calls_per_second"] == pytest.approx(603 / 301.5)
    assert fields["rate_sample"]["status"] == "second_snapshot_only"
    assert fields["rate_sample"]["first_count"] is None


def test_a_lower_count_keeps_the_single_snapshot_rate():
    single = statement_rate_fields(73_015, FIRST, LAST, None)
    fields = _fields(73_015, 80_000)
    assert fields["rate_window_basis"] == OBSERVED
    for key in ("frequency_per_hour", "calls_per_second", "rate_window_seconds"):
        assert fields[key] == single[key]
    assert fields["rate_sample"]["status"] == "count_decreased"
    assert fields["rate_sample"]["first_count"] == 80_000


def test_no_calls_is_at_most_one_call_over_the_interval():
    """Maintainer decision on #1980: min(single-snapshot rate, 1 / elapsed). 73,015 calls
    over two hours is 10.1/s, far above one call in 301.5 s."""
    fields = _fields(73_015, 73_015)
    assert fields["rate_window_basis"] == SAMPLED
    assert fields["calls_per_second"] == pytest.approx(1 / 301.5)
    assert fields["rate_window_seconds"] == 301.5
    assert fields["rate_sample"]["status"] == "no_calls_in_interval"
    assert fields["calls_per_second"] > 0


def test_no_calls_keeps_a_single_snapshot_rate_already_under_one_call():
    """A rare statement: 10 calls over 2 hours is under one call in 301.5 s."""
    single = statement_rate_fields(10, FIRST, LAST, None)
    fields = _fields(10, 10)
    assert fields["rate_window_basis"] == OBSERVED
    assert fields["calls_per_second"] == single["calls_per_second"] == pytest.approx(10 / 7200)
    assert fields["rate_sample"]["status"] == "no_calls_in_interval"


def test_no_calls_never_rates_above_one_call():
    """The ordering case from the review: the same history (100,000 calls over a day),
    0 calls in the interval must read lower than 1 call."""
    day = ("2026-11-04 04:00:00", "2026-11-05 04:00:00")

    def rate(first_count, count):
        return statement_rate_fields(count, *day, None, sample=ON, first_count=first_count)[
            "calls_per_second"
        ]

    zero, one = rate(100_000, 100_000), rate(100_000, 100_001)
    assert 0 < zero <= one
    assert one == pytest.approx(1 / 301.5)


def test_a_counter_reset_between_the_snapshots_keeps_every_single_snapshot_rate():
    """The server's counters restarted inside the interval (an uptime or a stats reset
    age shorter than it): no difference of counts means anything."""
    reset = rate_sample(300, 301.5, ServerWindow(120, UPTIME))
    assert reset.outcome == "counters_reset"
    fields = _fields(73_015, 72_000, sample=reset)
    assert fields["rate_window_basis"] == OBSERVED
    assert fields["rate_sample"]["status"] == "counters_reset"


@pytest.mark.parametrize("elapsed", [None, 0, -3.0, math.nan, math.inf, "soon"])
def test_an_interval_the_database_clock_did_not_measure_keeps_the_single_snapshot(elapsed):
    sample = rate_sample(300, elapsed, None)
    assert sample.outcome == "unavailable"
    fields = _fields(73_015, 72_000, sample=sample)
    assert fields["rate_window_basis"] == OBSERVED
    assert fields["rate_sample"] == {
        "status": "unavailable",
        "first_count": None,
        "second_count": None,
        "elapsed_seconds": None,
    }


@pytest.mark.parametrize("seconds", [0, None, -5])
def test_an_interval_of_0_is_todays_single_snapshot_bit_for_bit(seconds):
    sample = rate_sample(seconds, 300.0, None)
    assert sample.outcome == "off"
    today = statement_rate_fields(73_015, FIRST, LAST, None)
    assert _fields(73_015, 72_000, sample=sample) == today
    assert statement_rate_fields(73_015, FIRST, LAST, None, sample=None) == today
    assert "rate_sample" not in today


def test_without_a_window_of_its_own_a_fallback_still_assumes_24_hours():
    fields = statement_rate_fields(86_400, None, None, None, sample=ON, first_count=90_000)
    assert fields["rate_window_basis"] == ASSUMED_24H
    assert fields["calls_per_second"] == 1.0


def test_the_sampled_fields_validate_against_the_contract():
    base = {"query_id": "q", "query_text": "SELECT 1", "tables_accessed": ["t"]}
    for first_count in (72_000, None, 73_015):
        pattern = QueryPattern(**base, execution_count=73_015, **_fields(73_015, first_count))
        assert pattern.rate_sample is not None
    old = QueryPattern(**base, frequency_per_hour=1.0)
    assert old.rate_sample is None


def _snapshot_reader(snapshots):
    calls = iter(snapshots)

    def read():
        return next(calls)

    return read


def _rows(clock, *entries, expected=None):
    """A snapshot: the clock row (with the entries the server counted, ``expected``,
    all of them by default), then each ``(group, entry, count[, age])``."""
    rows = [
        {
            "group_key": e[0],
            "entry_key": e[1],
            "execution_count": e[2],
            "entry_age_seconds": e[3] if len(e) > 3 else None,
        }
        for e in entries
    ]
    count = len(rows) if expected is None else expected
    return [{"sampled_at": clock, "entry_count": count}] + rows


def test_a_live_sample_waits_between_two_snapshots_around_the_statement_query():
    order: list[str] = []
    first = _rows("2026-11-05T10:00:00.000000", ("a", "a1", 100), ("a", "a2", 50), ("b", "b1", 7))
    # a1 ran 30 more times; a2 was evicted and a3 cached; b1 was re-cached (a new entry);
    # c is new.
    second = _rows(
        "2026-11-05T10:05:00.250000",
        ("a", "a1", 130),
        ("a", "a3", 12),
        ("b", "b1x", 4),
        ("c", "c1", 9),
    )
    reads = _snapshot_reader([first, second])

    def read():
        order.append("snapshot")
        return reads()

    def collect():
        order.append("statements")
        return ["rows"]

    result, live = take_live_sample(read, 300, collect, sleep=lambda s: order.append(f"wait {s}"))
    assert result == ["rows"]
    assert order == ["snapshot", "wait 300", "statements", "snapshot"]
    sample = live.sample(None)
    assert sample.elapsed_seconds == 300.25 and sample.outcome == "sampled"
    assert live.counts("a") == {"first_count": 100, "second_count": 142}
    assert live.counts("b") == {"first_count": None, "second_count": 4}
    assert live.counts("c") == {"first_count": None, "second_count": 9}
    a = statement_rate_fields(142, None, None, None, sample=sample, **live.counts("a"))
    assert a["calls_per_second"] == pytest.approx(42 / 300.25)


def test_a_statement_missing_from_the_second_live_snapshot_is_unmeasured():
    reads = _snapshot_reader([_rows(1_000.0, ("a", "a1", 1)), _rows(1_300.0, ("a", "a1", 9))])
    _, live = take_live_sample(reads, 300, list, sleep=lambda s: None)
    sample = live.sample(None)
    assert live.counts("gone") == {"unmeasured": True}
    fields = statement_rate_fields(5, FIRST, LAST, None, sample=sample, **live.counts("gone"))
    assert fields["rate_window_basis"] == OBSERVED
    assert fields["rate_sample"]["status"] == "unavailable"


@pytest.mark.parametrize(
    "snapshots",
    [
        [_rows(None, ("a", "a1", 1)), _rows("2026-11-05T10:05:00", ("a", "a1", 9))],
        [_rows("2026-11-05T10:05:00", ("a", "a1", 1)), _rows("2026-11-05T10:00:00")],
        [RuntimeError("permission denied"), _rows(1_300.0)],
    ],
    ids=["no-clock", "clock-went-back", "snapshot-failed"],
)
def test_a_live_sample_without_a_forward_database_clock_is_unavailable(snapshots):
    calls = iter(snapshots)

    def read():
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    _, live = take_live_sample(read, 300, list, sleep=lambda s: None)
    assert live.sample(None).outcome == "unavailable"


def test_an_interval_of_0_reads_no_snapshot_and_never_waits():
    def read():
        raise AssertionError("no snapshot at 0 seconds")

    result, live = take_live_sample(read, 0, lambda: ["rows"], sleep=read)
    assert result == ["rows"] and live is None


def test_the_server_window_is_compared_with_the_elapsed_time():
    reads = _snapshot_reader([_rows(1_000.0, ("a", "a1", 1)), _rows(1_300.0, ("a", "a1", 9))])
    _, live = take_live_sample(reads, 300, list, sleep=lambda s: None)
    assert live.sample(ServerWindow(200, UPTIME)).outcome == "counters_reset"
    assert live.sample(lambda: ServerWindow(9_000, UPTIME)).outcome == "sampled"


def _with(basis, status=None):
    row = {"rate_window_basis": basis}
    if status:
        row["rate_sample"] = {"status": status}
    return row


def test_the_facts_name_the_sampled_basis_and_why_statements_fell_back():
    facts = rate_window_facts(
        [_with(SAMPLED, "both_snapshots")] * 5
        + [_with(SAMPLED, "second_snapshot_only")]
        + [_with(OBSERVED, "no_calls_in_interval")] * 2
        + [_with(UPTIME, "count_decreased")]
    )
    assert facts["sampled_count"] == 6
    assert facts["sample_no_calls_count"] == 2
    assert facts["sample_fallback_count"] == 1
    assert facts["statement"] == (
        "execution counts are divided by the time between two snapshots of the counters "
        "(6 statements), each statement's own observed window (2), the server uptime (1), "
        "or an assumed 24 hours (0); 2 statements made no calls between the snapshots and "
        "are rated at most one call over the interval; 1 statement kept a single-snapshot "
        "rate (1 whose count went down); costs assume that rate runs continuously"
    )


def test_the_facts_say_when_the_interval_could_not_be_measured():
    facts = rate_window_facts(
        [_with(OBSERVED, "unavailable"), _with(ASSUMED_24H, "counters_reset")]
    )
    assert facts["sampled_count"] == 0
    assert facts["statement"] == (
        "execution counts are divided by the time between two snapshots of the counters "
        "(0 statements), each statement's own observed window (1), the server uptime (0), "
        "or an assumed 24 hours (1); 2 statements kept a single-snapshot rate (1 after a "
        "counter reset between the snapshots, 1 whose interval was not measured); costs "
        "assume that rate runs continuously"
    )


def test_a_collection_without_sampling_states_the_rates_as_before():
    facts = rate_window_facts([_with(OBSERVED), _with(UPTIME)])
    assert facts["sampled_count"] == 0 and facts["sample_fallback_count"] == 0
    assert facts["statement"] == (
        "execution counts are divided by each statement's own observed window "
        "(1 statement), the server uptime (1), or an assumed 24 hours (0); costs assume "
        "that rate runs continuously"
    )


def test_rate_sample_records_what_it_measured():
    assert ON == RateSample(seconds=300, elapsed_seconds=301.5, outcome="sampled")


# -- the review on #1980: a cut first read, a short interval --------------------------


def _live(first, second):
    reads = _snapshot_reader([first, second])
    _, live = take_live_sample(reads, 300, list, sleep=lambda s: None)
    return live


def _status(live, group, count=1_300_300):
    sample = live.sample(None)
    fields = statement_rate_fields(count, FIRST, LAST, None, sample=sample, **live.counts(group))
    return fields["rate_sample"]["status"], fields


def test_a_statement_cut_from_the_first_read_is_not_new():
    """The probe on #1980: the remote output cut the first snapshot to its clock row. An
    entry with 1,000,000 calls before and 1,000,300 after is not 3334 calls/s."""
    first = _rows(1_000.0, expected=500)
    second = _rows(1_300.0, ("a", "a1", 1_000_300, 86_400.0))
    status, fields = _status(_live(first, second), "a", 1_000_300)
    assert status == "snapshot_incomplete"
    assert fields["rate_window_basis"] != SAMPLED


@pytest.mark.parametrize(
    ("age", "first_complete", "status"),
    [
        (120.0, False, "second_snapshot_only"),  # started counting inside the interval
        (301.5, False, "second_snapshot_only"),  # within the clocks' slack
        (900.0, True, "snapshot_incomplete"),  # older than the first read, yet missing
        (None, True, "second_snapshot_only"),  # no age: the whole first read says new
        (None, False, "snapshot_incomplete"),  # no age and a cut first read
    ],
)
def test_a_statement_missing_from_the_first_read_is_new_only_when_it_started_after_it(
    age, first_complete, status
):
    first = _rows(1_000.0, ("z", "z1", 5), expected=None if first_complete else 9)
    second = _rows(1_300.0, ("z", "z1", 5), ("a", "a1", 600, age))
    got, _ = _status(_live(first, second), "a", 600)
    assert got == status


def test_one_old_entry_cut_from_the_first_read_makes_its_statement_incomplete():
    first = _rows(1_000.0, ("a", "a1", 100), expected=2)
    second = _rows(1_300.0, ("a", "a1", 160, 5_000.0), ("a", "a2", 999, 5_000.0))
    got, _ = _status(_live(first, second), "a", 1_159)
    assert got == "snapshot_incomplete"


def test_a_cut_second_read_keeps_every_single_snapshot_rate(caplog):
    first = _rows(1_000.0, ("a", "a1", 100))
    second = _rows(1_300.0, ("a", "a1", 160), expected=40)
    with caplog.at_level("WARNING"):
        live = _live(first, second)
    assert "second snapshot came back with 1 entries" in caplog.text
    got, fields = _status(live, "a", 160)
    assert got == "snapshot_incomplete" and fields["rate_window_basis"] == OBSERVED


def test_a_clock_row_without_its_entry_count_is_not_a_whole_read():
    first = [{"sampled_at": 1_000.0}]
    second = _rows(1_300.0, ("a", "a1", 600))
    got, _ = _status(_live(first, second), "a", 600)
    assert got == "snapshot_incomplete"


@pytest.mark.parametrize(
    ("asked", "elapsed", "outcome"),
    [
        (300, 0.5, "interval_too_short"),  # a statement_timeout cancelled the wait
        (300, 149.9, "interval_too_short"),  # under half the interval asked
        (300, 150.0, "sampled"),
        (5, 5.0, "interval_too_short"),  # a small setting: under 10 seconds
        (10, 10.0, "sampled"),
    ],
)
def test_a_measured_interval_much_shorter_than_asked_is_not_a_rate(asked, elapsed, outcome):
    sample = rate_sample(asked, elapsed, None)
    assert sample.outcome == outcome
    fields = statement_rate_fields(100, FIRST, LAST, None, sample=sample, first_count=80)
    if outcome == "sampled":
        assert fields["rate_window_basis"] == SAMPLED
    else:
        assert fields["rate_window_basis"] == OBSERVED
        assert fields["rate_sample"]["status"] == "interval_too_short"
        assert fields["rate_sample"]["elapsed_seconds"] == elapsed


def test_the_facts_name_a_short_interval_and_a_cut_snapshot():
    facts = rate_window_facts(
        [_with(OBSERVED, "interval_too_short"), _with(OBSERVED, "snapshot_incomplete")]
    )
    assert facts["sample_fallback_count"] == 2
    assert (
        "2 statements kept a single-snapshot rate (1 whose measured interval was too short, "
        "1 missing from a snapshot that came back incomplete)" in facts["statement"]
    )
