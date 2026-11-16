# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine proof on the throwaway Aurora test cluster (#1945, ADR-034 Decision 5).

For one job's routine-migration artifact (``routines/v<N>/routine_migration.json``), every
carried routine (``as_is``, ``rewritten``, ``model_repaired``) is tested on the #1946 test
cluster through the Data API (:mod:`src.tools.routines.data_api_target`):

- **Side by side** for a routine that was rewritten or repaired, and for one that names
  such a routine (a trigger on a rewritten trigger function): the original, exactly as
  collected, on a disposable engine of the source's engine and version, the carried text on
  the test cluster, compared call by call (:func:`~src.tools.routines.comparison.compare_routines`).
- **Load and run** for a routine carried as it is: its carried text loaded on the test
  cluster and its generated calls run there (:func:`~src.tools.routines.comparison.run_routines`);
  no source engine runs.

A trigger function has no call of its own: it takes the outcome of the trigger that runs
it. On PostgreSQL the strict check (``plpgsql_check``, when the cluster offers it) runs on
every PL/pgSQL routine; an error it finds makes the routine ``needs_attention``.

Each routine's result goes into its row's ``proof`` and the artifact's ``proof_run``
(:class:`~src.contracts.routine_migration.RoutineProofEntry`, ``RoutineProofRun``), which
the routine facts read (``src.shared.routine_facts``). A failing routine, or a batch that
fails, never stops the others. Every scratch database the run creates is dropped, also when
it fails: each target drops its own when it closes, and :func:`prove_routines` sweeps the
job's prefix in a ``finally`` block.

Load and run's rule: a routine is ``proved`` only when every call succeeded. When a call
fails, the same batch runs on the disposable source engine with the original texts (when
the source's version is there). A call is left out only when the source ran that routine on
identical rows and calls and the call failed there exactly the same way (the same call, its
SQLSTATE, error number and first message line, and a SQLSTATE that names one failure): it is
the synthetic input, not the target. Any other failure leaves the routine ``not_proved``,
``target_gap`` when its SQLSTATE says Aurora lacks a feature or a function; a call that left
the role it runs as makes the routine need attention.

Nothing is logged, and no routine text is written into the artifact. A failed call's first
message line is kept in that routine's ``failed_calls`` (the job folder only, never a
deliverable), since it can quote a name or a synthetic value; the command's line carries
ids, counts, SQLSTATEs and fixed reasons only.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from src.contracts.routine_migration import (
    ProofCallFailure,
    RoutineMigration,
    RoutineMigrationContract,
    RoutineProofEntry,
    RoutineProofRun,
)
from src.contracts.routine_sources import RoutineSource
from src.tools.database import disposable_engine as de
from src.tools.routines.comparison import (
    COVERAGE_LIMITS,
    ERROR_FIELDS_LIMIT,
    ComparedRoutine,
    RoutineComparison,
    RoutineRun,
    compare_routines,
    run_routines,
)
from src.tools.routines.proof_inputs import (
    EventSpec,
    RoutineParameter,
    RoutineProofInput,
    TriggerSpec,
)
from src.tools.routines.proof_targets import TargetFactory

__all__ = [
    "CARRIED",
    "LOAD_AND_RUN_LIMIT",
    "ProofPlan",
    "apply_proof",
    "plan_proof",
    "prove_routines",
]

#: Why the MySQL proof stops when its restricted runner user cannot be made; the cleanup's
#: outcome follows (:data:`RUNNER_CLEANED` or :data:`RUNNER_LEFT`).
RUNNER_REFUSED = (
    "the MySQL proof runs its routines only as a restricted user granted the scratch database, "
    "and the RDS Data API could not create that user or sign in as it: nothing ran"
)
RUNNER_CLEANED = ", and its secret was deleted"
RUNNER_LEFT = ", and what could not be deleted is in left_on_cluster: {left}"
#: The statuses a routine is carried with.
CARRIED = ("as_is", "rewritten", "model_repaired")
_COMPARED = ("rewritten", "model_repaired")
LOAD_AND_RUN_LIMIT = (
    "a routine carried as it is was loaded and called on the test cluster only, not compared "
    "with the original: its calls ran, and nothing says they behave as on the source"
)
_MODES = {"IN", "OUT", "INOUT", "VARIADIC"}
_EVERY = re.compile(r"^\s*EVERY\s+(\d+)\s+([A-Z_]+)", re.I)


@dataclass
class ProofPlan:
    """The carried routines as the proof tests them, by mode, and what was left out."""

    dialect: str
    routines: list[ComparedRoutine]
    side_by_side: set[str] = field(default_factory=set)
    load_and_run: set[str] = field(default_factory=set)
    not_tested: dict[str, str] = field(default_factory=dict)  # routine id -> why
    trigger_of: dict[str, str] = field(default_factory=dict)  # trigger function -> its trigger


def _parameters(signature: str | None) -> tuple[RoutineParameter, ...]:
    """The artifact's ``signature`` (argument types in order, a MySQL mode first) as
    parameters, split at the commas outside parentheses."""
    if not signature or not signature.strip():
        return ()
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(signature):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(signature[start:i])
            start = i + 1
    parts.append(signature[start:])
    out = []
    for i, part in enumerate(p.strip() for p in parts):
        words = part.split(None, 1)
        mode = "IN"
        if len(words) == 2 and words[0].upper() in _MODES:
            mode, part = words[0].upper(), words[1]
        out.append(RoutineParameter(name=f"p{i + 1}", data_type=part, mode=mode))  # type: ignore[arg-type]
    return tuple(out)


def _event_spec(event: Mapping[str, Any]) -> EventSpec:
    schedule = str((event or {}).get("schedule") or "")
    status = str((event or {}).get("status") or "ENABLED").upper().replace(" ", "_")
    status = status if status in ("ENABLED", "DISABLED", "REPLICA_SIDE_DISABLED") else "ENABLED"
    every = _EVERY.match(schedule)
    if every:
        return EventSpec(schedule="RECURRING", interval_value=int(every.group(1)),
                         interval_field=every.group(2).upper(), status=status)  # type: ignore[arg-type]  # fmt: skip
    return EventSpec(schedule="ONE TIME", status=status)  # type: ignore[arg-type]


def _collected(collector: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    schema = collector.get("database_schema") or {}
    return [r for r in schema.get(key) or [] if isinstance(r, Mapping)]


def _proof_input(row: RoutineMigration, dialect: str, collector: Mapping[str, Any],
                 tables: list[str]) -> RoutineProofInput:  # fmt: skip
    qualified = (
        f"{row.schema_name}.{row.name}" if dialect == "postgresql" and row.schema_name else row.name
    )
    if row.kind == "trigger":
        found = next((t for t in _collected(collector, "triggers")
                      if t.get("trigger_name") == row.name
                      and str(t.get("table_id") or "").rpartition(".")[2] == (row.table or "")), {})  # fmt: skip
        events = found.get("events") or [found.get("event_type") or "INSERT"]
        spec = TriggerSpec(table=row.table or str(found.get("table_id") or ""),
                           timing=str(found.get("timing") or "AFTER"), events=events,  # type: ignore[arg-type]
                           level=str(found.get("for_each") or "ROW"))  # type: ignore[arg-type]  # fmt: skip
        return RoutineProofInput(routine_id=row.routine_id, kind="trigger", name=row.name,
                                 trigger=spec, tables=tuple(tables))  # fmt: skip
    if row.kind == "event":
        event: Mapping[str, Any] = next(
            (e for e in _collected(collector, "events") if e.get("event_name") == row.name), {}
        )
        return RoutineProofInput(routine_id=row.routine_id, kind="event", name=row.name,
                                 event=_event_spec(event), tables=tuple(tables))  # fmt: skip
    return RoutineProofInput(routine_id=row.routine_id, kind=row.kind, name=qualified,
                             parameters=_parameters(row.signature), returns=row.returns,
                             tables=tuple(tables))  # fmt: skip


def _carried_text(row: RoutineMigration, source: RoutineSource, migration: RoutineMigrationContract,
                  user: str) -> str | None:  # fmt: skip
    """The text the deploy script would carry for ``user``: PostgreSQL's exact text, MySQL's
    with the routine-migration rewrites applied again for that user (``routine_scripts``)."""
    if migration.source_engine == "postgresql":
        return source.create_statement
    from src.tools.routines.mysql_checks import MySqlRoutine, check_mysql_routine
    from src.tools.routines.rewrites import rewrite_mysql_routine

    routine = MySqlRoutine(routine_id=row.routine_id, kind=row.kind,  # type: ignore[arg-type]
                           create_sql=source.create_statement, sql_mode=source.session.sql_mode or "")  # fmt: skip
    verdict = check_mysql_routine(routine, target_engine=migration.target_engine,
                                  target_line=migration.target_line, target_user=user,
                                  source_version=migration.source_version)  # fmt: skip
    done = rewrite_mysql_routine(routine, verdict, target_user=user)
    return None if done.record.status == "needs_attention" else done.target_create


def plan_proof(migration: RoutineMigrationContract, sources: Iterable[RoutineSource],
               collector: Mapping[str, Any], *, target_user: str) -> ProofPlan:  # fmt: skip
    """Which carried routines go side by side and which load and run, each with its
    original and carried text (in deploy order); the rest of the carried ones, with why
    they are not tested."""
    dialect = migration.source_engine
    carried = [r for r in migration.routines if r.status in CARRIED]
    wanted: dict[tuple[str, str, str], RoutineMigration] = {
        (r.kind, r.schema_name or "", r.specific_name or ""): r for r in carried
    }
    texts: dict[str, tuple[str, str]] = {}
    plan = ProofPlan(dialect=dialect, routines=[])
    for source in sources:
        row = wanted.get(source.key())
        if row is None:
            continue
        if not source.exact:
            plan.not_tested[row.routine_id] = "its CREATE is not exact"
            continue
        text = _carried_text(row, source, migration, target_user)
        if text is None:
            plan.not_tested[row.routine_id] = (
                "its rewrites do not apply for the test cluster's user"
            )
            continue
        texts[row.routine_id] = (source.create_statement, text)
    depends: dict[str, tuple[str, ...]] = {}
    for row in carried:
        refs = [
            ref.routine_id for d in row.dependencies if d.kind == "routine" for ref in d.routines
        ]
        depends[row.routine_id] = tuple(dict.fromkeys(refs))
    for row in carried:
        rid = row.routine_id
        if rid not in texts:
            plan.not_tested.setdefault(rid, "the routine sources hold no CREATE for it")
            continue
        tables = [d.name for d in row.dependencies if d.kind == "table"]
        original, text = texts[rid]
        name = _proof_input(row, dialect, collector, tables).name
        plan.routines.append(ComparedRoutine(
            _proof_input(row, dialect, collector, tables),
            (de.SourceRoutine(name, row.kind, original),), (de.SourceRoutine(name, row.kind, text),),
            tuple(d for d in depends[rid] if d in texts),
        ))  # fmt: skip
        if row.kind == "trigger":
            for dep in depends[rid]:
                plan.trigger_of.setdefault(dep, rid)
    status = {r.routine_id: r.status for r in carried}
    reaches: dict[str, bool] = {}

    def compared(rid: str, seen: frozenset[str] = frozenset()) -> bool:
        if rid not in reaches:
            reaches[rid] = status.get(rid) in _COMPARED or any(
                compared(d, seen | {rid}) for d in depends.get(rid, ()) if d not in seen
            )
        return reaches[rid]

    for routine in plan.routines:
        (plan.side_by_side if compared(routine.routine_id) else plan.load_and_run).add(
            routine.routine_id
        )
    return plan


def _with_deps(plan: ProofPlan, ids: set[str]) -> list[ComparedRoutine]:
    by_id = {r.routine_id: r for r in plan.routines}
    keep, todo = set(), list(ids)
    while todo:
        rid = todo.pop()
        if rid in keep or rid not in by_id:
            continue
        keep.add(rid)
        todo.extend(by_id[rid].depends_on)
    return [r for r in plan.routines if r.routine_id in keep]


def _entry_of_comparison(c: RoutineComparison) -> RoutineProofEntry:
    outcome = {"no_differences_found": "proved", "different": "needs_attention"}.get(
        c.outcome, "not_proved"
    )
    if c.reason_code == "role_changed":
        outcome = "needs_attention"  # the routine left the role it runs as
    return RoutineProofEntry(mode="side_by_side", outcome=outcome, reason=c.reason,  # type: ignore[arg-type]
                             reason_code=c.reason_code, calls_run=c.calls_compared,
                             calls_succeeded=c.calls_succeeded, mismatches=c.differences_total)  # fmt: skip


#: SQLSTATE classes and codes that point at a gap on the target rather than at the routine:
#: a feature Aurora does not support, a function it does not have.
TARGET_GAP_CLASSES = ("0A",)
TARGET_GAP_STATES = {"42883"}


def _target_gap(state: str) -> bool:
    return state[:2] in TARGET_GAP_CLASSES or state in TARGET_GAP_STATES


#: SQLSTATEs that name no one failure: PostgreSQL's ``P0001`` (any ``RAISE``), ``XX000`` and
#: ``22000``, MySQL's ``HY000`` (hundreds of error numbers), and a missing one. Two of them
#: are never taken for the same failure.
GENERIC_STATES = frozenset({"P0001", "XX000", "22000", "HY000", "?????"})


def _left_out(r: RoutineRun, source: RoutineRun | None) -> tuple[set[int], str]:
    """The failed calls of ``r`` to leave out, and a note when none can be. A call is left
    out only when the disposable source engine ran this same routine on identical rows and
    calls (the same ``inputs_digest``) and that call failed there with the same call index
    and label, SQLSTATE, error number and first message line, and a SQLSTATE that names one
    failure (not in :data:`GENERIC_STATES`)."""
    if source is None or not r.failures:
        return set(), ""
    if source.outcome != "ran":
        return set(), "; the source engine could not run it, so no failure is left out"
    if r.inputs_digest is None or source.inputs_digest != r.inputs_digest:
        return set(), "; the source engine ran other rows or calls, so no failure is left out"
    theirs = set(source.failures)
    return {f[0] for f in r.failures if f[2] not in GENERIC_STATES and f in theirs}, ""


def _entry_of_run(r: RoutineRun, source: RoutineRun | None = None) -> RoutineProofEntry:
    """Load and run's outcome: ``proved`` only when every call succeeded, leaving out a call
    that failed exactly the same way on the disposable source engine on identical rows and
    calls (:func:`_left_out`). Any other failure leaves it ``not_proved``, with each failing
    call's SQLSTATE and first message line; a feature Aurora does not support or a function
    it does not have (``0A``, ``42883``) says so. A routine that does not load, or whose call
    left the role it runs as (``role_changed``), needs attention."""
    skip, note = _left_out(r, source)
    failures = [f for f in r.failures if f[0] not in skip]
    on_both = len(r.failures) - len(failures)
    failed = [
        ProofCallFailure(call=label, call_index=i, error_state=state, message=message or None)
        for i, label, state, message, _errno in failures
    ]  # fmt: skip
    code: str | None = r.reason_code
    reason = r.reason
    if r.outcome == "does_not_load" or r.reason_code == "role_changed":
        outcome = "needs_attention"
    elif r.outcome != "ran":
        outcome = "not_proved"
    elif r.timed_out:
        outcome, code = "not_proved", "timeout"
        reason += f"; {r.timed_out} ran past the time limit"
    elif failures:
        states = sorted({f[2] for f in failures})
        outcome = "not_proved"
        if any(_target_gap(st) for st in states):
            code = "target_gap"
            reason += ("; Aurora does not support what a call needs (SQLSTATE "
                       f"{', '.join(st for st in states if _target_gap(st))})")  # fmt: skip
        else:
            code = "calls_failed"
            reason += f"; {len(failures)} failed on the test cluster (SQLSTATE {', '.join(states)})"
        reason += note
    elif r.calls_succeeded == 0:
        outcome, code = "not_proved", "no_successful_call"
        reason += "; every call failed the same way on the source, so nothing ran to the end"
    else:
        outcome = "proved"
    if on_both and r.outcome == "ran":
        reason += (f"; {on_both} failed exactly the same way on the source engine, on the same "
                   "rows and calls, so they are left out")  # fmt: skip
    return RoutineProofEntry(mode="load_and_run", outcome=outcome, reason=reason,  # type: ignore[arg-type]
                             reason_code=code, calls_run=r.calls_run, calls_succeeded=r.calls_succeeded,
                             calls_failed_on_both=on_both, error_states=list(r.error_states),
                             failed_calls=failed)  # fmt: skip


def _inherit(
    entries: Mapping[str, RoutineProofEntry], trigger_of: Mapping[str, str]
) -> dict[str, RoutineProofEntry]:
    """A trigger function, which has no call of its own, takes its trigger's outcome."""
    out = dict(entries)
    for fn, trigger in trigger_of.items():
        mine, theirs = out.get(fn), out.get(trigger)
        if mine is not None and theirs is not None and mine.reason_code == "no_calls":
            out[fn] = theirs.model_copy(
                update={"reason": f"through its trigger {trigger}: {theirs.reason}"}
            )
    return out


def prove_routines(
    plan: ProofPlan,
    tables: Sequence[Mapping[str, Any]],
    *,
    target: TargetFactory,
    source: TargetFactory | None,
    cleanup: Callable[[], Any],
    strict: Mapping[str, list[tuple[str, str]]],
    strict_check: Sequence[str],
    extensions: Sequence[str] = (),
    seed: int = 0,
) -> dict[str, RoutineProofEntry]:
    """Every routine of ``plan`` tested on ``target`` (side by side against ``source`` for
    the rewritten and repaired ones), as one :class:`RoutineProofEntry` per routine id.
    ``cleanup`` (drop the job's scratch databases) runs in a ``finally`` block. ``strict``
    and ``strict_check`` are what the target's factory collected."""
    dialect: Any = plan.dialect
    out: dict[str, RoutineProofEntry] = {}
    try:
        if plan.side_by_side:
            if source is None:
                for rid in plan.side_by_side:
                    out[rid] = RoutineProofEntry(mode="side_by_side", outcome="not_proved",
                                                 reason="no engine of the source's version here to run the original on",
                                                 reason_code="source_engine_unavailable")  # fmt: skip
            else:
                report = compare_routines(_with_deps(plan, plan.side_by_side), tables, source=source,
                                          target=target, dialect=dialect, seed=seed,
                                          extensions=extensions)  # fmt: skip
                for c in report.results:
                    if c.routine_id in plan.side_by_side:
                        out[c.routine_id] = _entry_of_comparison(c)
        if plan.load_and_run:
            runs = {r.routine_id: r for r in run_routines(
                _with_deps(plan, plan.load_and_run), tables, target=target, dialect=dialect,
                seed=seed, extensions=extensions) if r.routine_id in plan.load_and_run}  # fmt: skip
            failing = {rid for rid, r in runs.items() if r.failures}
            on_source: dict[str, RoutineRun] = {}
            if failing and source is not None:
                # The same batch on the source's own engine, the original texts, so the
                # synthetic rows and calls are built alike: a call that fails there exactly
                # the same way, on identical inputs, is no gap of the target's.
                originals = [replace(r, carried=r.original)
                             for r in _with_deps(plan, plan.load_and_run)]  # fmt: skip
                on_source = {r.routine_id: r for r in run_routines(
                    originals, tables, target=source, dialect=dialect, seed=seed,
                    extensions=extensions) if r.outcome == "ran"}  # fmt: skip
            for rid, r in runs.items():
                out[rid] = _entry_of_run(r, on_source.get(rid))
    finally:
        # never hides the original error or a finished proof: the cleanup reports what it left
        with contextlib.suppress(Exception):
            cleanup()
    out = _inherit(out, plan.trigger_of)
    tool = strict_check[0] if strict_check else "not_available"
    for rid, entry in list(out.items()):
        findings = [f for f in strict.get(rid, []) if f[0] != "unchecked"]
        update: dict[str, Any] = {}
        if dialect == "postgresql":
            if rid in strict and any(f[0] == "unchecked" for f in strict[rid]):
                update["strict_check"] = "not_available"  # the check could not run on it
            elif rid in strict:
                update["strict_check"] = "failed" if findings else "passed"
            else:
                update["strict_check"] = (
                    "not_applicable" if tool != "not_available" else "not_available"
                )
            update["strict_check_errors"] = [s for _, s in findings]
        if findings and entry.outcome != "needs_attention":
            update["outcome"] = "needs_attention"
            update["reason"] = (
                f"the strict check ({tool}) found {len(findings)} "
                f"error{'s' if len(findings) != 1 else ''} (SQLSTATE "
                f"{', '.join(sorted({s for _, s in findings}))}); {entry.reason}"
            )
            update["reason_code"] = "strict_check_failed"
        if update:
            out[rid] = entry.model_copy(update=update)
    for rid, why in plan.not_tested.items():
        out[rid] = RoutineProofEntry(mode="load_and_run", outcome="not_proved", reason=why,
                                     reason_code="not_tested")  # fmt: skip
    return out


def apply_proof(migration: RoutineMigrationContract, entries: Mapping[str, RoutineProofEntry], *,
                target: str, source: str | None, strict_check: str, seconds: float,
                error_fields: bool = False) -> RoutineMigrationContract:  # fmt: skip
    """``migration`` with each carried row's ``proof`` and the ``proof_run``, validated."""
    rows = []
    counts: dict[Any, int] = {"proved": 0, "needs_attention": 0, "not_proved": 0}
    for row in migration.routines:
        entry = entries.get(row.routine_id) if row.status in CARRIED else None
        if row.status in CARRIED and entry is None:
            entry = RoutineProofEntry(mode="load_and_run", outcome="not_proved",
                                      reason="the proof gave it no result", reason_code="not_tested")  # fmt: skip
        if entry is not None:
            counts[entry.outcome] += 1
        rows.append(row.model_copy(update={"proof": entry}))
    limits = [*COVERAGE_LIMITS, LOAD_AND_RUN_LIMIT]
    if not error_fields:
        limits.append(ERROR_FIELDS_LIMIT)
    run = RoutineProofRun(
        target=target, source=source,
        strict_check=strict_check if migration.source_engine == "postgresql" else "not_applicable",  # type: ignore[arg-type]
        proved_at=datetime.now(UTC), seconds=round(seconds, 1),
        counts=counts, not_carried=sum(r.status not in CARRIED for r in migration.routines),
        coverage_limits=limits,
    )  # fmt: skip
    fields = {**dict(migration), "routines": rows, "proof_run": run}
    return RoutineMigrationContract(**fields)  # validated: the counts must match the rows


def _major(engine: str, version: str | None) -> str | None:
    """The source's major (``16``, ``8.0``) from the artifact's version text."""
    found = re.search(r"(\d+)\.(\d+)", version or "")
    if not found:
        return None
    return found.group(1) if engine == "postgresql" else f"{found.group(1)}.{found.group(2)}"


def run_proof(
    store: Any,
    db: str,
    job_id: str,
    *,
    data_api: Any,
    cluster: Mapping[str, Any],
    source_factory: Callable[[str, str], TargetFactory] | None = None,
    secrets_client: Any = None,
) -> dict[str, Any]:
    """The routine proof of the job's newest routine-migration artifact on the test
    cluster ``cluster`` (``proof-target-status``'s ``cluster``), written back into that
    artifact; returns the command's JSON line (ids, counts and fixed reasons only)."""
    from src.agents.collector.routine_sources import read_routine_sources
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.routine_migration_store import latest_routine_migration, routine_migration_path
    from src.tools.routines.proof_targets import disposable_target

    started = time.monotonic()
    found = latest_routine_migration(
        store, db, job_id, resolve_downstream_assignment_version(store, db, job_id)
    )
    if found is None:
        return {"status": "nothing_to_prove", "reason": "the job has no routine-migration artifact"}
    version, migration = found
    carried = [r for r in migration.routines if r.status in CARRIED]
    if not carried:
        return {
            "status": "nothing_to_prove",
            "reason": "no routine is carried",
            "assignment_version": version,
        }
    engine = migration.source_engine
    cluster_engine = str(cluster.get("engine") or "")
    if cluster_engine != ("aurora-postgresql" if engine == "postgresql" else "aurora-mysql"):
        return {"status": "error", "message": f"the test cluster runs {cluster_engine or 'no engine'}, "
                f"and the routines move to {migration.target_engine}"}  # fmt: skip
    if (
        not cluster.get("data_api")
        or not cluster.get("arn")
        or not cluster.get("master_user_secret_arn")
    ):
        return {
            "status": "error",
            "message": "the test cluster has no Data API or no master secret ARN",
        }
    if engine == "mysql" and secrets_client is None:
        return {"status": "error", "message": "the MySQL proof needs Secrets Manager for its "
                "restricted runner user's secret"}  # fmt: skip
    from src.tools.routines import data_api_target as dat

    arns = {
        "cluster_arn": str(cluster["arn"]),
        "secret_arn": str(cluster["master_user_secret_arn"]),
    }
    admin = str(cluster.get("database_name") or "") or None
    if engine == "mysql":
        scheduler = dat.mysql_event_scheduler(data_api, **arns, admin_database=admin)
        if scheduler == "ON":
            return {"status": "error", "message": "the test cluster's event_scheduler is ON, so a "
                    "loaded event could fire outside every call's rollback and change rows "
                    "between a call and its snapshot: refused. Deploy the stack again from "
                    "routine-proof-target.yaml, whose parameter group sets it OFF"}  # fmt: skip
    left: list[str] = []
    dropped: list[str] = []
    if engine == "mysql":
        # What a killed run left: its runner users and their secrets, by the job's prefixes.
        done, missed = dat.sweep_runners(data_api, secrets_client, **arns, job_id=job_id,
                                         admin_database=admin)  # fmt: skip
        dropped.extend(done)
        left.extend(missed)
    before = dat.cluster_baseline(data_api, **arns, engine=engine, admin_database=admin)
    runner: Any = None
    try:
        if engine == "mysql":
            try:
                runner = dat.create_runner(data_api, secrets_client, **arns, job_id=job_id,
                                           admin_database=admin, tags=_tags(job_id))  # fmt: skip
            except de.EngineError as exc:
                # Never a fallback to the master user: the MySQL proof stops here.
                left.extend(getattr(exc, "left", []))
                tail = RUNNER_LEFT.format(left=", ".join(left)) if left else RUNNER_CLEANED
                return {"status": "error", "sqlstate": exc.sqlstate,
                        "message": RUNNER_REFUSED + tail, "left_on_cluster": left}  # fmt: skip
        collector = store.read_json(f"{db}/{job_id}/collector/output.json")
        user = runner.definer if runner else str(cluster.get("master_username") or "proofadmin")
        plan = plan_proof(migration, read_routine_sources(store, db, job_id), collector,
                          target_user=user)  # fmt: skip
        strict: dict[str, list[tuple[str, str]]] = {}
        strict_check: list[str] = []
        ids = {r.routine_id: r.routine_id for r in plan.routines}
        ids.update(
            {dat.text_key(s.create_sql): r.routine_id for r in plan.routines for s in r.carried}
        )
        target = dat.data_api_target(
            data_api, cluster_arn=arns["cluster_arn"], secret_arn=arns["secret_arn"], engine=engine,
            version=str(cluster.get("engine_version") or ""), job_id=job_id, admin_database=admin,
            strict=strict, strict_check=strict_check, routine_ids=ids, runner=runner,
        )  # fmt: skip
        major = _major(engine, migration.source_version)
        make_source = source_factory or (lambda e, v: disposable_target(e, v))
        source = make_source(engine, major) if major else None

        def cleanup() -> None:
            done, missed = dat.drop_scratch(data_api, **arns, engine=engine, job_id=job_id,
                                            admin_database=admin)  # fmt: skip
            dropped.extend(done)
            left.extend(missed)

        schema = collector.get("database_schema") or {}
        entries = prove_routines(plan, schema.get("tables") or [], target=target, source=source,
                                 cleanup=cleanup, strict=strict, strict_check=strict_check)  # fmt: skip
    finally:
        if runner is not None:
            left.extend(dat.drop_runner(data_api, secrets_client, **arns, admin_database=admin,
                                        account=runner))  # fmt: skip
    try:
        changes = dat.baseline_changes(
            before, dat.cluster_baseline(data_api, **arns, engine=engine, admin_database=admin)
        )
    except Exception as exc:  # noqa: BLE001 - reported; the proof's result is still written
        changes = {
            "baseline": {"added": [], "removed": [f"could not be read ({type(exc).__name__})"]}
        }
    where = (f"an Aurora {'PostgreSQL' if engine == 'postgresql' else 'MySQL'} "
             f"{cluster.get('engine_version') or ''} test cluster").replace("  ", " ")  # fmt: skip
    written = apply_proof(migration, entries, target=where,
                          source=f"a disposable {engine} {major} engine" if plan.side_by_side and major else None,
                          strict_check=(strict_check[0] if strict_check else "not_available"),
                          seconds=time.monotonic() - started)  # fmt: skip
    key = routine_migration_path(db, job_id, version)
    store.write_json(key, written.model_dump(mode="json"))
    run = written.proof_run
    assert run is not None  # nosec B101 - apply_proof sets it
    clean = not left and not changes
    return {
        "status": ("proved" if run.counts["needs_attention"] == 0 and run.counts["not_proved"] == 0
                   and clean else "done"),
        "artifact": key,
        "target": run.target,
        "source": run.source,
        "strict_check": run.strict_check,
        "counts": run.counts,
        "seconds": run.seconds,
        "runner": "a restricted user granted the scratch database only" if runner else
        "a NOLOGIN role that owns the scratch database's public schema",
        "leftover_scratch_dropped": len(dropped),
        "left_on_cluster": left,
        "cluster_changes": changes,
        "routines": [
            {"routine_id": r.routine_id, "kind": r.kind, "status": r.status, "mode": r.proof.mode,
             "outcome": r.proof.outcome, "reason": r.proof.reason, "calls_run": r.proof.calls_run,
             "calls_succeeded": r.proof.calls_succeeded, "mismatches": r.proof.mismatches,
             "strict_check": r.proof.strict_check,
             "failed_calls": [f"{f.call} (SQLSTATE {f.error_state})" for f in r.proof.failed_calls]}
            for r in written.routines if r.proof is not None
        ],  # fmt: skip
    }


def _tags(job_id: str) -> list[dict[str, str]]:
    from src.tools.data_movement.proof_target import JOB_TAG, PURPOSE, PURPOSE_TAG

    return [{"Key": JOB_TAG, "Value": job_id}, {"Key": PURPOSE_TAG, "Value": PURPOSE}]
