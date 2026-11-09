# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: a value set in the results page's assumptions panel has origin ``ui``, which
every line states as "set in the UI"; the panel's body is checked by the assumptions
file's own parser, and the report contract takes the new origin."""

from __future__ import annotations

import pytest

from src.contracts.synthesis_output import AssumptionItem, GrowthScenarioFacts
from src.shared.assumptions import (
    RERUN_PHASES,
    SETTABLE,
    design_context,
    gate_assumption_lines,
    growth_assumption,
    growth_scenario_facts,
    origin_label,
    record_assumptions,
    settable_ranges,
)
from src.shared.assumptions_file import (
    AssumptionsFileError,
    parse_assumptions,
    read_assumptions_file,
)
from src.storage.local_store import LocalArtifactStore


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path))


def _collector() -> dict:
    return {"queries": {"query_patterns": [{"query_id": "q", "calls_per_second": 1.0}]}}


def test_a_ui_change_is_recorded_and_stated_as_set_in_the_ui(store) -> None:
    record = record_assumptions(
        store, "shop", "job-1", {"growth_multiplier": 3, "peak_to_avg_ratio": 2}, via="ui"
    )
    assert record["origins"] == {"growth_multiplier": "ui", "peak_to_avg_ratio": "ui"}
    assert growth_assumption(record) == (3.0, "ui")
    assert design_context(store, "shop", "job-1") == {
        "peak_to_avg_ratio": 2.0,
        "peak_origin": "ui",
    }
    lines = gate_assumption_lines(_collector(), record)
    assert lines[0].startswith("Growth: 3× (set in the UI)")
    assert lines[1] == "Peak: 2× peak-to-average ratio (set in the UI)"
    assert origin_label("ui") == "set in the UI"


def test_the_growth_scenario_names_the_growth_set_in_the_ui() -> None:
    facts = growth_scenario_facts(
        _collector(), growth=3.0, origin="ui", headline_cost=10.0, price=lambda c: 30.0
    )
    assert "(the growth set in the UI)" in facts["lines"][0]
    GrowthScenarioFacts.model_validate(facts)


def test_the_report_contract_takes_origin_ui() -> None:
    AssumptionItem.model_validate(
        {"key": "growth", "setting": 3.0, "origin": "ui", "line": "Growth: 3× (set in the UI)"}
    )


def test_the_ranges_the_panel_offers_are_the_files() -> None:
    rows = settable_ranges()
    assert {r["key"]: (r["name"], r["min"], r["min_included"], r["max"]) for r in rows} == {
        key: tuple(spec) for key, spec in SETTABLE.items()
    }
    assert {r["key"]: tuple(r["reruns"]) for r in rows} == RERUN_PHASES
    assert RERUN_PHASES["growth_multiplier"] == ("synthesis", "report")
    assert RERUN_PHASES["peak_to_avg_ratio"][0] == "schema_design"


@pytest.mark.parametrize(
    "raw", [b'{"growth": 3}', b'{"peak": 2.5, "growth": 1}', b'\xef\xbb\xbf{"peak": 1}']
)
def test_the_request_and_the_file_take_the_same_values(tmp_path, raw) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(raw)
    assert parse_assumptions(raw, subject="the request") == read_assumptions_file(path)


@pytest.mark.parametrize(
    "raw", [b'{"growth": 0}', b'{"x": 1}', b'{"pricing": 1}', b"[]", b'{"growth": Infinity}']
)
def test_the_request_and_the_file_refuse_the_same_values(tmp_path, raw) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(raw)
    with pytest.raises(AssumptionsFileError):
        read_assumptions_file(path)
    with pytest.raises(AssumptionsFileError):
        parse_assumptions(raw, subject="the request")


def test_a_refused_request_is_named_as_the_request() -> None:
    with pytest.raises(AssumptionsFileError, match="the request is not valid JSON"):
        parse_assumptions(b"{", subject="the request")
    with pytest.raises(AssumptionsFileError, match="the file is not valid JSON"):
        parse_assumptions(b"{")
