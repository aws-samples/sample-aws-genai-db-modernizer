# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine repair loop (#1941, ADR-034 Decision 2): re-checked candidates, proved
side by side, at most three attempts per routine.

The seam (``handler``) asks a model for a candidate on the redacted text and screens it
there. The loop then decides what happens to each routine:

1. **Before the model is asked**, each routine whose literals cannot be put back into a
   repair by position alone (``repair_rebuild``: a literal that reads ``?`` itself, one
   the model saw only in part, one holding SQL) is *needs attention*
   (``not_rebuildable``) and is never sent.
2. **Each attempt**: the model's candidate goes through the redacted screen (the seam),
   then onto the exact text: rebuilt token by token, screened again, checked again by
   every routine check, and proved side by side against the original
   (``src.tools.routines.repair_proof``). A candidate the proof accepts is
   ``model_repaired``, with its diff (of the redacted texts) and its change note. One
   that passes every check while no proof target is available (``--proof none``, or no
   disposable engine of the source's major here) is ``repaired_pending_proof``: never
   carried.
3. **A failed attempt** (refused by a screen, not rebuilt, a failed check, a difference
   side by side) is followed by another, with that error in the routine's block
   (``previous_attempt_error``, framed as untrusted data with the rest of the block, in
   words: never the exact text or a literal), up to :data:`MAX_ATTEMPTS`. After the last,
   the routine is *needs attention* (``attempts_used``), with every attempt and its error
   in the artifact. A routine the model leaves out of its answer is ``no_answer`` and is
   not asked again.

Modes: ``bedrock`` runs every round in one call; ``external`` writes round 1's request
(``routines/v<N>/repair/llm_input.json``) and stops ``awaiting_llm``; each ``--finalize``
applies the session's answer, verifies it and either writes the next round's request
(``awaiting_llm`` again, with ``round``) or the result. The loop's progress between
rounds sits in ``routines/v<N>/repair/loop_state.json`` (ids, attempts and words only).
``none`` runs no repair.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agents.routine_repair import handler as seam
from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_repair import (
    MAX_ATTEMPTS,
    REPAIR_STATES,
    RoutineRepairContract,
    RoutineRepairResponse,
)
from src.storage.collector_for_models import read_collector_for_models
from src.storage.routine_repair_store import (
    routine_repair_path,
    routine_repair_request_path,
    routine_repair_response_path,
    routine_repair_state_path,
)

__all__ = [
    "MAX_ATTEMPTS",
    "finalize_repair_loop",
    "run_repair_loop",
]

_NO_ANSWER_REASON = "the model gave no usable answer"
Ref = tuple[str, str]


def _key(ref: Ref) -> str:
    return f"{ref[0]}\u0000{ref[1]}"


def _ref(key: str) -> Ref:
    kind, _, rid = key.partition("\u0000")
    return (kind, rid)


class _State:
    """The loop's progress for one routine-migration version (ids, attempts and words
    only): what each external round resumes from."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.assignment_version = int(data["assignment_version"])
        self.llm_mode = str(data["llm_mode"])
        self.proof = str(data.get("proof") or "none")
        self.round = int(data.get("round") or 0)
        self.ignored = int(data.get("ignored") or 0)
        self.skipped_reason: str | None = data.get("skipped_reason")
        self.open: dict[Ref, list[dict[str, Any]]] = {
            _ref(k): list(v) for k, v in (data.get("open") or {}).items()
        }
        self.settled: dict[Ref, dict[str, Any]] = {
            _ref(k): dict(v) for k, v in (data.get("settled") or {}).items()
        }

    def dump(self) -> dict[str, Any]:
        return {
            "assignment_version": self.assignment_version,
            "llm_mode": self.llm_mode,
            "proof": self.proof,
            "round": self.round,
            "ignored": self.ignored,
            "skipped_reason": self.skipped_reason,
            "open": {_key(r): v for r, v in self.open.items()},
            "settled": {_key(r): v for r, v in self.settled.items()},
        }

    def previous_failures(self) -> dict[Ref, str]:
        out = {}
        for ref, attempts in self.open.items():
            if attempts and attempts[-1].get("error"):
                out[ref] = str(attempts[-1]["error"])
        return out


# --------------------------------------------------------------------------- pieces


def _load(
    store: Any, database_name: str, job_id: str, assignment_version: int | None
) -> tuple[RoutineMigrationContract, dict[str, Any]] | None:
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.routine_migration_store import latest_routine_migration

    version = assignment_version or resolve_downstream_assignment_version(
        store, database_name, job_id
    )
    found = latest_routine_migration(store, database_name, job_id, version)
    if found is None:
        return None
    return found[1], read_collector_for_models(store, database_name, job_id)


def _verifier(
    store: Any,
    migration: RoutineMigrationContract,
    refs: list[Ref],
    proof: str,
    options: Mapping[str, Any] | None,
) -> Any:
    from src.tools.routines.repair_proof import verifier_for_job

    return verifier_for_job(
        store,
        migration.database_name,
        migration.job_id,
        migration,
        refs=refs,
        proof=proof,
        **dict(options or {}),
    )


def _attempt(n: int, stage: str, outcome: str, code: Any = None, error: Any = None) -> dict:
    return {
        "attempt": n,
        "stage": stage,
        "outcome": outcome,
        "code": None if code is None else str(code)[:64],
        "error": None if error is None else " ".join(str(error).split())[:300],
    }


def _begin(det: seam.RoutineRepairPlan, verifier: Any, llm_mode: str, proof: str) -> _State:
    state = _State(
        {"assignment_version": det.assignment_version, "llm_mode": llm_mode, "proof": proof}
    )
    for c in det.candidates:
        blocked = verifier.blocker(c.kind, c.routine_id, c.create_text)
        if blocked is None:
            state.open[c.ref] = []
            continue
        _code, words = blocked
        state.settled[c.ref] = {
            "state": "needs_attention",
            "attention": "not_rebuildable",
            "attention_detail": words,
        }
    return state


def _round_plan(
    migration: RoutineMigrationContract, collector: Mapping[str, Any], state: _State
) -> seam.RoutineRepairPlan:
    return seam.run_routine_repair_deterministic(
        collector,
        migration,
        previous_failures=state.previous_failures(),
        only=set(state.open),
        attempt=state.round + 1,
    )


def _apply_round(
    state: _State,
    plan: seam.RoutineRepairPlan,
    answer: Mapping[str, Any] | RoutineRepairResponse | None,
    verifier: Any,
) -> None:
    """One round's answer: screened by the seam, then rebuilt, checked again and proved
    (module docstring). Raises ``pydantic.ValidationError`` for an answer off the
    contract, before anything changes."""
    from src.tools.routines.repair_proof import RepairItem
    from src.tools.routines.repair_rebuild import changed_lines

    n = state.round + 1
    if answer is None:
        state.round = n
        state.skipped_reason = _NO_ANSWER_REASON
        for ref in list(state.open):
            state.open[ref].append(
                _attempt(n, "answer", "no_answer", "no_answer", _NO_ANSWER_REASON)
            )
            state.settled[ref] = {"state": "no_answer", "attempts": state.open.pop(ref)}
        return
    result = seam.apply_routine_repair_llm_output(plan, answer, llm_mode=state.llm_mode)
    state.round = n
    state.ignored += result.ignored_answers
    seen = {c.ref: c.create_text for c in plan.candidates}
    items: list[Any] = []
    kept: dict[Ref, tuple[str, str]] = {}
    for row in result.routines:
        ref = (row.kind, row.routine_id)
        attempts = state.open[ref]
        if row.state == "no_answer":
            attempts.append(_attempt(n, "answer", "no_answer", "no_answer",
                                     "the answer left the routine out"))  # fmt: skip
            state.settled[ref] = {"state": "no_answer", "attempts": state.open.pop(ref)}
        elif row.state == "refused":
            attempts.append(_attempt(n, "screen", "failed", row.refusal, row.refusal_detail))
        else:
            assert row.repaired_create and row.change_note  # nosec B101 - the contract's
            kept[ref] = (row.repaired_create, row.change_note)
            items.append(RepairItem(row.kind, row.routine_id, seen[ref], row.repaired_create))
    for ref, check in verifier.verify(items).items():
        attempts = state.open[ref]
        attempts.append(_attempt(n, check.stage, check.outcome, check.code, check.error))
        if check.outcome == "failed":
            continue
        candidate, note = kept[ref]
        state.settled[ref] = {
            "state": "model_repaired" if check.outcome == "proved" else "repaired_pending_proof",
            "repaired_create": candidate,
            "change_note": note,
            "diff": "\n".join(changed_lines(seen[ref], candidate)),
            "proof_source": check.proof_source if check.outcome == "proved" else None,
            "attempts": state.open.pop(ref),
        }
    for ref in list(state.open):
        attempts = state.open[ref]
        if len(attempts) >= MAX_ATTEMPTS:
            errors = "; ".join(
                f"{a['attempt']}: {a['stage']} {a.get('code') or a['outcome']}" for a in attempts
            )
            state.settled[ref] = {
                "state": "needs_attention",
                "attention": "attempts_used",
                "attention_detail": f"every one of {MAX_ATTEMPTS} attempts failed ({errors})"[:300],
                "attempts": state.open.pop(ref),
            }


def _result(det: seam.RoutineRepairPlan, state: _State) -> RoutineRepairContract:
    rows = []
    for c in det.candidates:
        settled = state.settled.get(c.ref)
        if settled is None:  # still open: only when the rounds ran out early
            settled = {"state": "no_answer", "attempts": state.open.get(c.ref, [])}
        rows.append({"kind": c.kind, "routine_id": c.routine_id, "reasons": list(c.reasons),
                     **settled})  # fmt: skip
    counts = dict.fromkeys(REPAIR_STATES, 0)
    for row in rows:
        counts[row["state"]] += 1
    return RoutineRepairContract.model_validate(
        {
            "job_id": det.job_id,
            "database_name": det.database_name,
            "assignment_version": det.assignment_version,
            "llm_mode": state.llm_mode,
            "source_engine": det.source_engine,
            "target_engine": det.target_engine,
            "target_line": det.target_line,
            "skipped_reason": state.skipped_reason,
            "ignored_answers": state.ignored,
            "proof": state.proof,
            "rounds": state.round,
            "counts": counts,
            "routines": rows,
        }
    )


def _write(store: Any, result: RoutineRepairContract) -> str:
    key = routine_repair_path(result.database_name, result.job_id, result.assignment_version)
    store.write_json(key, result.model_dump(mode="json"))
    return key


def _status(status: str, result: RoutineRepairContract, key: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "assignment_version": result.assignment_version,
        "rounds": result.rounds,
        "proof": result.proof,
        "counts": dict(result.counts),
        "artifact": key,
    }
    if result.skipped_reason:
        out["reason"] = result.skipped_reason
    return out


def _awaiting(store: Any, det: seam.RoutineRepairPlan, plan: seam.RoutineRepairPlan,
              state: _State) -> dict[str, Any]:  # fmt: skip
    db, job, version = det.database_name, det.job_id, det.assignment_version
    request = seam.prepare_routine_repair_llm_input(plan)
    response = routine_repair_response_path(db, job, version)
    if store.exists(response) is True:
        store.delete(response)  # never finalize a round on an earlier round's answer
    store.write_json(routine_repair_request_path(db, job, version), request)
    store.write_json(routine_repair_state_path(db, job, version), state.dump())
    return {
        "status": "awaiting_llm",
        "assignment_version": version,
        "round": state.round + 1,
        "max_rounds": MAX_ATTEMPTS,
        "proof": state.proof,
        "routines": len(plan.candidates),
        "llm_request": routine_repair_request_path(db, job, version),
        "llm_response": response,
    }


# --------------------------------------------------------------------------- runs


def run_repair_loop(
    store: Any,
    database_name: str,
    job_id: str,
    *,
    llm_mode: str,
    assignment_version: int | None = None,
    proof: str = "none",
    proof_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the loop for the job's newest routine-migration artifact at or below
    ``assignment_version`` in ``llm_mode`` with the proof target ``proof``; a status dict:
    ``skipped`` (no artifact, or ``none``), ``awaiting_llm`` (external: round 1's request)
    or ``complete``. ``proof_options`` reach the verifier (a test's engine factory)."""
    loaded = _load(store, database_name, job_id, assignment_version)
    if loaded is None:
        return {"status": "skipped", "reason": "the job has no routine-migration artifact"}
    migration, collector = loaded
    det = seam.run_routine_repair_deterministic(collector, migration)
    if llm_mode == "none":
        result = seam.run_without_repair(det)
        return _status("skipped", result, _write(store, result))
    if llm_mode not in ("bedrock", "external"):
        raise ValueError(f"unknown llm_mode {llm_mode!r}")
    verifier = _verifier(store, migration, [c.ref for c in det.candidates], proof, proof_options)
    state = _begin(det, verifier, llm_mode, proof)
    if llm_mode == "external" and state.open:
        return _awaiting(store, det, _round_plan(migration, collector, state), state)
    while state.open and state.round < MAX_ATTEMPTS:
        plan = _round_plan(migration, collector, state)
        answer = seam._ask_bedrock(seam.prepare_routine_repair_llm_input(plan))
        _apply_round(state, plan, answer, verifier)
    result = _result(det, state)
    return _status("complete", result, _write(store, result))


def finalize_repair_loop(
    store: Any,
    database_name: str,
    job_id: str,
    *,
    assignment_version: int | None = None,
    proof_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the external session's answer to the current round, then write the next
    round's request (``awaiting_llm``) or the result (``complete``). Raises
    ``FileNotFoundError`` with no request, answer or loop state, ``StaleRepairRequest``
    when the request is not the one the job's artifacts and the loop give now, and
    ``pydantic.ValidationError`` for an answer off the contract (nothing is written)."""
    loaded = _load(store, database_name, job_id, assignment_version)
    if loaded is None:
        raise FileNotFoundError("the job has no routine-migration artifact")
    migration, collector = loaded
    db, version = database_name, migration.assignment_version
    paths = (
        routine_repair_request_path(db, job_id, version),
        routine_repair_response_path(db, job_id, version),
        routine_repair_state_path(db, job_id, version),
    )
    for path in paths:
        if store.exists(path) is not True:
            raise FileNotFoundError(f"{path} not found")
    state = _State(store.read_json(paths[2]))
    det = seam.run_routine_repair_deterministic(collector, migration)
    plan = _round_plan(migration, collector, state)
    if (
        state.assignment_version != version
        or not set(state.open) <= {c.ref for c in det.candidates}
        or store.read_json(paths[0]) != seam.prepare_routine_repair_llm_input(plan)
    ):
        raise seam.StaleRepairRequest(
            f"{paths[0]} is not the request the job's routines give now: run the repair again"
        )
    answer = store.read_json(paths[1])
    verifier = _verifier(store, migration, list(state.open), state.proof, proof_options)
    _apply_round(state, plan, answer, verifier)
    if state.open and state.round < MAX_ATTEMPTS:
        return _awaiting(store, det, _round_plan(migration, collector, state), state)
    result = _result(det, state)
    store.write_json(routine_repair_state_path(db, job_id, version), state.dump())
    return _status("complete", result, _write(store, result))
