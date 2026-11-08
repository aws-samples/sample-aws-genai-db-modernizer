# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: each assumption states where its value comes from: ``default``, ``flag`` (a
script's ``--growth``), ``file`` (the assumptions file) or ``collected``; one recorded
under ``decisions/assumptions.json`` keeps its own origin when another is set later, and
a decision written before the file existed still loads."""

from __future__ import annotations

import pytest

from src.shared.assumptions import (
    design_context,
    gate_assumption_lines,
    growth_assumption,
    peak_assumption,
    peak_context,
    record_assumptions,
    record_growth,
)
from src.storage.local_store import LocalArtifactStore

KEY = "shop/job-1/decisions/assumptions.json"


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path))


def _collector() -> dict:
    return {"queries": {"query_patterns": [{"query_id": "q", "calls_per_second": 1.0}]}}


def test_the_files_values_are_recorded_with_origin_file(store) -> None:
    record = record_assumptions(
        store,
        "shop",
        "job-1",
        {"growth_multiplier": 3.0, "peak_to_avg_ratio": 2.0},
        via="file",
        file_sha256="ab" * 32,
    )
    assert store.read_json(KEY) == record
    assert record["origins"] == {"growth_multiplier": "file", "peak_to_avg_ratio": "file"}
    assert record["file_sha256"] == "ab" * 32
    lines = gate_assumption_lines(_collector(), record)
    # The line says who set it in words; the facts keep the origin itself (#1991 review).
    assert lines[0].startswith("Growth: 3× (set in the assumptions file)")
    assert lines[1] == "Peak: 2× peak-to-average ratio (set in the assumptions file)"


def test_the_growth_flag_is_stated_as_a_flag(store) -> None:
    record = record_growth(store, "shop", "job-1", 3, via="cli")
    assert growth_assumption(record) == (3.0, "flag")
    assert gate_assumption_lines(_collector(), record)[0].startswith(
        "Growth: 3× (set in the run's options)"
    )


def test_a_flag_after_the_file_keeps_the_files_peak(store) -> None:
    record_assumptions(store, "shop", "job-1", {"peak_to_avg_ratio": 2.0}, via="file")
    record = record_growth(store, "shop", "job-1", 4, via="cli")
    assert record["origins"] == {"peak_to_avg_ratio": "file", "growth_multiplier": "flag"}
    assert growth_assumption(record) == (4.0, "flag")
    assert peak_context(record) == {"peak_to_avg_ratio": 2.0, "peak_origin": "file"}


def test_only_the_peak_reaches_the_designers(store) -> None:
    """Growth is a scenario line, never a design rate: the designers' context carries
    the customer's peak alone."""
    record_assumptions(
        store, "shop", "job-1", {"growth_multiplier": 3.0, "peak_to_avg_ratio": 2.0}, via="file"
    )
    assert design_context(store, "shop", "job-1") == {
        "peak_to_avg_ratio": 2.0,
        "peak_origin": "file",
    }
    record_growth(store, "shop", "job-2", 3, via="cli")
    assert design_context(store, "shop", "job-2") == {}
    assert design_context(store, "shop", "no-such-job") == {}


def test_the_files_peak_wins_over_a_measured_one() -> None:
    from tests.unit.shared.test_assumptions import _cw

    peak = peak_assumption(_cw(7.0), {"peak_to_avg_ratio": 2.0, "peak_origin": "file"})
    assert (peak["basis"], peak["ratio"], peak["origin"]) == ("customer", 2.0, "file")


@pytest.mark.parametrize(
    "decision,origin",
    [
        # Written by --growth before #1991: origin "customer", via "cli".
        ({"growth_multiplier": 3, "origin": "customer", "via": "cli"}, "flag"),
        # Any other writer stays the customer's.
        ({"growth_multiplier": 3, "origin": "customer", "via": "api"}, "customer"),
        ({"growth_multiplier": 3}, "customer"),
    ],
)
def test_a_decision_written_before_the_file_still_loads(decision, origin) -> None:
    assert growth_assumption(decision) == (3.0, origin)
    assert peak_context(decision) == {}


def test_an_invalid_recorded_value_keeps_the_default() -> None:
    assert growth_assumption({"growth_multiplier": "3", "origins": {}}) == (5.0, "default")
    assert peak_context({"peak_to_avg_ratio": 0}) == {}
    assert peak_context({"peak_to_avg_ratio": True}) == {}


def test_record_refuses_an_unknown_or_out_of_range_value(store) -> None:
    with pytest.raises(ValueError):
        record_assumptions(store, "shop", "job-1", {"region": 1.0}, via="file")
    with pytest.raises(ValueError):
        record_assumptions(store, "shop", "job-1", {"growth_multiplier": 0.0}, via="file")
    with pytest.raises(ValueError):
        record_growth(store, "shop", "job-1", 1000, via="cli")
    assert not store.exists(KEY)


def test_one_write_keeps_the_files_hash_with_a_flag(store) -> None:
    """#1991 review: the file's peak with --growth is one write; both origins and the
    file's hash survive, and a later flag keeps the hash while the file's peak stands."""
    record = record_assumptions(
        store,
        "shop",
        "job-1",
        {"peak_to_avg_ratio": 2.0, "growth_multiplier": 3.0},
        via="file",
        file_sha256="cd" * 32,
        origins={"growth_multiplier": "flag"},
    )
    assert record["origins"] == {"peak_to_avg_ratio": "file", "growth_multiplier": "flag"}
    assert record["file_sha256"] == "cd" * 32
    later = record_growth(store, "shop", "job-1", 4, via="cli")
    assert later["file_sha256"] == "cd" * 32
    assert later["origins"] == {"peak_to_avg_ratio": "file", "growth_multiplier": "flag"}
    # Once no value is the file's, the hash names nothing recorded and goes.
    record_assumptions(store, "shop", "job-2", {"growth_multiplier": 3.0}, via="file",
                       file_sha256="ef" * 32)  # fmt: skip
    assert "file_sha256" not in record_growth(store, "shop", "job-2", 4, via="cli")


def test_a_saved_value_out_of_range_says_why_the_default_stands() -> None:
    """#1991 review: before the ranges, --growth took any positive value; such a job
    still loads, at the default, and its line says the saved value is not used."""
    old = {"growth_multiplier": 150, "origin": "customer", "via": "cli"}
    assert growth_assumption(old) == (5.0, "default")
    line = gate_assumption_lines(_collector(), old)[0]
    assert line.startswith("Growth: 5× (default; the saved growth 150× is out of range (")
    assert "so it is not used)" in line
    assert gate_assumption_lines(_collector(), None)[0].startswith("Growth: 5× (default),")
