"""Aurora with a real, #157-derived schema design (issue #429, defect 1).

Earlier coverage (``test_synthesis_bedrock_context_aurora.py``,
``test_synthesis_shared_group_with_schema_design.py``) only ever gave Aurora
either no schema design at all, or hand-built access-pattern dicts standing
in for one. This exercises the real path end to end: ``derive_access_patterns``
produces the pattern, ``build_query_groups``' main loop (not the relational
branch's routing-record step-aside) builds the group from it, and the
Engineering Report shows the real table -- never a "-" destination, and never
duplicated in the generic "## Query groups" table (#478's own rule: a group
served only by relational engine(s) belongs in "Queries on Aurora by table"
alone).
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups, build_summary
from src.contracts.schema_design_input import (
    AgentColumn,
    AgentQueryPattern,
    AgentTable,
    NormalizedDataType,
    QueryType,
)
from src.report import renderers
from src.tools.schema.aurora_common.access_patterns import derive_access_patterns

QUERY_TEXT = "SELECT * FROM wordpress.wp_posts WHERE id = ?"


def _wp_posts() -> AgentTable:
    return AgentTable(
        table_id="wordpress.wp_posts",
        table_name="wp_posts",
        row_count=500,
        columns=[
            AgentColumn(
                column_name="id", nullable=False, normalized_data_type=NormalizedDataType.integer
            )
        ],
        primary_key=["id"],
    )


def _query() -> AgentQueryPattern:
    return AgentQueryPattern(
        query_id="aur1",
        query_text=QUERY_TEXT,
        query_type=QueryType.SELECT,
        frequency_per_hour=16200.0,
        calls_per_second=4.5,
        tables_accessed=["wordpress.wp_posts"],
        filter_columns=["id"],
    )


def _data() -> SynthesisData:
    derived = derive_access_patterns([_query()], [_wp_posts()], "aurora_mysql")
    assert len(derived.patterns) == 1  # sanity: this is a real derived pattern, not a stub
    return SynthesisData(
        job_id="test",
        database_name="wp",
        collector={
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "aur1",
                        "query_text": QUERY_TEXT,
                        "query_type": "SELECT",
                        "calls_per_second": 4.5,
                        "tables_accessed": ["wordpress.wp_posts"],
                    }
                ]
            }
        },
        engines={
            "aurora_mysql": EngineArtifacts(
                engine="aurora_mysql",
                schema_design={
                    "table_definitions": [{"table_name": "wp_posts"}],
                    "access_patterns": derived.patterns,
                },
            )
        },
        assignment={
            "query_assignments": [
                {
                    "query_id": "aur1",
                    "assigned_engine": "aurora_mysql",
                    "source_tables": ["wordpress.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                }
            ]
        },
    )


RANKING = [
    {
        "target": "aurora_mysql",
        "confidence_score": 70,
        "weight": 1.0,
        "assigned_queries": 1,
        "workload_percent": 100.0,
        "schema_design_available": True,
        "target_tables": 1,
        "access_patterns": 1,
        "pattern_groups": 1,
    },
]
MAPPINGS = [{"source_table": "wordpress.wp_posts", "recommended_database": "aurora_mysql"}]
TCO = {"projected_monthly_cost": 10.0, "savings_percent": 0}
RISKS = {"risks": [], "overall_risk_level": "LOW"}


def test_build_query_groups_uses_the_real_derived_pattern_not_a_routing_record() -> None:
    groups = build_query_groups(_data())
    (group,) = [g for g in groups if g["group_name"] == "wordpress.wp_posts"]
    assert group["engines"] == ["aurora_mysql"]
    (ap,) = group["access_patterns"]
    assert ap["engine"] == "aurora_mysql"
    assert ap["query_ids"] == ["aur1"]
    assert ap["in_scope"] is True
    # The structural description (#157's own derivation) is a separate fact
    # from why the query is on Aurora at all (#157 review, I3): the latter
    # comes from the assignment, through the same reasons/reason_index
    # mechanism the relational branch uses -- not duplicated into
    # "description", which stays the structural text.
    assert "SELECT" in ap["description"]
    assert group["reasons"] == ["relational core"]
    assert ap["reason_index"] == 0


def test_a_placeholder_only_source_table_never_leaks_as_unknown() -> None:
    """#157 review (C2): Aurora's own access patterns have no pattern_group
    to fall back on, so a query the collector could not resolve a table for
    (source_tables == ["unknown"]) must get the same
    UNRESOLVED_TABLE_GROUP_LABEL the relational branch already uses for this
    fact, never a literal "unknown" group name."""
    from src.agents.referee.synthesis_report import UNRESOLVED_TABLE_GROUP_LABEL

    data = SynthesisData(
        job_id="test",
        database_name="wp",
        collector={"queries": {"query_patterns": []}},
        engines={
            "aurora_mysql": EngineArtifacts(
                engine="aurora_mysql",
                schema_design={
                    "access_patterns": [
                        {
                            "pattern_id": "aur-unknown",
                            "engine": "aurora_mysql",
                            "operation": "SELECT",
                            "sql_text": "SELECT 1",
                            "source_tables": ["unknown"],
                            "source_query_ids": ["qx"],
                            "design_rps": 0.1,
                            "in_scope": False,
                            "out_of_scope_reason": "Query does not reference any table "
                            "designed for this target.",
                        }
                    ]
                },
            )
        },
    )
    groups = build_query_groups(data)
    names = [g["group_name"] for g in groups]
    assert UNRESOLVED_TABLE_GROUP_LABEL in names
    assert "unknown" not in names


def test_build_summary_counts_it_with_no_non_relational_wording() -> None:
    data = _data()
    groups = build_query_groups(data)
    text = build_summary(data, RANKING, MAPPINGS, TCO, RISKS, groups)

    assert "on non-relational engines" not in text
    assert "1 in-scope access pattern" in text


def test_engineering_report_shows_the_real_table_never_a_dash() -> None:
    data = _data()
    groups = build_query_groups(data)
    report = {
        "database_name": "wp",
        "query_groups": groups,
        "schema_designs": {
            "aurora_mysql": {
                "status": "completed",
                "tables": [{"table_name": "wp_posts"}],
                "access_pattern_count": 1,
            },
        },
    }
    md = renderers.render_engineering_report_md(report)

    assert "## Queries on Aurora by table" in md
    section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
    assert "wordpress.wp_posts" in section
    assert "—" not in section.split("\n")[0]  # header row only, no row reads as a dash

    # #478: a group served only by Aurora is shown here, not duplicated in
    # the generic "## Query groups" table (there is nothing else to share it
    # with here, so that table is omitted outright).
    assert "## Query groups (" not in md


def test_engineering_report_reason_column_shows_the_assignment_reason() -> None:
    """#157 review, I3: the "Reason" column names why the query is on
    Aurora at all (the assignment's own reason), not the pattern's own
    structural description."""
    data = _data()
    groups = build_query_groups(data)
    report = {"database_name": "wp", "query_groups": groups}
    md = renderers.render_engineering_report_md(report)

    section = md.split("## Queries on Aurora by table", 1)[1].split("\n## ", 1)[0]
    row = next(ln for ln in section.splitlines() if "wordpress.wp_posts" in ln)
    assert "relational core" in row
    assert "SELECT on wordpress.wp_posts" not in row


def test_target_schemas_by_engine_calls_a_designed_aurora_s_unit_access_patterns() -> None:
    """A designed Aurora engine is counted the same way as every other engine
    with a schema design -- "N access patterns", not "N queries" (#157 review,
    C2): the latter wording is reserved for an Aurora engine with no schema
    design at all yet (renderers.render_engineering_report_md)."""
    data = _data()
    groups = build_query_groups(data)
    report = {
        "database_name": "wp",
        "query_groups": groups,
        "schema_designs": {
            "aurora_mysql": {
                "status": "completed",
                "tables": [{"table_name": "wp_posts"}],
                "access_pattern_count": 1,
            },
        },
    }
    md = renderers.render_engineering_report_md(report)

    section = md.split("## Target schemas by engine", 1)[1].split("\n## ", 1)[0]
    assert "1 access pattern" in section
    assert "1 query" not in section
    assert "1 queries" not in section
