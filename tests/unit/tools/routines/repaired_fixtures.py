# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A job with a model-repaired routine, for step 1's deploy gate and the test cluster's proof
(#2384).

The repair seam's synthetic PostgreSQL 9.6 job (``repair_fixtures``): ``public.wal_pos``
calls a function PostgreSQL 10 renamed, so it needs attention; the repair loop carried it
(``model_repaired``) with ``WAL_REPAIRED``, the redacted text with the new name. Its exact
text holds ``LITERAL_MARK``, which the rebuilt repair gets back: only the routines file and
the scripts step 1 generates may ever hold the two together."""

from __future__ import annotations

from typing import Any

from src.agents.collector.routine_sources import routine_sources_digest
from src.contracts.routine_migration import RoutineProofEntry
from src.contracts.routine_repair import RoutineRepairContract
from src.contracts.transformation_contract import StepContract
from src.shared.routine_facts import repair_sha256
from src.storage.routine_repair_store import routine_repair_path
from src.tools.data_movement.step1_routines import build_step_routines
from src.tools.routines.migration import read_routine_migration, routine_migration_path
from src.tools.routines.proof_run import CARRIED, apply_proof
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract

DB, JOB, WAL = fx.DB, fx.JOB, fx.WAL
OK = "public.ok(integer)"
WAL_KEY = ("function", WAL)
NOTE = "Calls pg_current_wal_lsn, the name PostgreSQL 10 gave the function."
#: The repaired routine's exact text, as step 1 deploys it: the new name, the literal back.
REBUILT = fx.WAL_CREATE.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")
#: Another repair a model could have made (a cast added): passes every check too.
OTHER = fx.WAL_REPAIRED.replace("::text ||", "::text::text ||")


def repair_result(candidate: str = fx.WAL_REPAIRED, *, proof: str = "local") -> dict[str, Any]:
    """``routine_repair.json`` as the repair loop writes it with wal_pos carried."""
    row = {
        "kind": "function", "routine_id": WAL, "state": "model_repaired",
        "reasons": ["postgresql-version-renamed-function"], "repaired_create": candidate,
        "change_note": NOTE, "diff": "-a\n+b", "proof_source": "a disposable postgresql 9 engine",
        "attempts": [{"attempt": 1, "stage": "proof", "outcome": "proved"}],
    }  # fmt: skip
    counts = dict.fromkeys(
        ("repaired_pending_proof", "refused", "no_answer", "not_repaired", "model_repaired",
         "needs_attention"), 0)  # fmt: skip
    counts["model_repaired"] = 1
    return RoutineRepairContract.model_validate({
        "job_id": JOB, "database_name": DB, "assignment_version": 1, "llm_mode": "bedrock",
        "source_engine": "postgresql", "target_engine": "aurora_postgresql", "proof": proof,
        "rounds": 1, "counts": counts, "routines": [row],
    }).model_dump(mode="json")  # fmt: skip


def write_repair(store: Any, candidate: str = fx.WAL_REPAIRED, **kw: Any) -> None:
    store.write_json(routine_repair_path(DB, JOB, 1), repair_result(candidate, **kw))


def mark_tested(
    store: Any,
    outcomes: dict[str, str] | None = None,
    *,
    tested: str | None = fx.WAL_REPAIRED,
    digest: str | None = "current",
) -> None:
    """The job's artifact as a test on a test Aurora leaves it: every carried routine and
    wal_pos with an outcome (``proved`` unless ``outcomes`` says otherwise), wal_pos's entry
    naming the repair ``tested`` ran (None: the test ran no repair of it, so no entry)."""
    artifact = read_routine_migration(store, DB, JOB, 1)
    assert artifact is not None
    entries = {}
    for r in artifact.routines:
        if r.status in CARRIED or (r.routine_id == WAL and tested is not None):
            outcome = (outcomes or {}).get(r.routine_id, "proved")
            entries[r.routine_id] = RoutineProofEntry(
                mode="side_by_side" if r.routine_id == WAL else "load_and_run",
                outcome=outcome,  # type: ignore[arg-type]
                reason="the test's result", calls_run=2, calls_succeeded=2,
            )  # fmt: skip
    current = routine_sources_digest(store, DB, JOB) if digest == "current" else digest
    done = apply_proof(
        artifact, entries, target="an Aurora PostgreSQL 16 test cluster", source=None,
        strict_check="not_available", seconds=1.0, sources_digest=current,
        repaired={WAL_KEY: repair_sha256(tested)} if tested is not None else None,
    )  # fmt: skip
    store.write_json(routine_migration_path(DB, JOB, 1), done.model_dump(mode="json"))


def job(tmp_path: Any, *, repaired: bool = True) -> Any:
    """The repair seam's job with wal_pos repaired (or not)."""
    store = fx.job(tmp_path, hide=True)
    if repaired:
        write_repair(store)
    return store


def contract(store: Any) -> StepContract:
    """The step 1 contract with the routines section ``attach_step_routines`` writes."""
    from src.shared.routine_facts import repaired_routines
    from src.storage.routine_repair_store import read_routine_repair

    artifact = read_routine_migration(store, DB, JOB, 1)
    assert artifact is not None
    base = make_contract("aurora_postgresql")
    repaired = repaired_routines(read_routine_repair(store, DB, JOB, 1), 1)
    section = build_step_routines(artifact, base, version=1, repaired=set(repaired))
    return base.model_copy(update={"routines": section})


MYSQL_KEY = ("procedure", fx.MYSQL_ID)


def mysql_job(tmp_path: Any) -> Any:
    """A MySQL 5.7 job whose procedure names a word 8.0 reserves (``rank``), repaired by the
    loop with the variable renamed, and tested: the repair proved on the current routines
    file."""
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.routine_sources import RoutineSource
    from src.storage.local_store import LocalArtifactStore
    from src.tools.routines.repair_proof import seen_texts

    store = LocalArtifactStore(base_dir=str(tmp_path))
    col = fx.mysql_collector()
    store.write_json(f"{DB}/{JOB}/collector/output.json", col)
    source = RoutineSource(kind="procedure", routine_id="hashpw", schema_name="shop",
                           specific_name="hashpw", create_statement=fx.MYSQL_EXACT,
                           origin="show_create")  # fmt: skip
    write_routine_sources(store, DB, JOB, col, [source])
    artifact = fx.mysql_migration()
    store.write_json(routine_migration_path(DB, JOB, 1), artifact.model_dump(mode="json"))
    seen = seen_texts(store, DB, JOB, artifact)[MYSQL_KEY]
    candidate = seen.replace("rank", "rnk")
    data = repair_result(candidate)
    data.update(source_engine="mysql", target_engine="aurora_mysql")
    data["routines"][0].update(kind="procedure", routine_id=fx.MYSQL_ID)
    store.write_json(routine_repair_path(DB, JOB, 1), data)
    entry = RoutineProofEntry(mode="side_by_side", outcome="proved", reason="no differences found")
    done = apply_proof(artifact, {fx.MYSQL_ID: entry}, target="an Aurora MySQL 3 test cluster",
                       source=None, strict_check="not_applicable", seconds=1.0,
                       sources_digest=routine_sources_digest(store, DB, JOB),
                       repaired={MYSQL_KEY: repair_sha256(candidate)})  # fmt: skip
    store.write_json(routine_migration_path(DB, JOB, 1), done.model_dump(mode="json"))
    return store


def mysql_contract(store: Any) -> StepContract:
    from src.shared.routine_facts import repaired_routines
    from src.storage.routine_repair_store import read_routine_repair

    artifact = read_routine_migration(store, DB, JOB, 1)
    assert artifact is not None
    base = make_contract("aurora_mysql")
    repaired = repaired_routines(read_routine_repair(store, DB, JOB, 1), 1)
    section = build_step_routines(artifact, base, version=1, repaired=set(repaired))
    return base.model_copy(update={"routines": section})
