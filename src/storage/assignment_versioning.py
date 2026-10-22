"""Single source of truth for resolving assignment artifact versions (ADR-028).

Assignment artifacts are written under ``<db>/<job>/assignment/v<N>/assignment.json``
with a monotonically increasing ``N``. Historically at least six call sites each
re-derived "the latest version" with a slightly different `max(vN)` scan (and one
probed ``v10 -> v1`` by read). ADR-028 collapses them onto the two helpers here so
the resolution rule is stated once: the highest committed version in the lineage.

These helpers are duck-typed on the ``store`` argument: they only require
``list_prefix(prefix) -> Iterable[str]`` returning keys relative to the store
root, exactly as every ArtifactStore already provides. No storage type is
imported, so this stays a leaf module the whole codebase can depend on.

Callers that must never receive 0 (schema design and synthesis always operate on
at least the v1 the assessment core writes, per ADR-026) apply that "coerce to 1"
policy themselves; this module reports the raw truth (0 when nothing exists).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from typing import NamedTuple, Protocol

from src.contracts.reality_check_marker import MarkerStatus, RealityCheckMarker
from src.shared.contract_versioning import load_contract

logger = logging.getLogger(__name__)


class _Lister(Protocol):
    def list_prefix(self, prefix: str) -> Iterable[str]: ...


def _assignment_prefix(database_name: str, job_id: str) -> str:
    return f"{database_name}/{job_id}/assignment/"


def assignment_artifact_path(database_name: str, job_id: str, version: int) -> str:
    """Return the artifact key for a specific assignment version."""
    return f"{_assignment_prefix(database_name, job_id)}v{version}/assignment.json"


def _existing_versions(store: _Lister, database_name: str, job_id: str) -> list[int]:
    """Return every assignment version number found under the prefix (unsorted).

    Parses the ``vN`` directory component of each listed key; ignores anything
    that is not a ``v<digits>`` segment (e.g. the legacy flat ``assignments.json``,
    which no writer produces but which may still exist in old jobs).
    """
    prefix = _assignment_prefix(database_name, job_id)
    versions: list[int] = []
    for key in store.list_prefix(prefix):
        parts = str(key).replace(prefix, "").split("/")
        if parts and parts[0].startswith("v"):
            try:
                versions.append(int(parts[0][1:]))
            except ValueError:
                continue
    return versions


def resolve_effective_assignment_version(store: _Lister, database_name: str, job_id: str) -> int:
    """Return the highest committed assignment version, or 0 when none exists.

    This is the one authoritative "which version is effective" rule (ADR-028).
    Returns 0 rather than raising when no versioned assignment is present; the
    ATX schema/synthesis path coerces that to 1 at its own call sites.
    """
    versions = _existing_versions(store, database_name, job_id)
    return max(versions) if versions else 0


def resolve_downstream_assignment_version(store: _Lister, database_name: str, job_id: str) -> int:
    """Return the effective assignment version for schema design and synthesis.

    Same rule as :func:`resolve_effective_assignment_version`, with the ADR-026
    "coerce to 1" policy applied: downstream phases always operate on at least
    the v1 the assessment core writes, so they never receive 0.
    """
    return resolve_effective_assignment_version(store, database_name, job_id) or 1


def synthesis_report_candidates(store: _Lister, database_name: str, job_id: str) -> list[str]:
    """Return synthesis report keys to try, most authoritative first.

    The synthesis writer produces ``synthesis/v<N>/report.json`` keyed on the
    effective assignment version, or the legacy ``referee-synthesis/report.json``
    when no versioned assignment exists. An unversioned ``synthesis/report.json``
    is kept last for old jobs. Readers take the first key that exists.
    """
    prefix = f"{database_name}/{job_id}"
    version = resolve_effective_assignment_version(store, database_name, job_id)
    keys: list[str] = []
    if version > 0:
        keys.append(f"{prefix}/synthesis/v{version}/report.json")
    keys.append(f"{prefix}/referee-synthesis/report.json")
    keys.append(f"{prefix}/synthesis/report.json")
    return keys


def next_assignment_version(store: _Lister, database_name: str, job_id: str) -> int:
    """Return the version number a new assignment write should use (highest + 1).

    Starts at 1 when no assignment exists, so a fresh job's first write is v1.
    """
    return resolve_effective_assignment_version(store, database_name, job_id) + 1


_REALITY_CHECK_SOURCE = "reality_check"
_CUSTOMER_GATE_SOURCE = "customer_gate"
# A post-schema reroute (#629) edits the approved version it descends from, the
# same way a customer edit does: Reality Check already ran for that lineage.
_POST_SCHEMA_ROUTING_SOURCE = "post_schema_routing"
# A saved assignment re-routed to one source of truth per table when loaded for
# approval (#992) edits the version it was loaded from, like a customer edit.
_SOURCE_OF_TRUTH_REROUTE_SOURCE = "source_of_truth_reroute"
# The decide-for-me rule's settlement of pending engine choices (#1003) edits the
# version it was applied to, like a customer edit, but nobody approved it.
_AUTO_DECIDED_SOURCE = "auto_decided"
_EDIT_SOURCES = frozenset(
    {
        _CUSTOMER_GATE_SOURCE,
        _POST_SCHEMA_ROUTING_SOURCE,
        _SOURCE_OF_TRUTH_REROUTE_SOURCE,
        _AUTO_DECIDED_SOURCE,
    }
)


def is_reality_check_produced(assignment: dict) -> bool:
    """Return True when an assignment artifact was written by Reality Check.

    The marker is the ADR-028 ``source`` provenance stamp: ``"reality_check"``
    means Reality Check wrote it; any other recorded source (assignment
    resolution, customer gate) means it did not. ``reality_check_applied`` alone
    is NOT a marker, because every override writer copies it forward from the
    consolidated version it edits.

    Legacy artifacts (written before ``source`` existed) fall back to the shape
    each writer produced then: the old Reality Check writer spread a model dump
    whose ``previous_version`` was always null and set ``reality_check_applied``,
    while every override writer always set ``previous_version`` to an int.
    """
    source = assignment.get("source")
    if source is not None:
        return bool(source == _REALITY_CHECK_SOURCE)
    return bool(assignment.get("reality_check_applied")) and (
        assignment.get("previous_version") is None
    )


def _is_override_edit(assignment: dict) -> bool:
    """Return True when an assignment version is an edit of an earlier version.

    Override writers (the customer gate, REST and ATX) stamp
    ``source = customer_gate``, and the post-schema router stamps
    ``source = post_schema_routing`` (#629); both always set
    ``previous_version`` to the version they edited. Legacy artifacts without ``source`` are recognised by that int
    ``previous_version``. A fresh assignment resolution is never an edit: it
    starts a new lineage.
    """
    source = assignment.get("source")
    if source is not None:
        return source in _EDIT_SOURCES
    return isinstance(assignment.get("previous_version"), int)


def _versions_newest_first(
    store: _ListReader, database_name: str, job_id: str
) -> Iterator[tuple[int, dict]]:
    """Yield ``(version, assignment)`` for each readable version, newest first."""
    for version in sorted(set(_existing_versions(store, database_name, job_id)), reverse=True):
        path = assignment_artifact_path(database_name, job_id, version)
        if store.exists(path):
            yield version, store.read_json(path)


def resolve_reality_check_input_version(store: _ListReader, database_name: str, job_id: str) -> int:
    """Return the assignment version Reality Check should consolidate (issue #189).

    Rule: the highest assignment version that Reality Check did not produce
    itself (see :func:`is_reality_check_produced`). On a fresh job that is the v1
    the assignment resolver wrote; after the assignment phase re-runs it is that
    newer resolution. Reality Check's own consolidations are never fed back into
    it. Callers that resolve the input should first check
    :func:`reality_check_is_current`: when Reality Check already ran for
    the current lineage (including a customer edit made after it), it must not
    run again.

    Its output always goes to :func:`next_assignment_version`, so an existing
    version is never overwritten.

    Returns 1 when no non-Reality-Check version exists (ADR-026 "coerce to 1"),
    so the caller's existing missing-``assignment/v1`` check produces the error.
    """
    for version, assignment in _versions_newest_first(store, database_name, job_id):
        if not is_reality_check_produced(assignment):
            return version
    return 1


class RealityCheckRun(NamedTuple):
    """A Reality Check run already recorded for the current lineage."""

    input_version: int
    """The assignment version that run consolidated."""
    output_version: int | None
    """The version it wrote, or None when it consolidated nothing."""


def reality_check_output_path(database_name: str, job_id: str, version: int | None = None) -> str:
    """Return the key of a reality-check output (#193).

    ``version`` None is ``reality-check/output.json``, the latest run's output (a
    preview while an external-mode run awaits its model). A version is
    ``reality-check/v<N>/output.json``, the completed run kept under the assignment
    version it stands for (:func:`reality_check_record_version`).
    """
    prefix = f"{database_name}/{job_id}/reality-check"
    return f"{prefix}/output.json" if version is None else f"{prefix}/v{version}/output.json"


def reality_check_record_version(output: Mapping) -> int | None:
    """Return the assignment version a completed reality-check output is kept under (#193).

    A run that consolidated is kept under the version it wrote
    (``output_assignment_version``), one that changed nothing under the version it
    checked (``source_assignment_version``). Versions are never overwritten, so two
    runs share a key only when both checked the same version and changed nothing.

    An output written before #192 has no ``output_assignment_version`` key; that
    writer always put a consolidation at ``source_assignment_version + 1``.
    """
    source = output.get("source_assignment_version")
    if not isinstance(source, int):
        return None
    if predates_reality_check_preview(output):
        return source + 1 if output.get("consolidations") else source
    wrote = output.get("output_assignment_version")
    return wrote if isinstance(wrote, int) else source


def predates_reality_check_preview(output: Mapping) -> bool:
    """Return True when a reality-check output was written before #192 (#193).

    Since #192 every output, a preview included, records ``output_assignment_version``
    (null for a preview, which writes no revision). The writer before #192 left the key
    out, wrote the revision with the output in every mode and never cleared the
    ``awaiting_llm`` marker, so such an output is a completed run whatever the marker
    says.
    """
    return "output_assignment_version" not in output


def _completed_reality_check_output(
    store: _Reader, database_name: str, job_id: str, *, marker_fails_open: bool = False
) -> dict:
    """Return ``reality-check/output.json`` if a completed run wrote it, else ``{}``.

    An external-mode run writes a deterministic preview of the output before the
    LLM response arrives, with ``reality-check/awaiting_llm.json`` at
    ``status = awaiting_llm`` until the finalize step completes. That preview does
    not count as a run.

    A marker at ``awaiting_llm`` next to an output with no
    ``output_assignment_version`` key is the exception (#193): that output was
    written before #192, whose writer wrote the revision with the output in every
    mode and never cleared the marker, so it is a completed run. A preview written
    since #192 always carries the key (null, because a preview never writes a
    revision), so it never reads as completed.

    A marker that cannot be read (invalid JSON, another MAJOR, no valid ``status``)
    raises. With ``marker_fails_open`` its state is unknown: it is logged and the job
    is treated as awaiting, so no ``output.json`` (possibly a preview) counts as a
    run, and the reality check, which rewrites both files itself, can run again
    (#1267).
    """
    output_key = reality_check_output_path(database_name, job_id)
    awaiting_key = f"{database_name}/{job_id}/reality-check/awaiting_llm.json"
    if store.exists(awaiting_key):
        try:
            marker = load_contract(
                RealityCheckMarker, store.read_json(awaiting_key), artifact=awaiting_key
            )
        except Exception as exc:
            if not marker_fails_open:
                raise
            logger.warning(
                "%s: unreadable reality-check marker, treated as awaiting (%s)",
                awaiting_key,
                type(exc).__name__,
            )
            return {}
        else:
            if marker.status is MarkerStatus.AWAITING_LLM:
                output = store.read_json(output_key) if store.exists(output_key) else {}
                if output and predates_reality_check_preview(output):
                    logger.info(
                        "%s: marker still says awaiting_llm, but %s was written before #192 "
                        "(no output_assignment_version), so it is read as a completed run",
                        awaiting_key,
                        output_key,
                    )
                    return output
                return {}
    return store.read_json(output_key) if store.exists(output_key) else {}


def _lineage(
    store: _Reader, database_name: str, job_id: str, start: int
) -> Iterator[tuple[int, dict]]:
    """Yield ``(version, assignment)`` from ``start`` back through override edits.

    Follows ``previous_version`` while the version is an edit
    (:func:`_is_override_edit`) and stops after the first version that is not (a
    Reality Check output or a fresh resolution). A corrupt lineage (a cycle, an edit
    with no ``previous_version``, or one naming a version that does not exist) ends
    the walk with a warning (#193), so callers treat it as "no run found": the
    reality check may then run, which writes a new version and never moves a
    customer-pinned query, so the corruption costs a recompute, never a routing.
    """
    seen: set[int] = set()
    version = start
    while True:
        path = assignment_artifact_path(database_name, job_id, version)
        if not store.exists(path):
            if seen:
                logger.warning(
                    "%s/%s: assignment lineage names v%d, which does not exist",
                    database_name,
                    job_id,
                    version,
                )
            return
        seen.add(version)
        assignment = store.read_json(path)
        yield version, assignment
        if not _is_override_edit(assignment):
            return
        previous = assignment.get("previous_version")
        if not isinstance(previous, int):
            logger.warning(
                "%s/%s: assignment v%d is an edit with no previous_version",
                database_name,
                job_id,
                version,
            )
            return
        if previous in seen:
            logger.warning(
                "%s/%s: assignment lineage has a cycle (v%d -> v%d)",
                database_name,
                job_id,
                version,
                previous,
            )
            return
        version = previous


def reality_check_run_for_lineage(
    store: _ListReader, database_name: str, job_id: str, *, marker_fails_open: bool = False
) -> RealityCheckRun | None:
    """Return the Reality Check run the effective assignment descends from, if any.

    Walks from the newest version back along ``previous_version`` through
    override edits (:func:`_is_override_edit`). The walk finds a run when it
    reaches:

    - a version Reality Check produced: that run consolidated its
      ``previous_version`` into it; or
    - the version a completed ``reality-check/output.json`` records as its
      ``source_assignment_version`` with no output version and no consolidation:
      that run found nothing to consolidate.

    It returns None when it reaches a version that is neither and is not an edit
    (a fresh assignment resolution, which starts a new lineage), or when the
    lineage is corrupt (logged, see :func:`_lineage`).

    The pipeline order is Reality Check -> customer review gate -> schema design
    (ADR-028), and ADR-029 re-entry reopens the gate, not Reality Check. So a
    customer edit that descends from a run was made *after* consolidation, on
    purpose. Consolidating it again would undo the customer's routing.

    ``marker_fails_open`` is for the reality check's own entry points, which rewrite
    ``awaiting_llm.json``: a marker that cannot be read counts as no marker there
    (see :func:`_completed_reality_check_output`). Every other reader validates.
    """
    newest = next(iter(_versions_newest_first(store, database_name, job_id)), None)
    if newest is None:
        return None
    rc_output = _completed_reality_check_output(
        store, database_name, job_id, marker_fails_open=marker_fails_open
    )
    rc_source = rc_output.get("source_assignment_version")
    rc_wrote = rc_output.get("output_assignment_version")

    for version, assignment in _lineage(store, database_name, job_id, newest[0]):
        if is_reality_check_produced(assignment):
            previous = assignment.get("previous_version")
            if not isinstance(previous, int):
                # Legacy Reality Check output: its input is the one the output records.
                previous = rc_source if isinstance(rc_source, int) else version - 1
            return RealityCheckRun(input_version=previous, output_version=version)
        # A run that changed nothing wrote no version and no consolidation (#193):
        # an output with consolidations but no version is not one.
        if (
            rc_output
            and version == rc_source
            and rc_wrote is None
            and not rc_output.get("consolidations")
        ):
            return RealityCheckRun(input_version=version, output_version=None)
    return None


def keep_completed_reality_check_output(
    store: _ListReadWriter, database_name: str, job_id: str
) -> None:
    """Keep the completed run in ``reality-check/output.json`` under its version (#193).

    Called before anything replaces ``output.json`` (a new run's output or an
    external-mode preview), so a forced run never loses the record of the run
    before it. Every completed run is written to its versioned key as well; this
    copies an output written before that existed. A preview is never kept, and an
    existing versioned record is never overwritten.
    """
    previous = _completed_reality_check_output(store, database_name, job_id, marker_fails_open=True)
    version = reality_check_record_version(previous) if previous else None
    if version is None:
        return
    key = reality_check_output_path(database_name, job_id, version)
    if not store.exists(key):
        store.write_json(key, previous)


def reality_check_output_candidates(
    store: _ListReader, database_name: str, job_id: str, assignment_version: int | None = None
) -> list[str]:
    """Return the reality-check output keys for an assignment, most authoritative first.

    A deliverable shows the record of the run the assignment descends from (#193),
    and none from another lineage (#215). Walking back from ``assignment_version``
    through override edits (:func:`_lineage`), the first version Reality Check
    produced has its record at ``reality-check/v<N>/output.json``; with none, a run
    that checked a version on the walk and changed nothing has its record under that
    version.

    ``reality-check/output.json`` (the latest run) follows, for jobs written before
    versioned records, only when it belongs to the walk: its
    :func:`reality_check_record_version` is a version the walk reached. An output
    written before #192 records no ``output_assignment_version``, so it cannot be
    checked and is used as before, as it is when there is no versioned assignment
    (``assignment_version`` 0). An empty list means no reality check belongs to this
    assignment: readers then rely on the assignment alone. ``assignment_version``
    defaults to the effective version. Readers take the first key that exists.

    The flat file is not checked against ``awaiting_llm.json``: while an external run
    awaits its model, its preview can be read here, as it always could. No phase reads
    a reality-check output while one is pending.
    """
    flat = reality_check_output_path(database_name, job_id)
    if assignment_version is None:
        assignment_version = resolve_effective_assignment_version(store, database_name, job_id)
    if assignment_version <= 0:
        return [flat]
    keys: list[str] = []
    walked: set[int] = set()
    for version, assignment in _lineage(store, database_name, job_id, assignment_version):
        walked.add(version)
        key = reality_check_output_path(database_name, job_id, version)
        if is_reality_check_produced(assignment):
            keys = [key]
            break
        if not keys and store.exists(key):
            keys = [key]
    latest = store.read_json(flat) if store.exists(flat) else None
    if isinstance(latest, dict) and (
        predates_reality_check_preview(latest) or reality_check_record_version(latest) in walked
    ):
        keys.append(flat)
    return keys


def read_reality_check_output(
    store: _ListReader, database_name: str, job_id: str, assignment_version: int | None = None
) -> dict | None:
    """Return the reality-check output for an assignment (#193), or None when none exists.

    The first existing key of :func:`reality_check_output_candidates`.
    """
    for key in reality_check_output_candidates(store, database_name, job_id, assignment_version):
        if store.exists(key):
            output = store.read_json(key)
            return output if isinstance(output, dict) else None
    return None


def reality_check_is_current(store: _ListReader, database_name: str, job_id: str) -> bool:
    """Return True when Reality Check already ran for the current lineage.

    Reality Check runs once per lineage. True when the newest version is a
    Reality Check output, a customer edit that descends from one, or a version
    (or customer edit of a version) a completed run checked and left unchanged
    (:func:`reality_check_run_for_lineage`). A run that resolves its own input is
    then a no-op: re-consolidating would either duplicate the existing
    consolidation (advancing the effective version and marking every schema
    output stale, :func:`stale_schema_versions`) or undo the customer's edits.

    False on a fresh job, after the assignment phase re-resolves (a new
    lineage), while an external-mode run awaits its finalize step, or when
    nothing exists.
    """
    return reality_check_run_for_lineage(store, database_name, job_id) is not None


def stale_schema_versions(store: _Lister, database_name: str, job_id: str) -> dict[str, int]:
    """Return ``{engine: schema_version}`` for schema outputs behind the effective
    assignment (ADR-028 staleness).

    Each schema output lives at ``schema-<engine>/v<N>/schema_output.json`` where
    ``N`` is the assignment version it was built from. When the customer edits the
    routing at the review gate (or a future re-entry changes it), the effective
    assignment version advances; any engine whose newest schema output is older
    than that was designed against stale routing and must be re-run.

    Returns the newest schema version per stale engine (``< effective``). Empty
    when there is no assignment yet (nothing to be stale against) or no versioned
    schema output. Legacy unversioned outputs (``schema-<engine>/schema_output.json``)
    are ignored — they predate assignment versioning.

    This is the primitive the re-entry flow uses to re-dispatch only the affected
    engines instead of the whole pipeline; nothing consumes it yet.
    """
    effective = resolve_effective_assignment_version(store, database_name, job_id)
    if effective == 0:
        return {}

    prefix = f"{database_name}/{job_id}/"
    latest: dict[str, int] = {}
    for key in store.list_prefix(prefix):
        parts = str(key).replace(prefix, "").split("/")
        if (
            len(parts) == 3
            and parts[0].startswith("schema-")
            and parts[1].startswith("v")
            and parts[2] == "schema_output.json"
        ):
            engine = parts[0][len("schema-") :]
            try:
                version = int(parts[1][1:])
            except ValueError:
                continue
            latest[engine] = max(latest.get(engine, 0), version)

    return {engine: version for engine, version in latest.items() if version < effective}


class _Reader(Protocol):
    def read_json(self, path: str) -> dict: ...
    def exists(self, path: str) -> bool: ...


class _ListReader(_Lister, _Reader, Protocol):
    """A store that can both list keys and read artifacts."""


class _ListReadWriter(_ListReader, Protocol):
    """A store that can list, read and write artifacts."""

    def write_json(self, path: str, data: dict) -> None: ...


def read_assignment(store: _Reader, database_name: str, job_id: str, version: int) -> dict | None:
    """Return the assignment artifact for ``version``, or None when it is absent.

    ``version`` 0 (legacy, unversioned callers) has no artifact and returns None.
    """
    if version <= 0:
        return None
    path = assignment_artifact_path(database_name, job_id, version)
    if not store.exists(path):
        return None
    assignment = store.read_json(path)
    return assignment if isinstance(assignment, dict) else None


class EngineScope(NamedTuple):
    """What one assignment puts in scope for one engine."""

    query_ids: set[str]
    """In-scope query IDs assigned to the engine."""
    source_tables: set[str]
    """Source tables those queries access, exactly as the assignment spells them."""


def engine_scope(assignment: dict, engine: str) -> EngineScope:
    """Return the in-scope query IDs and source tables ``assignment`` gives ``engine``.

    This is the same scope the schema design input is filtered to: a table is in
    an engine's scope when at least one in-scope query assigned to that engine
    accesses it. A cache engine owns no query: its scope is the in-scope queries
    it fronts (``cache_engine``, the cache overlay of #296), so its design is the
    cache-aside keys for those reads.
    """
    query_ids: set[str] = set()
    tables: set[str] = set()
    for qa in assignment.get("query_assignments") or []:
        if not isinstance(qa, dict):
            continue
        if engine not in (qa.get("assigned_engine"), qa.get("cache_engine")):
            continue
        if not qa.get("in_scope", True):
            continue
        if qa.get("query_id") is not None:
            query_ids.add(str(qa["query_id"]))
        source_tables = qa.get("source_tables")
        if isinstance(source_tables, list):
            tables.update(t for t in source_tables if isinstance(t, str))
    return EngineScope(query_ids, tables)


def _in_scope_engine_query_sets(
    store: _Reader, database_name: str, job_id: str, version: int
) -> dict[str, set[str]]:
    """Return ``engine -> {in-scope query_id, ...}`` for one assignment version.

    A cache engine's set is the queries it fronts (``cache_engine``, #296).
    Empty when the version's artifact is absent.
    """
    path = assignment_artifact_path(database_name, job_id, version)
    if not store.exists(path):
        return {}
    assignment = store.read_json(path)
    result: dict[str, set[str]] = {}
    for qa in assignment.get("query_assignments", []):
        if not qa.get("in_scope", True):
            continue
        qid = qa.get("query_id")
        for engine in (qa.get("assigned_engine"), qa.get("cache_engine")):
            if engine and qid:
                result.setdefault(engine, set()).add(qid)
    return result


def engines_with_in_scope_queries(
    store: _Reader, database_name: str, job_id: str, version: int
) -> set[str]:
    """Return the engines with at least one in-scope query in one assignment version.

    Pass the effective version so engines Reality Check consolidated away (or a
    customer re-routed to zero) drop out. Empty when the version's artifact is
    absent; callers treat that as "unknown" and keep their prior engine list.
    """
    return set(_in_scope_engine_query_sets(store, database_name, job_id, version))


def assignment_engine_diff(
    store: _Reader,
    database_name: str,
    job_id: str,
    prev_version: int,
    new_version: int,
) -> dict[str, list[str]]:
    """Classify the NEW assignment's engines against the previous one (ADR-029 A).

    Returns ``{"affected": [...], "unaffected": [...]}`` (both sorted):

    - **affected** — an engine in ``new_version`` whose in-scope query set differs
      from ``prev_version`` (a query arrived, left, or its scope changed) or that is
      new. Its schema must be re-designed.
    - **unaffected** — an engine in ``new_version`` whose in-scope query set is
      identical to ``prev_version``. Its schema is byte-identical, so it can be
      copied forward instead of re-run.

    Engines that dropped to zero in-scope queries in ``new_version`` appear in
    neither list: they are eliminated and their schema is not carried forward.
    """
    prev = _in_scope_engine_query_sets(store, database_name, job_id, prev_version)
    new = _in_scope_engine_query_sets(store, database_name, job_id, new_version)
    affected = sorted(engine for engine, qids in new.items() if qids != prev.get(engine, set()))
    affected_set = set(affected)
    unaffected = sorted(engine for engine in new if engine not in affected_set)
    return {"affected": affected, "unaffected": unaffected}
