"""Shared customer-override write path (ADR-028).

Locks the one helper both the web route and the ATX gate call: it must bump the
version, stamp status=customer_modified + source=customer_gate + previous_version,
mark reassigned queries customer_override, honor table-scope narrowing, compute
skipped engines, and fail loudly (without writing) on unknown queries, a missing
assignment, or a hard validation error.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agents.referee.assignment_overrides import (
    AssignmentValidationFailed,
    ExcludedEngineOverride,
    LosingAuroraEngineOverride,
    NoAssignmentFound,
    QueryOverrideInput,
    UnknownQuery,
    apply_assignment_overrides,
    load_assignment_for_edit,
    refresh_consolidated_assignment,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    AuroraEngineChoice,
    QueryAssignment,
)


class _MemStore:
    """In-memory ArtifactStore-compatible store (read_json/write_json/list_prefix/exists)."""

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def read_json(self, path: str) -> dict:
        return self.objects[path]

    def write_json(self, path: str, data: dict) -> None:
        self.objects[path] = data

    def exists(self, path: str) -> bool:
        return path in self.objects

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


DB = "discourse"
JOB = "job-1"


def _qa(query_id: str, engine: str, tables: list[str], in_scope: bool = True) -> QueryAssignment:
    return QueryAssignment(
        query_id=query_id,
        assigned_engine=engine,
        confidence=80,
        source_tables=tables,
        assignment_reason="test",
        in_scope=in_scope,
    )


def _seed(
    store: _MemStore,
    query_assignments: list[QueryAssignment],
    engines: list[str],
    query_texts: dict[str, str] | None = None,
) -> None:
    """Seed v1 assignment + the collector/analysis the validator reads.

    ``query_texts`` (query_id -> SQL) is optional and only needed by tests that
    exercise ``check_exclusions`` (#548), which reads the collector's
    ``query_text`` field; every other caller leaves it unset, same as before.
    """
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=query_assignments,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    query_texts = query_texts or {}
    # Collector: one query_pattern per assignment (single-table so no co-dep groups form).
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": qa.query_id,
                        "tables_accessed": qa.source_tables,
                        "query_text": query_texts.get(qa.query_id, ""),
                    }
                    for qa in query_assignments
                ]
            },
            "database_schema": {"tables": []},
        },
    )
    for engine in engines:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})


def _store_with_two_ddb_queries() -> _MemStore:
    store = _MemStore()
    _seed(
        store,
        [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "dynamodb", ["t.posts"])],
        engines=["dynamodb", "opensearch"],
    )
    return store


def _store_with_heterogeneous_aurora_choice() -> _MemStore:
    """#381 review: a SQL Server source where the resolver picked aurora_postgresql
    over aurora_mysql -- both have an analysis artifact on disk (triage selected
    both), but the assignment only ever uses the winner."""
    store = _MemStore()
    query_assignments = [
        _qa("q1", "aurora_postgresql", ["Sales.Orders"]),
        _qa("q2", "aurora_postgresql", ["Sales.Customer"]),
    ]
    _seed(store, query_assignments, engines=["aurora_mysql", "aurora_postgresql"])
    assignment = Assignment.model_validate(
        store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    )
    assignment = assignment.model_copy(
        update={
            "aurora_engine_choice": AuroraEngineChoice(
                source_engine="sqlserver",
                engine="aurora_postgresql",
                totals={"aurora_mysql": 60.0, "aurora_postgresql": 65.0},
                margin=5.0,
                deciding_features=[],
                reason="higher total adjusted score wins (60.0 vs 65.0)",
            )
        }
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    return store


class TestApplyAssignmentOverrides:
    def test_engine_reassignment_writes_v2_with_provenance(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )

        assert result.assignment.version == 2
        assert result.assignment.status is AssignmentStatus.CUSTOMER_MODIFIED
        assert result.assignment.source is AssignmentSource.CUSTOMER_GATE
        assert result.assignment.previous_version == 1
        assert result.written_path == f"{DB}/{JOB}/assignment/v2/assignment.json"
        assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        # v1 is untouched.
        assert store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")["version"] == 1

        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].assigned_engine == "opensearch"
        assert by_id["q2"].customer_override is True
        assert by_id["q1"].customer_override is False  # untouched

    def test_in_scope_toggle_is_not_an_engine_override(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", in_scope=False)]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q1"].in_scope is False
        assert by_id["q1"].customer_override is False

    def test_skipped_engines_reports_zero_in_scope(self) -> None:
        # Drop the only opensearch query out of scope -> opensearch is skipped.
        store = _MemStore()
        _seed(
            store,
            [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "opensearch", ["t.docs"])],
            engines=["dynamodb", "opensearch"],
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", in_scope=False)]
        )
        assert result.skipped_engines == ["opensearch"]

    def test_exclude_tables_drops_query_out_of_scope(self) -> None:
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(store, DB, JOB, [], exclude_tables=["t.posts"])
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].in_scope is False  # only accesses the excluded table
        assert by_id["q1"].in_scope is True

    def test_unknown_query_raises_and_writes_nothing(self) -> None:
        store = _store_with_two_ddb_queries()
        with pytest.raises(UnknownQuery):
            apply_assignment_overrides(store, DB, JOB, [QueryOverrideInput("nope", in_scope=False)])
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_no_assignment_raises(self) -> None:
        with pytest.raises(NoAssignmentFound):
            apply_assignment_overrides(_MemStore(), DB, JOB, [])

    def test_hard_validation_error_raises_and_writes_nothing(self) -> None:
        # Reassign to an engine that never analyzed the query -> hard error.
        store = _store_with_two_ddb_queries()
        with pytest.raises(AssignmentValidationFailed) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="neptune")]
            )
        assert exc.value.errors
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestCustomerOverrideClearsAnOpenTie:
    """#503 review: a customer's own engine pick is a real decision, so it
    must clear any ``tied_engines`` the query carried in (round 2, finding
    I5) -- otherwise the gate banner and the counts keep reading "pending
    your choice" after the customer already chose -- and the reason text
    must say so too (round 3, finding 4), rather than keep reading the old
    "... pending your choice" sentence after the field it described has
    already been cleared.
    """

    def test_overriding_a_tied_query_clears_tied_engines_and_rewrites_the_reason(self) -> None:
        store = _MemStore()
        q1 = _qa("q1", "documentdb", ["t.users"]).model_copy(
            update={
                "tied_engines": ["documentdb", "dynamodb"],
                "assignment_reason": (
                    "scores tie with DynamoDB; no routing rule decides between them; "
                    "listed under DocumentDB pending your choice"
                ),
            }
        )
        q2 = _qa("q2", "dynamodb", ["t.posts"])
        _seed(store, [q1, q2], engines=["dynamodb", "documentdb", "opensearch"])

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q1"].assigned_engine == "dynamodb"
        assert by_id["q1"].customer_override is True
        assert by_id["q1"].tied_engines == []
        assert by_id["q1"].assignment_reason == (
            "Placed on DynamoDB by your choice; scores tied with DocumentDB."
        )
        assert "pending your choice" not in by_id["q1"].assignment_reason
        # Untouched query keeps whatever it had (empty here).
        assert by_id["q2"].tied_engines == []

    def test_propagated_co_dependent_member_also_clears_its_tie(self) -> None:
        # q1/q2 share a significant JOIN; overriding q1 propagates to q2
        # (ADR-029 Amendment 3). q2 came in tied -- the propagated move is
        # also a real decision for it, so its tie clears too.
        store = _MemStore()
        q1 = _qa("q1", "dynamodb", ["t.orders", "t.items"])
        q2 = _qa("q2", "dynamodb", ["t.orders", "t.items"]).model_copy(
            update={"tied_engines": ["dynamodb", "documentdb"]}
        )
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=[q1, q2],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": q,
                            "tables_accessed": ["t.orders", "t.items"],
                            "has_joins": True,
                            "join_count": 2,
                        }
                        for q in ("q1", "q2")
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in ("dynamodb", "aurora_postgresql", "documentdb"):
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )

        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].co_dependency_propagated is True
        assert by_id["q2"].assigned_engine == "aurora_postgresql"
        assert by_id["q2"].tied_engines == []


class TestHeterogeneousAuroraChoiceOverrides:
    """#381 review: Assignment.aurora_engine_choice.engine is authoritative --
    load_assignment_for_edit drops the losing engine's analysis, and (round 2) the
    override itself is rejected up front with a clear reason naming both engines."""

    def test_load_assignment_for_edit_drops_the_losing_engines_analysis(self) -> None:
        store = _store_with_heterogeneous_aurora_choice()
        _, _, analysis_outputs, _ = load_assignment_for_edit(store, DB, JOB, 1)
        assert "aurora_mysql" not in analysis_outputs
        assert "aurora_postgresql" in analysis_outputs

    def test_override_to_the_losing_engine_is_rejected(self) -> None:
        store = _store_with_heterogeneous_aurora_choice()
        with pytest.raises(LosingAuroraEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_mysql")]
            )
        # #381 review round 2: a clear, specific reason -- not the generic
        # validator's "... which did not analyze it".
        assert str(exc.value) == (
            "This assessment chose Aurora PostgreSQL for this SQL Server source; "
            "Aurora MySQL is not a target."
        )
        assert exc.value.winner == "aurora_postgresql"
        assert exc.value.loser == "aurora_mysql"
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_override_to_the_winning_engine_still_works(self) -> None:
        store = _store_with_heterogeneous_aurora_choice()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", in_scope=False)]
        )
        assert result.assignment.version == 2


class TestMarkCustomerApproved:
    """mark_assignment_customer_approved stamps the artifact for approve-as-is."""

    def test_stamps_customer_approved_in_place_without_new_version(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        store = _store_with_two_ddb_queries()
        updated = mark_assignment_customer_approved(store, DB, JOB)

        assert updated is not None
        assert updated.status is AssignmentStatus.CUSTOMER_APPROVED
        # Same version, rewritten in place — no v2 is created (staleness-safe).
        assert updated.version == 1
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        persisted = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        assert persisted["status"] == AssignmentStatus.CUSTOMER_APPROVED.value

    def test_leaves_customer_modified_untouched(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        store = _store_with_two_ddb_queries()
        # Apply an edit first -> v2 customer_modified becomes effective.
        apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )
        updated = mark_assignment_customer_approved(store, DB, JOB)

        assert updated is not None
        assert updated.status is AssignmentStatus.CUSTOMER_MODIFIED  # not downgraded to approved

    def test_none_when_no_assignment(self) -> None:
        from src.agents.referee.assignment_overrides import mark_assignment_customer_approved

        assert mark_assignment_customer_approved(_MemStore(), DB, JOB) is None


class TestRecomputesDerivedViews:
    """ADR-029 Layer B: co_dependency_groups and table_assignments are recomputed
    against the new routing, not carried forward from the previous version."""

    def _seed(
        self,
        store: _MemStore,
        query_assignments: list[QueryAssignment],
        query_patterns: list[dict],
        *,
        stale_codep: list[list[str]],
        engines: list[str],
        source_engine: str | None = None,
        known_tables: list[str] | None = None,
    ) -> None:
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=query_assignments,
            table_assignments=[],  # deliberately empty/stale
            co_dependency_groups=stale_codep,
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        collector: dict = {
            "queries": {"query_patterns": query_patterns},
            "database_schema": {"tables": [{"table_id": t} for t in known_tables or []]},
        }
        if source_engine:
            collector["metadata"] = {"source_database": {"engine": source_engine}}
        store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
        for engine in engines:
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )

    def test_stale_co_dependency_group_is_cleared(self) -> None:
        # v1 carries a bogus co-dep group, but the collector queries share no
        # significant JOIN, so the recomputed groups must be empty.
        store = _MemStore()
        self._seed(
            store,
            [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "dynamodb", ["t.posts"])],
            [
                {"query_id": "q1", "tables_accessed": ["t.users"]},
                {"query_id": "q2", "tables_accessed": ["t.posts"]},
            ],
            stale_codep=[["q1", "q2"]],
            engines=["dynamodb", "opensearch"],
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )
        assert result.assignment.co_dependency_groups == []

    def test_co_dependency_group_is_recomputed_from_collector(self) -> None:
        # Two queries share a significant JOIN on the same tables -> one group.
        store = _MemStore()
        self._seed(
            store,
            [_qa("q1", "dynamodb", ["t.orders"]), _qa("q2", "dynamodb", ["t.orders"])],
            [
                {
                    "query_id": "q1",
                    "tables_accessed": ["t.orders", "t.items"],
                    "has_joins": True,
                    "join_count": 2,
                },
                {
                    "query_id": "q2",
                    "tables_accessed": ["t.orders", "t.items"],
                    "has_joins": True,
                    "join_count": 2,
                },
            ],
            stale_codep=[],
            engines=["dynamodb", "opensearch"],
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
        )
        groups = [sorted(g) for g in result.assignment.co_dependency_groups]
        assert ["q1", "q2"] in groups

    def test_table_assignments_reflect_new_routing(self) -> None:
        # #317: t.posts ends up with every query on OpenSearch, a read model
        # that never durably owns a table, so the recomputed primary_engine
        # must fall back to the retained source-compatible engine (here
        # aurora_mysql for a MySQL source) rather than stay on opensearch.
        store = _MemStore()
        self._seed(
            store,
            [_qa("q1", "dynamodb", ["t.users"]), _qa("q2", "dynamodb", ["t.posts"])],
            [
                {"query_id": "q1", "tables_accessed": ["t.users"]},
                {"query_id": "q2", "tables_accessed": ["t.posts"]},
            ],
            stale_codep=[],
            engines=["dynamodb", "opensearch"],
            source_engine="mysql",
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="opensearch")]
        )
        by_table = {ta.table_id: ta for ta in result.assignment.table_assignments}
        assert by_table["t.users"].primary_engine == "dynamodb"
        assert by_table["t.posts"].primary_engine == "aurora_mysql"

    def test_table_assignments_drop_noise_and_record_unresolved_table_names(self) -> None:
        # #316: a name the SQL parser put in source_tables that is not in the
        # collector's schema (a CTE alias here) must not become a table row,
        # and the drop is recorded on the recomputed assignment.
        store = _MemStore()
        self._seed(
            store,
            [_qa("q1", "dynamodb", ["t.users", "cte_alias"])],
            [{"query_id": "q1", "tables_accessed": ["t.users", "cte_alias"]}],
            stale_codep=[],
            engines=["dynamodb", "opensearch"],
            known_tables=["t.users"],
        )
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
        )
        assert [ta.table_id for ta in result.assignment.table_assignments] == ["t.users"]
        assert result.assignment.unresolved_table_names.names == ["cte_alias"]
        assert result.assignment.unresolved_table_names.count == 1


class TestRefreshConsolidatedAssignment:
    """ADR-029 Layers B+E: reality-check consolidation refreshes derived views,
    re-validates, and prunes per-query warnings that name an eliminated engine."""

    @staticmethod
    def _collector(
        query_patterns: list[dict],
        source_engine: str | None = None,
        known_tables: list[dict] | None = None,
    ) -> dict:
        collector: dict = {
            "queries": {"query_patterns": query_patterns},
            "database_schema": {"tables": known_tables or []},
        }
        if source_engine:
            collector["metadata"] = {"source_database": {"engine": source_engine}}
        return collector

    def test_recomputes_views_and_prunes_dead_engine_warnings(self) -> None:
        raw = {
            "job_id": JOB,
            "version": 2,
            "previous_version": 1,
            "status": "auto_generated",
            "source": "reality_check",
            "timestamp": datetime.now(UTC).isoformat(),
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "confidence": 80,
                    "source_tables": ["t.users"],
                    "assignment_reason": "consolidated",
                    "warnings": ["moved off documentdb; verify embedding shape"],
                },
                {
                    "query_id": "q2",
                    "assigned_engine": "dynamodb",
                    "confidence": 80,
                    "source_tables": ["t.posts"],
                    "assignment_reason": "consolidated",
                    "warnings": [],
                },
            ],
            "table_assignments": [],
            "co_dependency_groups": [["stale-group"]],
            "validation_warnings": ["WARNING [HIGH]: documentdb split (stale)"],
            "reality_check_applied": True,
        }
        collector = self._collector(
            [
                {"query_id": "q1", "tables_accessed": ["t.users"]},
                {"query_id": "q2", "tables_accessed": ["t.posts"]},
            ]
        )
        out = refresh_consolidated_assignment(
            raw, collector, {"dynamodb": {"workload_analysis": {}}}, dead_engines={"documentdb"}
        )

        # Stale co-dependency group cleared (no significant JOINs in collector).
        assert out["co_dependency_groups"] == []
        # table_assignments recomputed from the consolidated routing.
        by_table = {ta["table_id"]: ta for ta in out["table_assignments"]}
        assert by_table["t.users"]["primary_engine"] == "dynamodb"
        assert by_table["t.posts"]["primary_engine"] == "dynamodb"
        # Per-query warning naming the eliminated engine is dropped.
        assert out["query_assignments"][0]["warnings"] == []
        # validation_warnings refreshed against current routing (no dead-engine ref).
        assert all("documentdb" not in w for w in out["validation_warnings"])
        # Non-model keys preserved.
        assert out["reality_check_applied"] is True
        assert out["source"] == "reality_check"
        assert out["previous_version"] == 1

    def test_tolerates_partial_query_assignment_dicts(self) -> None:
        # Legacy/minimal dicts (no confidence/source_tables) must not raise.
        raw = {
            "version": 2,
            "query_assignments": [
                {"query_id": "q1", "assigned_engine": "dynamodb", "assignment_reason": "x"},
            ],
        }
        collector = self._collector([{"query_id": "q1", "tables_accessed": ["t.users"]}])
        out = refresh_consolidated_assignment(raw, collector, {"dynamodb": {}})
        assert "table_assignments" in out
        assert out["co_dependency_groups"] == []

    def test_drops_noise_and_resolves_canonical_names(self) -> None:
        # #316: a bare name must resolve against the collector's qualified
        # table_id, and parser noise (a CTE alias) must not become a table.
        raw = {
            "version": 2,
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "confidence": 80,
                    "source_tables": ["wp_posts", "cte_alias"],
                    "assignment_reason": "consolidated",
                },
            ],
        }
        collector = self._collector(
            [{"query_id": "q1", "tables_accessed": ["wp_posts", "cte_alias"]}],
            known_tables=[{"table_id": "wordpress.wp_posts", "table_name": "wp_posts"}],
        )
        out = refresh_consolidated_assignment(raw, collector, {"dynamodb": {}})
        assert [ta["table_id"] for ta in out["table_assignments"]] == ["wordpress.wp_posts"]
        assert out["unresolved_table_names"] == {"count": 1, "names": ["cte_alias"]}

    def test_opensearch_only_table_falls_back_to_retained_engine(self) -> None:
        # #317 finding 1: every in-scope query on this table went to OpenSearch
        # (a read model), so the recomputed primary_engine must fall back to
        # the retained source-compatible engine, never stay on opensearch.
        raw = {
            "version": 2,
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "opensearch",
                    "confidence": 80,
                    "source_tables": ["t.search_log"],
                    "assignment_reason": "signal override: text_search -> opensearch",
                },
            ],
        }
        collector = self._collector(
            [{"query_id": "q1", "tables_accessed": ["t.search_log"]}], source_engine="postgresql"
        )
        out = refresh_consolidated_assignment(raw, collector, {"opensearch": {}})
        by_table = {ta["table_id"]: ta for ta in out["table_assignments"]}
        assert by_table["t.search_log"]["primary_engine"] == "aurora_postgresql"


class TestCoDependencyPropagation:
    """ADR-029 Amendment 3: re-routing one query in a co-dependent JOIN group
    moves the group's other in-scope members with it, so the group stays
    co-located instead of silently splitting."""

    @staticmethod
    def _seed_join_group(store: _MemStore, engines: list[str], *, q3_in_scope: bool = True) -> None:
        # q1, q2, q3 all on dynamodb, all sharing a significant JOIN on t.orders
        # -> one co-dependency group of three.
        qas = [
            _qa("q1", "dynamodb", ["t.orders", "t.items"]),
            _qa("q2", "dynamodb", ["t.orders", "t.items"]),
            _qa("q3", "dynamodb", ["t.orders", "t.items"], in_scope=q3_in_scope),
        ]
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=qas,
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": q,
                            "tables_accessed": ["t.orders", "t.items"],
                            "has_joins": True,
                            "join_count": 2,
                        }
                        for q in ("q1", "q2", "q3")
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in engines:
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )

    def test_moving_one_query_moves_its_codependent_group(self) -> None:
        store = _MemStore()
        self._seed_join_group(store, engines=["dynamodb", "aurora_postgresql"])
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        # q1 is the explicit customer pick.
        assert by_id["q1"].assigned_engine == "aurora_postgresql"
        assert by_id["q1"].customer_override is True
        assert by_id["q1"].co_dependency_propagated is False
        # q2, q3 moved with it to keep the JOIN group together, tagged propagated
        # (NOT customer_override).
        for q in ("q2", "q3"):
            assert by_id[q].assigned_engine == "aurora_postgresql", q
            assert by_id[q].co_dependency_propagated is True, q
            assert by_id[q].customer_override is False, q
        assert result.propagated_query_ids == ["q2", "q3"]

    def test_explicit_split_is_respected_no_propagation(self) -> None:
        # The customer sets two group members to DIFFERENT engines -> respect both
        # explicit picks and propagate nothing (they chose to split it).
        store = _MemStore()
        self._seed_join_group(store, engines=["dynamodb", "aurora_postgresql", "aurora_mysql"])
        result = apply_assignment_overrides(
            store,
            DB,
            JOB,
            [
                QueryOverrideInput("q1", assigned_engine="aurora_postgresql"),
                QueryOverrideInput("q2", assigned_engine="aurora_mysql"),
            ],
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q1"].assigned_engine == "aurora_postgresql"
        assert by_id["q2"].assigned_engine == "aurora_mysql"
        # q3 (untouched) is left alone because the group was explicitly split.
        assert by_id["q3"].co_dependency_propagated is False
        assert by_id["q3"].assigned_engine == "dynamodb"
        assert result.propagated_query_ids == []

    def test_out_of_scope_group_member_not_propagated(self) -> None:
        store = _MemStore()
        self._seed_join_group(store, engines=["dynamodb", "aurora_postgresql"], q3_in_scope=False)
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].co_dependency_propagated is True
        # q3 is out of scope -> left on its original engine, not propagated.
        assert by_id["q3"].co_dependency_propagated is False
        assert by_id["q3"].assigned_engine == "dynamodb"
        assert result.propagated_query_ids == ["q2"]

    def test_no_group_no_propagation(self) -> None:
        # Single-table queries with no shared JOIN form no group -> nothing moves.
        store = _store_with_two_ddb_queries()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="opensearch")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q2"].assigned_engine == "dynamodb"  # untouched
        assert by_id["q2"].co_dependency_propagated is False
        assert result.propagated_query_ids == []


class TestExcludedEngineOverride:
    """#548: an override onto an engine a hard exclusion rules out for the
    query is rejected -- with a plain, customer-facing reason naming the
    engine via ``display_engine`` -- never silently moved to a different
    engine on the customer's behalf."""

    def _seed_wildcard_query(self) -> _MemStore:
        store = _MemStore()
        _seed(
            store,
            [_qa("q1", "aurora_postgresql", ["t.posts"])],
            engines=["aurora_postgresql", "dynamodb", "opensearch"],
            query_texts={"q1": "SELECT id, title FROM t.posts WHERE title LIKE '%term%'"},
        )
        return store

    def test_override_onto_an_excluded_engine_is_rejected_and_writes_nothing(self) -> None:
        store = self._seed_wildcard_query()
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
            )
        assert len(exc.value.excluded) == 1
        assert exc.value.excluded[0].query_id == "q1"
        assert exc.value.excluded[0].engine == "dynamodb"
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_rejection_reason_names_the_query_the_engine_and_the_plain_cause(self) -> None:
        store = self._seed_wildcard_query()
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
            )
        # The query id (round 2 review: a batch failure must name its row),
        # display_engine (not the raw "dynamodb" code), and the plain
        # customer_message fragment -- never the technical, log-only
        # ``description``. Round 3 review: one "query" and one colon, and no
        # specific literal like "'%term%'" that could read as a quote of the
        # customer's own SQL.
        assert str(exc.value) == (
            "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...')."
        )

    def test_override_onto_a_supported_engine_still_applies(self) -> None:
        store = self._seed_wildcard_query()
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="opensearch")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["q1"].assigned_engine == "opensearch"
        assert by_id["q1"].customer_override is True
        assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_batch_reports_every_excluded_override_not_just_the_first(self) -> None:
        # #548 round 2 review item 2: a batch with two bad rows must name
        # both, not just the first one encountered.
        store = _MemStore()
        _seed(
            store,
            [
                _qa("q1", "aurora_postgresql", ["t.posts"]),
                _qa("q2", "aurora_postgresql", ["t.comments"]),
            ],
            engines=["aurora_postgresql", "dynamodb"],
            query_texts={
                "q1": "SELECT id FROM t.posts WHERE title LIKE '%term%'",
                "q2": "SELECT id FROM t.comments WHERE body LIKE '%other%'",
            },
        )
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store,
                DB,
                JOB,
                [
                    QueryOverrideInput("q1", assigned_engine="dynamodb"),
                    QueryOverrideInput("q2", assigned_engine="dynamodb"),
                ],
            )
        assert {e.query_id for e in exc.value.excluded} == {"q1", "q2"}
        # Round 3 review: one violation per line, so a batch of several
        # stays easy to scan instead of running together as one paragraph.
        assert str(exc.value) == (
            "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...').\n"
            "DynamoDB can't run query q2: it uses a leading-wildcard search (LIKE '%...')."
        )
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


class TestGroupMoveRejectsOnAnExcludedMember:
    """#548 round 2 review item 1: a co-dependency group moves as one unit
    (ADR-029 Amendment 3), so a group-mate the target engine cannot run at
    all blocks the WHOLE group's move -- not just that member, and not a
    silent partial move. This is the review's own repro."""

    def _seed_join_group(self) -> _MemStore:
        store = _MemStore()
        q1 = _qa("q1", "aurora_postgresql", ["t.posts", "t.users"])
        q2 = _qa("q2", "aurora_postgresql", ["t.posts", "t.users"])
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=[q1, q2],
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": "q1",
                            "query_text": (
                                "SELECT p.id FROM posts p JOIN users u ON u.id = "
                                "p.author_id WHERE p.id = ?"
                            ),
                            "tables_accessed": ["t.posts", "t.users"],
                            "has_joins": True,
                            "join_count": 2,
                        },
                        {
                            "query_id": "q2",
                            "query_text": (
                                "SELECT p.id FROM posts p JOIN users u ON u.id = "
                                "p.author_id WHERE p.title LIKE '%term%'"
                            ),
                            "tables_accessed": ["t.posts", "t.users"],
                            "has_joins": True,
                            "join_count": 2,
                        },
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in ("aurora_postgresql", "dynamodb"):
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )
        return store

    def test_group_move_is_rejected_when_a_member_cannot_run_on_the_target(self) -> None:
        store = self._seed_join_group()
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
            )
        # Groups move as one unit: nothing is written, not even for q1, which
        # has no exclusion problem of its own.
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        v1 = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        by_id = {qa["query_id"]: qa for qa in v1["query_assignments"]}
        assert by_id["q1"]["assigned_engine"] == "aurora_postgresql"
        assert by_id["q2"]["assigned_engine"] == "aurora_postgresql"
        # The blocking group-mate (q2), not the query the customer actually
        # overrode (q1), is named -- that is the whole point of the check.
        assert len(exc.value.excluded) == 1
        assert exc.value.excluded[0].query_id == "q2"
        assert exc.value.excluded[0].engine == "dynamodb"
        # Round 3 review: names both the query the customer moved (q1) and
        # the blocking group-mate (q2), and no specific literal like
        # "'%term%'".
        assert str(exc.value) == (
            "Can't move q1's group to DynamoDB: query q2 in that group uses a "
            "leading-wildcard search (LIKE '%...')."
        )


class TestCapabilityGateBlocksCustomerPicks:
    """#801: the customer gate holds every pick to the same capability gate
    the scorer uses (ADR-031 Decision 2 step 2), not only to the hard
    exclusions (#548). Groups move as one unit (Decision 4), so a group-mate
    the target engine is structurally unable to run blocks the whole batch.
    A pick among a query's tied engines is never blocked (Decision 12): every
    tied engine scored, so it passed the gate. This is the #800 review's repro:
    three JOIN queries on Aurora PostgreSQL, q2 a GROUP BY aggregation."""

    _JOIN = "SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id WHERE o.id = ?"
    _AGGREGATION = (
        "SELECT o.customer_id, COUNT(*) FROM orders o JOIN items i ON i.order_id = o.id "
        "GROUP BY o.customer_id"
    )

    def _seed_join_group(self, *, q2_tied: list[str] | None = None) -> _MemStore:
        store = _MemStore()
        tables = ["t.orders", "t.items"]
        q2 = _qa("q2", "aurora_postgresql", tables)
        if q2_tied:
            q2 = q2.model_copy(update={"tied_engines": q2_tied})
        qas = [_qa("q1", "aurora_postgresql", tables), q2, _qa("q3", "aurora_postgresql", tables)]
        assignment = Assignment(
            job_id=JOB,
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(UTC),
            query_assignments=qas,
            table_assignments=[],
            co_dependency_groups=[],
            validation_warnings=[],
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
        )
        texts = {"q1": self._JOIN, "q2": self._AGGREGATION, "q3": self._JOIN}
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {
                "queries": {
                    "query_patterns": [
                        {
                            "query_id": q,
                            "query_text": texts[q],
                            "tables_accessed": tables,
                            "has_joins": True,
                            "join_count": 2,
                        }
                        for q in ("q1", "q2", "q3")
                    ]
                },
                "database_schema": {"tables": []},
            },
        )
        for engine in ("aurora_postgresql", "dynamodb", "documentdb"):
            store.write_json(
                f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}}
            )
        return store

    def _assert_nothing_written(self, store: _MemStore) -> None:
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_picking_another_member_onto_a_gated_engine_rejects_the_whole_batch(self) -> None:
        store = self._seed_join_group()
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="dynamodb")]
            )
        self._assert_nothing_written(store)
        # The gated group-mate (q2), not the query the customer picked (q1).
        assert [(e.query_id, e.engine, e.gate_kind) for e in exc.value.excluded] == [
            ("q2", "dynamodb", "capability")
        ]
        assert str(exc.value) == (
            "Can't move q1's group to DynamoDB: query q2 in that group needs aggregation."
        )

    def test_picking_the_aggregation_itself_onto_a_gated_engine_is_rejected(self) -> None:
        store = self._seed_join_group()
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="dynamodb")]
            )
        self._assert_nothing_written(store)
        assert [(e.query_id, e.engine, e.gate_kind) for e in exc.value.excluded] == [
            ("q2", "dynamodb", "capability")
        ]
        assert str(exc.value) == "DynamoDB can't run query q2: it needs aggregation."

    def test_a_pick_among_the_querys_tied_engines_is_accepted(self) -> None:
        store = self._seed_join_group(q2_tied=["aurora_postgresql", "documentdb"])
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("q2", assigned_engine="documentdb")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert {q: by_id[q].assigned_engine for q in by_id} == {
            "q1": "documentdb",
            "q2": "documentdb",
            "q3": "documentdb",
        }
        assert by_id["q2"].tied_engines == []
        assert result.propagated_query_ids == ["q1", "q3"]
        # The accepted moves still go through the ledger (#616), with scores.
        moves = {(m.query_id, m.reason_code) for m in result.assignment.moves}
        assert moves == {
            ("q2", "customer.pick"),
            ("q1", "group.customer_propagation"),
            ("q3", "group.customer_propagation"),
        }
        assert all(m.score_after is not None for m in result.assignment.moves)

    def test_every_gated_query_in_the_batch_is_reported(self) -> None:
        # One exclusion (#548) and one capability gap (#801) in the same batch
        # are reported together, one per line.
        store = _MemStore()
        _seed(
            store,
            [
                _qa("q1", "aurora_postgresql", ["t.posts"]),
                _qa("q2", "aurora_postgresql", ["t.comments"]),
            ],
            engines=["aurora_postgresql", "dynamodb"],
            query_texts={
                "q1": "SELECT id FROM t.posts WHERE title LIKE '%term%'",
                "q2": "SELECT post_id, COUNT(*) FROM t.comments GROUP BY post_id",
            },
        )
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store,
                DB,
                JOB,
                [
                    QueryOverrideInput("q1", assigned_engine="dynamodb"),
                    QueryOverrideInput("q2", assigned_engine="dynamodb"),
                ],
            )
        assert str(exc.value) == (
            "DynamoDB can't run query q1: it uses a leading-wildcard search (LIKE '%...').\n"
            "DynamoDB can't run query q2: it needs aggregation."
        )
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_a_utility_statement_reads_as_one_not_as_needing_one(self) -> None:
        # Round 2 review: "it needs a utility or DDL statement" misread the
        # query; the query is the statement.
        store = _MemStore()
        _seed(
            store,
            [_qa("q1", "aurora_postgresql", ["t.posts"])],
            engines=["aurora_postgresql", "documentdb"],
            query_texts={"q1": "ALTER TABLE posts ADD COLUMN x int"},
        )
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("q1", assigned_engine="documentdb")]
            )
        assert str(exc.value) == (
            "DocumentDB can't run query q1: it is a utility or DDL statement."
        )
