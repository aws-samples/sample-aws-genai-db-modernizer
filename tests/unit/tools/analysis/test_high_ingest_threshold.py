"""One high-ingest threshold for every engine's detector (#493).

OpenSearch's ``os-07`` (high-ingest) and Aurora's ``aurora-anti-04``
(unbounded time-series ingest) read the same fact, a table's summed INSERT
calls per second, but used to compare it against different numbers (10 and
50). A table at 30 INSERTs/s was "high ingest" for one engine and not for the
other. The threshold now has one definition both detectors read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.tools.analysis.aurora_common_analysis_tools import analyze_aurora_common_use_cases
from src.tools.analysis.aurora_common_pattern_catalog import ANTI_PATTERN_BY_ID
from src.tools.analysis.opensearch_analysis_tools import analyze_opensearch_use_cases
from src.tools.analysis.opensearch_pattern_catalog import PATTERN_BY_ID
from src.tools.analysis.pattern_catalog_base import HIGH_INGEST_CPS_THRESHOLD
from tests.fixtures.opensearch_pattern_fixtures import base_query, base_table, wrap_fixture

SRC = Path(__file__).resolve().parents[4] / "src"


def _append_only_table(insert_cps: float) -> dict:
    """One write-only table: a single INSERT at ``insert_cps`` and no reads."""
    table = base_table(table_id="public.event_log", table_name="event_log")
    query = base_query(
        query_id="ins-1",
        query_text="INSERT INTO event_log (message) VALUES ($1)",
        query_type="INSERT",
        tables=["public.event_log"],
        calls_per_second=insert_cps,
    )
    return wrap_fixture("high-ingest-threshold", [table], [query])


def _opensearch_sees_high_ingest(collector: dict) -> bool:
    analysis = analyze_opensearch_use_cases(collector)
    return any(p.pattern_type == "high-ingest" for p in analysis.patterns_detected)


def _aurora_sees_high_ingest(collector: dict) -> bool:
    _patterns, anti_patterns = analyze_aurora_common_use_cases(collector)
    return any(ap.anti_pattern_type == "unbounded-time-series-ingest" for ap in anti_patterns)


def test_the_threshold_is_assigned_in_exactly_one_module() -> None:
    owners = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            targets = (
                [node.target]
                if isinstance(node, ast.AnnAssign)
                else node.targets if isinstance(node, ast.Assign) else []
            )
            if any(
                isinstance(t, ast.Name) and t.id == "HIGH_INGEST_CPS_THRESHOLD" for t in targets
            ):
                owners.append(str(path.relative_to(SRC.parent)))
    assert owners == ["src/tools/analysis/pattern_catalog_base.py"]


def test_the_shared_value_is_ten_inserts_per_second() -> None:
    """The value both rules now read; see pattern_catalog_base for why 10, not 50."""
    assert HIGH_INGEST_CPS_THRESHOLD == 10.0


@pytest.mark.parametrize(
    "detector", [_opensearch_sees_high_ingest, _aurora_sees_high_ingest], ids=["os-07", "anti-04"]
)
def test_both_detectors_flag_a_table_at_the_threshold(detector) -> None:
    assert detector(_append_only_table(HIGH_INGEST_CPS_THRESHOLD))


@pytest.mark.parametrize(
    "detector", [_opensearch_sees_high_ingest, _aurora_sees_high_ingest], ids=["os-07", "anti-04"]
)
def test_neither_detector_flags_a_table_just_below_it(detector) -> None:
    assert not detector(_append_only_table(HIGH_INGEST_CPS_THRESHOLD - 0.1))


def test_a_rate_between_the_old_values_is_high_ingest_for_both_engines() -> None:
    """30 INSERTs/s was high ingest for OpenSearch (>= 10) but not for Aurora (>= 50)."""
    collector = _append_only_table(30.0)
    assert _opensearch_sees_high_ingest(collector)
    assert _aurora_sees_high_ingest(collector)


def test_the_os07_description_states_the_shared_number() -> None:
    assert f">= {HIGH_INGEST_CPS_THRESHOLD:g} calls/second" in PATTERN_BY_ID["os-07"].description


def test_the_anti04_description_states_the_shared_number() -> None:
    """Both catalog texts are TOML literals; these two checks tie them to the constant."""
    description = ANTI_PATTERN_BY_ID["aurora-anti-04"].description
    assert f"{HIGH_INGEST_CPS_THRESHOLD:g} or more INSERTs per second" in description


def test_anti04_reads_as_retention_not_throughput() -> None:
    """Aurora runs 10 INSERTs/s natively: the concern is rows nobody reads (#884 review)."""
    entry = ANTI_PATTERN_BY_ID["aurora-anti-04"]
    assert entry.severity_weight < 0.7  # MEDIUM in deliverables, never HIGH
    text = f"{entry.description} {entry.guidance}".lower()
    assert "very high rate" not in text and "high-throughput" not in text
    assert "retention" in text or "expire" in text
