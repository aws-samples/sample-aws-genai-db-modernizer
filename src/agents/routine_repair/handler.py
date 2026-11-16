# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The model seam for repairing routines the deterministic path can't carry (#1940).

ADR-034 Decision 2: a model repairs only a routine the deterministic checks and rewrites
could not carry, its answer is untrusted data, and the seam runs the same way in every
LLM mode (AGENTS.md, "The LLM seam pattern"):

1. :func:`run_routine_repair_deterministic`: the routines that may go to a model, read
   from the routine-migration artifact and the collection, with no model call. A routine
   may go when it needs attention for its own failed checks (no dependency, cycle or
   missing-text reason, which no edit of its text settles), or when it is carried and
   the test cluster refused it (its proof needs attention); and only when its exact text
   was collected. A routine whose body the customer opted out of collecting (#1933),
   or whose body could not be read, is never one: its body is never sent to a model.
2. :func:`prepare_routine_repair_llm_input`: the request. Per routine: its redacted
   ``CREATE`` (the collection's text, literals replaced, #1625; never the routine
   sources' exact text), each failed rule with the catalog's reason, the target engine
   and version line, and the target's error when there is one. Everything about a
   routine is one block framed with ``frame_untrusted``.
3. :func:`apply_routine_repair_llm_output`: the answer, validated against
   :class:`~src.contracts.routine_repair.RoutineRepairResponse` (an answer off the
   contract raises ``ValidationError`` and marks nothing). A candidate that renames the
   routine, changes its signature, adds an object, names another schema, is not one
   statement of its kind, or changes nothing is refused (:func:`repair_refusal`); every
   other one is ``repaired_pending_proof``. Nothing here marks a routine proved or
   carries it: the side-by-side comparison (#1944) and the test-cluster proof (#1945)
   decide, in the repair loop (#1941).

The modes (:func:`run_routine_repair`): ``none`` runs no repair, and each routine stays
not carried, with its reasons (``not_repaired``); ``bedrock`` asks a Strands agent;
``external`` writes ``routines/v<N>/repair/llm_input.json`` and stops ``awaiting_llm``,
and :func:`finalize_routine_repair` applies the answer the session writes to
``routines/v<N>/repair/llm_response.json``. Each writes
``routines/v<N>/routine_repair.json`` (:class:`~src.contracts.routine_repair.
RoutineRepairContract`) for the routine-migration artifact of version N.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.agents.prompt_framing import (
    SYSTEM_PROMPT_DATA_DIRECTIVE,
    frame_untrusted,
    with_untrusted_notice,
)
from src.agents.routine_repair.source_text import redacted_create
from src.contracts.routine_migration import REASON_CODES, RoutineMigration, RoutineMigrationContract
from src.contracts.routine_repair import (
    REPAIR_STATES,
    RefusalCode,
    RoutineRepairContract,
    RoutineRepairResponse,
    RoutineRepairRow,
    parse_routine_repair_response,
)
from src.shared.bedrock_guardrail import GuardrailConfigError, GuardrailIntervened
from src.shared.skill_sections import skill_sections
from src.storage.collector_for_models import read_collector_for_models
from src.storage.routine_repair_store import (
    routine_repair_path,
    routine_repair_request_path,
    routine_repair_response_path,
)
from src.tools.routines.migration import collection_routines
from src.tools.routines.repair_screen import RepairedRoutine, refusal_of

__all__ = [
    "LLM_MODE_NONE_REASON",
    "RepairCandidate",
    "RoutineRepairPlan",
    "StaleRepairRequest",
    "apply_routine_repair_llm_output",
    "finalize_routine_repair",
    "prepare_routine_repair_llm_input",
    "repair_refusal",
    "routine_repair_prompt",
    "run_routine_repair",
    "run_routine_repair_deterministic",
    "run_without_repair",
]

logger = logging.getLogger(__name__)

#: The one instruction both modes follow: the command points Claude Code at it, and the
#: Bedrock prompt is built from its sections.
SKILL = "routine-repair.md"
_SKILL_SECTIONS = ("Untrusted input", "Instruction", "Rules", "Response")
LLM_MODE_NONE_REASON = "llm_mode=none: no repair runs; the routines stay not carried"
_NO_ANSWER_REASON = "the model gave no usable answer"
#: Reasons no edit of the routine's own text settles: it is not sent to a model.
_NOT_REPAIRABLE = frozenset(REASON_CODES) - {"depends_on_needs_attention"}
_NO_BODY = frozenset({"opted_out", "not_readable", "not_supported"})
#: The target error a request quotes: at most this many failed calls.
_FAILED_CALLS_QUOTED = 3


@dataclass(frozen=True)
class RepairCandidate:
    """One routine a model may repair, with all it is told (redacted text only)."""

    kind: str
    routine_id: str
    schema_name: str | None
    name: str
    signature: str | None
    returns: str | None
    table: str | None
    create_text: str
    sql_mode: str | None
    reasons: tuple[str, ...]
    failed_rules: tuple[Mapping[str, Any], ...]
    target_error: str | None

    @property
    def ref(self) -> tuple[str, str]:
        return (self.kind, self.routine_id)


@dataclass(frozen=True)
class RoutineRepairPlan:
    """The deterministic result: the routines that may go to a model (the seam's ``det``)."""

    job_id: str
    database_name: str
    assignment_version: int
    source_engine: str
    target_engine: str
    target_line: str | None
    candidates: tuple[RepairCandidate, ...]


# --------------------------------------------------------------------------- seam 1


def _value(item: object) -> str:
    return str(getattr(item, "value", item) or "")


def _target_error(row: RoutineMigration) -> str | None:
    proof = row.proof
    if proof is None or proof.outcome != "needs_attention":
        return None
    parts = [proof.reason]
    if proof.error_states:
        parts.append("error states: " + ", ".join(proof.error_states))
    for failure in proof.failed_calls[:_FAILED_CALLS_QUOTED]:
        parts.append(
            f"call {failure.call}: {failure.error_state}"
            + (f" {failure.message}" if failure.message else "")
        )
    if proof.strict_check_errors:
        parts.append("strict check: " + ", ".join(proof.strict_check_errors))
    return "; ".join(parts)


def _why(row: RoutineMigration, rule_ids: set[str]) -> tuple[str, ...] | None:
    """The reasons that make ``row`` a routine to repair, or None when it is not one."""
    if row.source_text != "exact" or (row.body_status or "") in _NO_BODY:
        return None
    if row.status == "needs_attention":
        own = [r for r in row.reasons if r in rule_ids]
        if not own or any(r in _NOT_REPAIRABLE for r in row.reasons):
            return None
        return tuple(row.reasons)
    if row.proof is not None and row.proof.outcome == "needs_attention":
        return ("target_error",)
    return None


def run_routine_repair_deterministic(
    collector: Mapping[str, Any],
    migration: RoutineMigrationContract,
    *,
    target_errors: Mapping[tuple[str, str], str] | None = None,
) -> RoutineRepairPlan:
    """The routines of ``migration`` a model may repair (module docstring), each with its
    redacted ``CREATE`` from ``collector`` (read for models, redacted on read).
    ``target_errors`` gives a routine the target's error from an earlier attempt (the
    repair loop, #1941), keyed ``(kind, routine_id)``; it replaces the proof's."""
    rules = {r.rule_id: r for r in migration.rules if r.rule == "check"}
    rows = collection_routines(collector)
    engine = migration.source_engine
    candidates: list[RepairCandidate] = []
    for row in migration.routines:
        reasons = _why(row, set(rules))
        if reasons is None:
            continue
        found = rows.get((row.kind, row.routine_id))
        # The collection's own word too: an opted-out body is never sent (#1933).
        if found is None or _value(found.get("body_status")) in _NO_BODY:
            continue
        text = redacted_create(found, engine, row.kind)
        if text is None:
            continue
        findings = {c.rule_id: c.findings for c in row.checks}
        failed = tuple(
            {
                "rule_id": rid,
                "impact": rules[rid].impact,
                "reason": rules[rid].reason,
                "fix_hint": rules[rid].fix_hint,
                "findings": list(findings.get(rid, [])),
            }
            for rid in row.reasons
            if rid in rules
        )
        error = (target_errors or {}).get((row.kind, row.routine_id)) or _target_error(row)
        candidates.append(
            RepairCandidate(
                kind=row.kind,
                routine_id=row.routine_id,
                schema_name=row.schema_name,
                name=row.name,
                signature=row.signature,
                returns=row.returns,
                table=row.table,
                create_text=text,
                sql_mode=str(found.get("sql_mode") or "") or None,
                reasons=reasons,
                failed_rules=failed,
                target_error=error,
            )
        )
    return RoutineRepairPlan(
        job_id=migration.job_id,
        database_name=migration.database_name,
        assignment_version=migration.assignment_version,
        source_engine=engine,
        target_engine=migration.target_engine,
        target_line=migration.target_line,
        candidates=tuple(candidates),
    )


# --------------------------------------------------------------------------- seam 2


def _block(c: RepairCandidate) -> dict[str, Any]:
    return {
        "kind": c.kind,
        "routine_id": c.routine_id,
        "schema": c.schema_name,
        "name": c.name,
        "signature": c.signature,
        "returns": c.returns,
        "table": c.table,
        "create_statement": c.create_text,
        "failed_rules": [dict(r) for r in c.failed_rules],
        "other_reasons": [r for r in c.reasons if r not in {f["rule_id"] for f in c.failed_rules}],
        "target_error": c.target_error,
    }


def _framed(c: RepairCandidate) -> str:
    return frame_untrusted(
        json.dumps(_block(c), indent=1, ensure_ascii=False),
        label=(
            "one routine to repair: its CREATE (literals redacted), the rules it failed and "
            "the target's error"
        ),
    )


def prepare_routine_repair_llm_input(det: RoutineRepairPlan) -> dict[str, Any]:
    """The request a model repairs from (module docstring, seam 2): the target, then one
    framed block per routine. A request file says in its notice that its content is
    data; the Bedrock prompt (:func:`routine_repair_prompt`) uses the framed blocks."""
    return with_untrusted_notice(
        {
            "instruction": f"src/skills/{SKILL}",
            "source_engine": det.source_engine,
            "target_engine": det.target_engine,
            "target_line": det.target_line,
            "assignment_version": det.assignment_version,
            "routine_count": len(det.candidates),
            "routines": [_framed(c) for c in det.candidates],
        }
    )


def routine_repair_prompt(request: Mapping[str, Any]) -> str:
    """The Bedrock prompt: the skill's sections, the target, then each framed routine."""
    sections = skill_sections(SKILL)
    head = "\n\n".join(f"{name.upper()}:\n{sections[name]}" for name in _SKILL_SECTIONS)
    target = (
        f"Target: {request['target_engine']}, version line {request['target_line'] or 'any'}; "
        f"source engine {request['source_engine']}."
    )
    blocks = "\n\n".join(str(b) for b in request.get("routines") or [])
    return f"{head}\n\n{target}\n\n{blocks}\n\nRepair the routines now."


# --------------------------------------------------------------------------- seam 3


def repair_refusal(c: RepairCandidate, text: str, engine: str) -> tuple[RefusalCode, str] | None:
    """Why the candidate ``text`` for ``c`` is refused, as ``(code, words)``; None when it
    may wait for the proof (``src.tools.routines.repair_screen.refusal_of``)."""
    routine = RepairedRoutine(
        kind=c.kind,
        schema_name=c.schema_name,
        name=c.name,
        create_text=c.create_text,
        sql_mode=c.sql_mode,
    )
    return refusal_of(routine, text, engine)


def _counts(rows: list[RoutineRepairRow]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(REPAIR_STATES, 0)
    for row in rows:
        counts[row.state] += 1
    return counts


def _result(
    det: RoutineRepairPlan,
    rows: list[RoutineRepairRow],
    llm_mode: str,
    *,
    skipped_reason: str | None = None,
    ignored: int = 0,
) -> RoutineRepairContract:
    return RoutineRepairContract(
        job_id=det.job_id,
        database_name=det.database_name,
        assignment_version=det.assignment_version,
        llm_mode=llm_mode,  # type: ignore[arg-type]
        source_engine=det.source_engine,  # type: ignore[arg-type]
        target_engine=det.target_engine,  # type: ignore[arg-type]
        target_line=det.target_line,
        skipped_reason=skipped_reason,
        ignored_answers=ignored,
        counts=_counts(rows),  # type: ignore[arg-type]
        routines=rows,
    )


def _unrepaired(det: RoutineRepairPlan, state: str) -> list[RoutineRepairRow]:
    return [
        RoutineRepairRow(
            kind=c.kind,  # type: ignore[arg-type]
            routine_id=c.routine_id,
            state=state,  # type: ignore[arg-type]
            reasons=list(c.reasons),
        )
        for c in det.candidates
    ]


def run_without_repair(det: RoutineRepairPlan) -> RoutineRepairContract:
    """``--llm-mode none``: no repair runs; each routine stays not carried, with its reasons."""
    return _result(
        det, _unrepaired(det, "not_repaired"), "none", skipped_reason=LLM_MODE_NONE_REASON
    )


def apply_routine_repair_llm_output(
    det: RoutineRepairPlan,
    llm_output: Mapping[str, Any] | RoutineRepairResponse,
    *,
    llm_mode: str = "external",
) -> RoutineRepairContract:
    """The seam's result for a model's answer (module docstring, seam 3). Raises
    ``pydantic.ValidationError`` when the answer is off the contract: nothing is marked."""
    response = parse_routine_repair_response(llm_output)
    answers: dict[tuple[str, str], list[Any]] = {}
    wanted = {c.ref for c in det.candidates}
    ignored = 0
    for answer in response.repairs:
        ref = (answer.kind, answer.routine_id)
        if ref not in wanted:
            ignored += 1
            continue
        answers.setdefault(ref, []).append(answer)
    rows: list[RoutineRepairRow] = []
    for c in det.candidates:
        given = answers.get(c.ref, [])
        base: dict[str, Any] = {
            "kind": c.kind,
            "routine_id": c.routine_id,
            "reasons": list(c.reasons),
        }
        if not given:
            rows.append(RoutineRepairRow(**base, state="no_answer"))
            continue
        if len(given) > 1:
            rows.append(
                RoutineRepairRow(
                    **base,
                    state="refused",
                    refusal="duplicate_answer",
                    refusal_detail="the answer repairs the routine more than once",
                )
            )
            continue
        answer = given[0]
        refused = repair_refusal(c, answer.repaired_create, det.source_engine)
        if refused is not None:
            code, words = refused
            rows.append(
                RoutineRepairRow(
                    **base,
                    state="refused",
                    refusal=code,
                    refusal_detail=words,
                )
            )
            continue
        rows.append(
            RoutineRepairRow(
                **base,
                state="repaired_pending_proof",
                repaired_create=answer.repaired_create,
                change_note=answer.change_note,
            )
        )
    return _result(det, rows, llm_mode, ignored=ignored)


# --------------------------------------------------------------------------- Bedrock


def _ask_bedrock(request: Mapping[str, Any]) -> RoutineRepairResponse | None:
    """The Bedrock answer to ``request``, or None when the model gives no usable one."""
    try:
        from strands import Agent
    except ImportError:
        logger.warning("Strands not available: no routine repair")
        return None
    from src.shared.models import Role, build_bedrock_model, resolve_model_id

    try:
        # The schema designers' role: a long structured answer with thinking.
        agent = Agent(
            model=build_bedrock_model(Role.SCHEMA_DESIGN),
            system_prompt=(
                "You repair database routines so they load and run on their Aurora target, "
                "changing as little as you can.\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE
            ),
            tools=[],
            structured_output_model=RoutineRepairResponse,
            callback_handler=None,
        )
        result = agent(routine_repair_prompt(request))
    except GuardrailConfigError:
        raise
    except GuardrailIntervened as exc:
        logger.error("%s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- a failed call repairs nothing
        # The error can carry prompt or answer fragments (#143): its type only.
        logger.error(
            "routine repair call failed (model=%s): %s",
            resolve_model_id(Role.SCHEMA_DESIGN),
            type(exc).__name__,
        )
        return None
    structured = getattr(result, "structured_output", None)
    return structured if isinstance(structured, RoutineRepairResponse) else None


# --------------------------------------------------------------------------- runs


def _plan(
    store: Any, database_name: str, job_id: str, assignment_version: int | None
) -> RoutineRepairPlan | None:
    from src.storage.assignment_versioning import resolve_downstream_assignment_version
    from src.storage.routine_migration_store import latest_routine_migration

    version = assignment_version or resolve_downstream_assignment_version(
        store, database_name, job_id
    )
    found = latest_routine_migration(store, database_name, job_id, version)
    if found is None:
        return None
    collector = read_collector_for_models(store, database_name, job_id)
    return run_routine_repair_deterministic(collector, found[1])


def _write(store: Any, result: RoutineRepairContract) -> str:
    key = routine_repair_path(result.database_name, result.job_id, result.assignment_version)
    store.write_json(key, result.model_dump(mode="json"))
    return key


def _status(status: str, result: RoutineRepairContract, key: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "assignment_version": result.assignment_version,
        "counts": dict(result.counts),
        "artifact": key,
    }
    if result.skipped_reason:
        out["reason"] = result.skipped_reason
    return out


def run_routine_repair(
    store: Any,
    database_name: str,
    job_id: str,
    *,
    llm_mode: str,
    assignment_version: int | None = None,
) -> dict[str, Any]:
    """Run the seam for the job's newest routine-migration artifact at or below
    ``assignment_version`` (default: the effective one) in ``llm_mode``; a status dict:
    ``skipped`` (no artifact, or ``none``), ``awaiting_llm`` (external, with the request
    and response paths) or ``complete``."""
    det = _plan(store, database_name, job_id, assignment_version)
    if det is None:
        return {"status": "skipped", "reason": "the job has no routine-migration artifact"}
    if llm_mode == "none":
        result = run_without_repair(det)
        return _status("skipped", result, _write(store, result))
    if not det.candidates:
        result = _result(det, [], llm_mode)
        return _status("complete", result, _write(store, result))
    request = prepare_routine_repair_llm_input(det)
    if llm_mode == "external":
        path = routine_repair_request_path(database_name, job_id, det.assignment_version)
        store.write_json(path, request)
        return {
            "status": "awaiting_llm",
            "assignment_version": det.assignment_version,
            "routines": len(det.candidates),
            "llm_request": path,
            "llm_response": routine_repair_response_path(
                database_name, job_id, det.assignment_version
            ),
        }
    if llm_mode != "bedrock":
        raise ValueError(f"unknown llm_mode {llm_mode!r}")
    response = _ask_bedrock(request)
    if response is None:
        result = _result(
            det, _unrepaired(det, "no_answer"), "bedrock", skipped_reason=_NO_ANSWER_REASON
        )
    else:
        result = apply_routine_repair_llm_output(det, response, llm_mode="bedrock")
    return _status("complete", result, _write(store, result))


class StaleRepairRequest(ValueError):
    """The request the session answered is not the one this job's artifacts give now."""


def finalize_routine_repair(
    store: Any,
    database_name: str,
    job_id: str,
    *,
    assignment_version: int | None = None,
) -> dict[str, Any]:
    """Apply the external session's answer (``routines/v<N>/repair/llm_response.json``)
    and write the result. Raises ``FileNotFoundError`` with no request or answer,
    :class:`StaleRepairRequest` when the artifacts changed since the request, and
    ``pydantic.ValidationError`` for an answer off the contract (nothing is written)."""
    det = _plan(store, database_name, job_id, assignment_version)
    if det is None:
        raise FileNotFoundError("the job has no routine-migration artifact")
    request_path = routine_repair_request_path(database_name, job_id, det.assignment_version)
    response_path = routine_repair_response_path(database_name, job_id, det.assignment_version)
    for path in (request_path, response_path):
        if store.exists(path) is not True:
            raise FileNotFoundError(f"{path} not found")
    if store.read_json(request_path) != prepare_routine_repair_llm_input(det):
        raise StaleRepairRequest(
            f"{request_path} is not the request the job's routines give now: run the "
            "repair again"
        )
    result = apply_routine_repair_llm_output(det, store.read_json(response_path))
    return _status("complete", result, _write(store, result))
