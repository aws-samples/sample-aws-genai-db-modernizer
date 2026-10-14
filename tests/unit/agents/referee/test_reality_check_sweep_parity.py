"""External Reality Check finalize must run the same sanity sweep as
``none``/``bedrock`` (#601).

Before the fix, ``finalize_reality_check`` (used by ``scripts/run_assessment.py
--resume-reality-check`` and ``scripts/run_reality_check.py --finalize``)
rebuilt the deterministic result and merged the LLM response, but never ran
``sanity_sweep`` -- the step every other ``llm_mode`` always runs as the
handler's step 3. So a tiny orphan engine the ``none``/``bedrock`` paths
remove could survive an external-mode run on the identical input and
corrections (ADR-031 Decision 11 says all three must agree).

These tests drive the real deterministic core through a real local store,
with the main consolidation pass (``run_reality_check``) stubbed to a
controlled result, so the only engine-level change left for the test to
observe comes from the sanity sweep itself -- isolating the regression from
unrelated consolidation/resettle behavior already covered elsewhere.
"""

from __future__ import annotations

import copy

import pytest

from src.agents.referee.reality_check_handler import (
    _run_sanity_sweep,
    apply_reality_check_llm_output,
    finalize_reality_check,
    run_reality_check_deterministic,
    run_reality_check_handler,
    write_reality_check_result,
)
from src.storage.assignment_versioning import assignment_artifact_path
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"


def _qa(query_id: str, engine: str) -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": [f"t_{query_id}"],
        "assignment_reason": "fixture",
    }


def _assignment(version: int = 1) -> dict:
    """6 DynamoDB queries (a committed destination, above the orphan
    threshold) plus 3 DocumentDB queries (an orphan: at/under
    ``ORPHAN_ENGINE_THRESHOLD``), each query on its own table so its score
    is independently controllable via ``_ANALYSIS_DYNAMODB``.
    """
    qas = [_qa(f"DDB-{i}", "dynamodb") for i in range(6)]
    qas += [_qa(f"DOC-{i}", "documentdb") for i in range(3)]
    return {
        "job_id": JOB,
        "version": version,
        "status": "auto_generated",
        "timestamp": "2026-10-01T00:00:00+00:00",
        "query_assignments": qas,
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
        "source": "assignment_resolution",
    }


def _query_patterns(assignment: dict) -> list[dict]:
    return [
        {"query_id": qa["query_id"], "tables_accessed": [f"t_{qa['query_id']}"]}
        for qa in assignment["query_assignments"]
    ]


# DocumentDB's own 3 queries score well on DynamoDB -- their own engine's
# score is never consulted by the sweep (ADR-031 Decision 7) -- so the sweep
# removes DocumentDB and moves every one of its queries onto DynamoDB.
_ANALYSIS_DYNAMODB = {
    "table_recommendations": [{"table_id": f"t_DOC-{i}", "confidence_score": 90} for i in range(3)]
}

_EXPECTED_SWEEP_DECISION = {
    "engine": "documentdb",
    "decision": "removed",
    "reason": "3 queries, each with a serviceable committed engine: 3 to DynamoDB",
    "blocking_query_ids": [],
    "blocking_rule": None,
}


def _identity_reality_check(assignment, triage, analysis, collector, caps, source_engine=None):
    """A no-op stand-in for the main consolidation pass: returns the input
    unchanged, so the only engine-level change a test can observe comes
    from the sanity sweep this issue is about."""
    return {
        "consolidations": [],
        "recommendations": [],
        "architectural_patterns": [],
        "unique_value_assessment": {},
        "revised_assignments": copy.deepcopy(assignment["query_assignments"]),
        "lightweight_recommendations": [],
    }


@pytest.fixture(autouse=True)
def _stub_consolidation(monkeypatch):
    monkeypatch.setattr(
        "src.agents.referee.reality_check_handler.run_reality_check",
        _identity_reality_check,
    )


def _seed(store: LocalArtifactStore, assignment: dict | None = None) -> None:
    assignment = assignment or _assignment(1)
    prefix = f"{DB}/{JOB}"
    store.write_json(
        f"{prefix}/referee-triage/triage.json",
        {
            "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "documentdb"}],
            "signals": [],
            "query_capabilities": {},
        },
    )
    store.write_json(
        f"{prefix}/collector/output.json",
        {
            "database_schema": {"tables": []},
            "queries": {"query_patterns": _query_patterns(assignment)},
        },
    )
    store.write_json(f"{prefix}/analysis-dynamodb/analysis.json", _ANALYSIS_DYNAMODB)
    store.write_json(f"{prefix}/analysis-documentdb/analysis.json", {"table_recommendations": []})
    store.write_json(assignment_artifact_path(DB, JOB, 1), assignment)


def _rc_output(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/reality-check/output.json")


def _engines_of(store: LocalArtifactStore, version: int) -> set[str]:
    qas = store.read_json(assignment_artifact_path(DB, JOB, version))["query_assignments"]
    return {qa["assigned_engine"] for qa in qas}


def _assignment_without_timestamp(store: LocalArtifactStore, version: int) -> dict:
    """The written assignment, minus ``timestamp`` (each write stamps
    ``datetime.now(UTC)``, so two independent writes never share one even
    when every other field is identical)."""
    assignment = store.read_json(assignment_artifact_path(DB, JOB, version))
    return {k: v for k, v in assignment.items() if k != "timestamp"}


class TestNoneModeSweepsTheOrphanEngine:
    """Baseline: ``none`` mode already ran the sweep, never had the bug."""

    def test_sweep_removes_documentdb_into_dynamodb(self, tmp_path) -> None:
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)

        summary = run_reality_check_handler(JOB, DB, store, llm_mode="none")

        assert _rc_output(store)["sweep_decisions"] == [_EXPECTED_SWEEP_DECISION]
        assert _engines_of(store, summary["output_version"]) == {"dynamodb"}


class TestExternalFinalizeRunsTheSameSweep:
    """#601: ``finalize_reality_check`` must run ``sanity_sweep``, the same
    step ``none``/``bedrock`` always ran, so external agrees with them."""

    def test_external_finalize_with_no_corrections_matches_none_mode(self, tmp_path) -> None:
        """The issue's own repro: ``--llm-mode external``, then
        ``--resume-reality-check`` with an empty correction list, must reach
        the same outcome as ``--llm-mode none`` on the identical input."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        _seed(store)

        preview = run_reality_check_handler(JOB, DB, store, llm_mode="external")
        assert preview["status"] == "awaiting_llm"

        summary = finalize_reality_check(store, JOB, DB, {}, assignment_version=1)

        assert _rc_output(store)["sweep_decisions"] == [_EXPECTED_SWEEP_DECISION]
        assert _engines_of(store, summary["output_version"]) == {"dynamodb"}

    def test_bedrock_style_apply_and_external_resume_give_identical_results(self, tmp_path) -> None:
        """Same input, same (mocked) model corrections: applying them
        directly plus the shared sweep (what ``bedrock`` mode does inside
        the handler) and finalizing externally must write the same
        assignment and the same ``sweep_decisions``."""
        corrections_payload: dict[str, list] = {"consolidation_corrections": []}

        store_a = LocalArtifactStore(base_dir=str(tmp_path / "a"))
        _seed(store_a)
        det = run_reality_check_deterministic(JOB, DB, store_a, assignment_version=1)
        result = apply_reality_check_llm_output(det, corrections_payload)
        _run_sanity_sweep(result)
        version_a = write_reality_check_result(store_a, JOB, DB, result, assignment_version=1)

        store_b = LocalArtifactStore(base_dir=str(tmp_path / "b"))
        _seed(store_b)
        summary_b = finalize_reality_check(
            store_b, JOB, DB, corrections_payload, assignment_version=1
        )

        out_a, out_b = _rc_output(store_a), _rc_output(store_b)
        assert out_a["sweep_decisions"] == out_b["sweep_decisions"] == [_EXPECTED_SWEEP_DECISION]
        assert out_a["after_distribution"] == out_b["after_distribution"]
        assert version_a == summary_b["output_version"]
        assert _assignment_without_timestamp(store_a, version_a) == _assignment_without_timestamp(
            store_b, summary_b["output_version"]
        )

    def test_bedrock_style_apply_and_external_resume_agree_with_a_real_correction(
        self, tmp_path
    ) -> None:
        """A real model flag on part of OpenSearch's consolidation must leave
        both paths agreeing: the flag re-routes OS-0 by score, the sweep then
        honors the flag's gap, and the only question is whether finalize's
        ordering (apply, then sweep) matches the handler's."""

        def _partial_opensearch_consolidation(
            assignment, triage, analysis, collector, caps, source_engine=None
        ):
            revised = []
            for qa in assignment["query_assignments"]:
                if qa["query_id"] == "OS-0":
                    qa = {**qa, "assigned_engine": "dynamodb"}
                revised.append(qa)
            return {
                "consolidations": [
                    {
                        "from_engine": "opensearch",
                        "to_engine": "dynamodb",
                        "query_count": 1,
                        "reason": "fixture partial consolidation",
                        "saved_cost_estimate": 50,
                        "action": "partial",
                        "queries_retained": ["OS-1"],
                        "retention_reason": "fixture",
                    }
                ],
                "recommendations": [],
                "architectural_patterns": [],
                "unique_value_assessment": {},
                "revised_assignments": revised,
                "lightweight_recommendations": [],
            }

        assignment = _assignment(1)
        assignment["query_assignments"] += [
            _qa("OS-0", "opensearch"),
            _qa("OS-1", "opensearch"),
        ]
        correction = {
            "consolidation_corrections": [
                {
                    "query_ids": ["OS-0"],
                    "flagged_engine": "dynamodb",
                    "reason_category": "missing_feature",
                    "explanation": "fixture: needs geo-spatial, not key-value",
                }
            ]
        }

        def _seed_opensearch(store: LocalArtifactStore) -> None:
            _seed(store, assignment)
            store.write_json(
                f"{DB}/{JOB}/analysis-opensearch/analysis.json", {"table_recommendations": []}
            )
            # OS-0/OS-1's tables also score well on DynamoDB, same as DocumentDB's,
            # so the sweep (after the correction reverts OS-0, leaving OpenSearch
            # with 2 queries, also an orphan) moves them there too.
            store.write_json(
                f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
                {
                    "table_recommendations": _ANALYSIS_DYNAMODB["table_recommendations"]
                    + [
                        {"table_id": "t_OS-0", "confidence_score": 90},
                        {"table_id": "t_OS-1", "confidence_score": 90},
                    ]
                },
            )
            store.write_json(
                f"{DB}/{JOB}/referee-triage/triage.json",
                {
                    "selected_agents": [
                        {"agent_type": "dynamodb"},
                        {"agent_type": "documentdb"},
                        {"agent_type": "opensearch"},
                    ],
                    "signals": [],
                    "query_capabilities": {},
                },
            )

        store_a = LocalArtifactStore(base_dir=str(tmp_path / "a"))
        _seed_opensearch(store_a)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "src.agents.referee.reality_check_handler.run_reality_check",
                _partial_opensearch_consolidation,
            )
            det = run_reality_check_deterministic(JOB, DB, store_a, assignment_version=1)
            result = apply_reality_check_llm_output(det, correction)
        _run_sanity_sweep(result)
        version_a = write_reality_check_result(store_a, JOB, DB, result, assignment_version=1)

        store_b = LocalArtifactStore(base_dir=str(tmp_path / "b"))
        _seed_opensearch(store_b)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "src.agents.referee.reality_check_handler.run_reality_check",
                _partial_opensearch_consolidation,
            )
            summary_b = finalize_reality_check(store_b, JOB, DB, correction, assignment_version=1)

        out_a, out_b = _rc_output(store_a), _rc_output(store_b)
        assert out_a["sweep_decisions"] == out_b["sweep_decisions"]
        assert out_a["after_distribution"] == out_b["after_distribution"]
        assert version_a == summary_b["output_version"]
        assert _assignment_without_timestamp(store_a, version_a) == _assignment_without_timestamp(
            store_b, summary_b["output_version"]
        )
        # OS-0's flag gates DynamoDB for it (#647); the scorer sends it to its
        # best remaining engine, DocumentDB. The sweep then moves OpenSearch's
        # last query (OS-1) to DynamoDB, but keeps DocumentDB: OS-0 cannot go
        # back to DynamoDB, the engine its flag named.
        engines = _assignment_without_timestamp(store_a, version_a)["query_assignments"]
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in engines}
        assert engine_of["OS-0"] == "documentdb"
        assert engine_of["OS-1"] == "dynamodb"
        assert _engines_of(store_a, version_a) == {"documentdb", "dynamodb"}
        (decision,) = out_a["flag_decisions"]
        assert (decision["outcome"], decision["destination_engine"]) == ("moved", "documentdb")
