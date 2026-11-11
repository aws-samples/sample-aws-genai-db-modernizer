# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The bounded redesign loop for held waves (#1992, decision 4 on #1987).

After synthesis, a wave the deterministic checks hold for a reason a redesign can fix
goes back to that engine's schema design with the failed checks as its input, and is
checked again by the same rules, for at most :data:`MAX_REDESIGN_ROUNDS` rounds. The
engine stays: routing never changes (no assignment version is written, no query moves).

**What a redesign can fix** (:func:`fixable_requests`), on an engine that has a design:
access patterns its design leaves unserved, assigned queries its design marks
unsupported, an open review that asked for changes, and an open high or critical risk
that a design can resolve: a ``design.hot_partition`` risk, or an analysis flag on an
engine whose design resolves analysis flags, by the same rule synthesis resolves them
with (``synthesis_report.design_resolves_analysis_flags``: never Aurora). Everything else
is reported at once with its next step: a longer collection, a plan costlier than the
source, a wave 1 not suggested, a wave not designed yet.

**One round**, per engine sent back:

1. its design moves to ``wave-redesign/v<N>/round_<k>/`` (designers reuse a design that
   exists, so the next one starts fresh, and the round keeps the one it replaced): the
   whole engine, or, with ``parts=True`` and a DynamoDB design in parts (its groups),
   only the parts a check names (#2119): the parts holding a check's queries, the parts
   whose last review held the design (an ``open_review`` hold names them,
   :func:`_name_review_parts`), and every part sharing a placement group
   (``write_groups.placement_groups``: a co-dependency group, a write group or a
   routine unit) with one of those. The split keeps each such group in one part unless
   its manifest records the cut (``unit_cuts``, #2141), so this adds a part only there.
   One check that names no query keeps the whole engine. ``redesign_request.json``
   records the failed checks, which the design input carries (:func:`redesign_input`,
   risk text framed; in a parts round only the parts redesigned get it), and, in a
   parts round, the parts kept with the digests of their draft, review and input
   (:func:`kept_parts_at`): ``--split`` marks those ``carried_from`` (their own index),
   so no designer and no reviewer is asked for them again;
2. the engine (or its parts) is designed and reviewed again, then synthesis runs again;
3. :func:`plan_round` closes the round. The new design is kept only when it exists, its
   review ran (every engine's design is reviewed in both modes, #2081; in a parts round,
   every part it redesigned has its own review) and it is
   **strictly better**: fewer holds on the engine's waves or fewer assigned queries it
   marks unsupported, and more of neither. Otherwise the earlier design is put back
   (``did_not_improve``, ``not_reviewed``, or ``failed`` when no design was written) and
   synthesis runs once more on it. A design that failed to run is put back at once
   (:func:`fail_round`).
   ``fixed`` means the kept design is reviewed and no redesign-fixable hold is left;
   ``improved`` means it is kept and still held.

An engine whose round failed or did not improve is not sent back again. A round never
stays open at the end of a run (#2122): a run that goes on to its report without
running it closes it with :func:`stop_round` (``stopped``, ``stopped_by`` ``closed``,
the earlier design put back), which ends the loop, and the in-process loop does the
same when anything but a design stops it. The log is per
assignment version (``wave-redesign/v<N>/rounds.json``); synthesis copies it into
``report.json`` (``wave_redesign``), and a round closed after that synthesis is written
back into the report, so the two always agree.

Why two rounds: the review loop a design goes through stops after two
(``MAX_PE_ITERATIONS``), as does the post-schema reroute cascade; a check that fails on
the same design twice is one the designer cannot fix from the same input, and every
round costs one more design and review of the engine and one more synthesis.

Spend (Bedrock and ``assess()``): a round starts only when the run's ceilings
(``MODERNIZER_MAX_RUN_*``) leave room for one more schema design phase and synthesis
(:func:`ledger_round_budget`). Claude Code's model calls are not counted in the process
(``run_limits``), so ``/modernize`` is bounded by the round limit only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.agents.prompt_framing import frame_untrusted
from src.shared.engine_names import display_engine
from src.shared.report_facts import risk_engine_and_body
from src.shared.wave_readiness import redesign_facts as redesign_facts
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: At most this many redesign rounds per assignment version (see the docstring).
MAX_REDESIGN_ROUNDS = 2

#: The holds a redesign of the wave's engine can clear, whatever their source.
DESIGN_CHECKS = frozenset({"unserved_patterns", "unsupported_queries", "open_review"})
#: The risk rules a redesign can resolve (an analysis flag is decided per engine).
FIXABLE_RISK_RULES = frozenset({"design.hot_partition"})

#: At most this many query ids per failed check go back to the designer.
MAX_QUERY_IDS_PER_CHECK = 50


def _in_parts(engine: str) -> bool:
    """Whether a round can redesign ``engine`` in parts (#2119): an engine designed and
    reviewed per part (``pe_review_external``'s rule); every other one is whole."""
    from src.agents.schema_design.pe_review_external import _grouped

    return _grouped(engine)


FIXED, IMPROVED, DID_NOT_IMPROVE, FAILED, STOPPED, NOT_REVIEWED = (
    "fixed",
    "improved",
    "did_not_improve",
    "failed",
    "stopped",
    "not_reviewed",
)
#: An engine whose last round ended so is not sent back again.
_FINAL = frozenset({DID_NOT_IMPROVE, FAILED, STOPPED, NOT_REVIEWED})

#: The status lines the loop ends on when it ran as designed (#2085): nothing a redesign
#: can fix is held (``complete``), rounds ran and a wave stays held (``exhausted``), or a
#: run ceiling stopped the next round (``stopped``, with its ``reason``). A design that
#: raised ends it ``failed`` instead.
COMPLETE, EXHAUSTED = "complete", "exhausted"
LOOP_ENDINGS = frozenset({COMPLETE, EXHAUSTED, STOPPED})
#: The status line of a run with no in-process design (no model): no round, with its
#: ``reason``.
SKIPPED = "skipped"
#: The status line of a round whose redesign's review is still to come (#2081, a
#: headless run): review it, synthesize, then check again; the round stays open.
AWAITING_REVIEW = "awaiting_review"
#: The status lines a caller continues from, not endings of the loop.
LOOP_STEPS = frozenset({"awaiting_synthesis", "awaiting_redesign", AWAITING_REVIEW})
#: How a ``stopped`` round stopped (#2122, the log's ``stopped_by``): a run ceiling left no
#: room before it started, or the run closed it with its design never checked
#: (:func:`stop_round`), which also ends the loop. A stopped round saved before the field
#: was a ceiling's.
CEILING, CLOSED = "ceiling", "closed"
#: :func:`stop_round`'s status line when no round is open: nothing to close.
NO_OPEN_ROUND = "no_open_round"


def log_key(db: str, job_id: str, version: int) -> str:
    return f"{db}/{job_id}/wave-redesign/v{version}/rounds.json"


def request_key(db: str, job_id: str, engine: str, version: int) -> str:
    return f"{db}/{job_id}/schema-{engine}/v{version}/redesign_request.json"


def _design_prefix(db: str, job_id: str, engine: str, version: int) -> str:
    return f"{db}/{job_id}/schema-{engine}/v{version}/"


def _archive_prefix(db: str, job_id: str, version: int, round_n: int, engine: str) -> str:
    return f"{db}/{job_id}/wave-redesign/v{version}/round_{round_n}/schema-{engine}/"


def read_log(store: ArtifactStore, db: str, job_id: str, version: int) -> dict[str, Any]:
    """The rounds run so far on ``version``; an empty log when none ran."""
    key = log_key(db, job_id, version)
    try:
        log = store.read_json(key) if store.exists(key) else None
    except (OSError, ValueError):
        log = None
    if not isinstance(log, dict):
        return {"max_rounds": MAX_REDESIGN_ROUNDS, "rounds": []}
    log.setdefault("max_rounds", MAX_REDESIGN_ROUNDS)
    log.setdefault("rounds", [])
    return log


def _designs(report: Mapping[str, Any]) -> Mapping[str, Any]:
    designs = report.get("schema_designs")
    return designs if isinstance(designs, Mapping) else {}


def _designed(report: Mapping[str, Any]) -> set[str]:
    return {
        str(e)
        for e, d in _designs(report).items()
        if isinstance(d, Mapping) and d.get("status") == "completed"
    }


def _hold_fixable(hold: Mapping[str, Any], risk: Mapping[str, Any]) -> bool:
    check = hold.get("check")
    if check in DESIGN_CHECKS:
        return True
    if check != "open_high_risk":
        return False
    from src.agents.referee.synthesis_report import design_resolves_analysis_flags

    rule = risk.get("rule")
    if rule:
        return str(rule) in FIXABLE_RISK_RULES
    return design_resolves_analysis_flags(str(hold.get("engine") or ""))


def fixable_requests(
    report: Mapping[str, Any],
    exclude: set[str] | frozenset[str] = frozenset(),
    waves: set[int] | frozenset[int] | None = None,
    *,
    max_query_ids: int | None = MAX_QUERY_IDS_PER_CHECK,
) -> dict[str, dict[str, Any]]:
    """Per designed engine (outside ``exclude``), the holds on its waves a redesign can
    fix, as one request: the waves, and each failed check with its risk's text and
    queries (at most ``max_query_ids`` of them, the ones the designer is shown; None
    keeps every one, which a parts round picks its parts from, #2119). ``waves``: only
    the holds of those waves (``/wave-edit``, #2067)."""
    facts = report.get("facts") or {}
    risks = {
        r.get("risk_id"): r
        for r in ((report.get("risk_assessment") or {}).get("risks") or [])
        if isinstance(r, Mapping) and r.get("risk_id")
    }
    designed = _designed(report) - set(exclude)
    out: dict[str, dict[str, Any]] = {}
    for row in facts.get("waves") or []:
        if not isinstance(row, Mapping) or row.get("readiness") != "held":
            continue
        if waves is not None and int(row.get("wave") or 0) not in waves:
            continue
        for hold in row.get("holds") or []:
            engine = hold.get("engine") if isinstance(hold, Mapping) else None
            risk = risks.get(hold.get("risk_id")) or {}
            if engine not in designed or not _hold_fixable(hold, risk):
                continue
            query_ids = [str(q) for q in risk.get("query_ids") or []]
            if hold.get("check") == "unsupported_queries":
                query_ids = _unsupported_ids(report, str(engine))
            entry = out.setdefault(str(engine), {"engine": str(engine), "waves": [], "checks": []})
            if int(row.get("wave") or 0) not in entry["waves"]:
                entry["waves"].append(int(row.get("wave") or 0))
            entry["checks"].append(
                {
                    "check": hold["check"],
                    "risk_id": hold.get("risk_id"),
                    "severity": hold.get("severity"),
                    "line": hold.get("line"),
                    # The risk text without its "[engine]" attribution tag, which the
                    # deck parses from the description: the request reaches report.json,
                    # where a raw engine id is internal wording (#2107).
                    "detail": risk_engine_and_body(risk.get("description"))[1] or None,
                    "mitigation": risk.get("mitigation"),
                    "query_ids": query_ids[:max_query_ids],
                }
            )
    return out


def _unsupported_ids(report: Mapping[str, Any], engine: str) -> list[str]:
    """The query ids the engine's design marks unsupported, as synthesis lists them."""
    from src.shared.unsupported_pattern import unsupported_pattern_ids

    design = _designs(report).get(engine) or {}
    return sorted(
        {
            str(q)
            for e in design.get("unsupported_patterns") or []
            if isinstance(e, Mapping)
            for q in unsupported_pattern_ids(dict(e))
        }
    )


def engine_score(report: Mapping[str, Any], engine: str) -> list[int]:
    """``[holds on the engine's waves raised for it, assigned queries its design marks
    unsupported]``: a round's design is kept only when this is strictly lower."""
    holds = sum(
        1
        for row in (report.get("facts") or {}).get("waves") or []
        if isinstance(row, Mapping) and engine in (row.get("engines") or [])
        for h in row.get("holds") or []
        if isinstance(h, Mapping) and h.get("engine") == engine
    )
    design = _designs(report).get(engine) or {}
    return [holds, int(design.get("unsupported_query_count") or 0)]


#: The check a ``/wave-edit`` round sends a design back with for each review warning the
#: customer did not accept (#2067).
REJECTED_WARNING = "rejected_warning"


def review_warnings_of(report: Mapping[str, Any], engine: str) -> list[dict[str, Any]]:
    """Every warning of the engine's last design review (or of each design group's), each
    with its engine, as synthesis lists them: a review with a blocker included
    (``all_review_warnings``; ``review_warnings`` for a report written before it)."""
    design = _designs(report).get(engine) or {}
    listed = design.get("all_review_warnings")
    if not isinstance(listed, list):
        listed = design.get("review_warnings") or []
    return [{**dict(w), "engine": engine} for w in listed if isinstance(w, Mapping)]


def _review_ran(report: Mapping[str, Any], engine: str) -> bool:
    """The engine's design has a recorded review verdict, and no review of it failed."""
    d = _designs(report).get(engine) or {}
    return int(d.get("reviewed_count") or 0) > 0 and not int(d.get("failed_review_count") or 0)


def _round_score(report: Mapping[str, Any], engine: str, before: list[int]) -> list[int]:
    """The engine's score after the round, as long as its score before. A wave edit's
    score adds how many warnings the new review lists on the engine: never matched to
    the warnings the customer rejected (a review rewords them), only counted."""
    score = engine_score(report, engine)
    if len(before) == 3:
        score.append(len(review_warnings_of(report, engine)))
    return score


def _strictly_better(after: list[int], before: list[int]) -> bool:
    return all(a <= b for a, b in zip(after, before, strict=True)) and after != before


def redesign_input(
    store: ArtifactStore, db: str, job_id: str, engine: str, version: int
) -> dict[str, Any] | None:
    """What the engine's next design is given for this round, or None outside a round:
    the round, the instructions (the check lines are this tool's own words; a risk's
    text is framed as untrusted data) and each failed check's ids."""
    key = request_key(db, job_id, engine, version)
    if not store.exists(key):
        return None
    try:
        request = store.read_json(key)
    except (OSError, ValueError):
        return None
    checks = [c for c in request.get("checks") or [] if isinstance(c, Mapping)]
    if request.get("wave_edit") is not None:
        # #2067: the customer revisits one wave; the rejected warnings and the wave's
        # blockers come back together.
        lines = [
            f"Wave edit of wave {request.get('wave_edit')}, round {request.get('round')} of "
            f"at most {request.get('max_rounds')}: the customer asked to revisit the "
            f"{display_engine(engine)} design. Revise it so that each item below is resolved: "
            "a blocker passes its check, and the review no longer raises a warning the "
            "customer does not accept. The engine and the queries it serves stay as they "
            "are: routing does not change, and every access pattern stays served."
        ]
    else:
        lines = [
            f"Round {request.get('round')} of at most {request.get('max_rounds')}: the previous "
            f"{display_engine(engine)} design failed these deterministic checks. Revise the "
            "design so that each one passes. The engine and the queries it serves stay as they "
            "are: routing does not change, and every access pattern stays served."
        ]
    for c in checks:
        lines.append(f"- {c.get('line')}")
        text = " ".join(str(c.get(k)) for k in ("detail", "mitigation") if c.get(k))
        if text:
            label = (
                "the design review warning, in the reviewer's words"
                if c.get("check") == REJECTED_WARNING
                else "the failed check's risk text"
            )
            lines.append("  " + frame_untrusted(text, label=label))
        if c.get("query_ids"):
            lines.append("  Queries: " + ", ".join(str(q) for q in c["query_ids"]))
    return {
        "round": request.get("round"),
        "max_rounds": request.get("max_rounds"),
        "instructions": "\n".join(lines),
        "checks": [
            {k: c.get(k) for k in ("check", "risk_id", "severity", "line", "query_ids")}
            for c in checks
        ],
    }


# ---------------------------------------------------------------------------
# Moving designs aside and back
# ---------------------------------------------------------------------------


def _move(store: ArtifactStore, keys: list[str], src: str, dst: str) -> None:
    for key in keys:
        store.write_bytes(dst + key[len(src) :], store.read_bytes(key))
        store.delete(key)


#: A post-schema reroute's record of its carried parts (``carry_forward``, #1650).
_CARRIED_FORWARD = "carried_forward.json"

#: The merged design files a parts round moves with the parts it redesigns.
_MERGED = ("schema_output.json", "design_trace.json", "merge_record.json")


def _part_files(index: int) -> tuple[str, ...]:
    return (f"schema_draft_group_{index}.json", f"design_trace_group_{index}.json")


def _part_ids(store: ArtifactStore, prefix: str) -> dict[int, list[str]] | None:
    """``{group index: every statement its input lists}`` (a query pattern's members
    included) for a design in parts; None for one not in parts or a part whose input
    cannot be read."""
    from src.agents.schema_design.pattern_input import input_query_ids

    manifest_key = prefix + "groups_manifest.json"
    if not store.exists(manifest_key):
        return None
    parts: dict[int, list[str]] = {}
    try:
        groups = store.read_json(manifest_key).get("groups") or []
        for group in groups:
            data = store.read_json(prefix + str(group.get("input_file") or ""))
            idx = int(group.get("group_index") or 0)
            parts[idx] = input_query_ids(data.get("collector_output") or {})
    except (OSError, ValueError, AttributeError):
        return None
    return parts or None


def _affected_parts(
    request: Mapping[str, Any], part_ids: Mapping[int, list[str]]
) -> set[int] | None:
    """The parts the request's checks name: those a check names itself (``parts``, a
    review hold) and those holding a check's queries. None (the whole engine) when a
    check names neither, or no part holds its queries."""
    affected: set[int] = set()
    checks = [c for c in request.get("checks") or [] if isinstance(c, Mapping)]
    for check in checks:
        named = {int(p) for p in check.get("parts") or [] if int(p) in part_ids}
        ids = {str(q) for q in check.get("query_ids") or []}
        held = {i for i, own in part_ids.items() if ids.intersection(own)}
        if not (named | held):
            return None
        affected |= named | held
    return affected or None


def _with_placement_groups(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    part_ids: Mapping[int, list[str]],
    affected: set[int],
) -> list[int]:
    """``affected`` with every part that shares a placement group (a co-dependency
    group, a write group or a routine unit, ``write_groups.placement_groups``) with one
    of its parts, until none is left. The split keeps a group's statements in one part
    unless the group alone is too large for one and the manifest records the cut
    (``unit_cuts``, #2141): only then do they sit in two parts, which are redesigned
    together. A split written before #2141 can cut a group unrecorded; this reads the
    parts' own statements, so it covers that too."""
    from src.agents.referee.write_groups import assignment_placement_groups
    from src.storage.assignment_versioning import assignment_artifact_path
    from src.storage.collector_for_models import read_collector_for_models

    key = assignment_artifact_path(db, job_id, version)
    try:
        assignment = store.read_json(key) if store.exists(key) else None
        collector = read_collector_for_models(store, db, job_id, missing_ok=True)
    except (OSError, ValueError):
        return sorted(affected)
    if not isinstance(assignment, Mapping):
        return sorted(affected)
    part_of = {q: i for i, own in part_ids.items() for q in own}
    units = [
        {part_of[q] for q in unit if q in part_of}
        for unit in assignment_placement_groups(assignment, collector)
    ]
    grown = set(affected)
    changed = True
    while changed:
        changed = False
        for parts in units:
            if parts & grown and not parts <= grown:
                grown |= parts
                changed = True
    return sorted(grown)


def _held_review_parts(store: ArtifactStore, prefix: str, parts: Iterable[int]) -> list[int]:
    """The parts whose own last design review holds the design (a blocker, the rule
    ``wave_readiness.open_review_count`` counts the ``open_review`` hold with)."""
    from src.shared.wave_readiness import open_review_count

    held: list[int] = []
    for idx in parts:
        try:
            trace = store.read_json(prefix + _part_files(idx)[1])
        except (OSError, ValueError):
            continue
        if open_review_count(trace if isinstance(trace, Mapping) else None):
            held.append(idx)
    return held


def _name_review_parts(
    store: ArtifactStore, db: str, job_id: str, version: int, request: Mapping[str, Any]
) -> dict[str, Any]:
    """The request with each ``open_review`` check naming the parts whose last review
    held the design (``parts``, group indexes) and those parts' statements
    (``query_ids``): the hold counts the reviews that did not approve a design, so it
    names no query of its own (#2119). A design not in parts, or a check that already
    names queries, is left as it is."""
    engine = str(request.get("engine") or "")
    prefix = _design_prefix(db, job_id, engine, version)
    part_ids = _part_ids(store, prefix) if _in_parts(engine) else None
    if not part_ids:
        return dict(request)
    checks: list[dict[str, Any]] = []
    for check in request.get("checks") or []:
        check = dict(check)
        if check.get("check") == "open_review" and not check.get("query_ids"):
            held = _held_review_parts(store, prefix, sorted(part_ids))
            if held:
                ids = list(dict.fromkeys(q for i in held for q in part_ids[i]))
                check["parts"] = held
                check["query_ids"] = ids[:MAX_QUERY_IDS_PER_CHECK]
        checks.append(check)
    return {**request, "checks": checks}


def _with_every_query(request: Mapping[str, Any], uncapped: Mapping[str, Any]) -> dict[str, Any]:
    """``request`` with each check's ``query_ids`` taken from the same check of
    ``uncapped`` (:func:`fixable_requests` with no cap, built from the same holds in the
    same order); a check named by its parts (a review hold) keeps its own."""
    full = [c for c in uncapped.get("checks") or [] if isinstance(c, Mapping)]
    checks: list[dict[str, Any]] = []
    for n, check in enumerate(request.get("checks") or []):
        check = dict(check)
        other = full[n] if n < len(full) else None
        if (
            other is not None
            and other.get("check") == check.get("check")
            and not check.get("parts")
        ):
            check["query_ids"] = list(other.get("query_ids") or [])
        checks.append(check)
    return {**request, "checks": checks}


def _set_aside(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    round_n: int,
    engine: str,
    request: Mapping[str, Any],
    parts: bool,
) -> list[int] | None:
    src = _design_prefix(db, job_id, engine, version)
    dst = _archive_prefix(db, job_id, version, round_n, engine)
    keys = store.list_prefix(src)
    affected: list[int] | None = None
    part_ids = _part_ids(store, src) if parts and _in_parts(engine) else None
    named = _affected_parts(request, part_ids) if part_ids else None
    if part_ids and named:
        affected = _with_placement_groups(store, db, job_id, version, part_ids, named)
    if affected is not None:
        names = {*_MERGED, *(f for i in affected for f in _part_files(i))}
        keys = [k for k in keys if k[len(src) :] in names]
        # A post-schema reroute's record of the parts it carried (#1650) stays in place,
        # and a copy goes with the round: the round's --split gives a carried part it
        # redesigns another input and so records its copy dropped, and putting the
        # round back restores the record with the draft, so that part reads carried
        # again rather than missing (#2119 review).
        record = src + _CARRIED_FORWARD
        if store.exists(record):
            store.write_bytes(dst + _CARRIED_FORWARD, store.read_bytes(record))
    _move(store, keys, src, dst)
    return affected


def _put_back(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    round_n: int,
    engine: str,
    parts: list[int] | None = None,
) -> None:
    """The design the round set aside, back in place of whatever the round wrote: for a
    whole-engine round (``parts`` None) every file the round wrote goes; for a parts
    round, the parts it redesigned (a draft or a review the round wrote for a part that
    had none before included) and the merged design."""
    src = _design_prefix(db, job_id, engine, version)
    dst = _archive_prefix(db, job_id, version, round_n, engine)
    kept = store.list_prefix(dst)
    names = {k[len(dst) :] for k in kept}
    if parts is not None:
        names |= {*_MERGED, *(f for i in parts for f in _part_files(i))}
    for key in store.list_prefix(src):
        if parts is None or key[len(src) :] in names or key.endswith("redesign_request.json"):
            store.delete(key)
    for key in kept:
        store.write_bytes(src + key[len(dst) :], store.read_bytes(key))


def _sha256(store: ArtifactStore, key: str) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(store.read_bytes(key)).hexdigest() if store.exists(key) else None
    except (OSError, ValueError):
        return None


def _input_digest(store: ArtifactStore, key: str) -> str | None:
    """A part's input without the round's ``redesign_request``: what its designer was
    given, whichever round wrote the file."""
    import hashlib
    import json

    try:
        data = store.read_json(key) if store.exists(key) else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    view = {k: v for k, v in data.items() if k != "redesign_request"}
    return hashlib.sha256(json.dumps(view, sort_keys=True, default=str).encode()).hexdigest()


def _kept_record(
    store: ArtifactStore, prefix: str, affected: list[int] | None
) -> list[dict[str, Any]] | None:
    """For a parts round, every part it keeps with a draft, and the digests of that
    draft, its design trace (with its review) and its input; None for a whole-engine
    round."""
    if affected is None:
        return None
    try:
        groups = store.read_json(prefix + "groups_manifest.json").get("groups") or []
    except (OSError, ValueError, AttributeError):
        return []
    kept: list[dict[str, Any]] = []
    for group in groups:
        idx = int(group.get("group_index") or 0)
        draft = _sha256(store, prefix + _part_files(idx)[0])
        if idx in affected or draft is None:
            continue
        kept.append(
            {
                "part": idx,
                "draft_sha256": draft,
                "trace_sha256": _sha256(store, prefix + _part_files(idx)[1]),
                "input_sha256": _input_digest(store, f"{prefix}input_group_{idx}.json"),
            }
        )
    return kept


def _request_at(store: ArtifactStore, base_key: str) -> Mapping[str, Any]:
    key = f"{base_key.rstrip('/')}/redesign_request.json"
    try:
        request = store.read_json(key) if store.exists(key) else None
    except (OSError, ValueError):
        request = None
    return request if isinstance(request, Mapping) else {}


def redesign_parts(
    store: ArtifactStore, db: str, job_id: str, engine: str, version: int
) -> list[int] | None:
    """The parts the running round designs again, or None (the whole engine, or no
    round): only those parts' inputs carry the round's ``redesign_request``."""
    parts = _request_at(store, _design_prefix(db, job_id, engine, version)).get("parts")
    return [int(p) for p in parts] if isinstance(parts, list) else None


def kept_parts_at(store: ArtifactStore, base_key: str) -> dict[int, int]:
    """``{part: part}`` for the parts of the design at ``base_key``
    (``<db>/<job>/schema-<engine>/v<N>``) a parts round keeps (#2119): each still holds
    the draft it had when the round started, byte for byte, and its input is the one
    that draft was designed for. Their design and review count as done, as a part a
    post-schema reroute carried forward does (``carry_forward.carried_parts_at``)."""
    base = base_key.rstrip("/")
    out: dict[int, int] = {}
    for entry in _request_at(store, base).get("kept_parts") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("part"), int):
            continue
        idx = int(entry["part"])
        draft, trace = _part_files(idx)
        if (
            entry.get("draft_sha256")
            and _sha256(store, f"{base}/{draft}") == entry["draft_sha256"]
            and _input_digest(store, f"{base}/input_group_{idx}.json") == entry.get("input_sha256")
        ):
            out[idx] = idx
    return out


def stale_kept_review(store: ArtifactStore, base_key: str, part: int) -> bool:
    """Whether ``part`` is one the running parts round kept whose draft has changed
    since while its design trace has not: the review it holds is of the earlier draft."""
    base = base_key.rstrip("/")
    draft, trace = _part_files(part)
    for entry in _request_at(store, base).get("kept_parts") or []:
        if isinstance(entry, Mapping) and entry.get("part") == part:
            if _sha256(store, f"{base}/{trace}") != entry.get("trace_sha256"):
                return False  # a review recorded since
            return bool(_sha256(store, f"{base}/{draft}") != entry.get("draft_sha256"))
    return False


def _parts_reviewed(
    store: ArtifactStore,
    db: str,
    job_id: str,
    engine: str,
    version: int,
    parts: list[int] | None,
) -> bool:
    """Every part a parts round redesigned has a review recorded that ran (True for a
    whole-engine round, which the report's counts decide)."""
    if not parts:
        return True
    from src.shared.wave_readiness import reviewed_count

    prefix = _design_prefix(db, job_id, engine, version)
    for idx in parts:
        try:
            trace = store.read_json(prefix + _part_files(idx)[1])
        except (OSError, ValueError):
            return False
        if not reviewed_count(trace if isinstance(trace, Mapping) else None):
            return False
    return True


def _clear_requests(store: ArtifactStore, db: str, job_id: str, version: int, log: Mapping) -> None:
    for rnd in log.get("rounds") or []:
        for engine in rnd.get("engines") or []:
            key = request_key(db, job_id, engine, version)
            if store.exists(key):
                store.delete(key)


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------


def _open_round(log: Mapping[str, Any]) -> dict[str, Any] | None:
    rounds = log.get("rounds") or []
    last = rounds[-1] if rounds else None
    return last if isinstance(last, dict) and last.get("outcome") is None else None


def _reviewed(report: Mapping[str, Any], engine: str) -> bool:
    d = _designs(report).get(engine) or {}
    return (
        int(d.get("reviewed_count") or 0) > 0
        and not int(d.get("open_review_count") or 0)
        and not int(d.get("failed_review_count") or 0)
    )


def pending_reviews(
    store: ArtifactStore, db: str, job_id: str, version: int, log: Mapping[str, Any]
) -> list[str]:
    """The open round's designs whose review is not recorded yet (``missing_reviews``):
    a headless run reviews a redesign as its own step, so the round must not close
    ``not_reviewed`` before it. A review recorded as failed is recorded."""
    rnd = _open_round(log)
    if rnd is None:
        return []
    from src.agents.schema_design.pe_review_external import missing_reviews

    return missing_reviews(store, db, job_id, version, list(rnd.get("engines") or []))


def _close(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    log: dict[str, Any],
    report: Mapping[str, Any],
) -> list[str]:
    """Close the open round from the report synthesized after it: keep or put back each
    engine's design. Returns the engines whose earlier design was put back."""
    rnd = _open_round(log)
    if rnd is None:
        return []
    n = int(rnd["round"])
    designed = _designed(report)
    still = fixable_requests(report)
    parts: Mapping[str, list[int] | None] = rnd.get("parts") or {}
    outcome: dict[str, str] = {}
    restored: list[str] = []
    for engine in rnd.get("engines") or []:
        before = list((rnd.get("before") or {}).get(engine) or [0, 0])
        if engine not in designed:
            outcome[engine] = FAILED
            rnd.setdefault("failed", {})[
                engine
            ] = f"redesign round {n} failed: schema_design_{engine} wrote no design"
        elif not _review_ran(report, engine) or not _parts_reviewed(
            store, db, job_id, engine, version, parts.get(engine)
        ):
            # Every design is reviewed in both modes (#2081): a design whose review did
            # not run or failed is never kept, whatever the one it replaced had. In a
            # parts round the parts kept carry their reviews, so each part the round
            # redesigned must have its own (#2119).
            outcome[engine] = NOT_REVIEWED
        elif not _strictly_better(_round_score(report, engine, before), before):
            outcome[engine] = DID_NOT_IMPROVE
        elif (
            engine not in still
            and _reviewed(report, engine)
            and not any(_round_score(report, engine, before)[2:])
        ):
            outcome[engine] = FIXED
            continue
        else:
            outcome[engine] = IMPROVED
            continue
        _put_back(store, db, job_id, version, n, engine, parts.get(engine))
        restored.append(engine)
    rnd["outcome"] = outcome
    return restored


def _write_report_log(
    store: ArtifactStore, db: str, job_id: str, version: int, log: Mapping
) -> None:
    """Write the closed log into the version's ``report.json`` (its ``wave_redesign``),
    so the report states the last round's outcome without another synthesis."""
    from src.contracts.synthesis_output import SynthesisOutputContract, WaveRedesignLog
    from src.shared.contract_versioning import load_contract

    key = f"{db}/{job_id}/synthesis/v{version}/report.json"
    if not store.exists(key):
        return
    report = load_contract(SynthesisOutputContract, store.read_json(key), artifact=key)
    data = report.model_dump(mode="json")
    data["wave_redesign"] = WaveRedesignLog.model_validate(dict(log)).model_dump(mode="json")
    store.write_json(f"{db}/{job_id}/synthesis/v{version}/report.json", data)


def plan_round(
    store: ArtifactStore,
    db: str,
    job_id: str,
    report: Mapping[str, Any],
    version: int,
    *,
    parts: bool = False,
    budget: Callable[[], str | None] | None = None,
    wait_for_reviews: bool = False,
) -> dict[str, Any]:
    """Close the last round from ``report`` and start the next one when a fixable hold
    is left and rounds remain. The status line:

    - ``awaiting_review`` (only with ``wait_for_reviews``, a headless run, which reviews
      a design as its own step): ``missing_reviews`` lists the round's designs with no
      review recorded yet: review them, synthesize, then run this again;
    - ``awaiting_synthesis``: an engine's earlier design was put back (``restored``):
      synthesize again, then run this again;
    - ``awaiting_redesign``: design again exactly ``engines`` (``parts``: the DynamoDB
      groups, or None for the whole engine), synthesize, then run this again;
    - ``complete`` (nothing a redesign can fix is held), ``exhausted`` (rounds ran out,
      the engines left are not sent back again, or the run closed a round, #2122:
      ``held`` lists them, ``stopped`` says why a closed round ended the loop) or
      ``stopped`` (``budget`` gave a reason: no round starts; synthesize once more to
      state it).
    """
    log = read_log(store, db, job_id, version)
    pending = pending_reviews(store, db, job_id, version, log) if wait_for_reviews else []
    if pending:
        return {"status": AWAITING_REVIEW, "missing_reviews": pending, "rounds": len(log["rounds"])}
    restored = _close(store, db, job_id, version, log, report)
    if restored:
        store.write_json(log_key(db, job_id, version), log)
        return {"status": "awaiting_synthesis", "restored": restored, "rounds": len(log["rounds"])}
    if log["rounds"]:
        _write_report_log(store, db, job_id, version, log)
    # The automatic loop's own rounds: a /wave-edit round never counts against it (#2067).
    own = [rnd for rnd in log["rounds"] if rnd.get("wave_edit") is None]
    final = {
        str(e) for rnd in own for e, out in (rnd.get("outcome") or {}).items() if out in _FINAL
    }
    held = fixable_requests(report)
    requests = {e: r for e, r in held.items() if e not in final}
    done = len(own)
    base = {"rounds": done, "max_rounds": MAX_REDESIGN_ROUNDS}
    # A round the run closed (stop_round, #2122) ends the loop: no further round starts.
    closed = next((rnd for rnd in own if rnd.get("stopped_by") == CLOSED), None)
    if not requests or done >= MAX_REDESIGN_ROUNDS or closed is not None:
        _clear_requests(store, db, job_id, version, log)
        store.write_json(log_key(db, job_id, version), log)
        if not held:
            return {"status": COMPLETE, **base}
        ended = {"stopped": str(closed.get("stopped") or "")} if closed is not None else {}
        return {"status": EXHAUSTED, **base, "held": sorted(held), **ended}
    reason = budget() if budget is not None else None
    if reason:
        log["rounds"].append(
            {
                "round": len(log["rounds"]) + 1,
                "assignment_version": version,
                "engines": sorted(requests),
                "requests": [requests[e] for e in sorted(requests)],
                "outcome": {e: STOPPED for e in sorted(requests)},
                "stopped": reason,
                "stopped_by": CEILING,
            }
        )
        store.write_json(log_key(db, job_id, version), log)
        return {"status": STOPPED, "reason": reason, **base}
    n = len(log["rounds"]) + 1
    # A review hold names the parts whose review held the design (#2119).
    requests = {e: _name_review_parts(store, db, job_id, version, r) for e, r in requests.items()}
    # The parts are picked from every query a check names, not only the ones the
    # designer is shown (#2119 review): a part past the cap is redesigned too.
    every_id = fixable_requests(report, max_query_ids=None)
    affected: dict[str, list[int] | None] = {}
    for engine, request in sorted(requests.items()):
        chosen = _with_every_query(request, every_id.get(engine) or {})
        affected[engine] = _set_aside(store, db, job_id, version, n, engine, chosen, parts)
        store.write_json(
            request_key(db, job_id, engine, version),
            {
                "round": done + 1,
                "max_rounds": MAX_REDESIGN_ROUNDS,
                **request,
                # A parts round: the parts designed again, and the parts kept as they
                # are, which --split marks carried_from (#2119).
                "parts": affected[engine],
                "kept_parts": _kept_record(
                    store, _design_prefix(db, job_id, engine, version), affected[engine]
                ),
            },
        )
    log["rounds"].append(
        {
            "round": n,
            "assignment_version": version,
            "engines": sorted(requests),
            "requests": [requests[e] for e in sorted(requests)],
            "before": {e: engine_score(report, e) for e in sorted(requests)},
            "parts": affected,
            "outcome": None,
        }
    )
    store.write_json(log_key(db, job_id, version), log)
    return {
        "status": "awaiting_redesign",
        # This loop's own round; the log's ``round`` numbers every round of the version.
        "round": done + 1,
        "max_rounds": MAX_REDESIGN_ROUNDS,
        "engines": sorted(requests),
        "parts": affected,
        "assignment_version": version,
    }


def fail_round(
    store: ArtifactStore, db: str, job_id: str, version: int, reason: str
) -> dict[str, Any]:
    """The open round's design failed to run (an error, a failed subagent): put every
    engine's earlier design back and record ``failed`` with ``reason``. Synthesize again
    after it; no further round starts for those engines."""
    log = read_log(store, db, job_id, version)
    rnd = _open_round(log)
    if rnd is None:
        return {"status": "error", "message": "no redesign round is running"}
    n = int(rnd["round"])
    for engine in rnd.get("engines") or []:
        _put_back(store, db, job_id, version, n, engine, (rnd.get("parts") or {}).get(engine))
    rnd["outcome"] = {e: FAILED for e in rnd.get("engines") or []}
    rnd["failed"] = {e: reason for e in rnd.get("engines") or []}
    _clear_requests(store, db, job_id, version, log)
    store.write_json(log_key(db, job_id, version), log)
    return {"status": "rolled_back", "round": n, "engines": list(rnd.get("engines") or [])}


def stop_round(
    store: ArtifactStore, db: str, job_id: str, version: int, reason: str | None = None
) -> dict[str, Any]:
    """Close the open round without checking its design (#2122): the run is ending, or
    goes on to its report, with the round's design not designed, reviewed or synthesized.
    Every engine's earlier design is put back (a parts round's ``carried_forward.json``
    with it, #2119), the round records ``stopped`` (``stopped_by`` ``closed``, with
    ``reason``) and the report's log states it; no further round starts. Synthesize
    again (``awaiting_synthesis``) so the waves state it on the earlier design.
    ``no_open_round`` when no round of the automatic loop is open: nothing changes.

    An open ``/wave-edit`` round (``wave_edit`` set, #2067) is never closed here: it
    belongs to that command, which checks it or rolls it back (``--rollback``), so a
    ``/modernize`` run's close leaves it as it is (``open_wave_edit_round`` names it)."""
    log = read_log(store, db, job_id, version)
    rnd = _open_round(log)
    if rnd is None or rnd.get("wave_edit") is not None:
        out: dict[str, Any] = {"status": NO_OPEN_ROUND, "rounds": len(log["rounds"])}
        if rnd is not None:
            out["open_wave_edit_round"] = int(rnd["round"])
        return out
    n = int(rnd["round"])
    # The customer line already names the round ("redesign round N was not completed").
    reason = reason or "its design was never checked"
    engines = [str(e) for e in rnd.get("engines") or []]
    for engine in engines:
        _put_back(store, db, job_id, version, n, engine, (rnd.get("parts") or {}).get(engine))
    rnd["outcome"] = {e: STOPPED for e in engines}
    rnd["stopped"] = reason
    rnd["stopped_by"] = CLOSED
    _clear_requests(store, db, job_id, version, log)
    store.write_json(log_key(db, job_id, version), log)
    from src.shared.contract_versioning import ContractVersionError

    try:
        _write_report_log(store, db, job_id, version, log)
    except (OSError, ValueError, ContractVersionError):
        # The synthesis this asks for copies the log into the report anyway: a report
        # that does not load (another MAJOR, unreadable) must not keep the round open.
        pass
    return {
        "status": "awaiting_synthesis",
        "round": n,
        "stopped": reason,
        "restored": engines,
        "rounds": len(log["rounds"]),
    }


def run_rounds(
    store: ArtifactStore,
    db: str,
    job_id: str,
    *,
    version: int,
    read_report: Callable[[], Mapping[str, Any]],
    design: Callable[[str], object],
    synthesize: Callable[[], object],
    budget: Callable[[], str | None] = lambda: None,
) -> dict[str, Any]:
    """The loop for a run that designs in process (Bedrock, ``assess()``): check,
    redesign the held engines' failing parts, synthesize again, until nothing fixable is
    held or the rounds run out. A design that raises puts the earlier design back and
    ends the loop. Anything else that stops the loop with a round open (a synthesis that
    raises, an interrupt) closes that round ``stopped`` with the earlier design put back
    (:func:`stop_round`, #2122) before it propagates. Returns the last status line."""
    try:
        return _run_rounds(store, db, job_id, version, read_report, design, synthesize, budget)
    except BaseException as exc:
        try:
            stop_round(
                store,
                db,
                job_id,
                version,
                f"the run stopped before its design was checked ({type(exc).__name__})",
            )
        except Exception as close_exc:  # noqa: BLE001 -- the original error must propagate
            # The round stays open in the log (outcome None), as it is on disk: a later
            # --stop can still close it. The failure is logged and noted on the error.
            note = (
                f"the open redesign round was not closed ({type(close_exc).__name__}: "
                f"{close_exc}); close it with run_wave_redesign.py --stop"
            )
            logger.warning("wave redesign: %s", note)
            exc.add_note(note)
        raise


def _run_rounds(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    read_report: Callable[[], Mapping[str, Any]],
    design: Callable[[str], object],
    synthesize: Callable[[], object],
    budget: Callable[[], str | None],
) -> dict[str, Any]:
    while True:
        status = plan_round(store, db, job_id, read_report(), version, parts=True, budget=budget)
        if status["status"] == "awaiting_synthesis":
            synthesize()
            continue
        if status["status"] == STOPPED:
            synthesize()
            return status
        if status["status"] != "awaiting_redesign":
            return status
        for engine in status["engines"]:
            try:
                design(engine)
            except Exception as exc:  # noqa: BLE001 -- the round is put back, not lost
                reason = (
                    f"redesign round {status['round']} failed: schema_design_{engine} "
                    f"({type(exc).__name__})"
                )
                failed = fail_round(store, db, job_id, version, reason)
                synthesize()
                _write_report_log(store, db, job_id, version, read_log(store, db, job_id, version))
                return {**failed, "status": FAILED, "reason": reason}
        synthesize()


def latest_report(store: ArtifactStore, db: str, job_id: str) -> dict[str, Any] | None:
    """The synthesis ``report.json`` the next check reads, or None before synthesis."""
    from src.storage.assignment_versioning import synthesis_report_candidates

    for key in synthesis_report_candidates(store, db, job_id):
        if store.exists(key):
            report = store.read_json(key)
            return report if isinstance(report, dict) else None
    return None


def _usage_spend(rows: Mapping[str, Mapping[str, Any]]) -> tuple[int, int]:
    from src.shared.model_usage import FIELDS

    tokens = sum(int(r.get(f) or 0) for r in rows.values() for f in FIELDS)
    return tokens, sum(int(r.get("calls") or 0) for r in rows.values())


def ledger_round_budget(
    limits: Any, ledger: Any, saved_runs: list[Mapping[str, Any]] | None = None
) -> str | None:
    """Why one more round does not fit ``limits`` (a ``RunLimits``) given the run in
    ``ledger`` (a ``model_usage.UsageLedger``), or None. One round is estimated as one
    schema design phase plus one synthesis, from this run's ledger, or, for a resumed
    run whose ledger has no schema design yet, from the job's saved usage
    (``_logs/model_usage.json`` runs, ``saved_runs``). None with no ceiling."""
    if not getattr(limits, "any_set", False):
        return None
    snap = ledger.snapshot()
    phases = ("schema_design", "synthesis")
    by_phase: Mapping[str, Any] = snap["by_phase"]
    timings: Mapping[str, Any] = snap["phases"]
    if not by_phase.get("schema_design"):
        for run in reversed(saved_runs or []):
            if (run.get("by_phase") or {}).get("schema_design"):
                by_phase, timings = run.get("by_phase") or {}, run.get("phases") or {}
                break
    tokens, calls = _usage_spend({"all": snap["totals"]})
    round_tokens = round_calls = 0
    for phase in phases:
        t, c = _usage_spend(by_phase.get(phase) or {})
        round_tokens, round_calls = round_tokens + t, round_calls + c
    seconds = sum(float((timings.get(p) or {}).get("seconds") or 0) for p in phases)
    return ceiling_stop_reason(
        {"tokens": limits.tokens, "model_calls": limits.model_calls, "seconds": limits.seconds},
        {"tokens": tokens, "model_calls": calls, "seconds": ledger.elapsed()},
        {"tokens": round_tokens, "model_calls": round_calls, "seconds": seconds},
    )


def ceiling_stop_reason(
    limits: Mapping[str, float | None],
    observed: Mapping[str, float],
    projected: Mapping[str, float],
) -> str | None:
    """Why one more round does not fit the run's ceilings, or None when it does: for each
    ceiling set (``tokens``, ``model_calls``, ``seconds``), the run so far plus one
    round's projected spend must stay within it."""
    units = {"tokens": "tokens", "model_calls": "model calls", "seconds": "seconds"}
    for ceiling, limit in limits.items():
        if limit is None:
            continue
        need = float(observed.get(ceiling) or 0) + float(projected.get(ceiling) or 0)
        if need > float(limit):
            return (
                f"the run's {units.get(ceiling, ceiling)} ceiling ({limit:g}) leaves no room "
                f"for another redesign round (about {float(projected.get(ceiling) or 0):g} "
                "each)"
            )
    return None


def saved_usage_runs(store: ArtifactStore, db: str, job_id: str) -> list[Mapping[str, Any]]:
    """The job's saved usage runs (``_logs/model_usage.json``), for a resumed run's
    round estimate; empty when none was saved."""
    key = f"{db}/{job_id}/_logs/model_usage.json"
    try:
        data = store.read_json(key) if store.exists(key) else {}
    except (OSError, ValueError):
        data = {}
    return [r for r in (data.get("runs") or []) if isinstance(r, Mapping)]
