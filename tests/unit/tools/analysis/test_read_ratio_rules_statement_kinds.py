"""How a locking read, an UPDATE and a DELETE count in each read-ratio rule (#1392).

Since #1391 a table profile counts a write exactly when ``write_groups.written_tables``
names the table, so a locking read (``SELECT ... FOR UPDATE``) counts as a write of every
table it locks (ADR-031 Amendment 9). Each rule below reads that count through
``read_ratio`` (or, for OpenSearch's append credit, ``insert_ratio``); these tests pin
what each statement kind does to it, from a collector output through
``build_table_profiles`` to the rule.

- OpenSearch time-series "append-heavy" (+15 complexity and its rationale): appends
  only. A data stream takes appends; row locks, updates, deletes and upserts do not suit
  it, so none of them earns the credit.
- ElastiCache write-heavy (read ratio < 0.3, a cache-invalidation penalty), Aurora's
  mixed band (0.3 <= read ratio <= 0.8, OLTP) and DynamoDB's read ratio < 0.5 (write
  throughput): every write kind, a locking read included, is write-side traffic for
  these rules' reasons (it changes or locks the rows a cache holds, it is the
  transactional OLTP work Aurora is credited for, and it is the conditional or
  transactional write traffic DynamoDB takes), so they keep counting all of them.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.contracts.analysis_output import Confidence, Pattern, ScoreBreakdown, WorkloadAnalysis
from src.tools.analysis.aurora_common_analysis_tools import _apply_aurora_common_adjustments
from src.tools.analysis.dynamodb_analysis_tools import _apply_dynamodb_adjustments
from src.tools.analysis.opensearch_analysis_tools import (
    WorkloadType,
    _apply_opensearch_timeseries_adjustments,
    _build_rationale,
)
from src.tools.analysis.redis_analysis_tools import _apply_redis_adjustments, _build_concerns
from src.tools.analysis.scoring import TableProfile, build_table_profiles

_TABLE = "events"
_APPEND_RATIONALE = "Append-heavy write pattern suits OpenSearch data streams."

# Each statement kind, as (query_type, text) per source dialect. A locking read is
# collected as a SELECT.
_KINDS: dict[str, dict[str, tuple[str, str]]] = {
    "locking_read": {
        "postgresql": ("SELECT", "SELECT id FROM events WHERE id = ? FOR UPDATE"),
        "mysql": ("SELECT", "SELECT id FROM events WHERE id = ? LOCK IN SHARE MODE"),
        "sqlserver": ("SELECT", "SELECT id FROM events WITH (UPDLOCK) WHERE id = ?"),
    },
    "update": {
        "postgresql": ("UPDATE", "UPDATE events SET n = n + 1 WHERE id = ?"),
        "mysql": ("UPDATE", "UPDATE events SET n = n + 1 WHERE id = ?"),
        "sqlserver": ("UPDATE", "UPDATE events SET n = n + 1 WHERE id = ?"),
    },
    "delete": {
        "postgresql": ("DELETE", "DELETE FROM events WHERE id = ?"),
        "mysql": ("DELETE", "DELETE FROM events WHERE id = ?"),
        "sqlserver": ("DELETE", "DELETE FROM events WHERE id = ?"),
    },
    "upsert": {
        "postgresql": (
            "INSERT",
            "INSERT INTO events (id, n) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET n = 1",
        ),
        "mysql": (
            "INSERT",
            "INSERT INTO events (id, n) VALUES (?, ?) ON DUPLICATE KEY UPDATE n = 1",
        ),
    },
    "insert": {
        "postgresql": ("INSERT", "INSERT INTO events (id, n) VALUES (?, ?)"),
        "mysql": ("INSERT", "INSERT INTO events (id, n) VALUES (?, ?)"),
        "sqlserver": ("INSERT", "INSERT INTO events (id, n) VALUES (?, ?)"),
    },
}

_NOT_APPENDS = ("locking_read", "update", "delete", "upsert")
_WRITE_SIDE = ("locking_read", "update", "delete", "insert")

_BASE = ScoreBreakdown(
    pattern_match_score=50, complexity_score=50, performance_score=50, cost_score=50
)


def _cases(kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(kind, engine) for kind in kinds for engine in _KINDS[kind]]


def _profile(kind: str, engine: str, *, plain_reads: int, statements: int) -> TableProfile:
    """``plain_reads`` plain SELECTs and ``statements`` statements of ``kind`` on one
    time-series table, read in ``engine``'s dialect."""
    qtype, text = _KINDS[kind][engine]
    queries = [
        {
            "query_id": f"r{i}",
            "query_type": "SELECT",
            "query_text": "SELECT id, n FROM events WHERE ts > ?",
            "tables_accessed": [_TABLE],
            "calls_per_second": 5.0,
        }
        for i in range(plain_reads)
    ] + [
        {
            "query_id": f"w{i}",
            "query_type": qtype,
            "query_text": text,
            "tables_accessed": [_TABLE],
            "calls_per_second": 5.0,
        }
        for i in range(statements)
    ]
    collector = {
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {"tables": [{"table_id": _TABLE, "columns": []}]},
        "queries": {"query_patterns": queries},
    }
    pattern = Pattern(
        pattern_id="ts-1",
        pattern_type="time-range-query",
        confidence=Confidence.HIGH,
        description="time range",
        query_ids=["r0"],
        table_ids=[_TABLE],
    )
    return build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[pattern]))[_TABLE]


def _all_reads(profile: TableProfile) -> TableProfile:
    """The same table with every statement a plain read: the rules' reference point."""
    return _profile("insert", "postgresql", plain_reads=profile.total_query_count, statements=0)


# -- The profile ---------------------------------------------------------------


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("kind", "engine"), _cases(_WRITE_SIDE))
def test_every_write_side_kind_counts_as_a_write(kind: str, engine: str) -> None:
    profile = _profile(kind, engine, plain_reads=1, statements=3)
    assert (profile.read_query_count, profile.write_query_count) == (1, 3)
    assert profile.read_ratio == pytest.approx(0.25)


@pytest.mark.parametrize(("kind", "engine"), _cases(_NOT_APPENDS))
def test_no_lock_update_delete_or_upsert_is_an_insert(kind: str, engine: str) -> None:
    profile = _profile(kind, engine, plain_reads=1, statements=3)
    assert profile.insert_query_count == 0
    assert profile.insert_ratio == 0.0


@pytest.mark.parametrize("engine", list(_KINDS["insert"]))
def test_a_plain_insert_is_an_insert(engine: str) -> None:
    profile = _profile("insert", engine, plain_reads=1, statements=3)
    assert profile.insert_query_count == 3
    assert profile.insert_ratio == pytest.approx(0.75)


def test_an_insert_select_appends_to_its_target_only() -> None:
    """The table an ``INSERT ... SELECT`` reads gets no append."""
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [{"table_id": "events"}, {"table_id": "staging"}]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "w",
                    "query_type": "INSERT",
                    "query_text": "INSERT INTO events (id) SELECT id FROM staging",
                    "tables_accessed": ["events", "staging"],
                }
            ]
        },
    }
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    assert profiles["events"].insert_query_count == 1
    assert profiles["staging"].insert_query_count == 0


def test_a_locked_source_of_an_insert_select_gets_no_append() -> None:
    """``INSERT ... SELECT ... FOR UPDATE`` appends to its target and locks its source:
    the source counts a write (its lock), never an append."""
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [{"table_id": "events"}, {"table_id": "staging"}]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "w",
                    "query_type": "INSERT",
                    "query_text": "INSERT INTO events (id) SELECT id FROM staging FOR UPDATE",
                    "tables_accessed": ["events", "staging"],
                }
            ]
        },
    }
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    assert profiles["events"].insert_query_count == 1
    assert profiles["staging"].write_query_count == 1
    assert profiles["staging"].insert_query_count == 0


# -- OpenSearch time-series: appends only ----------------------------------------


@pytest.mark.parametrize(("kind", "engine"), _cases(_NOT_APPENDS))
def test_opensearch_append_credit_ignores_locks_updates_deletes_and_upserts(
    kind: str, engine: str
) -> None:
    """Write ratio 0.75 (above the 0.7 threshold), yet no append: no credit, no rationale."""
    profile = _profile(kind, engine, plain_reads=1, statements=3)
    scored = _apply_opensearch_timeseries_adjustments(_BASE, profile)
    reference = _apply_opensearch_timeseries_adjustments(_BASE, _all_reads(profile))
    assert scored.complexity_score == reference.complexity_score
    rationale = _build_rationale(profile, [_ts_pattern()], WorkloadType.TIMESERIES)
    assert _APPEND_RATIONALE not in rationale


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", list(_KINDS["insert"]))
def test_opensearch_append_credit_counts_inserts(engine: str) -> None:
    profile = _profile("insert", engine, plain_reads=1, statements=3)
    scored = _apply_opensearch_timeseries_adjustments(_BASE, profile)
    reference = _apply_opensearch_timeseries_adjustments(_BASE, _all_reads(profile))
    assert scored.complexity_score - reference.complexity_score == 15
    rationale = _build_rationale(profile, [_ts_pattern()], WorkloadType.TIMESERIES)
    assert _APPEND_RATIONALE in rationale


@pytest.mark.parametrize(
    ("inserts", "updates", "credited"),
    [(7, 3, True), (6, 4, False), (0, 10, False)],
)
def test_opensearch_append_threshold_is_the_insert_share(
    inserts: int, updates: int, credited: bool
) -> None:
    """The 0.7 threshold reads the inserts' share of all statements: updates dilute it."""
    insert = _profile("insert", "postgresql", plain_reads=0, statements=inserts)
    update = _profile("update", "postgresql", plain_reads=0, statements=updates)
    total = inserts + updates
    profile = dataclasses.replace(
        insert,
        write_query_count=total,
        insert_query_count=insert.insert_query_count,
        read_ratio=0.0,
        insert_ratio=insert.insert_query_count / total,
    )
    assert update.insert_query_count == 0
    scored = _apply_opensearch_timeseries_adjustments(_BASE, profile)
    reference = _apply_opensearch_timeseries_adjustments(_BASE, _all_reads(profile))
    assert (scored.complexity_score - reference.complexity_score == 15) is credited


def _ts_pattern() -> Pattern:
    return Pattern(
        pattern_id="ts-1",
        pattern_type="time-range-query",
        confidence=Confidence.HIGH,
        description="time range",
        query_ids=["r0"],
        table_ids=[_TABLE],
    )


# -- ElastiCache write-heavy (read ratio < 0.3): every write kind ------------------


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("kind", "engine"), _cases(_WRITE_SIDE))
def test_elasticache_write_heavy_penalty_counts_every_write_kind(kind: str, engine: str) -> None:
    profile = _profile(kind, engine, plain_reads=1, statements=3)  # read ratio 0.25
    scored = _apply_redis_adjustments(_BASE, profile)
    reference = _apply_redis_adjustments(_BASE, _all_reads(profile))
    assert reference.pattern_match_score - scored.pattern_match_score == 10
    assert reference.performance_score - scored.performance_score == 10
    assert any("Write-heavy workload" in c for c in _build_concerns(profile, []))


# -- Aurora mixed band (0.3 <= read ratio <= 0.8): every write kind ---------------


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("kind", "engine"), _cases(_WRITE_SIDE))
def test_aurora_mixed_band_counts_every_write_kind(kind: str, engine: str) -> None:
    profile = _profile(kind, engine, plain_reads=1, statements=1)  # read ratio 0.5
    assert profile.read_ratio == pytest.approx(0.5)
    scored = _apply_aurora_common_adjustments(_BASE, profile)
    reference = _apply_aurora_common_adjustments(_BASE, _all_reads(profile))
    assert scored.complexity_score - reference.complexity_score == 10


# -- DynamoDB read ratio < 0.5: every write kind ---------------------------------


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("kind", "engine"), _cases(_WRITE_SIDE))
def test_dynamodb_write_ratio_bonus_counts_every_write_kind(kind: str, engine: str) -> None:
    profile = _profile(kind, engine, plain_reads=1, statements=3)  # read ratio 0.25
    scored = _apply_dynamodb_adjustments(_BASE, profile)
    reference = _apply_dynamodb_adjustments(_BASE, _all_reads(profile))
    assert scored.performance_score - reference.performance_score == 10


def test_a_locking_insert_whose_target_is_not_collected_appends_to_nothing() -> None:
    """The target is none of the collected tables, so ``written_tables`` falls back to
    all of them; the statement also locks them, so none of them gets an append."""
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [{"table_id": "staging"}]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "w",
                    "query_type": "INSERT",
                    "query_text": "INSERT INTO archive (id) SELECT id FROM staging FOR UPDATE",
                    "tables_accessed": ["staging"],
                }
            ]
        },
    }
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    assert profiles["staging"].write_query_count == 1
    assert profiles["staging"].insert_query_count == 0


# -- Appends the collected type or the upsert flag hid (#1459) -------------------

# Each one only adds rows, whatever its collected ``query_type``: a data-modifying CTE
# that inserts and ends in a SELECT is collected as OTHER, and ``ON CONFLICT DO NOTHING``
# never overwrites the row it collides with.
_HIDDEN_APPENDS: dict[str, tuple[str, str, str]] = {
    "cte_insert": (
        "postgresql",
        "OTHER",
        "WITH w AS (INSERT INTO events (id, n) VALUES (?, ?) RETURNING id) SELECT id FROM w",
    ),
    "on_conflict_do_nothing": (
        "postgresql",
        "INSERT",
        "INSERT INTO events (id, n) VALUES (?, ?) ON CONFLICT DO NOTHING",
    ),
    "on_conflict_on_constraint_do_nothing": (
        "postgresql",
        "INSERT",
        "INSERT INTO events (id, n) VALUES (?, ?) ON CONFLICT ON CONSTRAINT events_pk DO NOTHING",
    ),
    "on_conflict_keys_do_nothing": (
        "postgresql",
        "INSERT",
        "INSERT INTO events (id, n) VALUES (?, ?) ON CONFLICT (id) DO NOTHING",
    ),
}


def _one_kind_profile(engine: str, qtype: str, text: str) -> TableProfile:
    """One plain read and three statements ``text`` on the events table."""
    queries = [
        {
            "query_id": "r0",
            "query_type": "SELECT",
            "query_text": "SELECT id, n FROM events WHERE ts > ?",
            "tables_accessed": [_TABLE],
        }
    ] + [
        {
            "query_id": f"w{i}",
            "query_type": qtype,
            "query_text": text,
            "tables_accessed": [_TABLE],
        }
        for i in range(3)
    ]
    collector = {
        "metadata": {"source_database": {"engine": engine}},
        "database_schema": {"tables": [{"table_id": _TABLE, "columns": []}]},
        "queries": {"query_patterns": queries},
    }
    return build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))[_TABLE]


@pytest.mark.parametrize("kind", list(_HIDDEN_APPENDS))
def test_a_cte_insert_and_on_conflict_do_nothing_are_appends(kind: str) -> None:
    engine, qtype, text = _HIDDEN_APPENDS[kind]
    profile = _one_kind_profile(engine, qtype, text)
    assert profile.insert_query_count == 3
    assert profile.insert_ratio == pytest.approx(0.75)


@pytest.mark.passes_on_main  # what must stay no append
@pytest.mark.parametrize(
    ("engine", "qtype", "text"),
    [
        ("mysql", "INSERT", "REPLACE INTO events (id, n) VALUES (?, ?)"),
        (
            "postgresql",
            "OTHER",
            "WITH w AS (INSERT INTO events (id, n) VALUES (?, ?) "
            "ON CONFLICT (id) DO UPDATE SET n = 1 RETURNING id) SELECT id FROM w",
        ),
        (
            "postgresql",
            "OTHER",
            "WITH w AS (UPDATE events SET n = 1 WHERE id = ? RETURNING id) SELECT id FROM w",
        ),
        ("postgresql", "OTHER", "WITH w AS (SELECT 'INSERT INTO events' AS s) SELECT s FROM w"),
    ],
)
def test_an_overwrite_or_a_cte_that_inserts_nothing_is_no_append(
    engine: str, qtype: str, text: str
) -> None:
    assert _one_kind_profile(engine, qtype, text).insert_query_count == 0


def test_a_cte_insert_appends_to_its_target_only() -> None:
    """``WITH w AS (INSERT INTO events ... SELECT ... FROM staging) SELECT ...``: the
    table the CTE reads gets no append."""
    collector = {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [{"table_id": "events"}, {"table_id": "staging"}]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "w",
                    "query_type": "OTHER",
                    "query_text": (
                        "WITH w AS (INSERT INTO events (id) SELECT id FROM staging "
                        "RETURNING id) SELECT count(*) FROM w"
                    ),
                    "tables_accessed": ["events", "staging"],
                }
            ]
        },
    }
    profiles = build_table_profiles(collector, WorkloadAnalysis(patterns_detected=[]))
    assert profiles["events"].insert_query_count == 1
    assert profiles["staging"].insert_query_count == 0
