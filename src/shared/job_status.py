"""Durable job-status side effects for a script-driven local job (#517).

Two gaps in ``LocalExecutionService._infer_status``
(``src/api/services/local_execution.py``) left a script-driven local job
stuck showing the wrong status forever:

1. A job whose ``scripts/run_*.py`` invocation errors out mid-run (an
   explicit ``_error()`` call or an unhandled exception) left no durable
   record of the failure, so it kept showing ``RUNNING``. ``write_failure_marker``
   writes the same ``_status.json`` marker ``stop_execution`` already writes
   for a deliberate cancel (#383), with ``status: FAILED`` instead, so
   ``_infer_status`` reports it the same way. ``clear_failure_marker``
   supersedes a stale FAILED marker once a later run of the exact same
   phase (and engine, for a fan-out phase) makes progress again.
2. A job whose assignment was approved in chat (never through the API's
   confirm-all/resume routes), or run with ``--llm-mode none`` straight
   through to Synthesis, never updates ``.meta/<job_id>.json`` -- only the
   orchestrator's own ``/resume`` path does that -- so
   ``_is_awaiting_assignment_approval`` kept reporting ``WAITING`` long after
   nothing was actually waiting on a human. ``mark_assignment_reviewed``
   records that gate as passed as soon as a script that could only run
   after it (schema design or synthesis) actually starts.

Review follow-ups baked into this module directly, so every caller gets
them for free:

- **C1** (round 1): a message passed in here is recorded verbatim, but
  every catch-all caller (``scripts._job_status.run_main`` /
  ``record_unhandled_failure``) passes a fixed, content-free string for an
  unhandled exception -- never ``str(exc)``, which can carry
  customer-derived content (e.g. a pydantic ``ValidationError``'s repr
  includes the invalid value). An explicit ``_error("...")`` call's
  message is a different story -- those are code-authored constants and
  paths, not raw exception text, so they are passed straight through here
  unchanged.
- **I1** (round 1): every function below is best-effort. A failure to
  write the marker itself (a read-only artifact root, a full disk, a
  concurrent writer) is caught, logged at DEBUG, and swallowed -- it must
  never break the caller's own error reporting, which already ran first.
- **I2** (round 1): a marker (or the assignment-review record) is only
  ever written for a job that actually exists -- ``collector/output.json``
  present -- so a mistyped ``--job-id``/``--db`` never litters the
  artifact tree with a phantom job directory. ``require_existing_job=False``
  opts out, for ``run_collect.py``'s own phase: the job it is creating has
  no ``collector/output.json`` yet by definition.
- **I4** (round 1 and round 2): clearing is scoped to the exact
  ``(phase, engine)`` about to run -- a schema-design or analysis engine
  that is about to retry must never erase a sibling engine's still-relevant
  FAILED record just because they share one ``_status.json`` per job. Pass
  ``engine=ANY_ENGINE`` instead when the *whole* fan-out phase is about to
  rerun (every engine at once, e.g. ``LocalOrchestrator.resume`` or
  ``run_assessment.py``'s own ``_start_phase``) -- that must clear a
  FAILED marker for *any* engine of that phase, not just a specific one.
- **N1** (round 2, critical): ``mark_assignment_reviewed`` must never
  create ``.meta/<job_id>.json`` when it doesn't already exist. A
  hand-written, partial progression validates fine as JSON but is still
  missing every other phase; ``LocalOrchestrator`` then loads *that* as
  the job's entire progression (instead of inferring one properly from
  real artifacts, the way it would for a ``.meta``-less job otherwise),
  and ``get_progression``/``resume`` either KeyErrors on a missing phase
  or 500s over the API. When ``.meta`` already exists (the orchestrator
  already owns a complete, valid progression document for this job),
  merging the one ``assignment_review`` field in-place is safe. When it
  doesn't, the approval is instead recorded in
  ``decisions/assignment_review.json`` -- the job's existing audit-trail
  folder for human approvals (see AGENTS.md) -- and
  ``is_assignment_reviewed`` reads it directly;
  ``LocalExecutionService._is_awaiting_assignment_approval`` checks both.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_STATUS_MARKER = "_status.json"
# This module's own inferred record (never a human decision -- see
# mark_assignment_reviewed's "source": "inferred").
_ASSIGNMENT_REVIEW_DECISION = "decisions/assignment_review.json"
# A genuine human approval, written elsewhere (scripts/check_decision.py's
# "--decision assignment_approval" convention, used by e.g.
# .claude/commands/assign.md's own decision gate) -- is_assignment_reviewed
# accepts this too (#517 round-3 review): a human having approved anywhere
# along this chain is at least as strong a signal as this module's own
# inference that nothing is actually waiting on anyone.
_ASSIGNMENT_APPROVAL_DECISION = "decisions/assignment_approval.json"
_MAX_MESSAGE_LEN = 1000

# Content-free (#143's logging rule, #517 round-1 review C1): never str(exc),
# which can carry customer-derived content -- only the exception's type and
# one of these fixed messages are ever recorded in the durable marker. The
# real traceback and any str(exc)-carrying stdout status line a script
# already printed are unaffected; this only changes what _status.json records.
UNHANDLED_MESSAGE = "unhandled error; see _logs/"
INTERRUPTED_MESSAGE = "interrupted"


# Sentinel for clear_failure_marker's engine= kwarg (#517 round-2 review,
# I3): matches a FAILED marker for *any* engine of the given phase, for a
# caller that is about to rerun the whole fan-out phase (every engine at
# once) rather than retrying one specific engine. Distinct from engine=None,
# which only matches a marker that itself has no engine (a non-fan-out
# phase).
class _AnyEngine:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "ANY_ENGINE"


ANY_ENGINE = _AnyEngine()

# clear_failure_marker's engine= accepts a real engine name, None (a
# non-fan-out phase), or the ANY_ENGINE sentinel.
EngineFilter = str | _AnyEngine | None


def _marker_path(db_name: str, job_id: str) -> str:
    return f"{db_name}/{job_id}/{_STATUS_MARKER}"


def _decision_path(db_name: str, job_id: str) -> str:
    return f"{db_name}/{job_id}/{_ASSIGNMENT_REVIEW_DECISION}"


def _approval_decision_path(db_name: str, job_id: str) -> str:
    return f"{db_name}/{job_id}/{_ASSIGNMENT_APPROVAL_DECISION}"


def _job_is_real(store, db_name: str, job_id: str) -> bool:
    """True once collect has actually written output for this job (#517
    round-1 review, I2). Swallows its own errors -- an unreadable store
    just means "can't confirm it's real", which is the safe answer here:
    callers skip the write rather than risk fabricating a phantom job."""
    try:
        return bool(store.exists(f"{db_name}/{job_id}/collector/output.json"))
    except Exception:
        logger.debug("job_status: could not confirm job %s/%s is real", db_name, job_id)
        return False


def _marker_status(store, path: str) -> str | None:
    if not store.exists(path):
        return None
    value = store.read_json(path).get("status")
    return str(value) if value is not None else None


def write_failure_marker(
    store,
    db_name: str,
    job_id: str,
    phase: str,
    error_type: str,
    message: str,
    *,
    engine: str | None = None,
    require_existing_job: bool = True,
) -> None:
    """Record a script-driven phase failure (#517).

    Never overwrites a deliberate ``CANCELLED`` stop (``stop_execution``,
    #383) -- a cancellation always wins over a failure that happens to be
    recorded for the same job afterward. Best-effort (I1): any exception
    writing the marker is logged at DEBUG and swallowed.
    """
    try:
        if require_existing_job and not _job_is_real(store, db_name, job_id):
            return
        path = _marker_path(db_name, job_id)
        if _marker_status(store, path) == "CANCELLED":
            return
        store.write_json(
            path,
            {
                "status": "FAILED",
                "phase": phase,
                "engine": engine,
                "error_type": error_type,
                "message": message[:_MAX_MESSAGE_LEN],
                "failed_at": datetime.now(UTC).isoformat(),
            },
        )
    except Exception:
        logger.debug("job_status: failed to write failure marker for %s/%s", db_name, job_id)


def record_unhandled_failure(ctx: tuple | None, exc: BaseException) -> None:
    """Record an exception that escaped a script's ``main()`` unhandled --
    as opposed to an explicit ``_error()`` call -- with a fixed,
    content-free message (#517 round-1 review, C1). Public so a script
    with its own try/except shape (``run_assessment.py``, which prints its
    own status line first) can call it directly, instead of reaching into
    this module's private message constants (#517 round-2 review nit).

    ``ctx`` is ``(store, db_name, job_id, phase, engine, require_existing_job)``
    or ``None`` when job/db aren't known yet -- a no-op in that case.
    """
    if ctx is None:
        return
    store, db_name, job_id, phase, engine, require_existing_job = ctx
    message = INTERRUPTED_MESSAGE if isinstance(exc, KeyboardInterrupt) else UNHANDLED_MESSAGE
    write_failure_marker(
        store,
        db_name,
        job_id,
        phase,
        type(exc).__name__,
        message,
        engine=engine,
        require_existing_job=require_existing_job,
    )


def clear_failure_marker(
    store,
    db_name: str,
    job_id: str,
    phase: str,
    *,
    engine: EngineFilter = None,
    require_existing_job: bool = True,
) -> None:
    """Supersede a FAILED marker at the start of a new attempt of ``phase``
    (#517), so a stale failure from a previous run doesn't linger once
    that phase is making progress again. Leaves a ``CANCELLED`` marker, or
    a FAILED marker for a *different* phase, alone.

    ``engine`` scopes which marker counts as "this attempt" (#517 round-1
    review I4, round-2 review I3):

    - ``None`` (default): matches only a marker with no engine recorded --
      a non-fan-out phase, or nothing to clear.
    - a specific engine name: matches only that engine's own marker --
      one schema-design/analysis engine retrying on its own must never
      erase a sibling engine's still-relevant FAILED record.
    - ``ANY_ENGINE``: matches a marker for this phase regardless of which
      engine it names -- the whole fan-out phase is about to rerun (every
      engine at once), so whichever engine previously failed is about to
      get a fresh run too.

    Best-effort (I1).
    """
    try:
        if require_existing_job and not _job_is_real(store, db_name, job_id):
            return
        path = _marker_path(db_name, job_id)
        if not store.exists(path):
            return
        marker = store.read_json(path)
        if marker.get("status") != "FAILED":
            return
        if marker.get("phase") != phase:
            return
        if engine is not ANY_ENGINE and marker.get("engine") != engine:
            return
        store.write_json(
            path, {"status": "SUPERSEDED", "cleared_at": datetime.now(UTC).isoformat()}
        )
    except Exception:
        logger.debug("job_status: failed to clear failure marker for %s/%s", db_name, job_id)


def is_assignment_reviewed(store, db_name: str, job_id: str) -> bool:
    """True once either ``mark_assignment_reviewed`` recorded the
    assignment-review gate via ``decisions/assignment_review.json`` (used
    when ``.meta`` doesn't exist for this job -- see
    ``mark_assignment_reviewed``, #517 round-2 review N1), or a human
    actually approved via ``decisions/assignment_approval.json`` (#517
    round-3 review -- a genuine human decision, written elsewhere, is at
    least as strong a signal). Best-effort: an unreadable store reads as
    False, the same safe default as ``_job_is_real``.
    """
    try:
        return bool(
            store.exists(_decision_path(db_name, job_id))
            or store.exists(_approval_decision_path(db_name, job_id))
        )
    except Exception:
        logger.debug("job_status: could not read assignment-review decision for %s", job_id)
        return False


def mark_assignment_reviewed(store, db_name: str, job_id: str, inferred_from: str) -> None:
    """Record the assignment-review gate as passed (#517).

    Schema design and synthesis can only ever run after that gate --
    whether it was passed through the API, or (as here) a chat approval or
    ``--llm-mode none`` that never touches ``.meta`` at all. Recording it
    as soon as either phase's script starts keeps
    ``LocalExecutionService._is_awaiting_assignment_approval`` from
    reporting ``WAITING`` for a job that is actually running. ``inferred_from``
    is the calling script's own phase (``"schema_design"`` or ``"synthesis"``)
    -- recorded alongside the decision (below) so it's clear *why* this was
    inferred, not actually clicked by a human.

    When ``.meta/<job_id>.json`` already exists, the orchestrator already
    owns a complete, valid progression document for this job, so merging
    in just the ``assignment_review`` field in-place is safe. When it
    doesn't, this *never* creates one (#517 round-2 review N1, critical):
    a hand-written partial progression still validates as JSON, but
    ``LocalOrchestrator`` would then load it as the job's *entire*
    progression -- every other phase missing -- instead of inferring one
    properly from real artifacts the way it does for a ``.meta``-less job
    otherwise, and a later ``resume``/``get_progression`` call KeyErrors
    or 500s. The approval goes to ``decisions/assignment_review.json``
    instead -- this job's existing audit-trail folder for human approvals
    (AGENTS.md) -- marked ``source: inferred`` (#517 round-3 review) since,
    unlike a genuine ``decisions/assignment_approval.json`` a human
    actually produced, nobody clicked anything here. ``is_assignment_reviewed``
    reads both.

    A no-op once already recorded either way. Best-effort (I1) and only
    for a real job (I2).
    """
    try:
        if not _job_is_real(store, db_name, job_id):
            return
        meta_path = f".meta/{job_id}.json"
        if store.exists(meta_path):
            meta = store.read_json(meta_path)
            phases = meta.setdefault("phases", {})
            existing = phases.get("assignment_review") or {}
            if existing.get("status") == "completed":
                return
            phases["assignment_review"] = {
                **existing,
                "phase": "assignment_review",
                "status": "completed",
            }
            store.write_json(meta_path, meta)
            return

        if is_assignment_reviewed(store, db_name, job_id):
            return
        store.write_json(
            _decision_path(db_name, job_id),
            {
                "status": "completed",
                "recorded_at": datetime.now(UTC).isoformat(),
                "source": "inferred",
                "inferred_from": inferred_from,
            },
        )
    except Exception:
        logger.debug("job_status: failed to record assignment review for %s", job_id)
