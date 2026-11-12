# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Statements on SQL Server temporary objects only are not routed on their own (#1876).

A statement whose only objects are temp tables (``#t``, ``##t``) or table variables
(``@t``) reads or writes scratch rows local to its session or batch. It used to get the
``unknown`` placeholder table, score 0 for every engine and be settled by a 0-vs-0 tie
(DNN Platform's ``SELECT TotalRecords = COUNT(*) FROM #PageIndexForUsers`` went to
Aurora PostgreSQL, ``INSERT INTO @tmp ...`` to DynamoDB). Now the utility pin keeps it
with the utility statements, says why, and lists no source table. One whose rows come
from a table-valued function or a procedure is kept the same way with a reason of its
own (review of #1879), and one that also touches a real table is still routed on that
table. The report groups and counts the pinned ones only, from the pin, never from the
text alone. The fixture is synthetic
(``tests/fixtures/sqlserver_temp_objects_collection.json``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee import (
    assignment_resolver,
    capability_registry,
    feasibility_review,
    write_groups,
)
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.engine_scope import record_engine_scope
from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.agents.referee.synthesis_grounding import (
    build_effective_architecture,
    build_fallback_summary,
)
from src.agents.referee.synthesis_handler import run_synthesis
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.triage_handler import run_triage
from src.agents.referee.utility_statements import (
    PROCEDURE_INTERNAL_REASON,
    PROCEDURE_INTERNAL_ROUTINE_REASON,
    is_procedure_internal_statement,
    is_utility_statement,
    procedure_internal_reason,
)
from src.report.engineering_report import _procedure_internal_text, render_engineering_report_md
from src.shared.report_facts import PROCEDURE_INTERNAL_GROUP_LABEL
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / "tests" / "fixtures" / "sqlserver_temp_objects_collection.json"
DB = "TempObjects"
JOB = "job-1876"
SQLSERVER = SourceScope("TempObjects", "sqlserver")
AURORA = {"aurora_postgresql", "aurora_mysql"}

# Temporary objects alone.
TEMP_ONLY = (
    "tt-temp-select",
    "tt-tablevar-insert",
    "tt-global-temp",
    "tt-plan-store",
    "tt-tablevar-if",
    "tt-drop-guard",
)
# Temporary objects with rows from a table-valued function or a procedure.
ROUTINE = ("tt-fn-insert", "tt-exec-insert", "tt-apply")
PROCEDURE_INTERNAL = TEMP_ONLY + ROUTINE
ROUTED = (
    "tt-app-user",
    "tt-app-orders",
    "tt-app-order-insert",
    "tt-temp-and-real",
    "tt-app-plan",
    "tt-real-apply",
    "tt-real-exec",
)


def _queries() -> dict[str, dict]:
    collection = json.loads(FIXTURE.read_text())
    return {q["query_id"]: q for q in collection["queries"]["query_patterns"]}


def _run(
    base: Path, *, aurora: bool = True, scope: list[str] | None = None
) -> tuple[LocalArtifactStore, int]:
    """The deterministic pipeline on the synthetic collection: the store and the
    assignment version synthesis read. ``aurora=False`` analyses no Aurora engine;
    ``scope`` is the job's engine scope."""
    store = LocalArtifactStore(base_dir=str(base))
    store.write_json(f"{DB}/{JOB}/collector/output.json", json.loads(FIXTURE.read_text()))
    if scope is not None:
        record_engine_scope(store, DB, JOB, scope, via="library")
    run_triage(JOB, DB, store)
    triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
    for engine in [a["agent_type"] for a in triage["selected_agents"]]:
        if aurora or engine not in AURORA:
            run_analysis(JOB, DB, engine, store, llm_mode="none")
    run_assignment_resolver(JOB, DB, store)
    run_reality_check_handler(JOB, DB, store, llm_mode="none")
    version = resolve_downstream_assignment_version(store, DB, JOB)
    run_synthesis(JOB, DB, store, version, llm_mode="none")
    return store, version


def _assignment(store: LocalArtifactStore, version: int = 1) -> dict[str, dict]:
    result: dict = store.read_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json")
    return {qa["query_id"]: qa for qa in result["query_assignments"]}


def _report(store: LocalArtifactStore, version: int) -> dict:
    report: dict = store.read_json(f"{DB}/{JOB}/synthesis/v{version}/report.json")
    return report


def _group_names(report: dict) -> set[str]:
    return {str(g["group_name"]) for g in report["query_groups"]}


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> tuple[LocalArtifactStore, int]:
    return _run(tmp_path_factory.mktemp("temp-objects"))


@pytest.fixture(scope="module")
def assignment(pipeline) -> dict[str, dict]:
    """The first assignment of the synthetic collection, by query id."""
    return _assignment(pipeline[0])


@pytest.fixture(scope="module")
def report(pipeline) -> dict:
    return _report(*pipeline)


@pytest.fixture(scope="module")
def saved_before(tmp_path_factory) -> Iterator[tuple[LocalArtifactStore, int]]:
    """An assignment as the resolver wrote it before #1876 (no statement is
    procedure-internal there, so each went through the tie-break), synthesized now."""

    def old_utility(text, tables=None, source=None):
        return original(text, tables, source) and not is_procedure_internal_statement(
            text, tables, source
        )

    original = is_utility_statement
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(assignment_resolver, "procedure_internal_reason", lambda *a, **k: None)
        for module in (assignment_resolver, capability_registry, feasibility_review, write_groups):
            mp.setattr(module, "is_utility_statement", old_utility)
        store = LocalArtifactStore(base_dir=str(tmp_path_factory.mktemp("saved-before")))
        store.write_json(f"{DB}/{JOB}/collector/output.json", json.loads(FIXTURE.read_text()))
        run_triage(JOB, DB, store)
        triage = store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")
        for engine in [a["agent_type"] for a in triage["selected_agents"]]:
            run_analysis(JOB, DB, engine, store, llm_mode="none")
        run_assignment_resolver(JOB, DB, store)
    # The first assignment: its tie-break put some of them on an Aurora engine, where
    # the relational branch of the query groups lists them.
    run_synthesis(JOB, DB, store, 1, llm_mode="none")
    yield store, 1


# -- The classification -------------------------------------------------------------


def test_the_fixture_tables_are_what_the_collector_reads() -> None:
    from src.tools.sql.facade import table_references

    for q in _queries().values():
        refs = [r.written for r in table_references(q["query_text"], "sqlserver").tables]
        assert (refs or ["unknown"]) == q["tables_accessed"], q["query_id"]


@pytest.mark.parametrize("qid", TEMP_ONLY)
def test_a_temp_only_statement_is_procedure_internal(qid: str) -> None:
    q = _queries()[qid]
    assert procedure_internal_reason(q["query_text"], q["tables_accessed"], SQLSERVER) == (
        PROCEDURE_INTERNAL_REASON
    )
    assert is_utility_statement(q["query_text"], q["tables_accessed"], SQLSERVER)


@pytest.mark.parametrize("qid", ROUTINE)
def test_rows_from_a_function_or_procedure_have_their_own_reason(qid: str) -> None:
    q = _queries()[qid]
    assert procedure_internal_reason(q["query_text"], q["tables_accessed"], SQLSERVER) == (
        PROCEDURE_INTERNAL_ROUTINE_REASON
    )
    assert is_utility_statement(q["query_text"], q["tables_accessed"], SQLSERVER)


@pytest.mark.parametrize(
    "text",
    [
        "INSERT INTO @t SELECT value FROM STRING_SPLIT(@s, ?)",
        "SELECT * FROM #t CROSS APPLY OPENJSON(@j)",
    ],
)
def test_a_built_in_row_source_is_no_function_source(text: str) -> None:
    assert procedure_internal_reason(text, ["unknown"], SQLSERVER) == PROCEDURE_INTERNAL_REASON


@pytest.mark.passes_on_main  # main routes them on their table already
@pytest.mark.parametrize("qid", ROUTED)
def test_a_statement_with_a_real_table_is_not(qid: str) -> None:
    q = _queries()[qid]
    assert not is_procedure_internal_statement(q["query_text"], q["tables_accessed"], SQLSERVER)
    assert not is_utility_statement(q["query_text"], q["tables_accessed"], SQLSERVER)


@pytest.mark.passes_on_main  # no other source was ever read for temp objects
def test_only_a_sql_server_source_has_temp_objects() -> None:
    # MySQL's ``@v`` is a session variable, and no source means no dialect to read.
    text, tables = "SELECT 1 INTO @v", ["unknown"]
    assert not is_procedure_internal_statement(text, tables, SourceScope("shop", "mysql"))
    q = _queries()["tt-temp-select"]
    assert not is_procedure_internal_statement(q["query_text"], q["tables_accessed"], None)


@pytest.mark.passes_on_main  # a no-table SELECT was never procedure-internal
def test_a_statement_with_no_object_at_all_is_not_procedure_internal() -> None:
    # Entity Framework's no-table SELECT stays a no-source-table statement.
    text = "SELECT ? AS [C1] FROM ( SELECT ? AS X ) AS [SingleRowTable1] WHERE ? = ?"
    assert not is_procedure_internal_statement(text, ["unknown"], SQLSERVER)


# -- Routing --------------------------------------------------------------------------


@pytest.mark.parametrize("qid", PROCEDURE_INTERNAL)
def test_routing_keeps_it_with_the_utility_statements(assignment, qid: str) -> None:
    qa = assignment[qid]
    reason = PROCEDURE_INTERNAL_ROUTINE_REASON if qid in ROUTINE else PROCEDURE_INTERNAL_REASON
    assert qa["pinned"] == "utility"
    assert qa["assignment_reason"].startswith(f"{reason}, kept on the ")
    # No placeholder table and no tie: the pin is not an engine choice.
    assert qa["source_tables"] == []
    assert qa["tied_engines"] == []
    assert qa["assigned_engine"] in AURORA


@pytest.mark.passes_on_main  # main routes them on their table already
@pytest.mark.parametrize("qid", ROUTED)
def test_a_query_on_a_real_table_is_still_routed(assignment, qid: str) -> None:
    qa = assignment[qid]
    assert qa["pinned"] is None
    assert "procedure-internal" not in qa["assignment_reason"]
    assert qa["source_tables"] == _queries()[qid]["tables_accessed"]


# -- The report ---------------------------------------------------------------------


def test_the_report_groups_and_counts_them(report) -> None:
    groups = [
        g for g in report["query_groups"] if g["group_name"] == PROCEDURE_INTERNAL_GROUP_LABEL
    ]
    assert len(groups) == 1
    grouped = {q for ap in groups[0]["access_patterns"] for q in ap["query_ids"]}
    assert grouped == set(PROCEDURE_INTERNAL)
    counts = report["facts"]["query_group_counts"]
    assert counts["procedure_internal_queries"] == len(PROCEDURE_INTERNAL)
    # The query on a temp table and a real one stays with its table's group.
    users = [g for g in report["query_groups"] if "dbo.Users" in g["group_name"]]
    assert any("tt-temp-and-real" in ap["query_ids"] for g in users for ap in g["access_patterns"])


def test_the_engineering_report_states_the_figure(report) -> None:
    md = render_engineering_report_md(report)
    assert _procedure_internal_text(len(PROCEDURE_INTERNAL)) in md
    assert f"| {PROCEDURE_INTERNAL_GROUP_LABEL} | {len(PROCEDURE_INTERNAL)} |" in md


def test_the_engineering_report_formats_the_stored_figure(report) -> None:
    """The sentence prints the fact, never a count of its own."""
    report = json.loads(json.dumps(report))
    report["facts"]["query_group_counts"]["procedure_internal_queries"] = 1
    md = render_engineering_report_md(report)
    assert _procedure_internal_text(1) in md
    assert _procedure_internal_text(len(PROCEDURE_INTERNAL)) not in md


@pytest.mark.parametrize("n", [1, 9])
def test_the_ui_and_the_engineering_report_say_the_same_sentence(n: int) -> None:
    en = json.loads((REPO / "src/ui/src/locales/en.json").read_text())
    key = "analysis-results.aurora-design.procedure-internal_" + ("one" if n == 1 else "other")
    assert en[key].replace("{{count}}", str(n)) == _procedure_internal_text(n)


@pytest.fixture(scope="module")
def no_aurora(tmp_path_factory) -> tuple[LocalArtifactStore, int]:
    """An engine scope with no Aurora engine: the utility pin has no engine to pin to."""
    return _run(
        tmp_path_factory.mktemp("no-aurora"), scope=["dynamodb", "documentdb", "opensearch"]
    )


@pytest.mark.passes_on_main  # the guard the #2152 rewrite had dropped, restated for a scope
def test_with_no_aurora_engine_nothing_is_labelled_procedure_internal(no_aurora) -> None:
    store, version = no_aurora
    v1 = _assignment(store)
    for qid in PROCEDURE_INTERNAL:
        # No engine in the scope serves it, so it is placed nowhere, never pinned.
        assert qid not in v1
    report = _report(store, version)
    assert PROCEDURE_INTERNAL_GROUP_LABEL not in _group_names(report)
    assert report["facts"]["query_group_counts"]["procedure_internal_queries"] == 0
    assert "procedure-internal" not in render_engineering_report_md(report)


def test_with_no_aurora_analysis_the_first_assignment_is_refused(tmp_path) -> None:
    """No Aurora engine analysed: every analysed engine is gated off these statements, so
    the Aurora fallback would place them on an engine that did not analyse them, a hard
    error that writes no version (#2152)."""
    with pytest.raises(RuntimeError) as refused:  # InvalidAssignment, named so main loads
        _run(tmp_path, aurora=False)
    assert type(refused.value).__name__ == "InvalidAssignment"
    assert "first assignment" in str(refused.value)
    # One error per engine, readable (#2152 review): its name, the count, what to do.
    errors = getattr(refused.value, "errors", [])
    analysed = {e for e in errors if "has no analysis" in e}
    assert analysed and all("Run its analysis again, then the assignment." in e for e in analysed)
    assert len(analysed) == len({e.split(" has no analysis")[0] for e in analysed})
    assert not any("aurora_" in e or "['" in e for e in errors)
    # It fits run_assessment.py's status line, type name included.
    assert len(f"InvalidAssignment: {refused.value}") <= 1000
    assert not LocalArtifactStore(base_dir=str(tmp_path)).exists(
        f"{DB}/{JOB}/assignment/v1/assignment.json"
    )


def test_an_assignment_saved_before_is_not_labelled_procedure_internal(saved_before) -> None:
    store, version = saved_before
    final = _assignment(store, version)
    assert not any(final[qid]["pinned"] for qid in PROCEDURE_INTERNAL)
    # Some of them went to an Aurora engine through the tie-break, where the
    # relational branch lists them: with no pin they are not procedure-internal.
    assert all(
        "procedure-internal" not in final[qid]["assignment_reason"] for qid in PROCEDURE_INTERNAL
    )
    assert any(final[qid]["assigned_engine"] in AURORA for qid in PROCEDURE_INTERNAL)
    report = _report(store, version)
    assert PROCEDURE_INTERNAL_GROUP_LABEL not in _group_names(report)
    assert report["facts"]["query_group_counts"]["procedure_internal_queries"] == 0
    md = render_engineering_report_md(report)
    assert "procedure-internal" not in md


# -- Never a busiest group ------------------------------------------------------------


def test_the_group_is_never_a_busiest_group() -> None:
    """``synthesis_grounding._GROUPS_NEVER_TOP``: the summary context and the fallback
    summary never name the procedure-internal group, however busy."""
    pg = "aurora_postgresql"
    groups = [
        {
            "group_name": PROCEDURE_INTERNAL_GROUP_LABEL,
            "access_patterns": [{"engine": pg, "design_rps": 999.0}],
        },
        {"group_name": "dbo.Users", "access_patterns": [{"engine": pg, "design_rps": 1.0}]},
    ]
    eff = build_effective_architecture(
        {pg: ["TempObjects.dbo.Users"]}, [], [{"target": pg}], groups, "TempObjects", {}
    )
    (entry,) = eff["engines"]
    assert entry["top_query_groups"] == ["dbo.Users"]
    entry["assigned_queries"] = 12
    entry["top_query_groups"] = [PROCEDURE_INTERNAL_GROUP_LABEL, "dbo.Users"]
    text = build_fallback_summary(eff) or ""
    assert "led by dbo.Users." in text
    assert PROCEDURE_INTERNAL_GROUP_LABEL not in text
