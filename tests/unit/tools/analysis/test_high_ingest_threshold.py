# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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


# --- anti-04 fires only on an append-only table nobody reads (#904) -------------------


def _ingest_table_with(*others: dict) -> dict:
    """``event_log`` at 20 INSERTs/s plus ``others`` (more queries on it)."""
    table = base_table(table_id="public.event_log", table_name="event_log")
    insert = base_query(
        query_id="ins-1",
        query_text="INSERT INTO event_log (message) VALUES ($1)",
        query_type="INSERT",
        tables=["public.event_log"],
        calls_per_second=20.0,
    )
    return wrap_fixture("anti-04-rule", [table], [insert, *others])


def _on_event_log(qid: str, sql: str, query_type: str, cps: float = 1.0) -> dict:
    return base_query(
        query_id=qid,
        query_text=sql,
        query_type=query_type,
        tables=["public.event_log"],
        calls_per_second=cps,
    )


def test_anti04_fires_on_an_append_only_table_nobody_reads() -> None:
    assert _aurora_sees_high_ingest(_ingest_table_with())


@pytest.mark.parametrize(
    ("sql", "query_type"),
    [
        ("SELECT message FROM event_log WHERE id = $1", "SELECT"),
        ("SELECT * FROM event_log ORDER BY id DESC LIMIT 50", "SELECT"),
    ],
    ids=["read-by-key", "recent-rows"],
)
def test_a_plain_read_means_the_rows_are_read(sql: str, query_type: str) -> None:
    """A read by key, with no join and no aggregate, reads the rows too."""
    assert not _aurora_sees_high_ingest(_ingest_table_with(_on_event_log("r1", sql, query_type)))


def _negligible_share() -> float:
    from src.shared.catalog.patterns import pattern

    share = pattern("unbounded-time-series-ingest").negligible_read_share
    assert share is not None
    return share


def test_the_negligible_read_share_is_the_catalogs_one_percent() -> None:
    assert _negligible_share() == 0.01


def _read_at_share(share: float) -> dict:
    """A read whose calls are ``share`` of the table's calls (20 INSERTs/s plus it)."""
    cps = 20.0 * share / (1 - share)
    return _on_event_log("r1", "SELECT message FROM event_log WHERE id = $1", "SELECT", cps)


def test_a_read_below_the_negligible_share_leaves_the_rows_unread() -> None:
    """Maintainer decision on #904: negligible reads count as no reads."""
    assert _aurora_sees_high_ingest(_ingest_table_with(_read_at_share(_negligible_share() * 0.9)))


def test_a_read_at_the_negligible_share_reads_the_rows() -> None:
    assert not _aurora_sees_high_ingest(_ingest_table_with(_read_at_share(_negligible_share())))


@pytest.mark.parametrize(
    ("sql", "query_type"),
    [
        ("UPDATE event_log SET message = $1 WHERE id = $2", "UPDATE"),
        ("DELETE FROM event_log WHERE id = $1", "DELETE"),
    ],
    ids=["update", "delete"],
)
def test_a_table_the_workload_updates_or_deletes_is_not_append_only(
    sql: str, query_type: str
) -> None:
    write = _on_event_log("w1", sql, query_type, 0.1)
    assert not _aurora_sees_high_ingest(_ingest_table_with(write))


def test_a_read_of_another_table_does_not_clear_this_one() -> None:
    other = base_query(
        query_id="r-other",
        query_text="SELECT * FROM users WHERE id = $1",
        query_type="SELECT",
        tables=["public.users"],
        calls_per_second=50.0,
    )
    assert _aurora_sees_high_ingest(_ingest_table_with(other))


def test_anti04_text_states_the_rule_it_applies() -> None:
    description = ANTI_PATTERN_BY_ID["aurora-anti-04"].description
    share = f"{_negligible_share():.0%}"
    assert (
        f"no other writes (no UPDATE, DELETE, MERGE or REPLACE) and reads under {share} of its"
        " calls" in description
    )
    assert "no join or aggregate reads" not in description


@pytest.mark.parametrize(
    ("sql", "query_type"),
    [
        ("REPLACE INTO event_log (id, message) VALUES ($1, $2)", "REPLACE"),
        (
            "MERGE INTO event_log t USING (VALUES ($1, $2)) s (id, message) ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET message = s.message",
            "MERGE",
        ),
    ],
    ids=["replace", "merge"],
)
def test_a_merge_or_replace_is_not_append_only(sql: str, query_type: str) -> None:
    """The text names MERGE and REPLACE because the rule rules them out (review of #1046)."""
    assert not _aurora_sees_high_ingest(_ingest_table_with(_on_event_log("w1", sql, query_type)))
