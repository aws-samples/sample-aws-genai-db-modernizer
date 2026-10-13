"""Cross-deliverable agreement on the heterogeneous-source Aurora tie (#583/#586).

``migration_waves.build_migration_waves`` (wave 1) and ``synthesis_report.
_source_compatible_aurora_engine`` (the architecture section's cache-layer
"Why" cell, via ``cache_front_description``, and the "stuck on the wrong
engine" risk note) both name the retained Aurora engine for a heterogeneous
source (SQL Server, Oracle, DB2) with no Aurora dialect of its own. When the
resolver's own ``aurora_engine_choice`` is missing and ``query_assignments``
genuinely has both Aurora engines in use (a legacy assignment written before
that choice existed, or a later customer override), that is a real,
unresolved tie: both callers must now go through the same shared helper,
``assignment_resolver.retained_engine_for``, so they cannot name different
engines for the same assignment the way two independent alphabetical sorts
could.
"""

from __future__ import annotations

from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    _source_compatible_aurora_engine,
    build_architecture_recommendation,
)

# A heterogeneous source: no Aurora dialect of its own.
COLLECTOR = {"metadata": {"source_database": {"engine": "sqlserver"}}}

# Both Aurora engines genuinely have assigned queries -- aurora_postgresql's
# settled share (3) is larger than aurora_mysql's (1) -- with no recorded
# aurora_engine_choice. The pre-fix code resolved this by sorting the two
# engine names, which always favored aurora_mysql regardless of this data.
QUERY_ASSIGNMENTS = [
    {"query_id": "q1", "assigned_engine": "aurora_mysql", "source_tables": ["t"]},
    {"query_id": "q2", "assigned_engine": "aurora_postgresql", "source_tables": ["t"]},
    {"query_id": "q3", "assigned_engine": "aurora_postgresql", "source_tables": ["t"]},
    {"query_id": "q4", "assigned_engine": "aurora_postgresql", "source_tables": ["t"]},
]

RANKING = [
    {
        "target": "aurora_mysql",
        "assigned_queries": 1,
        "workload_percent": 1.0,
        "confidence_score": 70,
        "rationale": "placeholder: not under test",
    },
    {
        "target": "aurora_postgresql",
        "assigned_queries": 3,
        "workload_percent": 3.0,
        "confidence_score": 75,
        "rationale": "placeholder: not under test",
    },
    {
        "target": "elasticache",
        "role": "cache_layer",
        "assigned_queries": 0,
        "workload_percent": 0.0,
        "confidence_score": 60,
        "cache_overlay_queries": 5,
        "cache_call_share_percent": 50.0,
        "cache_overlay_owners": None,
        "routed_lead": None,
        "target_tables": 0,
        "monthly_cost_usd": 0,
    },
]


def test_wave1_architecture_and_cache_note_agree_on_the_tied_engine():
    waves = build_migration_waves(
        ranking=RANKING,
        table_assignments=[],
        query_assignments=QUERY_ASSIGNMENTS,
        co_dependency_groups=[],
        cache_overlay=None,
        source_engine="sqlserver",
        known_tables=set(),
    )
    wave1_engine = waves[0]["engines"][0]
    # The settled-workload-share tie-break (ADR-031 Decision 12), not the
    # alphabetically-first "aurora_mysql" the pre-fix code would have named.
    assert wave1_engine == "aurora_postgresql"

    data = SynthesisData(
        job_id="job-1",
        database_name="db",
        collector=COLLECTOR,
        engines={
            "aurora_mysql": EngineArtifacts("aurora_mysql"),
            "aurora_postgresql": EngineArtifacts("aurora_postgresql"),
        },
        assignment={"query_assignments": QUERY_ASSIGNMENTS},
    )

    # The exact helper behind the risk note's Aurora safety-net name agrees
    # with wave 1.
    assert _source_compatible_aurora_engine(data) == wave1_engine

    # The architecture section's cache-layer "Why" cell -- built from the
    # same helper via ``cache_front_description`` -- names the same engine.
    architecture = build_architecture_recommendation(data, RANKING, [])
    cache_row = next(d for d in architecture["databases"] if d["service"] == "elasticache")
    assert "Aurora PostgreSQL" in cache_row["rationale"]
    assert "Aurora MySQL" not in cache_row["rationale"]
