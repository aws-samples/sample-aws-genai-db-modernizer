"""Every consumer of a query no engine in the customer's engine scope can serve (#1329).

The customer gate refuses to edit one (a job's scope is fixed: a new assessment widens it)
and carries the list into every version it writes; the legacy cache owner fallback
(the #1331 review's note) makes a query unservable instead of placing it outside the
scope; wave 1 names no engine the scope leaves out; synthesis lists the queries in
``facts.unservable`` (kind ``no_engine_in_scope``) and raises one blocking risk; the
engineering report and the executive report print them from the facts (ADR-032).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.agents.referee.assignment_overrides import (
    AssignmentOverrideError,
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.cache_overlay import normalize_cache_owners
from src.agents.referee.migration_waves import build_migration_waves
from src.agents.referee.move_ledger import replay, state_of
from src.agents.referee.triage import triage
from src.report.renderers import render_engineering_report_md
from src.shared.migration_risk_rules import backfill_report_risks, report_fact_risks
from src.shared.report_facts import compute_report_facts
from src.storage.local_store import LocalArtifactStore
from tests.executive_pdf import engineering_text, executive_text
from tests.unit.agents.referee.test_engine_scope import _analysis, _collector, _triage_doc
from tests.unit.agents.referee.test_unservable_in_scope import READ, SHOW, W1, W2

DB, JOB = "shop", "job-1"
PREFIX = f"{DB}/{JOB}"


def _job(tmp_path, scope: list[str] | None, queries: list[dict]) -> LocalArtifactStore:
    """A job with its collection, triage (and scope), analyses and first assignment."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    collector = {**_collector("mysql", queries), "job_id": JOB}
    result = triage(collector, engine_scope=scope)
    triage_doc = _triage_doc(result, scope)
    analyses = {e: _analysis() for e in result.selected}
    store.write_json(f"{PREFIX}/collector/output.json", collector)
    store.write_json(f"{PREFIX}/referee-triage/triage.json", triage_doc)
    for engine, analysis in analyses.items():
        store.write_json(f"{PREFIX}/analysis-{engine}/analysis.json", analysis)
    v1 = AssignmentResolver().resolve(triage_doc, analyses, collector)
    store.write_json(f"{PREFIX}/assignment/v1/assignment.json", v1.model_dump(mode="json"))
    return store


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheCustomerGate:
    @pytest.mark.parametrize(
        "override",
        [
            QueryOverrideInput(query_id="w1", assigned_engine="dynamodb"),
            QueryOverrideInput(query_id="w1", assigned_engine="aurora_mysql"),
            QueryOverrideInput(query_id="w1", in_scope=False),
            QueryOverrideInput(query_id="w1", cached=True),
        ],
        ids=["in-scope pick", "out-of-scope pick", "scope toggle", "cache pin"],
    )
    def test_an_edit_of_an_unservable_query_is_refused_and_nothing_written(
        self, tmp_path, override
    ) -> None:
        from src.agents.referee.assignment_overrides import UnservableQueryOverride

        store = _job(tmp_path, ["dynamodb"], [READ, W1, W2])
        with pytest.raises(UnservableQueryOverride) as refused:
            apply_assignment_overrides(store, DB, JOB, [override])
        # The API and the AWS Transform tools report it as a refused edit.
        assert isinstance(refused.value, AssignmentOverrideError)
        message = str(refused.value)
        assert "Query w1 has no engine" in message
        assert "(DynamoDB)" in message
        assert "start a new job whose scope adds Aurora MySQL" in message
        assert not store.exists(f"{PREFIX}/assignment/v2/assignment.json")

    def test_a_version_the_gate_writes_carries_the_unservable_queries(self, tmp_path) -> None:
        store = _job(tmp_path, ["dynamodb"], [READ, W1, W2])
        v1 = store.read_json(f"{PREFIX}/assignment/v1/assignment.json")
        apply_assignment_overrides(store, DB, JOB, [QueryOverrideInput("r1", in_scope=False)])
        v2 = store.read_json(f"{PREFIX}/assignment/v2/assignment.json")
        assert v2["unservable_queries"] == v1["unservable_queries"]
        assert [qa["query_id"] for qa in v2["query_assignments"]] == ["r1"]
        # A rebuilt group view leaves out the unit with no assigned member.
        assert v2["write_groups"] == {}
        moves = [m for m in v2.get("moves") or [] if m["query_id"] in ("w1", "w2")]
        assert moves == []


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheLegacyCacheOwnerFallback:
    """The #1331 review's note: a query a cache owned in an assignment written before
    the overlay falls back through ``cache_overlay.owner_decision``, which names the
    source-compatible engine whatever the scope. Outside the scope it is unservable."""

    def _legacy(self) -> dict[str, Any]:
        return {
            "query_assignments": [
                {
                    "query_id": "q9",
                    "assigned_engine": "elasticache",
                    "confidence": 50,
                    "source_tables": ["app.orders"],
                    "assignment_reason": "legacy cache owner",
                }
            ],
            "moves": [],
        }

    def _normalize(self, assignment: dict, scope: list[str] | None) -> list[str]:
        collector = _collector("mysql", [SHOW])
        result = triage(collector, engine_scope=scope)
        return normalize_cache_owners(
            assignment,
            [SHOW],
            {"dynamodb": _analysis()},
            "mysql",
            triage=_triage_doc(result, scope),
            collector=collector,
        )

    def test_a_fallback_owner_outside_the_scope_is_unservable(self) -> None:
        assignment = self._legacy()
        notes = self._normalize(assignment, ["dynamodb"])
        assert assignment["query_assignments"] == []
        (record,) = assignment["unservable_queries"]
        assert (record["query_id"], record["closest_engine"]) == ("q9", "aurora_mysql")
        assert record["gates"] == [
            {
                "engine": "dynamodb",
                "gate": "capability",
                "query_id": None,
                "gate_text": "missing a capability the query needs",
            }
        ]
        (move,) = assignment["moves"]
        assert (move["pass"], move["to_engine"], move["from_engine"]) == (
            "unservable",
            "unservable",
            "elasticache",
        )
        assert "1 has no engine in the customer's engine scope" in notes[0]
        assert replay([], start=state_of([], assignment["unservable_queries"]))["q9"] == (
            "unservable",
            [],
        )

    @pytest.mark.passes_on_main  # with no scope the fallback must not change
    def test_with_no_scope_it_still_falls_back_to_the_source_compatible_engine(self) -> None:
        assignment = self._legacy()
        self._normalize(assignment, None)
        assert [qa["assigned_engine"] for qa in assignment["query_assignments"]] == ["aurora_mysql"]
        assert not assignment.get("unservable_queries")


def _waves(scope: list[str] | None) -> list[dict[str, Any]]:
    return (
        build_migration_waves(
            ranking=[{"target": "dynamodb", "assigned_queries": 1, "workload_percent": 100.0}],
            table_assignments=[
                {"table_id": "app.orders", "primary_engine": "dynamodb", "query_count": 1}
            ],
            query_assignments=[
                {"query_id": "r1", "assigned_engine": "dynamodb", "source_tables": ["app.orders"]}
            ],
            co_dependency_groups=[],
            cache_overlay=None,
            source_engine="mysql",
            known_tables={"app.orders"},
            engine_scope=scope,
        )
        or []
    )


class TestWaveOne:
    def test_a_scope_with_no_aurora_engine_names_no_relational_target(self) -> None:
        waves = _waves(["dynamodb"])
        assert waves[0]["engines"] == []
        assert all("aurora_mysql" not in (w.get("engines") or []) for w in waves)
        assert "Aurora MySQL" not in waves[0]["title"]

    def test_with_no_scope_wave_one_moves_to_the_source_compatible_engine(self) -> None:
        assert _waves(None)[0]["engines"] == ["aurora_mysql"]

    def test_a_scope_listing_the_source_compatible_engine_keeps_it(self) -> None:
        assert _waves(["aurora_mysql", "dynamodb"])[0]["engines"] == ["aurora_mysql"]


SCOPE_ROW = {
    "query_id": "q9",
    "source_tables": ["app.orders"],
    "rule": "fallback.no_score",
    "reason": (
        "No engine in the customer's engine scope (DynamoDB) can serve this query: "
        "DynamoDB (missing a capability the query needs)."
    ),
    "closest_engine": "aurora_mysql",
    "gates": [
        {
            "engine": "dynamodb",
            "gate": "capability",
            "query_id": None,
            "gate_text": "missing a capability the query needs",
        }
    ],
    "group_key": None,
    "required_capabilities": ["sql_admin"],
}


def _report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "job_id": JOB,
        "timestamp": "2026-10-22T00:00:00Z",
        "ranking": [
            {
                "target": "dynamodb",
                "confidence_score": 70,
                "workload_percent": 100.0,
                "assigned_queries": 1,
            }
        ],
        "schema_designs": {},
        "tied_queries": [],
        "query_traces": {"has_ledger": True, "query_count": 1, "queries": [], "unservable": []},
        "scope_unservable": rows,
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "mitigation_strategies": []},
    }
    report["facts"] = compute_report_facts(report, engine_scope=["dynamodb"])
    return report


class TestTheFactsAndTheRisk:
    def test_the_facts_count_them_as_their_own_kind_with_no_engine(self) -> None:
        facts = _report([SCOPE_ROW])["facts"]
        u = facts["unservable"]
        assert u["unservable_query_count"] == 1
        assert u["no_engine_in_scope_query_count"] == 1
        assert u["no_fit_query_count"] == 0
        (row,) = u["unservable_queries"]
        assert (row["kind"], row["engine"], row["closest_engine"]) == (
            "no_engine_in_scope",
            "",
            "aurora_mysql",
        )
        assert row["blocking_gates"] == [
            {
                "engine": "dynamodb",
                "gate": "capability",
                "gate_text": "missing a capability the query needs",
            }
        ]
        # Never in an engine's query count.
        assert facts["in_scope_query_count"] == 1

    def test_one_blocking_risk_at_cutover(self) -> None:
        risks = report_fact_risks(_report([SCOPE_ROW, {**SCOPE_ROW, "query_id": "q8"}]))[
            "routing.no_engine_in_scope"
        ]
        (risk,) = risks
        assert risk["severity"] == "HIGH"
        assert risk["wave"] is None
        assert risk["query_ids"] == ["q8", "q9"]
        assert risk["affected_tables"] == ["app.orders"]
        assert risk["description"].startswith("2 queries have no engine")
        assert "Aurora MySQL" in risk["description"]
        assert "start a new assessment whose engine scope adds Aurora MySQL" in risk["mitigation"]
        assert "keep them on the source database" in risk["mitigation"]

    def test_no_row_no_risk_and_an_old_report_is_unchanged(self) -> None:
        report = _report([])
        assert report_fact_risks(report)["routing.no_engine_in_scope"] == []
        assert report["facts"]["unservable"]["no_engine_in_scope_query_count"] == 0
        old = copy.deepcopy(report)
        del old["scope_unservable"]
        assert backfill_report_risks(old) is old


FALLBACK_ROW = {
    "query_id": "q-fb",
    "engine": "dynamodb",
    "kind": "fallback",
    "rule": "fallback.no_score",
    "explanation": "No engine could score this query, so it falls back to DynamoDB.",
    "blocking_gates": [],
    "required_capabilities": [],
}


def _mixed_report() -> dict[str, Any]:
    """A no-engine-in-scope query next to a query on a fallback engine."""
    report = _report([SCOPE_ROW])
    report["query_traces"]["unservable"] = [FALLBACK_ROW]
    report["facts"] = compute_report_facts(report, engine_scope=["dynamodb"])
    return report


# The edit the assignment review refuses for a query with no engine in the scope.
GATE_EDIT = "change a query's engine or take it out of scope"


class TestTheDeliverables:
    """The review's item 1: with only no-engine-in-scope queries, no deliverable tells
    the customer to edit them at the assignment review (which refuses that edit); each
    says to start a new assessment whose scope adds the engine, or keep them on the
    source database. With other kinds listed too, both ways are stated."""

    def test_the_engineering_report_lists_them_with_no_engine(self) -> None:
        from src.report.unservable import UNSERVABLE_INTRO, UNSERVABLE_SCOPE_INTRO

        md = render_engineering_report_md(_report([SCOPE_ROW]))
        row = next(line for line in md.splitlines() if line.startswith("| `q9`"))
        assert "| No engine (Aurora MySQL is outside the scope) |" in row
        assert "DynamoDB (missing a capability the query needs)" in row
        assert (
            "Start a new assessment whose scope adds Aurora MySQL, or keep it on the source "
            "database." in row
        )
        assert "Keep it on" not in row
        assert UNSERVABLE_SCOPE_INTRO in md
        assert UNSERVABLE_INTRO not in md
        assert GATE_EDIT not in md
        assert (
            "To serve them, start a new assessment whose scope adds Aurora MySQL, or keep them "
            "on the source database." in md
        )
        assert "`scope_unservable`" in md

    def test_the_executive_report_states_them(self) -> None:
        from src.report.unservable import UNSERVABLE_INTRO, UNSERVABLE_SCOPE_INTRO

        text = executive_text(_report([SCOPE_ROW]))
        assert "Queries that cannot be served as designed (1 query)" in text
        assert " ".join(UNSERVABLE_SCOPE_INTRO.split()) in text
        assert " ".join(UNSERVABLE_INTRO.split()) not in text
        assert GATE_EDIT not in text
        assert "start a new assessment whose scope adds Aurora MySQL" in text

    def test_next_to_another_kind_both_ways_are_stated(self) -> None:
        from src.report.unservable import (
            UNSERVABLE_INTRO,
            UNSERVABLE_RERUN_TEXT,
            UNSERVABLE_SCOPE_SOME,
        )

        report = _mixed_report()
        md = render_engineering_report_md(report)
        assert UNSERVABLE_INTRO in md and UNSERVABLE_SCOPE_SOME in md
        assert UNSERVABLE_RERUN_TEXT in md
        assert "To serve them, start a new assessment whose scope adds Aurora MySQL" in md
        text = executive_text(report)
        assert " ".join(UNSERVABLE_SCOPE_SOME.split()) in text
        assert " ".join(UNSERVABLE_RERUN_TEXT.split()) in text

    def test_the_ui_callout_says_the_same_sentences(self) -> None:
        import json
        from pathlib import Path

        from src.report.sources_of_truth import source_database_text
        from src.report.unservable import (
            UNSERVABLE_SCOPE_INTRO,
            UNSERVABLE_SCOPE_LEAD,
            UNSERVABLE_SCOPE_LEAD_NO_ENGINE,
            UNSERVABLE_SCOPE_RERUN,
            UNSERVABLE_SCOPE_RERUN_WIDER,
            UNSERVABLE_SCOPE_SOME,
        )

        en = json.loads((Path(__file__).parents[4] / "src/ui/src/locales/en.json").read_text())
        key = "analysis-results.unservable."
        assert en[key + "scope_intro"] == UNSERVABLE_SCOPE_INTRO
        assert en[key + "scope_some"] == UNSERVABLE_SCOPE_SOME
        assert en[key + "rerun_scope"] == UNSERVABLE_SCOPE_RERUN.replace("{engines}", "{{engines}}")
        assert en[key + "rerun_scope_wider"] == UNSERVABLE_SCOPE_RERUN_WIDER
        assert en[key + "redesign_scope"] == (
            UNSERVABLE_SCOPE_LEAD.replace("{engine}", "{{engine}}") + " {{hints}}"
        )
        assert en[key + "redesign_scope_wider"] == UNSERVABLE_SCOPE_LEAD_NO_ENGINE + " {{hints}}"
        sot = "analysis-results.sources-of-truth.source-database"
        assert en[sot + "_other"].replace("{{count}}", "24") == source_database_text(24)
        assert en[sot + "_one"].replace("{{count}}", "1") == source_database_text(1)
        stats = "analysis-results.executive-summary."
        assert (
            "{{count}} have no engine in the customer's engine scope"
            in en[stats + "no-engine-in-scope_other"]
        )

    def test_the_renderers_print_the_stored_facts(self) -> None:
        report = _report([SCOPE_ROW])
        doctored = copy.deepcopy(report)
        doctored["facts"]["unservable"]["unservable_queries"][0]["closest_engine"] = "documentdb"
        doctored["facts"]["unservable"]["no_engine_in_scope_closest_engines"] = ["documentdb"]
        text = engineering_text(doctored)
        assert "No engine (DocumentDB is outside the scope)" in text
        assert "start a new assessment whose scope adds DocumentDB" in text


class TestTheHeadlineTotals:
    """The review's item 3: the totals name the queries with no engine, computed once in
    the facts; both reports state the same figures, and the cost says it leaves them out."""

    def _report(self) -> dict[str, Any]:
        rows = [{**SCOPE_ROW, "query_id": f"u{i}"} for i in range(42)]
        report = _report(rows)
        report["ranking"][0]["assigned_queries"] = 65
        report["tco_analysis"] = {"projected_monthly_cost": 98.41}
        report["facts"] = compute_report_facts(report, engine_scope=["dynamodb"])
        return report

    def test_the_facts_hold_the_total_and_the_no_engine_count(self) -> None:
        facts = self._report()["facts"]
        assert facts["in_scope_query_count"] == 65
        assert facts["no_engine_in_scope_query_count"] == 42
        assert facts["query_total_count"] == 107
        assert facts["unservable"]["no_engine_in_scope_closest_engines"] == ["aurora_mysql"]

    def test_with_none_the_total_is_the_in_scope_count(self) -> None:
        facts = _report([])["facts"]
        assert facts["no_engine_in_scope_query_count"] == 0
        assert facts["query_total_count"] == facts["in_scope_query_count"]

    def test_the_engineering_report_and_the_pdf_name_them(self) -> None:
        report = self._report()
        md = render_engineering_report_md(report)
        # (This synthetic report's engine has no migration role, so 0 move.)
        assert (
            "Queries moved: 0 of 107 queries move to a new engine (a migration target or a "
            "search read model); 42 have no engine in the customer's engine scope;" in md
        )
        assert (
            "Workload shares are of the 65 queries placed on an engine; 42 have no engine in "
            "the customer's engine scope." in md
        )
        excludes = (
            "Excludes the 42 queries with no engine in the customer's engine scope, which keep "
            "running on the source database."
        )
        assert f"- {excludes}" in md
        pdf = executive_text(report)
        assert "of 107 queries move to a new engine" in pdf
        assert "42 have no engine in the customer's engine scope." in pdf
        assert "Workload shares are of the 65 queries placed on an engine" in pdf
        assert excludes in pdf

    def test_without_them_the_headline_is_unchanged(self) -> None:
        report = _report([])
        md = render_engineering_report_md(report)
        assert "have no engine in the customer's engine scope" not in md
        assert "Workload shares are of" not in md
        assert "Excludes the" not in md

    def test_a_facts_block_written_before_the_total_gains_it(self) -> None:
        from src.report.facts import report_facts

        report = _report([])
        del report["facts"]["query_total_count"]
        del report["facts"]["no_engine_in_scope_query_count"]
        facts = report_facts(report)
        assert facts["no_engine_in_scope_query_count"] == 0
        assert facts["query_total_count"] == facts["in_scope_query_count"]


class TestTheSourceDatabaseKeepsTheWrites:
    """The review's item 5 (ADR-031 Amendments 9 and 15): a table whose every write has no
    engine in the scope keeps the source database as its writer, and each in-scope engine
    that reads it holds a replica kept in sync from it."""

    def _assignment(self) -> dict[str, Any]:
        return {
            "query_assignments": [
                {"query_id": "r1", "assigned_engine": "dynamodb", "source_tables": ["app.orders"]},
                {"query_id": "r2", "assigned_engine": "dynamodb", "source_tables": ["app.other"]},
            ],
            "write_groups": {},
            "unservable_queries": [{"query_id": "w1"}, {"query_id": "w2"}],
        }

    def test_table_sources_records_the_source_database_and_the_replica(self) -> None:
        from src.agents.referee.write_groups import table_sources

        rows = table_sources(self._assignment(), {"app.orders": ["w2", "w1"]})
        assert rows == [
            {
                "table": "app.orders",
                "source_of_truth": "source_database",
                "write_engines": [],
                "write_query_ids": ["w1", "w2"],
                "replicas": [{"engine": "dynamodb", "read_query_ids": ["r1"]}],
            }
        ]
        assert table_sources(self._assignment()) == []

    @pytest.mark.usefixtures("table_confidence_scores")
    def test_the_unservable_write_groups_come_from_the_collection(self) -> None:
        from src.agents.referee.write_groups import unservable_write_groups

        collector = _collector("mysql", [READ, W1, W2])
        assert unservable_write_groups(collector, [{"query_id": "w1"}, {"query_id": "w2"}]) == {
            "app.orders": ["w1", "w2"]
        }
        # A group with one assigned write is not the source database's.
        assert unservable_write_groups(collector, [{"query_id": "w1"}]) == {}
        assert unservable_write_groups(collector, []) == {}

    def test_the_facts_and_the_engineering_report_state_it(self) -> None:
        from src.agents.referee.write_groups import table_sources
        from src.report.sources_of_truth import source_database_text

        report = _report([SCOPE_ROW])
        report["table_sources"] = table_sources(self._assignment(), {"app.orders": ["w1", "w2"]})
        report["facts"] = compute_report_facts(report, engine_scope=["dynamodb"])
        ts = report["facts"]["table_sources"]
        assert (ts["source_database_table_count"], ts["replicated_table_count"]) == (1, 1)
        (row,) = ts["replicated_tables"]
        assert (row["source_of_truth"], row["replica_engines"]) == ("source_database", ["dynamodb"])
        assert "kept in sync from the source database via CDC" in row["risk"]
        md = render_engineering_report_md(report)
        assert source_database_text(1) in md
        assert "| `app.orders` | Source database | Source database → DynamoDB | 1 |" in md


def test_a_facts_block_written_before_the_count_reads_it_as_0() -> None:
    from src.report.facts import report_facts

    report = _report([])
    del report["facts"]["unservable"]["no_engine_in_scope_query_count"]
    assert report_facts(report)["unservable"]["no_engine_in_scope_query_count"] == 0


@pytest.mark.usefixtures("table_confidence_scores")
class TestTheScriptsSayHowMany:
    """``run_assessment.py``'s and ``run_assignment.py``'s assignment line counts the
    unservable queries, which are never in the distribution or the total."""

    @staticmethod
    def _line(out: str, key: str = "distribution") -> dict[str, Any]:
        import json

        lines = [x for x in out.splitlines() if x.startswith("{") and key in x]
        line: dict[str, Any] = json.loads(lines[-1])
        return line

    @pytest.mark.parametrize(
        "scope, unservable",
        [(["dynamodb"], 2), pytest.param(None, None, marks=pytest.mark.passes_on_main)],
    )
    def test_run_assessment(self, tmp_path, monkeypatch, capsys, scope, unservable) -> None:
        from scripts import run_assessment

        store = _job(tmp_path, scope, [READ, W1, W2])
        monkeypatch.setattr(
            "src.agents.referee.assignment_handler.run_assignment_resolver", lambda *a, **k: None
        )
        run_assessment.phase_assignment(store, JOB, DB)
        line = self._line(capsys.readouterr().out)
        assert line.get("unservable_queries") == unservable
        assert line["total_queries"] == (1 if scope else 3)

    @pytest.mark.parametrize(
        "scope, unservable",
        [(["dynamodb"], 2), pytest.param(None, None, marks=pytest.mark.passes_on_main)],
    )
    def test_run_assignment(self, tmp_path, monkeypatch, capsys, scope, unservable) -> None:
        import sys

        from scripts import run_assignment

        _job(tmp_path, scope, [READ, W1, W2])
        monkeypatch.setattr(
            "src.agents.referee.assignment_handler.run_assignment_resolver", lambda *a, **k: None
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_assignment.py", "--job-id", JOB, "--db", DB, "--artifact-root", str(tmp_path)],
        )
        run_assignment.main()
        line = self._line(capsys.readouterr().out)
        assert line.get("unservable_queries") == unservable


def test_the_results_api_fills_the_count_in_for_an_older_block() -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.routes import results

    report = _report([])
    del report["facts"]["unservable"]["no_engine_in_scope_query_count"]
    sfn, s3 = MagicMock(), MagicMock()
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "started_at": "2026-10-22T00:00:00Z",
        "stopped_at": "2026-10-22T01:00:00Z",
        "input": {"database_name": "shop", "source_database_type": "mysql"},
    }
    s3.read_synthesis.return_value = report
    s3.read_triage.return_value = None
    results.sfn_service, results.s3_service = sfn, s3
    try:
        body = TestClient(app).get(f"/api/v1/assessments/{JOB}/results").json()
    finally:
        results.sfn_service, results.s3_service = None, None
    assert body["synthesis"]["facts"]["unservable"]["no_engine_in_scope_query_count"] == 0
