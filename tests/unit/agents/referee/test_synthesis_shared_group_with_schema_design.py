"""A query group with a real, non-relational schema design that shares a
source table with Aurora-assigned queries (#478).

``--llm-mode none`` never produces a schema design, so no earlier test built
this exact shape: the generic loop in ``build_query_groups`` creates a group
from a real engine's schema design (DynamoDB, here) with no "reasons" key at
all -- that key only ever existed on a group the relational branch created
itself. When the relational branch later reused that same group (its own
assignment named the same source table), a plain
``groups[group_name]["reasons"]`` lookup raised ``KeyError`` on a live run
that reached ``run_synthesis.py`` with a real schema design, twice. Every
reader downstream of ``build_query_groups`` must also tolerate the mixed
shape this produces: some access patterns in the group carry
``reason_index`` (the relational branch's), others carry ``description``
directly (DynamoDB's own) -- covered here through the deterministic
summary, the engineering report and the export projection.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups, build_summary
from src.report import renderers
from src.report.analysis_report import _project_query_groups_for_export

RANKING = [
    {
        "target": "dynamodb",
        "confidence_score": 60,
        "weight": 0.5,
        "assigned_queries": 1,
        "workload_percent": 50.0,
        "schema_design_available": True,
        "target_tables": 1,
        "access_patterns": 1,
        "pattern_groups": 1,
    },
    {
        "target": "aurora_mysql",
        "confidence_score": 50,
        "weight": 0.4,
        "assigned_queries": 1,
        "workload_percent": 50.0,
        "schema_design_available": False,
        "target_tables": 0,
        "access_patterns": 0,
        "pattern_groups": 0,
    },
]
MAPPINGS = [{"source_table": "wp.wp_posts", "recommended_database": "dynamodb"}]
TCO = {"projected_monthly_cost": 10.0, "savings_percent": 0}
RISKS = {"risks": [], "overall_risk_level": "LOW"}


def _shared_table_data() -> SynthesisData:
    """DynamoDB's own schema design and Aurora's assignment both name
    "wp.wp_posts" -- the generic loop builds the group first, from
    DynamoDB's access pattern; the relational branch below reuses it."""
    return SynthesisData(
        job_id="test",
        database_name="wp",
        collector={
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "ddb1",
                        "query_text": "GetItem wp_posts",
                        "query_type": "GetItem",
                        "calls_per_second": 7.0,
                        "tables_accessed": ["wp.wp_posts"],
                    },
                    {
                        "query_id": "aur1",
                        "query_text": "SELECT * FROM wp_posts WHERE id = ?",
                        "query_type": "SELECT",
                        "calls_per_second": 4.5,
                        "tables_accessed": ["wp.wp_posts"],
                    },
                ]
            }
        },
        engines={
            "dynamodb": EngineArtifacts(
                engine="dynamodb",
                schema_design={
                    "access_patterns": [
                        {
                            "pattern_id": "DDB-AP-1",
                            "source_tables": ["wp.wp_posts"],
                            "design_rps": 7.0,
                            "operation": "GetItem",
                            "description": "point lookup by id",
                            "query_ids": ["ddb1"],
                        }
                    ]
                },
            )
        },
        assignment={
            "query_assignments": [
                {
                    "query_id": "aur1",
                    "assigned_engine": "aurora_mysql",
                    "source_tables": ["wp.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                }
            ]
        },
    )


def test_build_query_groups_does_not_raise_on_a_shared_group() -> None:
    groups = build_query_groups(_shared_table_data())
    (group,) = [g for g in groups if g["group_name"] == "wp.wp_posts"]
    assert sorted(group["engines"]) == ["aurora_mysql", "dynamodb"]
    assert len(group["access_patterns"]) == 2
    assert group["reasons"] == ["relational core"]


def test_deterministic_summary_does_not_raise_on_a_shared_group() -> None:
    data = _shared_table_data()
    groups = build_query_groups(data)
    text = build_summary(data, RANKING, MAPPINGS, TCO, RISKS, groups)
    assert "wp.wp_posts" in text


def test_engineering_report_does_not_raise_on_a_shared_group() -> None:
    groups = build_query_groups(_shared_table_data())
    report = {
        "database_name": "wp",
        "query_groups": groups,
        "schema_designs": {
            "dynamodb": {"status": "completed", "tables": [], "access_pattern_count": 1},
        },
    }
    md = renderers.render_engineering_report_md(report)
    assert "## Queries on Aurora by table" in md
    section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
    # Aurora's own entry in the shared group, not DynamoDB's.
    assert "| wp.wp_posts | 1 |" in section
    # The generic table still lists the group too (shared with DynamoDB).
    generic_section = md.split("## Query groups", 1)[1].split("\n## ", 1)[0]
    assert "wp.wp_posts" in generic_section


def test_export_projection_does_not_raise_on_a_shared_group() -> None:
    """``_project_query_groups_for_export`` only reads ``engine``/``query_ids``
    off each access pattern -- unaffected by the mixed reason-index shape --
    but must still tolerate it when a shared group's entries are capped."""
    data = _shared_table_data()
    groups = build_query_groups(data)
    (group,) = [g for g in groups if g["group_name"] == "wp.wp_posts"]
    # Force the cap so query_count_by_engine actually gets computed.
    (projected,) = _project_query_groups_for_export([group], max_entries=1)
    assert projected["query_count_by_engine"] == {"dynamodb": 1, "aurora_mysql": 1}
    assert len(projected["access_patterns"]) == 1
