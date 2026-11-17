# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``facts.routines.interventions`` (#1940): the routines that needed an intervention
and what changed, from the routine repair seam's result, so the repair loop (#1941) and
the deliverables can say it."""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import RoutineFacts
from src.shared.routine_facts import read_routine_facts, routine_facts
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.routine_repair import repair_fixtures as fx

NOTE = "Calls pg_current_wal_lsn, the name PostgreSQL 10 gave the function."


def _repair(version: int = 1, **row: Any) -> dict[str, Any]:
    rows = [
        {
            "kind": "function",
            "routine_id": fx.WAL,
            "state": "repaired_pending_proof",
            "reasons": ["postgresql-routine-v10-renamed-functions"],
            "repaired_create": fx.WAL_REPAIRED,
            "change_note": NOTE,
            **row,
        },
        {
            "kind": "function",
            "routine_id": fx.HIDDEN,
            "state": "refused",
            "reasons": ["postgresql-routine-v10-renamed-functions"],
            "refusal": "renamed",
        },
    ]
    counts = {"repaired_pending_proof": 0, "refused": 0, "no_answer": 0, "not_repaired": 0}
    for r in rows:
        counts[r["state"]] += 1
    return {
        "job_id": fx.JOB,
        "database_name": fx.DB,
        "assignment_version": version,
        "llm_mode": "external",
        "source_engine": "postgresql",
        "target_engine": "aurora_postgresql",
        "counts": counts,
        "routines": rows,
    }


def test_a_repaired_routine_is_an_intervention_with_what_changed() -> None:
    facts = routine_facts(fx.migration(), 1, None, _repair())
    assert facts is not None
    assert facts["interventions"] == [
        {
            "kind": "function",
            "routine_id": fx.WAL,
            "state": "repaired_pending_proof",
            "reasons": ["postgresql-routine-v10-renamed-functions"],
            "change_note": NOTE,
            # #1941: what the loop recorded, and the words the deliverables print; a
            # pending candidate's note is never printed as what changed.
            "proof_source": None,
            "attention": None,
            "attention_detail": None,
            "attempts": [],
            # A result of the seam alone: no attempt, so no claim of the loop's checks.
            "line": (
                "A model proposed a repair that no check after the model and no side-by-side "
                "proof has accepted, so it is not carried."
            ),
            "attempt_lines": [],
        }
    ]
    RoutineFacts.model_validate(facts)


def test_an_intervention_is_never_counted_as_carried() -> None:
    without = routine_facts(fx.migration(), 1)
    facts = routine_facts(fx.migration(), 1, None, _repair())
    assert without is not None and facts is not None
    for key in ("carried_count", "repaired_count", "needs_repair_count", "migrate_yourself_count"):
        assert facts[key] == without[key], key
    assert without["interventions"] == []


def test_the_facts_hold_no_routine_text() -> None:
    facts = routine_facts(fx.migration(), 1, None, _repair())
    assert "pg_current_wal_lsn()" not in str(facts["interventions"])
    assert "CREATE" not in str(facts["interventions"])


def test_a_result_for_another_version_is_not_read() -> None:
    facts = routine_facts(fx.migration(), 1, None, _repair(version=2))
    assert facts is not None and facts["interventions"] == []


def test_synthesis_reads_the_result_beside_the_migration_artifact(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{fx.DB}/{fx.JOB}/routines/v1/routine_migration.json",
        fx.migration().model_dump(mode="json"),
    )
    assert read_routine_facts(store, fx.DB, fx.JOB, 1)["interventions"] == []
    store.write_json(f"{fx.DB}/{fx.JOB}/routines/v1/routine_repair.json", _repair())
    # The version-2 report reads the newest migration artifact at or below it (v1), and
    # the repair result of that same version.
    facts = read_routine_facts(store, fx.DB, fx.JOB, 2)
    assert [i["routine_id"] for i in facts["interventions"]] == [fx.WAL]
