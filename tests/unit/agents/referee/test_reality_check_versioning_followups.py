"""Reality-check versioning follow-ups (#193, after #192's review).

1. A forced run keeps the record of the run before it: every completed output is
   also kept under ``reality-check/v<N>/output.json``, and readers take the record
   of the run the effective assignment descends from.
2. The "ran but changed nothing" lineage check needs an output with no consolidations.
3. An awaiting marker written before #192 next to that run's output counts as a
   completed run; a preview written since then still does not.
4. A consolidation record names the customer-pinned queries its engine keeps.
5. A corrupt lineage (a cycle, a missing or absent ``previous_version``) logs a warning.

The harness (store, stubbed consolidation, assignment helpers) is
``test_reality_check_versioning``'s.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.agents.referee.reality_check_handler import run_reality_check_handler
from src.contracts.move_ledger import MovePass, ReasonCode
from src.storage.assignment_versioning import reality_check_run_for_lineage
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee import test_reality_check_versioning as harness
from tests.unit.agents.referee.test_reality_check_versioning import (
    DB,
    JOB,
    _assignment,
    _customer_edit,
    _path,
    _qa,
    _rc_output,
    _run,
)

# The harness's fixtures: its store and its stubbed consolidation (autouse).
store = harness.store
_stub_consolidation = harness._stub_consolidation

RC = f"{DB}/{JOB}/reality-check"
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _versioned(version: int) -> str:
    return f"{RC}/v{version}/output.json"


def _read_record(store: LocalArtifactStore, version: int | None = None) -> dict | None:
    from src.storage.assignment_versioning import read_reality_check_output

    return read_reality_check_output(store, DB, JOB, version)


# =============================================================================
# 1. A forced run never loses the record of the original consolidation


class TestForcedRunKeepsTheRecord:
    def test_a_forced_run_on_a_customer_edit_keeps_the_consolidation(self, store) -> None:
        _run(store)  # v1 -> v2: DOC-AP-1 consolidated
        original = _rc_output(store)
        _customer_edit(store, 3)  # DOC-AP-1 back on documentdb, pinned

        forced = _run(store, assignment_version=3)

        assert forced == {"status": "complete", "input_version": 3, "output_version": None}
        assert _rc_output(store)["consolidations"] == []  # the latest run, as before
        kept = store.read_json(_versioned(2))
        assert kept == original
        assert len(kept["consolidations"]) == 1
        assert store.read_json(_versioned(3))["source_assignment_version"] == 3

    def test_readers_take_the_run_the_effective_assignment_descends_from(self, store) -> None:
        _run(store)
        original = _rc_output(store)
        _customer_edit(store, 3)
        _run(store, assignment_version=3)

        assert _read_record(store) == original  # v3 descends from the v2 consolidation
        assert _read_record(store, 2) == original

    def test_a_job_written_before_versioned_records_keeps_its_record(self, store) -> None:
        """An old job has only the flat output.json; a forced run copies it first."""
        _run(store)
        original = _rc_output(store)
        (store.base_dir / _versioned(2)).unlink()  # as an old job wrote it
        assert _read_record(store) == original  # the flat file still reads

        _customer_edit(store, 3)
        _run(store, assignment_version=3)

        assert store.read_json(_versioned(2)) == original
        assert _read_record(store) == original

    def test_an_external_run_keeps_the_record_before_its_preview(self, store) -> None:
        _run(store)
        original = _rc_output(store)
        (store.base_dir / _versioned(2)).unlink()
        _customer_edit(store, 3)

        run_reality_check_handler(JOB, DB, store, assignment_version=3, llm_mode="external")

        assert store.read_json(_versioned(2)) == original
        assert _rc_output(store)["source_assignment_version"] == 3  # the preview

    def test_a_new_lineage_reads_its_own_record(self, store) -> None:
        _run(store)
        store.write_json(_path(3), _assignment(3, source="assignment_resolution"))
        _run(store)  # v3 -> v4

        assert (_read_record(store) or {}).get("output_assignment_version") == 4
        assert (_read_record(store, 2) or {}).get("output_assignment_version") == 2

    def test_a_run_that_changed_nothing_is_read_for_its_version(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        _run(store)

        record = _read_record(store)
        assert record is not None
        assert record["source_assignment_version"] == 1
        assert record["output_assignment_version"] is None
        assert store.exists(_versioned(1))

    @pytest.mark.passes_on_main
    def test_a_preview_is_not_kept_as_a_versioned_record(self, store) -> None:
        run_reality_check_handler(JOB, DB, store, llm_mode="external")

        assert not store.exists(_versioned(1))
        assert not store.exists(_versioned(2))

    def test_no_record_reads_as_none(self, store) -> None:
        from src.storage.assignment_versioning import read_reality_check_output

        assert read_reality_check_output(store, DB, JOB) is None

    def test_the_ui_reads_the_same_record(self, store) -> None:
        from src.api.services.local_s3 import LocalS3Service

        _run(store)
        original = _rc_output(store)
        _customer_edit(store, 3)
        _run(store, assignment_version=3)

        assert LocalS3Service(store).read_reality_check(DB, JOB) == original


# =============================================================================
# 2. "Ran but changed nothing" needs an output with no consolidations


class TestChangedNothingGuard:
    def _write_output(self, store, consolidations: list[dict]) -> None:
        store.write_json(
            f"{RC}/output.json",
            {
                "source_assignment_version": 1,
                "output_assignment_version": None,
                "consolidations": consolidations,
                "architectural_patterns": [],
                "recommendations": [],
                "before_distribution": {"dynamodb": 1, "documentdb": 1},
                "after_distribution": {"dynamodb": 2},
            },
        )

    def test_an_output_with_consolidations_and_no_version_is_not_a_noop_run(self, store) -> None:
        self._write_output(
            store,
            [
                {
                    "from_engine": "documentdb",
                    "to_engine": "dynamodb",
                    "query_count": 1,
                    "reason": "r",
                    "saved_cost_estimate": 0,
                }
            ],
        )

        assert reality_check_run_for_lineage(store, DB, JOB) is None

    @pytest.mark.passes_on_main
    def test_an_output_without_consolidations_is_a_noop_run(self, store) -> None:
        self._write_output(store, [])

        run = reality_check_run_for_lineage(store, DB, JOB)

        assert run is not None
        assert (run.input_version, run.output_version) == (1, None)


# =============================================================================
# 3. A marker left at awaiting_llm by a job finalized before #192


def _pre_192_output(consolidations: list[dict] | None = None) -> dict:
    """``reality-check/output.json`` as the writer before #192 wrote it: no
    ``output_assignment_version`` key, contract 1.x."""
    return {
        "contract_version": "1.2",
        "source_assignment_version": 1,
        "unique_value_assessment": {},
        "consolidations": consolidations or [],
        "architectural_patterns": [],
        "executive_summary": None,
        "recommendations": ["No consolidation opportunities found."],
        "before_distribution": {"dynamodb": 2},
        "after_distribution": {"dynamodb": 2},
        "lightweight_recommendations": [],
    }


def _stale_marker(store: LocalArtifactStore) -> None:
    marker = store.base_dir / RC / "awaiting_llm.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text((FIXTURES / "reality_check_awaiting_marker_pre_1267.json").read_text())


class TestStaleAwaitingMarker:
    def test_a_pre_192_output_next_to_an_awaiting_marker_is_a_completed_run(
        self, store, caplog
    ) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        store.write_json(f"{RC}/output.json", _pre_192_output())
        _stale_marker(store)

        with caplog.at_level(logging.INFO, logger="src.storage.assignment_versioning"):
            run = reality_check_run_for_lineage(store, DB, JOB)

        assert run is not None
        assert (run.input_version, run.output_version) == (1, None)
        assert "written before #192" in caplog.text

    def test_the_handler_skips_such_a_job_instead_of_recomputing(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        store.write_json(f"{RC}/output.json", _pre_192_output())
        _stale_marker(store)

        assert _run(store)["status"] == "skipped"

    @pytest.mark.passes_on_main
    def test_a_preview_next_to_an_awaiting_marker_is_still_awaiting(self, store) -> None:
        """A preview since #192 always has ``output_assignment_version`` (null)."""
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        run_reality_check_handler(JOB, DB, store, llm_mode="external")
        assert "output_assignment_version" in json.loads(
            (store.base_dir / RC / "output.json").read_text()
        )

        assert reality_check_run_for_lineage(store, DB, JOB) is None

    @pytest.mark.passes_on_main
    def test_a_bad_marker_next_to_a_pre_192_output_still_counts_as_awaiting(self, store) -> None:
        """#1349's rule: a marker that cannot be read leaves the state unknown."""
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        store.write_json(f"{RC}/output.json", _pre_192_output())
        marker = store.base_dir / RC / "awaiting_llm.json"
        marker.write_text("{not json")

        assert reality_check_run_for_lineage(store, DB, JOB, marker_fails_open=True) is None


# =============================================================================
# 4. A record never overstates what moved


class TestRecordsNameCustomerRetainedQueries:
    def _pinned_run(self, store) -> dict:
        _run(store)
        doc = _assignment(3, source="customer_gate", previous_version=2)
        doc["query_assignments"][1]["customer_override"] = True
        doc["query_assignments"].append(_qa("DOC-AP-2", "documentdb"))
        store.write_json(_path(3), doc)
        _run(store, assignment_version=3)
        return _rc_output(store)

    def test_the_record_states_the_queries_the_customer_kept(self, store) -> None:
        out = self._pinned_run(store)

        (record,) = out["consolidations"]
        assert (record["from_engine"], record["to_engine"]) == ("documentdb", "dynamodb")
        assert record["query_count"] == 1  # DOC-AP-2 only
        assert record["action"] == "partial"  # DocumentDB stays for the pin
        assert record["queries_retained"] == ["DOC-AP-1"]
        assert record["retention_reason"] == "The customer chose DocumentDB for DOC-AP-1"
        assert "1 of them the customer's choice" in record["reason"]

    def test_the_recommendation_line_says_the_same(self, store) -> None:
        """The trade-off line every deliverable shows is built from the record."""
        out = self._pinned_run(store)

        (line,) = [r for r in out["recommendations"] if r.startswith("Consolidated")]
        assert out["consolidations"][0]["reason"] in line
        assert "the customer's choice" in line
        assert "No operational savings are claimed" in line

    @pytest.mark.passes_on_main
    def test_without_a_pin_the_record_is_a_full_move(self, store) -> None:
        _run(store)

        (record,) = _rc_output(store)["consolidations"]
        assert record["action"] == "full"
        assert record["queries_retained"] == []
        assert "customer" not in record["reason"]


# =============================================================================
# 5. A corrupt lineage logs a warning and runs (a run never overwrites a version)


class TestCorruptLineage:
    def _edit(self, store, version: int, previous: object) -> None:
        doc = _assignment(version, source="customer_gate", previous_version=previous)
        store.write_json(_path(version), doc)

    def _warned(self, store, caplog) -> str:
        with caplog.at_level(logging.WARNING, logger="src.storage.assignment_versioning"):
            assert reality_check_run_for_lineage(store, DB, JOB) is None
        return str(caplog.text)

    def test_a_cycle_logs_a_warning(self, store, caplog) -> None:
        self._edit(store, 2, 3)
        self._edit(store, 3, 2)

        assert "cycle" in self._warned(store, caplog)

    def test_a_previous_version_that_does_not_exist_logs_a_warning(self, store, caplog) -> None:
        self._edit(store, 2, 7)

        assert "v7, which does not exist" in self._warned(store, caplog)

    def test_an_edit_without_a_previous_version_logs_a_warning(self, store, caplog) -> None:
        self._edit(store, 2, None)

        assert "no previous_version" in self._warned(store, caplog)

    @pytest.mark.passes_on_main
    def test_a_sound_lineage_logs_nothing(self, store, caplog) -> None:
        _run(store)
        _customer_edit(store, 3)

        with caplog.at_level(logging.WARNING, logger="src.storage.assignment_versioning"):
            assert reality_check_run_for_lineage(store, DB, JOB) is not None
        assert caplog.text == ""

    @pytest.mark.passes_on_main
    def test_the_run_on_a_corrupt_lineage_writes_a_new_version(self, store) -> None:
        self._edit(store, 2, 3)
        self._edit(store, 3, 2)
        before = {v: store.read_json(_path(v)) for v in (1, 2, 3)}

        summary = _run(store)

        assert summary["status"] == "complete"
        assert summary["output_version"] == 4
        assert {v: store.read_json(_path(v)) for v in (1, 2, 3)} == before


class TestFitRecordsWithPins:
    """``consolidations_from_moves`` directly: a pin joins the pass's own retained queries."""

    def _records(self, prior: dict, pin: bool = True, in_scope: bool = True) -> list[dict]:
        from src.agents.referee.move_ledger import apply_to_assignment, ledger_for
        from src.agents.referee.reality_check_records import consolidations_from_moves

        pinned: dict = {"query_id": "a1", "assigned_engine": "documentdb", "customer_override": pin}
        if not in_scope:
            pinned["in_scope"] = False
        before: list[dict] = [
            pinned,
            {"query_id": "a2", "assigned_engine": "documentdb"},
            {"query_id": "a3", "assigned_engine": "documentdb"},
            {"query_id": "b1", "assigned_engine": "dynamodb"},
        ]
        revised = [dict(qa) for qa in before]
        ledger = ledger_for(revised)
        apply_to_assignment(
            ledger,
            revised[1],
            "dynamodb",
            pass_=MovePass.SETTLE,
            reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
        )
        return consolidations_from_moves(before, ledger.moves, [prior])

    PRIOR = {
        "from_engine": "documentdb",
        "to_engine": "dynamodb",
        "query_count": 1,
        "reason": "r",
        "saved_cost_estimate": 0,
        "action": "partial",
        "queries_retained": ["a3"],
        "retention_reason": "a3 scores best on DocumentDB",
    }

    def test_the_pin_follows_the_pass_retained_queries(self) -> None:
        (record,) = self._records(self.PRIOR)

        assert record["queries_retained"] == ["a3", "a1"]
        assert record["retention_reason"] == (
            "a3 scores best on DocumentDB; the customer chose DocumentDB for a1"
        )
        assert record["query_count"] == 1
        assert record["action"] == "partial"

    @pytest.mark.passes_on_main
    def test_without_a_pin_only_the_pass_retained_queries_are_listed(self) -> None:
        (record,) = self._records(self.PRIOR, pin=False)

        assert record["queries_retained"] == ["a3"]
        assert record["retention_reason"] == "a3 scores best on DocumentDB"

    @pytest.mark.passes_on_main
    def test_an_out_of_scope_pin_is_not_listed(self) -> None:
        (record,) = self._records(self.PRIOR, in_scope=False)

        assert record["queries_retained"] == ["a3"]


class TestSynthesisReadsTheSameRecord:
    def test_the_report_shows_the_consolidation_the_assignment_carries(self, store) -> None:
        from src.agents.referee.synthesis_handler import (
            _write_synthesis_report,
            run_synthesis_deterministic,
        )

        _run(store)
        _customer_edit(store, 3)
        _run(store, assignment_version=3)  # a forced run that changed nothing

        result = run_synthesis_deterministic(JOB, DB, store, assignment_version=3)
        _write_synthesis_report(store, result, assignment_version=3)
        report = store.read_json(f"{DB}/{JOB}/synthesis/v3/report.json")

        moves = report["reality_check"]["consolidations"]
        assert [(c["from_engine"], c["to_engine"]) for c in moves] == [("documentdb", "dynamodb")]


class TestGraphReadsTheSameRecord:
    def test_the_context_graph_gets_the_consolidation_the_assignment_carries(self, store) -> None:
        from unittest.mock import MagicMock, patch

        from src.graph.populators import rebuild_graph

        _run(store)
        original = _rc_output(store)
        _customer_edit(store, 3)
        _run(store, assignment_version=3)

        with patch("src.graph.populators.populate_from_reality_check") as populate:
            rebuild_graph(DB, JOB, store, MagicMock())

        populate.assert_called_once()
        assert populate.call_args.args[0] == original


# =============================================================================
# #215: synthesis uses a reality check only from the assignment's own lineage


def _synthesize(store: LocalArtifactStore, version: int) -> tuple[dict, dict]:
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        run_synthesis_deterministic,
    )

    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=version)
    _write_synthesis_report(store, result, assignment_version=version)
    return result, store.read_json(f"{DB}/{JOB}/synthesis/v{version}/report.json")


def _pinned(query_id: str, engine: str) -> dict:
    return {**_qa(query_id, engine), "customer_override": True}


class TestRealityCheckBelongsToTheLineage:
    def _re_resolution_without_documentdb(self, store) -> None:
        _run(store)  # v1 -> v2: DocumentDB consolidated into DynamoDB
        store.write_json(
            _path(3), _assignment(3, doc_engine="dynamodb", source="assignment_resolution")
        )

    def test_a_new_lineage_does_not_read_the_old_record(self, store) -> None:
        self._re_resolution_without_documentdb(store)

        assert _read_record(store) is None
        result, report = _synthesize(store, 3)

        assert result["eliminated_engines"] == {}
        assert report["considered_not_recommended"] == []
        assert not (report.get("reality_check") or {}).get("consolidations")
        assert "reality check" not in json.dumps(report.get("trade_offs") or []).lower()

    def test_the_ui_relies_on_the_assignment_alone_there_too(self, store) -> None:
        from src.api.services.local_s3 import LocalS3Service

        self._re_resolution_without_documentdb(store)

        ui = LocalS3Service(store).read_reality_check(DB, JOB)
        assert ui is not None
        assert not ui.get("consolidations")

    def test_an_old_output_without_the_versions_is_read_as_before(self, store) -> None:
        """An output written before #192 cannot be placed in a lineage: today's rule."""
        self._re_resolution_without_documentdb(store)
        legacy = {
            **_pre_192_output(
                [
                    {
                        "from_engine": "documentdb",
                        "to_engine": "dynamodb",
                        "query_count": 1,
                        "reason": "r",
                        "saved_cost_estimate": 0,
                    }
                ]
            ),
            "before_distribution": {"dynamodb": 1, "documentdb": 1},
        }
        store.write_json(f"{RC}/output.json", legacy)
        for version in (1, 2):
            (store.base_dir / _versioned(version)).unlink(missing_ok=True)

        record = _read_record(store)
        assert record is not None
        assert "output_assignment_version" not in record
        result, _ = _synthesize(store, 3)
        assert result["eliminated_engines"] == {"documentdb": "dynamodb"}

    @pytest.mark.passes_on_main
    def test_the_lineage_the_run_belongs_to_still_reads_it(self, store) -> None:
        _run(store)

        result, report = _synthesize(store, 2)

        assert result["eliminated_engines"] == {"documentdb": "dynamodb"}
        assert len(report["reality_check"]["consolidations"]) == 1


class TestAnEditThatEmptiesAnEngine:
    """The #215 repro: Reality Check v1 -> v2, then a customer edit v3 moves every
    remaining query off DocumentDB (onto Aurora PostgreSQL), then synthesis."""

    def _job(self, store) -> None:
        v1 = _assignment(1, source="assignment_resolution")
        v1["query_assignments"] += [_pinned("DOC-AP-2", "documentdb")]
        store.write_json(_path(1), v1)
        _run(store)  # DOC-AP-1 -> DynamoDB; DocumentDB stays for the pinned DOC-AP-2
        v3 = _assignment(3, source="customer_gate", previous_version=2)
        v3["query_assignments"][1]["assigned_engine"] = "dynamodb"
        v3["query_assignments"] += [_pinned("DOC-AP-2", "aurora_postgresql")]
        store.write_json(_path(3), v3)

    def test_the_engine_is_absorbed_where_the_edit_put_its_queries(self, store) -> None:
        self._job(store)

        result, report = _synthesize(store, 3)

        assert result["eliminated_engines"] == {"documentdb": "aurora_postgresql"}
        assert report["considered_not_recommended"] == [
            {"engine": "documentdb", "absorbed_by": "aurora_postgresql"}
        ]

    def test_no_deliverable_says_the_reality_check_removed_it(self, store) -> None:
        self._job(store)

        _, report = _synthesize(store, 3)

        text = json.dumps(report)
        assert "eliminated by the reality check" not in text
        assert "consolidated into DynamoDB" not in text

    def test_a_risk_naming_the_engine_is_replanned_on_the_absorber(self, store) -> None:
        from src.agents.referee.synthesis_grounding import ground_risks

        self._job(store)
        result, _ = _synthesize(store, 3)
        risk = {
            "engine": "aurora_postgresql",
            "description": "Nested documents in DOC-AP-2.",
            "mitigation": "Recommendation: keep these on DocumentDB.",
            "query_ids": ["DOC-AP-2"],
            "severity": "MEDIUM",
        }

        (grounded,) = ground_risks(
            [risk],
            result["eliminated_engines"],
            {"DOC-AP-2": "aurora_postgresql", "DDB-AP-1": "dynamodb", "DOC-AP-1": "dynamodb"},
        )

        assert "DynamoDB" not in grounded["mitigation"]
        assert "Aurora PostgreSQL" in grounded["mitigation"]


class TestTheDeliverablesAgree:
    """The facts layer and the engineering report render the one report.json."""

    def test_a_new_lineage_lists_no_engine_the_old_run_removed(self, store) -> None:
        from src.report.engineering_report import render_engineering_report_md

        TestRealityCheckBelongsToTheLineage()._re_resolution_without_documentdb(store)
        _, report = _synthesize(store, 3)

        assert report["facts"]["removed_engines"] == []
        assert report["facts"]["retired_in_use_engines"] == []
        assert "Consolidated 1 query from DocumentDB" not in render_engineering_report_md(report)

    def test_an_edit_that_empties_an_engine_is_not_a_reality_check_removal(self, store) -> None:
        from src.report.engineering_report import render_engineering_report_md

        TestAnEditThatEmptiesAnEngine()._job(store)
        _, report = _synthesize(store, 3)

        assert report["facts"]["removed_engines"] == []
        assert "consolidated into DynamoDB" not in render_engineering_report_md(report)


class TestALegacyConsolidation:
    def test_a_pre_192_consolidation_is_kept_under_the_version_it_wrote(self, store) -> None:
        """The writer before #192 put a consolidation at source + 1 and recorded no
        output version; a forced run keeps that record as v2's, which a reader finds."""
        legacy_rc = _assignment(2, doc_engine="dynamodb", reality_check_applied=True)
        legacy_rc["previous_version"] = None  # the old writer's model dump
        store.write_json(_path(2), legacy_rc)
        record = _pre_192_output(
            [
                {
                    "from_engine": "documentdb",
                    "to_engine": "dynamodb",
                    "query_count": 1,
                    "reason": "r",
                    "saved_cost_estimate": 0,
                }
            ]
        )
        store.write_json(f"{RC}/output.json", record)
        _customer_edit(store, 3)

        _run(store, assignment_version=3)

        assert store.exists(_versioned(2))
        assert not store.exists(_versioned(1))
        kept = _read_record(store)
        assert kept is not None
        assert len(kept["consolidations"]) == 1


class TestEliminatedEnginesWithLaterMoves:
    RC = {
        "consolidations": [
            {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 3},
            {"from_engine": "opensearch", "to_engine": "dynamodb", "query_count": 1},
        ],
        "before_distribution": {"dynamodb": 2, "documentdb": 4, "opensearch": 1},
        "eliminated_engines": ["opensearch"],
    }
    EFFECTIVE = {"dynamodb", "aurora_postgresql", "aurora_mysql"}

    def _eliminated(self, later: dict) -> dict:
        from src.agents.referee.synthesis_grounding import eliminated_engines

        return eliminated_engines(self.EFFECTIVE, self.RC, later)

    def test_an_engine_an_edit_emptied_goes_where_the_edit_put_most(self) -> None:
        later = {"documentdb": {"aurora_postgresql": 1, "aurora_mysql": 2}}
        assert self._eliminated(later)["documentdb"] == "aurora_mysql"

    def test_a_tie_names_no_absorber(self) -> None:
        later = {"documentdb": {"aurora_postgresql": 1, "aurora_mysql": 1}}
        assert self._eliminated(later)["documentdb"] is None

    def test_an_engine_the_check_removed_keeps_its_consolidation_target(self) -> None:
        later = {"opensearch": {"aurora_postgresql": 5}}
        assert self._eliminated(later)["opensearch"] == "dynamodb"

    @pytest.mark.passes_on_main
    def test_without_later_moves_the_consolidation_target_stands(self) -> None:
        from src.agents.referee.synthesis_grounding import eliminated_engines

        assert eliminated_engines(self.EFFECTIVE, self.RC)["documentdb"] == "dynamodb"

    def test_a_destination_outside_the_plan_is_ignored(self) -> None:
        later = {"documentdb": {"elasticache": 4}}
        assert self._eliminated(later)["documentdb"] == "dynamodb"


# =============================================================================
# Review round 1: every reader, the job status, and no same-version overwrite


def _forced_noop_on_an_edit(store: LocalArtifactStore) -> dict:
    """Check v1 -> v2, customer edit v3, forced check on v3 that changes nothing."""
    _run(store)
    original = _rc_output(store)
    _customer_edit(store, 3)
    _run(store, assignment_version=3)
    return original


class TestEveryReaderTakesTheLineageRecord:
    def test_the_gate_status_line_shows_the_consolidation_the_assignment_carries(
        self, store
    ) -> None:
        from scripts.run_assessment import _reality_check_artifact

        _forced_noop_on_an_edit(store)

        line = _reality_check_artifact(store, JOB, DB)

        assert line["assignment_version"] == 3
        assert line["artifact"].endswith("reality-check/v2/output.json")
        assert [(c["from"], c["to"]) for c in line["consolidations"]] == [
            ("documentdb", "dynamodb")
        ]

    def test_the_aws_transform_core_reports_the_same_record(self, store, monkeypatch) -> None:
        from src.atx_orchestrator.core import run_reality_check_core

        monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "none")
        original = _forced_noop_on_an_edit(store)

        summary = run_reality_check_core(JOB, DB, store=store)  # skipped: already ran

        assert summary["consolidations"] == 1
        assert summary["before_distribution"] == original["before_distribution"]
        assert summary["reality_check_artifact"].endswith("reality-check/v2/output.json")


class TestJobStatusWithAStaleMarker:
    """Only the reality-check files and a collector, so no other gate reads as a wait."""

    def _status(self, store: LocalArtifactStore) -> str:
        from src.api.services.local_execution import LocalExecutionService

        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {"metadata": {"source_database": {"engine": "mysql", "database_name": DB}}},
        )
        execution = LocalExecutionService(store).describe_execution(JOB)
        assert execution is not None
        return str(execution["status"])

    def test_a_pre_192_job_with_an_awaiting_marker_is_not_waiting(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path / "bare"))
        store.write_json(f"{RC}/output.json", _pre_192_output())
        _stale_marker(store)

        assert self._status(store) != "WAITING"

    @pytest.mark.passes_on_main
    def test_a_preview_with_an_awaiting_marker_is_waiting(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path / "bare"))
        store.write_json(
            f"{RC}/output.json", {**_pre_192_output(), "output_assignment_version": None}
        )
        _stale_marker(store)

        assert self._status(store) == "WAITING"


class TestNoSameVersionOverwrite:
    def test_a_second_unchanged_check_keeps_the_first_record(self, store) -> None:
        store.write_json(_path(1), _assignment(1, doc_engine="dynamodb"))
        _run(store)
        # Mark the first record, so a rewrite with the same content would still show.
        path = store.base_dir / _versioned(1)
        first = {**json.loads(path.read_text()), "executive_summary": "the first run"}
        path.write_text(json.dumps(first))

        _run(store, assignment_version=1)  # forced: the same version, nothing to change

        assert store.read_json(_versioned(1)) == first
