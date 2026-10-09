"""Schema query groups retain source-query provenance for every engine (#237)."""

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups, reason_text_for


def data_for(engine: str, patterns: list[dict]) -> SynthesisData:
    return SynthesisData(
        job_id="test",
        database_name="sample",
        collector={
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "query_text": "SELECT * FROM users", "query_type": "SELECT"}
                ]
            }
        },
        engines={
            engine: EngineArtifacts(engine=engine, schema_design={"access_patterns": patterns})
        },
    )


@pytest.mark.parametrize("engine", ["elasticache", "documentdb"])
def test_source_query_ids_are_linked_and_grouped_by_source(engine: str) -> None:
    data = data_for(
        engine,
        [
            {
                "pattern_id": "AP-1",
                "source_query_ids": ["q1"],
                "source_tables": ["public.users"],
                "design_rps": 12.5,
            }
        ],
    )
    groups = build_query_groups(data)
    assert len(groups) == 1
    group = groups[0]
    assert group["group_name"] == "public.users"
    assert group["access_patterns"][0]["query_ids"] == ["q1"]
    assert group["source_queries"][0]["query_id"] == "q1"
    assert group["source_queries"][0]["linked_patterns"] == ["AP-1"]
    assert group["total_design_rps"] == 12.5


def test_empty_legacy_ids_fall_back_and_links_are_deduplicated() -> None:
    data = data_for(
        "documentdb",
        [
            {
                "pattern_id": "AP-1",
                "query_ids": [],
                "source_query_ids": ["q1"],
                "source_tables": ["users", "profiles"],
            },
            {
                "pattern_id": "AP-2",
                "source_query_ids": ["q1"],
                "source_tables": ["profiles", "users"],
            },
        ],
    )
    (group,) = build_query_groups(data)
    assert group["group_name"] == "profiles, users"
    assert len(group["source_queries"]) == 1
    assert group["source_queries"][0]["linked_patterns"] == ["AP-1", "AP-2"]


def test_explicit_groups_and_query_ids_keep_precedence() -> None:
    data = data_for(
        "dynamodb",
        [
            {
                "pattern_id": "AP-1",
                "pattern_group": "User lookups",
                "query_ids": ["q1"],
                "source_query_ids": ["other"],
                "source_tables": ["users"],
            }
        ],
    )
    (group,) = build_query_groups(data)
    assert group["group_name"] == "User lookups"
    assert group["access_patterns"][0]["query_ids"] == ["q1"]
    assert group["source_queries"][0]["query_id"] == "q1"


def test_missing_group_metadata_keeps_legacy_ungrouped_fallback() -> None:
    (group,) = build_query_groups(data_for("dynamodb", [{"pattern_id": "AP-1"}]))
    assert group["group_name"] == "ungrouped"


def test_unknown_source_query_ids_do_not_fabricate_queries() -> None:
    (group,) = build_query_groups(
        data_for("elasticache", [{"source_query_ids": ["missing"], "source_tables": ["users"]}])
    )
    assert group["source_queries"] == []
    assert group["access_patterns"][0]["query_ids"] == ["missing"]


# ---------------------------------------------------------------------------
# #478: a query is the atomic unit synthesis groups by, so a query
# the assignment routed to an Aurora engine belongs in query_groups like any
# other -- Aurora's schema-design contract simply has no access_patterns to
# drive this from (#157 adds real ones later). Built from the assignment,
# not the schema design, so a group exists even without one.
# ---------------------------------------------------------------------------


# A realistic 64-character query id (the collector's actual format), kept
# recognizable with a "q1" prefix -- long enough to prove pattern_id is a
# short derivative of it, not the id itself.
Q1 = "q1" + "a" * 62


def _relational_data(engine: str, schema_design: dict | None = None) -> SynthesisData:
    engines = {}
    if schema_design is not None:
        engines[engine] = EngineArtifacts(engine=engine, schema_design=schema_design)
    return SynthesisData(
        job_id="test",
        database_name="sample",
        collector={
            "queries": {
                "query_patterns": [
                    {
                        "query_id": Q1,
                        "query_text": "SELECT * FROM wp_posts WHERE id = ?",
                        "query_type": "SELECT",
                        "calls_per_second": 4.5,
                        "tables_accessed": ["wp.wp_posts"],
                    },
                    {
                        "query_id": "q2",
                        "query_text": "UPDATE wp_postmeta SET meta_value = ?",
                        "query_type": "UPDATE",
                        "calls_per_second": 1.2,
                        "tables_accessed": ["wp.wp_postmeta"],
                    },
                    {
                        "query_id": "q3",
                        "query_text": "SELECT 1",
                        "query_type": "SELECT",
                        "calls_per_second": 0.5,
                        "tables_accessed": ["unknown"],
                    },
                ]
            }
        },
        engines=engines,
        assignment={
            "query_assignments": [
                {
                    "query_id": Q1,
                    "assigned_engine": engine,
                    "source_tables": ["wp.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                },
                {
                    "query_id": "q2",
                    "assigned_engine": engine,
                    "source_tables": ["wp.wp_postmeta", "DUAL"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                },
                {
                    "query_id": "q3",
                    "assigned_engine": engine,
                    "source_tables": ["unknown"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                },
                {
                    "query_id": "q4",
                    "assigned_engine": "dynamodb",
                    "source_tables": ["wp.wp_options"],
                    "assignment_reason": "key-value lookup",
                    "in_scope": True,
                },
            ]
        },
    )


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_relational_branch_builds_groups_from_assignment(engine: str) -> None:
    groups = build_query_groups(
        _relational_data(engine, schema_design={"table_definitions": [{"table_name": "wp_posts"}]})
    )

    by_name = {g["group_name"]: g for g in groups}
    assert "wp.wp_posts" in by_name
    assert "wp.wp_postmeta" in by_name  # DUAL dropped, real table kept
    assert "wp.wp_options" not in by_name  # routed elsewhere, not Aurora's

    posts_group = by_name["wp.wp_posts"]
    assert posts_group["engines"] == [engine]
    ap = posts_group["access_patterns"][0]
    assert ap["engine"] == engine
    assert ap["operation"] == "SELECT"
    assert ap["query_ids"] == [Q1]
    # #478: the query's own id, not a truncated prefix of it
    # (a truncated id risks two different queries silently merging under a
    # shared prefix on a non-cryptographic id scheme).
    assert ap["pattern_id"] == f"relational-{engine}-{Q1}"
    # table_name/key_condition dropped: redundant with the
    # group's own name and always None, respectively; nothing reads them.
    assert "table_name" not in ap
    assert "key_condition" not in ap
    assert posts_group["source_queries"][0]["query_id"] == Q1
    assert "tables_accessed" not in posts_group["source_queries"][0]
    assert "linked_patterns" not in posts_group["source_queries"][0]


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_relational_branch_reuses_a_group_a_non_relational_engine_already_created(
    engine: str,
) -> None:
    """A real DynamoDB schema design and the assignment can both name the
    same source table: the generic loop above creates "wp.wp_posts" first
    (DynamoDB's own access pattern), with no "reasons" key at all -- that
    key only ever existed on a group this branch created itself. A plain
    ``groups[group_name]["reasons"]`` lookup crashed with ``KeyError`` here
    (``--llm-mode none`` never produces a schema design, so no earlier test
    caught it); the fix must make the key available either way, without
    disturbing DynamoDB's own entry, which carries no ``reason_index`` and
    keeps reading its reason from ``description`` directly."""
    data = SynthesisData(
        job_id="test",
        database_name="sample",
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
                        "query_id": Q1,
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
                    "query_id": Q1,
                    "assigned_engine": engine,
                    "source_tables": ["wp.wp_posts"],
                    "assignment_reason": "relational core",
                    "in_scope": True,
                }
            ]
        },
    )

    groups = build_query_groups(data)  # must not raise KeyError

    (group,) = [g for g in groups if g["group_name"] == "wp.wp_posts"]
    assert sorted(group["engines"]) == sorted(["dynamodb", engine])
    assert len(group["access_patterns"]) == 2
    by_pattern_id = {ap["pattern_id"]: ap for ap in group["access_patterns"]}

    ddb_ap = by_pattern_id["DDB-AP-1"]
    assert "reason_index" not in ddb_ap
    assert reason_text_for(group, ddb_ap) == "point lookup by id"

    aurora_ap = by_pattern_id[f"relational-{engine}-{Q1}"]
    assert isinstance(aurora_ap["reason_index"], int)
    assert reason_text_for(group, aurora_ap) == "relational core"

    # The reader side already tolerates the mix -- every consumer resolves
    # through reason_text_for/_reason_text_for/reasonTextFor, never a plain
    # ``ap["description"]`` or ``group["reasons"]`` lookup.
    reasons_seen = {reason_text_for(group, ap) for ap in group["access_patterns"]}
    assert reasons_seen == {"point lookup by id", "relational core"}


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_relational_branch_works_without_a_schema_design(engine: str) -> None:
    """#478: the queries still run on Aurora even when its schema
    design hasn't completed (or never will) -- only the design-dependent
    fields are unavailable then, not the groups."""
    groups = build_query_groups(_relational_data(engine, schema_design=None))
    by_name = {g["group_name"]: g for g in groups}
    assert "wp.wp_posts" in by_name
    assert by_name["wp.wp_posts"]["access_patterns"][0]["operation"] == "SELECT"


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_relational_branch_labels_tableless_queries(engine: str) -> None:
    """#478: a query with no real source table is never
    labelled "(no source table)"/"unknown" -- a real utility/session
    statement (SHOW/SET/EXPLAIN/catalog introspection, already routed to the
    relational engine by the assignment) gets its own readable group, and
    anything else left with only placeholder tables says the collector,
    not the query, is why."""
    groups = build_query_groups(_relational_data(engine))
    by_name = {g["group_name"]: g for g in groups}
    assert "unknown" not in by_name
    # q3 is "SELECT 1" with tables_accessed ["unknown"] -- not a utility
    # statement, so it lands in the collector-blame group.
    assert "Table not identified by the collector" in by_name
    assert by_name["Table not identified by the collector"]["access_patterns"][0]["query_ids"] == [
        "q3"
    ]


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_relational_branch_labels_utility_statements(engine: str) -> None:
    data = _relational_data(engine)
    data.collector["queries"]["query_patterns"].append(
        {
            "query_id": "q5",
            "query_text": "SHOW FULL FIELDS FROM wp_options",
            "query_type": "SHOW",
            "calls_per_second": 0.1,
            "tables_accessed": ["unknown"],
        }
    )
    data.assignment["query_assignments"].append(
        {
            "query_id": "q5",
            "assigned_engine": engine,
            "source_tables": ["unknown"],
            "assignment_reason": "utility statement",
            "in_scope": True,
        }
    )
    groups = build_query_groups(data)
    by_name = {g["group_name"]: g for g in groups}
    assert "Utility and session statements" in by_name
    assert by_name["Utility and session statements"]["access_patterns"][0]["query_ids"] == ["q5"]


def test_relational_branch_caps_the_assignment_reason() -> None:
    data = _relational_data("aurora_mysql")
    data.assignment["query_assignments"][0]["assignment_reason"] = "x" * 300
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    ap = group["access_patterns"][0]
    description = reason_text_for(group, ap)
    # #478: the reason lives once in the group's own
    # "reasons" list, indexed by "reason_index" -- not copied onto every
    # access pattern (146 distinct reasons backed 1281 entries on the
    # discourse sample).
    assert "description" not in ap
    assert isinstance(ap["reason_index"], int)
    # #478: cut on a word boundary with an ellipsis, not a
    # bare mid-word cut -- a single run of "x" has no word boundary to cut
    # on, so the whole 180-char prefix is kept plus the ellipsis character.
    assert description == "x" * 180 + "…"


def test_relational_branch_caps_on_a_word_boundary() -> None:
    data = _relational_data("aurora_mysql")
    reason = " ".join(["word"] * 50)  # far longer than 180 chars, real words
    data.assignment["query_assignments"][0]["assignment_reason"] = reason
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    description = reason_text_for(group, group["access_patterns"][0])
    assert description.endswith("…")
    assert not description[:-1].endswith("wor")  # never a mid-word cut
    assert len(description) <= 181


def test_relational_branch_dedupes_repeated_reason_clauses() -> None:
    data = _relational_data("aurora_mysql")
    data.assignment["query_assignments"][0]["assignment_reason"] = (
        "highest confidence for aurora_mysql; highest confidence for aurora_mysql "
        "| [capability] dynamodb lacks required capability: aggregation"
    )
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    description = reason_text_for(group, group["access_patterns"][0])
    assert description.count("highest confidence for aurora_mysql") == 1


def test_relational_branch_replaces_unknown_in_reason_text() -> None:
    data = _relational_data("aurora_mysql")
    data.assignment["query_assignments"][0][
        "assignment_reason"
    ] = "reality check: consolidated (no unique value, data synced for unknown)"
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    description = reason_text_for(group, group["access_patterns"][0])
    assert "unknown" not in description
    assert "an unresolved table" in description


def test_relational_branch_shares_one_reasons_list_across_duplicate_reasons() -> None:
    """#478: two different queries that land in the same group
    with the exact same assignment reason share one entry in the group's
    "reasons" list, not two copies of the string."""
    data = _relational_data("aurora_mysql")
    data.assignment["query_assignments"].append(
        {
            "query_id": "q10",
            "assigned_engine": "aurora_mysql",
            "source_tables": data.assignment["query_assignments"][0]["source_tables"],
            "assignment_reason": data.assignment["query_assignments"][0]["assignment_reason"],
            "in_scope": True,
        }
    )
    data.source_queries.append(
        {
            "query_id": "q10",
            "query_text": "SELECT * FROM wp_posts",
            "query_type": "SELECT",
            "calls_per_second": 2.0,
        }
    )
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    assert len(group["access_patterns"]) == 2
    assert len(group["reasons"]) == 1
    indices = {ap["reason_index"] for ap in group["access_patterns"]}
    assert indices == {0}


def test_relational_branch_reasons_list_has_one_entry_per_distinct_reason() -> None:
    data = _relational_data("aurora_mysql")
    data.assignment["query_assignments"].append(
        {
            "query_id": "q10",
            "assigned_engine": "aurora_mysql",
            "source_tables": data.assignment["query_assignments"][0]["source_tables"],
            "assignment_reason": "a completely different reason",
            "in_scope": True,
        }
    )
    data.source_queries.append(
        {
            "query_id": "q10",
            "query_text": "SELECT * FROM wp_posts",
            "query_type": "SELECT",
            "calls_per_second": 2.0,
        }
    )
    (group,) = [g for g in build_query_groups(data) if g["group_name"] == "wp.wp_posts"]
    assert len(group["reasons"]) == 2
    reasons_used = {reason_text_for(group, ap) for ap in group["access_patterns"]}
    assert reasons_used == {
        group["reasons"][0],
        "a completely different reason",
    }


def test_reason_text_for_a_none_entry_in_reasons_is_empty_not_the_word_none() -> None:
    """#478: ``reason_text_for`` must match the JS
    ``reasonTextFor``'s ``reasons[reasonIndex] || ''`` -- a ``None`` entry
    (shouldn't happen, but never worth shipping) resolves to an empty
    string, not the literal word "None"."""
    group = {"reasons": [None, "a real reason"]}
    assert reason_text_for(group, {"reason_index": 0}) == ""
    assert reason_text_for(group, {"reason_index": 1}) == "a real reason"


def test_relational_branch_deduplicates_repeated_assignment_rows() -> None:
    """#478: some assignments carry more than one query_assignments
    row for the same query_id (one per table a multi-table query touches --
    seen on the AdventureWorks sample: 657 rows, 79 distinct queries). A
    duplicate row for a query already recorded under the exact same table
    group must not double its design_rps or add a second, redundant entry;
    pattern_id is deterministic from the query_id (not a per-row counter) so
    this collapses correctly."""
    data = _relational_data("aurora_mysql")
    # A second row for Q1, same engine, same table -- as if the resolver
    # emitted one row per table for a query that also touches another one.
    data.assignment["query_assignments"].insert(
        1,
        {
            "query_id": Q1,
            "assigned_engine": "aurora_mysql",
            "source_tables": ["wp.wp_posts"],
            "assignment_reason": "relational core",
            "in_scope": True,
        },
    )
    groups = build_query_groups(data)
    (posts_group,) = [g for g in groups if g["group_name"] == "wp.wp_posts"]
    assert len(posts_group["access_patterns"]) == 1
    assert posts_group["total_design_rps"] == 4.5
    assert len(posts_group["source_queries"]) == 1


def test_relational_branch_steps_aside_once_real_access_patterns_exist() -> None:
    """#157 will give Aurora real access_patterns; the relational branch must
    not also build groups from the assignment then, or every Aurora query
    would be double-counted."""
    groups = build_query_groups(
        _relational_data(
            "aurora_mysql",
            schema_design={
                "table_definitions": [{"table_name": "wp_posts"}],
                "access_patterns": [
                    {
                        "pattern_id": "AM-AP-1",
                        "pattern_group": "Post reads",
                        "source_tables": ["wp.wp_posts"],
                        "query_ids": ["q1"],
                        "design_rps": 4.5,
                    }
                ],
            },
        )
    )
    assert len(groups) == 1
    assert groups[0]["group_name"] == "Post reads"
    assert groups[0]["access_patterns"][0]["pattern_id"] == "AM-AP-1"
