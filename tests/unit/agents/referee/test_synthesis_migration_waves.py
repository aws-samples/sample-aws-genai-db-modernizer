"""Synthesis writes ``migration_waves`` to report.json deterministically (#225).

End to end through ``run_synthesis_deterministic``: the roadmap is built from
the same assignment artifact every other synthesis builder reads (ranking,
cache overlay, table assignments, co-dependency groups), with no model call.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-waves"


def _query(qid: str, cps: float, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": f"SELECT * FROM {table} WHERE id = ?",  # nosec B608 -- SQL text is test fixture data, never executed
        "query_type": "SELECT",
        "calls_per_second": cps,
        "rows_returned_avg": 1,
        "tables_accessed": [table],
    }


QUERIES = [
    _query("hot1", 6.0, "users"),
    _query("kv1", 3.0, "users"),
    _query("kv2", 3.0, "sessions"),
    _query("rel1", 2.0, "orders"),
]

ASSIGNMENT = {
    "job_id": JOB,
    "version": 2,
    "query_assignments": [
        {
            "query_id": "hot1",
            "assigned_engine": "dynamodb",
            "source_tables": ["users"],
            "assignment_reason": "highest confidence for dynamodb",
            "cache_engine": "elasticache",
            "cache_pattern": "point_lookup",
        },
        {
            "query_id": "kv1",
            "assigned_engine": "dynamodb",
            "source_tables": ["users"],
            "assignment_reason": "highest confidence for dynamodb",
        },
        {
            "query_id": "kv2",
            "assigned_engine": "dynamodb",
            "source_tables": ["sessions"],
            "assignment_reason": "highest confidence for dynamodb",
        },
        {
            "query_id": "rel1",
            "assigned_engine": "aurora_mysql",
            "source_tables": ["orders"],
            "assignment_reason": "highest confidence for aurora_mysql",
        },
    ],
    "table_assignments": [
        {
            "table_id": "users",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb"],
            "query_count": 2,
        },
        {
            "table_id": "sessions",
            "primary_engine": "dynamodb",
            "engines": ["dynamodb"],
            "query_count": 1,
        },
        {
            "table_id": "orders",
            "primary_engine": "aurora_mysql",
            "engines": ["aurora_mysql"],
            "query_count": 1,
        },
    ],
    "co_dependency_groups": [["kv1", "kv2"]],
}


def _analysis(conf: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": "users", "confidence_score": conf},
            {"table_id": "sessions", "confidence_score": conf},
            {"table_id": "orders", "confidence_score": conf},
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        "cost_estimate": {"monthly_cost_usd": 10},
    }


@pytest.fixture
def store(tmp_path):
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ["dynamodb", "aurora_mysql", "elasticache"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {
                "tables": [{"table_id": "users"}, {"table_id": "sessions"}, {"table_id": "orders"}]
            },
            "queries": {"query_patterns": QUERIES},
        },
    )
    conf = {"dynamodb": 90, "aurora_mysql": 60, "elasticache": 80}
    for engine in engines:
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(conf[engine]))
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", ASSIGNMENT)
    return s


class TestMigrationWaves:
    def test_waves_present_in_the_deterministic_result(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        waves = result["migration_waves"]
        assert waves is not None
        engine_lists = [w["engines"] for w in waves]
        # #321: Aurora moves first; cache fronts it; DynamoDB moves last.
        assert engine_lists == [["aurora_mysql"], ["elasticache"], ["dynamodb"]]
        assert [w["wave"] for w in waves] == [1, 2, 3]

    def test_aurora_wave_is_first_and_carries_the_whole_schema(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        aurora = result["migration_waves"][0]
        assert aurora["engines"] == ["aurora_mysql"]
        assert sorted(aurora["tables"]) == ["orders", "sessions", "users"]
        assert aurora["homogeneity"] == "homogeneous"
        assert "moves 1:1 to Aurora MySQL" in aurora["rationale"]

    def test_cache_wave_fronts_aurora_and_is_reversible(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        cache = result["migration_waves"][1]
        assert cache["query_count"] == 1
        assert cache["share_basis"] == "calls"
        assert cache["fronts"] == "aurora_mysql"
        assert "reversible" in cache["rationale"]

    def test_dynamodb_wave_respects_co_dependency_groups(self, store):
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        dynamo = result["migration_waves"][2]
        assert sorted(dynamo["tables"]) == ["sessions", "users"]
        # All 3 DynamoDB-assigned queries touch the group's tables: kv1 and kv2
        # are the group's own members, and hot1 -- not a member, but it reads
        # "users", one of the group's tables -- is still counted in it (#225:
        # every DynamoDB-assigned query touching an owned table goes to the
        # first group whose tables it touches, not just a group's own members).
        # Not TableAssignment.query_count's 3-per-table sum either, which would
        # double-count a multi-table query and count every engine's queries.
        assert dynamo["table_groups"] == [
            {"tables": ["sessions", "users"], "query_count": 3, "kind": "co_dependency"}
        ]
        # #321: moves from the retained Aurora engine, not the legacy source.
        assert dynamo["moves_from"] == ["aurora_mysql"]

    def test_written_report_round_trips_through_the_contract(self, store):
        from src.agents.referee.synthesis_handler import _write_synthesis_report

        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        _write_synthesis_report(store, result, assignment_version=2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        # #1020: facts.in_scope_query_count, facts.moving_query_count and
        # facts.resolved_risks_by_severity added
        # #746: facts.unreviewed_move_count added
        # #992: table_sources (1.32); #471: facts.relational_gaps (1.35)
        # #1034: prose_style_rejections added (1.37)
        # #888: flagged_unservable added (1.38)
        # #950: Risk.rule and Risk.wave added (1.40)
        # #1108: facts.query_trace.trace_groups added (1.41)
        # #1169: the Migration map split and kept-in-sync counts (1.42)
        # #1148: routing.flagged, FlaggedUnservableQuery.source_tables (1.43)
        # #1149: relational.gaps risks (1.44)
        assert report["contract_version"] == "1.44"
        assert len(report["migration_waves"]) == 3
        assert report["migration_waves"][2]["engines"] == ["dynamodb"]

    def test_written_report_carries_the_facts_block(self, store):
        """#615: synthesis computes every figure once, into report.json's facts."""
        from src.agents.referee.synthesis_handler import _write_synthesis_report

        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        _write_synthesis_report(store, result, assignment_version=2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        facts = report["facts"]
        assert facts["engine_count"] == len(report["ranking"])
        assert [w["wave"] for w in facts["waves"]] == [1, 2, 3]
        dynamo = facts["waves"][2]
        assert dynamo["migrating"] is True
        assert dynamo["served_tables"] == report["migration_waves"][2]["table_count"]
        assert facts["tables_migrate"] == dynamo["moving_tables"]
        # Wave 1 carries the whole schema: 3 tables, no views here.
        assert (facts["waves"][0]["tables_only"], facts["waves"][0]["views"]) == (3, 0)
        assert facts["source_table_count"] == report["source_table_count"] == 3

    def test_written_report_carries_the_query_traces(self, store):
        """#339: the lineage's ledger, traced; the moved query listed in full."""
        from src.agents.referee.synthesis_handler import _write_synthesis_report

        def move(seq, qid, to, frm=None, code="highest_score", pss="score"):
            return {
                "seq": seq,
                "query_id": qid,
                "pass": pss,
                "from_engine": frm,
                "to_engine": to,
                "score_before": 60.0 if frm else None,
                "score_after": 80.0,
                "reason_code": code,
                "actor": "rule",
            }

        v1 = {**ASSIGNMENT, "version": 1, "previous_version": None}
        v1["query_assignments"] = [
            {
                **qa,
                "assigned_engine": (
                    "aurora_mysql" if qa["query_id"] == "kv2" else qa["assigned_engine"]
                ),
            }
            for qa in ASSIGNMENT["query_assignments"]
        ]
        v1["moves"] = [
            move(i, qa["query_id"], qa["assigned_engine"])
            for i, qa in enumerate(v1["query_assignments"], start=1)
        ]
        v2 = {
            **ASSIGNMENT,
            "previous_version": 1,
            "moves": [
                move(1, "kv2", "dynamodb", "aurora_mysql", "consolidate.no_unique_value", "settle")
            ],
        }
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", v1)
        store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", v2)
        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
        _write_synthesis_report(store, result, assignment_version=2)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v2/report.json")
        traces = report["query_traces"]
        assert traces["has_ledger"] is True
        assert traces["query_count"] == len(ASSIGNMENT["query_assignments"])
        assert [t["query_id"] for t in traces["queries"]] == ["kv2"]
        kv2 = traces["queries"][0]
        assert [
            (s["version"], s["to_engine"]) for s in [kv2["first_assignment"], *kv2["moves"]]
        ] == [
            (1, "aurora_mysql"),
            (2, "dynamodb"),
        ]
        facts = report["facts"]["query_trace"]
        assert (facts["listed_query_count"], facts["moved_query_count"]) == (1, 1)
        assert facts["unlisted_query_count"] == traces["query_count"] - 1


@pytest.fixture
def store_with_schema_design_noise(tmp_path):
    """Reproduces the Discourse evidence end to end (#380): a real table the
    collector saw but no schema design ever mapped (``extra_table``, a stand-in
    for a framework table like ``ar_internal_metadata``), plus a schema design
    that hallucinates a "source table" nothing collected (``db.made_up_seq``,
    a sequence name). The sequence inflated the migration map's own "tables
    migrate" count (confidence 0, never a real table); the real-but-unmapped
    table must NOT disappear from wave 1 -- it is still part of "the whole
    database moves to Aurora" (#380 review).
    """
    s = LocalArtifactStore(base_dir=str(tmp_path))
    engines = ["dynamodb", "aurora_mysql"]
    s.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
    )
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "mysql"}},
            "database_schema": {
                "tables": [
                    {"table_id": "users"},
                    {"table_id": "orders"},
                    # Collected, but no schema design will map it below.
                    {"table_id": "extra_table"},
                ]
            },
            "queries": {"query_patterns": QUERIES},
        },
    )
    conf = {"dynamodb": 90, "aurora_mysql": 60}
    for engine in engines:
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(conf[engine]))
    s.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json",
        {
            "table_definitions": [
                {
                    "table_name": "users",
                    "aggregate_pattern": "relational_table",
                    "source_tables": ["users"],
                },
                {
                    # Never a real collected table: a schema-design hallucination.
                    "table_name": "id_sequence_counters",
                    "aggregate_pattern": "separate",
                    "source_tables": ["db.made_up_seq"],
                },
            ]
        },
    )
    s.write_json(
        f"{DB}/{JOB}/schema-aurora_mysql/v2/schema_output.json",
        {"source_database": DB, "table_definitions": [{"table_name": "orders", "columns": []}]},
    )
    assignment = {
        **ASSIGNMENT,
        "table_assignments": [
            t for t in ASSIGNMENT["table_assignments"] if t["table_id"] != "sessions"
        ],
        "query_assignments": [
            qa for qa in ASSIGNMENT["query_assignments"] if qa["query_id"] != "kv2"
        ],
        "co_dependency_groups": [],
    }
    s.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", assignment)
    return s


class TestMigrationWavesKeepRealUnmappedTablesButDropNoise:
    def test_wave_1_carries_every_real_table_including_unmapped_ones(
        self, store_with_schema_design_noise
    ):
        result = run_synthesis_deterministic(
            JOB, DB, store_with_schema_design_noise, assignment_version=2
        )
        mapped_sources = {m["source_table"] for m in result["table_mappings"]}
        # The hallucinated sequence never counts as a mapped table (build_table_mappings
        # resolves it against the collector's real schema and drops it); only the two
        # real, schema-design-covered tables are "mapped".
        assert mapped_sources == {"users", "orders"}
        aurora = result["migration_waves"][0]
        # #380 review: "extra_table" is real (the collector saw it) but no schema
        # design maps it -- it still belongs to "the whole database moves to Aurora"
        # and must not vanish from wave 1.
        assert sorted(aurora["tables"]) == ["extra_table", "orders", "users"]
        assert aurora["table_count"] == 3
        # The sequence hallucination is still gone from wave 1: it was never a real
        # collected table (not in data.source_tables), and build_table_mappings has
        # already dropped it from table_mappings too.
        assert "db.made_up_seq" not in aurora["tables"]
        # The rationale reconciles wave 1's full count against the smaller mapped
        # total, instead of leaving the two numbers to disagree with no explanation.
        assert "2 of these tables and views are mapped to a target engine in a later wave" in (
            aurora["rationale"]
        )
        assert "the other 1 stay" in aurora["rationale"]
