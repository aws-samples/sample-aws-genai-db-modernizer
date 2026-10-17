"""Per-query routing snapshot harness (#497).

This is not an identity gate: a routing refactor is expected to change which
engine some queries land on, and that is fine. ``test_routing_baseline.py``
only checks engine totals within a tolerance, so a change that moves queries
between engines without moving the totals outside tolerance (#474, #487)
passes silently either way, intended or not. This module instead measures,
from one deterministic pipeline run, a compact per-query fingerprint of the
whole routing decision, and reports every change grouped by the step that
produced it, so a reviewer can see exactly which queries moved and why --
whether that is the point of the PR or a sign something moved by accident:

- every candidate engine's score for the query, through each step the
  resolver applies to it (base fit, the anti-pattern penalty, or the hard
  exclusion/capability/write gate that zeroed it instead) -- captured via
  ``AssignmentResolver.resolve(..., routing_trace=RoutingTrace())``, an
  explicit, off-by-default hook (see ``routing_trace.RoutingTrace``)
  that records the score ``resolve()`` actually used (``scores[qid][engine]``),
  never a recompute; ``base``/``penalty`` are kept alongside only as
  diagnostics, with any gap between them recorded as ``adj`` rather than
  raised, so a future drift shows up in the diff instead of crashing the run;
- the resolver's own first-pick engine and a short reason category, parsed
  from its ``assignment_reason`` text, plus (when they apply) the
  co-dependency group it belonged to (a short key hashed from the sorted
  member query ids, and every candidate engine's group score), the signal
  name for a signal override, and the tie-break rule that decided it;
- the reality check's final engine, reason category and (for a consolidation)
  the parsed ``fit=NN`` score -- omitted from the snapshot when it matches the
  first pick, which is the common case (most queries are not consolidated);
- the cache overlay engine, if any;
- the first assignment's move ledger (#616), read from the pipeline's own
  ``assignment/v1`` (moves are per-version, so these are v1's own Moves and
  nothing a later version recorded): each query's Moves as ``pass:reason_code`` in ``seq``
  order (``mv``), and per sample the count of Moves by pass and reason code
  (``moves_by_pass``);
- the reality check's own Moves, read from the version it wrote (its
  ``assignment/v<N>``, holding only that check's Moves): per query
  ``pass:reason_code`` in ``seq`` order (``rmv``, only for a query the check
  moved or re-tied), and per sample the count by pass and reason code
  (``rc_moves_by_pass``);
- the consolidation records from ``reality-check/output.json``, including
  ``queries_retained`` and ``retention_reason`` for a partial consolidation.

``measure()`` also asserts the snapshot's first-assignment engine matches the
pipeline's own ``assignment/v1`` for every query -- a cheap, early guard that
the trace is actually describing the real run, not a resolver re-run that
has quietly diverged from it -- that ``assignment/v1``'s Moves replay to
its own ``assigned_engine`` and ``tied_engines`` exactly (#616), and that the
reality check's version replays exactly from its input version plus its own
Moves.

``tests/e2e/test_routing_snapshot.py`` compares a run against
``tests/e2e/baselines/routing_snapshot.json`` and reports every per-query
change, grouped by step. A PR that changes routing regenerates the snapshot
with ``--reason`` and pastes the printed diff into the PR description, so a
reviewer sees exactly what moved without re-deriving it themselves::

    uv run python -m tests.e2e.routing_snapshot --write --reason "why it moved (#NNN)"

The file is written with one query per line, sorted keys (see
``dump_snapshot``), so a diff lists exactly the queries that moved instead of
one unreadable multi-hundred-KB line.

``ROUTING_SNAPSHOT_EXTRA`` (optional, local only, never committed): the path
to an extra, non-bundled ``collection.json`` to snapshot the same way (the
bundled samples, including the synthetic SQL Server one, ``trailgear``, are
always in the committed snapshot). ``ROUTING_SNAPSHOT_EXTRA_DB`` names its engine
family / database name passed to the pipeline (default ``"extra"``); the
output file is ``routing_snapshot_<db>.json``. Setting ``ROUTING_SNAPSHOT_EXTRA``
switches ``--write`` entirely to the extra-fixture mode: it writes only
``routing_snapshot_<db>.json``, next to the input file, and never touches
``tests/e2e/baselines/routing_snapshot.json``. Unset it to go back to writing
the committed baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).parent / "baselines" / "routing_snapshot.json"

# A readable failure lists every changed query, but caps how many are printed
# per step so a wide refactor's failure message stays scannable.
MAX_QUERIES_SHOWN = 30


# ---------------------------------------------------------------------------
# Reason-text -> short category (parsed from AssignmentResolver's own
# assignment_reason strings and the reality check's, which both describe
# which mechanism picked the engine: a signal/utility override, a
# co-dependency group, a plain score comparison (possibly tied), a fallback
# with no score at all, or one of the reality check's consolidation
# mechanisms -- the justification floor or "no unique value" -- each kept as
# its own category so a change in one is never masked by "nothing changed" in
# another; the Aurora absorption pass and its category are gone, #588, #616).
# ---------------------------------------------------------------------------


def categorize(reason: str) -> str:
    """A short, stable category for an ``assignment_reason`` string."""
    if "utility/metadata statement" in reason:
        return "utility_pin"
    if "justification floor" in reason:
        return "reality_check_justification_floor"
    if "no unique value" in reason:
        return "reality_check_no_unique_value"
    if "no analyzed engine can serve every member" in reason:
        # #787: a co-dependency group no engine can host takes the Aurora
        # fallback (it read "no analysis available" before).
        return "co_dependency_fallback"
    if "write group: one source of truth" in reason:
        # #992 (ADR-031 Amendment 9): a unit holding a table's write group.
        return "write_group_tie" if "scores tie with" in reason else "write_group"
    if "co-dependency group" in reason:
        # "scores tie with" (#503): break_owner_tie's wording changed from
        # "(tie with X: ...)" to a full, honest sentence built with
        # display_engine, so the tie marker moved with it.
        return "co_dependency_tie" if "scores tie with" in reason else "co_dependency_group"
    if "signal override:" in reason:
        return "signal_override"
    if "highest fit for" in reason:
        return "individual_tie" if "scores tie with" in reason else "individual_highest_confidence"
    if "no engine scored this query" in reason:
        return "fallback_no_score"
    return "other"


_FIT_RE = re.compile(r"fit=(-?\d+)")


def parse_fit(reason: str) -> int | None:
    """The ``fit=NN`` (or ``Aurora fit=NN``) score embedded in a reality-check
    consolidation's ``assignment_reason``, or ``None`` when the text has none
    (every reason that is not a reality-check consolidation)."""
    m = _FIT_RE.search(reason)
    return int(m.group(1)) if m else None


# ``break_owner_tie``'s own rule strings (``src/agents/referee/
# assignment_resolver.py``, via the shared ``tie_break.break_tie``, #503),
# mapped to a short, stable code. ADR-031 Decision 2 recognizes exactly one
# rule for a non-Aurora tie -- there is none -- so "table_traffic",
# "query_count" and "name_order" are gone: a tie that is not
# source-compatible is reported as ``tie_unresolved``, never decided by one
# of those.
def tie_break_code(tie_kind: str) -> str:
    """A short, stable code for a ``RoutingTrace.tie_break_by_query`` entry.

    ``record_tie`` stores ``break_owner_tie``'s structured ``tie_kind``
    directly (``"source_compatible"`` or ``"tie_unresolved"``) rather than
    its customer-facing prose sentence (#503: a customer-facing sentence
    should never be the thing a test parses to find out what happened) --
    this is now a thin pass-through, kept as its
    own function so a caller never has to know ``RoutingTrace``'s raw field
    name, and so an unrecognized value still falls back to ``"other"``
    instead of silently misreporting one of the two known kinds.
    """
    return tie_kind if tie_kind in ("source_compatible", "tie_unresolved") else "other"


def _latest(job_dir: Path, sub: str, name: str) -> Path:
    paths = sorted(job_dir.glob(f"{sub}/v*/{name}"), key=lambda p: int(p.parent.name.lstrip("v")))
    if not paths:
        raise FileNotFoundError(f"no {sub}/v*/{name} under {job_dir}")
    return paths[-1]


def measure(job_dir: Path) -> dict[str, Any]:
    """The per-query routing snapshot for one job (see module docstring)."""
    from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace

    triage = json.loads((job_dir / "referee-triage" / "triage.json").read_text())
    collector_output = json.loads((job_dir / "collector" / "output.json").read_text())
    selected = [a["agent_type"] for a in triage.get("selected_agents", [])]
    analysis_outputs = {
        engine: json.loads((job_dir / f"analysis-{engine}" / "analysis.json").read_text())
        for engine in selected
        if (job_dir / f"analysis-{engine}" / "analysis.json").exists()
    }

    # Re-running the resolver on the exact artifacts the pipeline just wrote
    # is pure and deterministic (#497's "wrap in the test layer" option): it
    # reproduces assignment/v1 bit for bit, this time with a trace attached.
    trace = RoutingTrace()
    AssignmentResolver().resolve(triage, analysis_outputs, collector_output, routing_trace=trace)

    # Guard the above claim instead of just asserting it in a docstring: if
    # this re-run ever diverges from the pipeline's own v1 (a resolver change
    # with a hidden side effect, an input read in a different order, ...),
    # fail loudly here rather than silently snapshot the wrong engine.
    from src.agents.referee.move_ledger import replay
    from src.contracts.assignment_models import Assignment

    v1 = Assignment.model_validate_json(
        (job_dir / "assignment" / "v1" / "assignment.json").read_text()
    )
    v1_by_id = {qa.query_id: qa.assigned_engine for qa in v1.query_assignments}
    # The move ledger (#616) must rebuild assignment/v1 exactly from v1's own
    # Moves (per-version: no later version's Moves are involved).
    replayed = replay(v1.moves)
    expected = {
        qa.query_id: (qa.assigned_engine, list(qa.tied_engines)) for qa in v1.query_assignments
    }
    if replayed != expected:
        bad = sorted(q for q in set(replayed) | set(expected) if replayed.get(q) != expected.get(q))
        raise AssertionError(
            f"routing_snapshot.measure(): assignment/v1's moves do not replay to its own "
            f"assignment for {len(bad)} queries, e.g. {bad[:5]}"
        )
    moves_by_query: dict[str, list[str]] = {}
    moves_by_pass: dict[str, dict[str, int]] = {}
    for move in sorted(v1.moves, key=lambda m: m.seq):
        moves_by_query.setdefault(move.query_id, []).append(
            f"{move.pass_.value}:{move.reason_code.value}"
        )
        by_reason = moves_by_pass.setdefault(move.pass_.value, {})
        by_reason[move.reason_code.value] = by_reason.get(move.reason_code.value, 0) + 1
    rc_moves_by_query, rc_moves_by_pass = _reality_check_moves(job_dir)
    for qid, payload in trace.by_query.items():
        v1_engine = v1_by_id.get(qid)
        if v1_engine != payload["assigned_engine"]:
            raise AssertionError(
                f"routing_snapshot.measure(): re-running the resolver picked "
                f"{payload['assigned_engine']!r} for query {qid!r}, but the pipeline's own "
                f"assignment/v1 picked {v1_engine!r} -- the trace no longer reproduces the "
                "real run"
            )

    report = json.loads(_latest(job_dir, "synthesis", "report.json").read_text())
    final_version = (report.get("assignment_summary") or {}).get("version")
    final_path = (
        job_dir / "assignment" / f"v{final_version}" / "assignment.json"
        if final_version
        else _latest(job_dir, "assignment", "assignment.json")
    )
    final_by_id = {
        qa["query_id"]: qa for qa in json.loads(final_path.read_text()).get("query_assignments", [])
    }

    consolidations = []
    rc_path = job_dir / "reality-check" / "output.json"
    if rc_path.exists():
        rc = json.loads(rc_path.read_text())
        for c in rc.get("consolidations", []):
            entry: dict[str, Any] = {
                "from": c["from_engine"],
                "to": c["to_engine"],
                "n": c["query_count"],
                "action": c["action"],
                "cost": round(float(c.get("saved_cost_estimate") or 0), 2),
                "retained": sorted(c.get("queries_retained") or []),
            }
            if c.get("retention_reason"):
                entry["retention_reason"] = c["retention_reason"]
            consolidations.append(entry)
    consolidations.sort(key=lambda c: (c["from"], c["to"], c["n"]))

    queries: dict[str, Any] = {}
    for qid, payload in trace.by_query.items():
        first: dict[str, Any] = {
            "e": payload["assigned_engine"],
            "c": categorize(payload["assigned_reason"]),
        }
        if "signal" in payload:
            first["sig"] = payload["signal"]
        if "tie_break" in payload:
            first["tie"] = tie_break_code(payload["tie_break"])
        if "group" in payload:
            first["grp"] = payload["group"]

        fqa = final_by_id.get(qid, {})
        final_engine = fqa.get("assigned_engine", payload["assigned_engine"])
        final_reason = fqa.get("assignment_reason", payload["assigned_reason"])
        final_cat = categorize(final_reason)

        query_entry: dict[str, Any] = {
            "s": payload["engines"],
            "f": first,
            "mv": moves_by_query.get(qid, []),
        }
        if payload.get("access_class"):
            query_entry["k"] = payload["access_class"]
        if qid in rc_moves_by_query:
            query_entry["rmv"] = rc_moves_by_query[qid]
        if final_engine != first["e"] or final_cat != first["c"]:
            final: dict[str, Any] = {"e": final_engine, "c": final_cat}
            fit = parse_fit(final_reason)
            if fit is not None:
                final["fit"] = fit
            query_entry["r"] = final
        cache_engine = fqa.get("cache_engine")
        if cache_engine:
            query_entry["cache"] = cache_engine
        queries[qid] = query_entry

    return {
        "query_count": len(queries),
        "queries": queries,
        "consolidations": consolidations,
        "moves_by_pass": moves_by_pass,
        "rc_moves_by_pass": rc_moves_by_pass,
    }


def _version_state(assignment: Any) -> dict[str, tuple[str, list[str]]]:
    return {
        qa.query_id: (qa.assigned_engine, list(qa.tied_engines))
        for qa in assignment.query_assignments
    }


def _reality_check_moves(
    job_dir: Path,
) -> tuple[dict[str, list[str]], dict[str, dict[str, int]]]:
    """The reality check's own Moves (#616), by query and by pass and reason code.

    Read from the assignment version the check wrote (``output_assignment_version``
    in ``reality-check/output.json``; none when it consolidated nothing). Fails
    loudly when that version does not replay from its input version plus its own
    Moves.
    """
    from src.agents.referee.move_ledger import replay
    from src.contracts.assignment_models import Assignment

    rc_path = job_dir / "reality-check" / "output.json"
    version = (
        json.loads(rc_path.read_text()).get("output_assignment_version")
        if rc_path.exists()
        else None
    )
    if not version:
        return {}, {}
    root = job_dir / "assignment"
    revised = Assignment.model_validate_json((root / f"v{version}" / "assignment.json").read_text())
    source = Assignment.model_validate_json(
        (root / f"v{revised.previous_version}" / "assignment.json").read_text()
    )
    expected = _version_state(revised)
    replayed = replay(revised.moves, start=_version_state(source))
    if replayed != expected:
        bad = sorted(q for q in set(replayed) | set(expected) if replayed.get(q) != expected.get(q))
        raise AssertionError(
            f"routing_snapshot.measure(): assignment/v{version} (the reality check's) does not "
            f"replay from v{revised.previous_version} plus its own moves for {len(bad)} "
            f"queries, e.g. {bad[:5]}"
        )
    by_query: dict[str, list[str]] = {}
    by_pass: dict[str, dict[str, int]] = {}
    for move in sorted(revised.moves, key=lambda m: m.seq):
        code = f"{move.pass_.value}:{move.reason_code.value}"
        by_query.setdefault(move.query_id, []).append(code)
        by_reason = by_pass.setdefault(move.pass_.value, {})
        by_reason[move.reason_code.value] = by_reason.get(move.reason_code.value, 0) + 1
    return by_query, by_pass


# ---------------------------------------------------------------------------
# Diff: compact per-step comparison, readable failure message
# ---------------------------------------------------------------------------


def _consolidation_key(c: dict[str, Any]) -> tuple[str, str, str]:
    """A consolidation record's identity: which engine moved to which, and how.

    (from_engine, to_engine, action) is stable across a routing change that
    only moves the record's other fields (query_count, cost, which queries
    were retained) -- those should diff as a change to the SAME record, not
    read as one record disappearing and an unrelated one appearing.
    """
    return (c["from"], c["to"], c["action"])


def compare(baseline: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Differences between two ``measure()`` results for one sample; ``{}`` if none.

    Up to nine keys, each present only when non-empty:
    ``scores`` (the per-engine score ladder changed for a query_id),
    ``first_assignment`` (the resolver's own pick, category, co-dependency
    group, signal or tie-break rule changed), ``final_assignment`` (the
    reality check's final pick, category or parsed fit score changed -- a
    query with no override is compared by its first pick), ``cache_engine``
    (the cache overlay engine changed), ``moves`` (the query's first-assignment
    Moves, ``pass:reason_code`` in order, changed), ``moves_by_pass`` (the
    sample's Move counts by pass and reason code changed, ``{"before": ...,
    "after": ...}``), ``rc_moves`` / ``rc_moves_by_pass`` (the same two for the
    reality check's own Moves), and ``consolidations`` (a record, keyed by
    ``_consolidation_key``, whose fields -- query_count, cost, retained
    queries, retention_reason -- changed).
    """
    diff: dict[str, Any] = {}
    b_queries, a_queries = baseline.get("queries", {}), actual.get("queries", {})
    for qid in sorted(set(b_queries) | set(a_queries)):
        b, a = b_queries.get(qid), a_queries.get(qid)
        b_scores = b.get("s") if b else None
        a_scores = a.get("s") if a else None
        if b_scores != a_scores:
            diff.setdefault("scores", {})[qid] = {"before": b_scores, "after": a_scores}
        b_first = b.get("f") if b else None
        a_first = a.get("f") if a else None
        if b_first != a_first:
            diff.setdefault("first_assignment", {})[qid] = {"before": b_first, "after": a_first}
        b_final = (b.get("r") or b.get("f")) if b else None
        a_final = (a.get("r") or a.get("f")) if a else None
        if b_final != a_final:
            diff.setdefault("final_assignment", {})[qid] = {"before": b_final, "after": a_final}
        b_class = b.get("k") if b else None
        a_class = a.get("k") if a else None
        if b_class != a_class:
            diff.setdefault("access_class", {})[qid] = {"before": b_class, "after": a_class}
        b_cache = b.get("cache") if b else None
        a_cache = a.get("cache") if a else None
        if b_cache != a_cache:
            diff.setdefault("cache_engine", {})[qid] = {"before": b_cache, "after": a_cache}
        b_moves = b.get("mv") if b else None
        a_moves = a.get("mv") if a else None
        if b_moves != a_moves:
            diff.setdefault("moves", {})[qid] = {"before": b_moves, "after": a_moves}
        b_rc_moves = (b.get("rmv") if b else None) or []
        a_rc_moves = (a.get("rmv") if a else None) or []
        if b_rc_moves != a_rc_moves:
            diff.setdefault("rc_moves", {})[qid] = {"before": b_rc_moves, "after": a_rc_moves}
    b_by_pass = baseline.get("moves_by_pass")
    a_by_pass = actual.get("moves_by_pass")
    if b_by_pass != a_by_pass:
        diff["moves_by_pass"] = {"before": b_by_pass, "after": a_by_pass}
    b_rc_by_pass = baseline.get("rc_moves_by_pass") or {}
    a_rc_by_pass = actual.get("rc_moves_by_pass") or {}
    if b_rc_by_pass != a_rc_by_pass:
        diff["rc_moves_by_pass"] = {"before": b_rc_by_pass, "after": a_rc_by_pass}
    b_cons = {_consolidation_key(c): c for c in baseline.get("consolidations", [])}
    a_cons = {_consolidation_key(c): c for c in actual.get("consolidations", [])}
    for key in sorted(set(b_cons) | set(a_cons)):
        bc, ac = b_cons.get(key), a_cons.get(key)
        if bc != ac:
            diff.setdefault("consolidations", {})[key] = {"before": bc, "after": ac}
    return diff


def _engine_diff_text(before: dict | None, after: dict | None) -> str:
    before, after = before or {}, after or {}
    changed = sorted(e for e in set(before) | set(after) if before.get(e) != after.get(e))
    parts = [f"{e}: {before.get(e)!r} -> {after.get(e)!r}" for e in changed]
    return "; ".join(parts)


def _consolidation_diff_text(before: dict | None, after: dict | None) -> str:
    """Field-by-field diff of two consolidation records with the same identity key."""
    if before is None:
        return f"added: {json.dumps(after, sort_keys=True)}"
    if after is None:
        return f"removed: {json.dumps(before, sort_keys=True)}"
    fields = sorted(set(before) | set(after))
    changed = [
        f"{f}: {before.get(f)!r} -> {after.get(f)!r}"
        for f in fields
        if before.get(f) != after.get(f)
    ]
    return "; ".join(changed)


def _assignment_text(value: dict | None) -> str:
    if not value:
        return "(none)"
    extras = []
    if "sig" in value:
        extras.append(f"sig={value['sig']}")
    if "tie" in value:
        extras.append(f"tie={value['tie']}")
    if "grp" in value:
        grp = value["grp"]
        extras.append(
            f"grp={grp.get('key')} scores={json.dumps(grp.get('scores'), sort_keys=True)}"
        )
    if "fit" in value:
        extras.append(f"fit={value['fit']}")
    extra_text = f", {', '.join(extras)}" if extras else ""
    return f"{value['e']} ({value['c']}{extra_text})"


_STEP_LABELS = (
    ("scores", "per-engine scores"),
    ("first_assignment", "first assignment (resolver)"),
    ("final_assignment", "final assignment (reality check)"),
    ("cache_engine", "cache overlay engine"),
    ("moves", "first-assignment moves (pass:reason_code)"),
    ("rc_moves", "reality-check moves (pass:reason_code)"),
    ("access_class", "access-pattern class (ADR-031 Decision 14 Amendment 5)"),
)


def format_diff(sample: str, diff: dict[str, Any], query_text: dict[str, str] | None = None) -> str:
    """A readable, grouped-by-step message for a snapshot mismatch.

    ``query_text`` (optional, ``query_id -> query_text``) adds a short excerpt
    of each changed query's SQL/text under its line, so a failure doesn't
    require a separate lookup to see what the query actually does.
    """
    if not diff:
        return f"{sample}: no routing differences"
    lines = [f"{sample}: routing snapshot differs"]
    for step, label in _STEP_LABELS:
        changed = diff.get(step)
        if not changed:
            continue
        plural = "y" if len(changed) == 1 else "ies"
        lines.append(f"\n{label} changed for {len(changed)} quer{plural}:")
        for qid in sorted(changed)[:MAX_QUERIES_SHOWN]:
            d = changed[qid]
            if step == "scores":
                detail = _engine_diff_text(d["before"], d["after"])
            elif step in ("cache_engine", "moves", "rc_moves", "access_class"):
                detail = f"{d['before']!r} -> {d['after']!r}"
            else:
                detail = f"{_assignment_text(d['before'])} -> {_assignment_text(d['after'])}"
            lines.append(f"  {qid}: {detail}")
            if query_text and qid in query_text:
                excerpt = query_text[qid].strip().replace("\n", " ")[:80]
                lines.append(f"    text: {excerpt!r}")
        if len(changed) > MAX_QUERIES_SHOWN:
            lines.append(f"  ... and {len(changed) - MAX_QUERIES_SHOWN} more")
    for key, label in (
        ("moves_by_pass", "moves by pass"),
        ("rc_moves_by_pass", "reality-check moves by pass"),
    ):
        by_pass = diff.get(key)
        if by_pass:
            lines.append(f"\n{label} changed:")
            lines.append(f"  before: {json.dumps(by_pass['before'], sort_keys=True)}")
            lines.append(f"  after:  {json.dumps(by_pass['after'], sort_keys=True)}")
    cons = diff.get("consolidations")
    if cons:
        plural = "" if len(cons) == 1 else "s"
        lines.append(f"\nconsolidation record{plural} changed for {len(cons)} key{plural}:")
        for key in sorted(cons)[:MAX_QUERIES_SHOWN]:
            d = cons[key]
            lines.append(
                f"  {key[0]} -> {key[1]} ({key[2]}): {_consolidation_diff_text(d['before'], d['after'])}"
            )
        if len(cons) > MAX_QUERIES_SHOWN:
            lines.append(f"  ... and {len(cons) - MAX_QUERIES_SHOWN} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compact, diffable serialization: one query per line, sorted keys (#497
# review) -- a single minified line hid every change from `git diff` and
# GitHub's file view.
# ---------------------------------------------------------------------------


def _dump_queries_block(queries: dict[str, Any], indent: str) -> list[str]:
    lines = [f'{indent}"queries": {{']
    qids = sorted(queries)
    for i, qid in enumerate(qids):
        comma = "," if i < len(qids) - 1 else ""
        val = json.dumps(queries[qid], sort_keys=True, separators=(",", ":"))
        lines.append(f"{indent}  {json.dumps(qid)}:{val}{comma}")
    lines.append(f"{indent}}}")
    return lines


def _dump_sample_block(sample: dict[str, Any], indent: str) -> list[str]:
    lines = [f"{indent}{{"]
    keys = sorted(sample)
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        if key == "queries":
            block = _dump_queries_block(sample["queries"], indent + "  ")
            block[-1] += comma
            lines.extend(block)
        else:
            val = json.dumps(sample[key], sort_keys=True, separators=(",", ":"))
            lines.append(f"{indent}  {json.dumps(key)}:{val}{comma}")
    lines.append(f"{indent}}}")
    return lines


def dump_snapshot(document: dict[str, Any]) -> str:
    """Serialize a snapshot document (committed or extra-fixture shape).

    Every field is one compact line, except ``samples``/``sample``'s
    ``queries`` map, which gets one line per query_id, sorted -- so a diff
    lists exactly the queries that moved.
    """
    lines = ["{"]
    keys = sorted(document)
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        value = document[key]
        if key == "samples" and isinstance(value, dict):
            lines.append('  "samples": {')
            names = sorted(value)
            for si, name in enumerate(names):
                scomma = "," if si < len(names) - 1 else ""
                block = _dump_sample_block(value[name], "    ")
                block[0] = f"    {json.dumps(name)}: " + block[0].lstrip()
                block[-1] += scomma
                lines.extend(block)
            lines.append("  }" + comma)
        elif key == "sample" and isinstance(value, dict):
            block = _dump_sample_block(value, "  ")
            block[0] = '  "sample": ' + block[0].lstrip()
            block[-1] += comma
            lines.extend(block)
        else:
            val = json.dumps(value, sort_keys=True, separators=(",", ":"))
            lines.append(f"  {json.dumps(key)}:{val}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


_ABOUT = (
    "Per-query routing snapshot (tests/e2e/test_routing_snapshot.py). Update "
    'deliberately with: uv run python -m tests.e2e.routing_snapshot --write --reason "..."'
)


# ---------------------------------------------------------------------------
# --write: two independent modes (#497 review -- the committed baseline and
# the local-only ROUTING_SNAPSHOT_EXTRA fixture must never touch each other).
# ---------------------------------------------------------------------------


def _write_extra(reason: str) -> None:
    """Write the ``ROUTING_SNAPSHOT_EXTRA`` fixture's own snapshot, outside the repo.

    Never reads or writes ``BASELINE`` -- the committed snapshot is a
    completely separate file, only ever touched by ``_write``.
    """
    extra = os.environ["ROUTING_SNAPSHOT_EXTRA"]
    from tests.e2e.pipeline import run_pipeline_from_file

    extra_path = Path(extra).resolve()
    db = os.environ.get("ROUTING_SNAPSHOT_EXTRA_DB", "extra")
    out_path = extra_path.parent / f"routing_snapshot_{db}.json"
    previous = json.loads(out_path.read_text()) if out_path.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        result = run_pipeline_from_file(
            extra_path, db, Path(tmp) / "artifacts", job_id=f"routing-snapshot-{db}"
        )
        actual = measure(result.job_dir())
    diff = compare(previous.get("sample", {}), actual) if previous else {}
    print(format_diff(db, diff) if previous else f"{db}: first snapshot, nothing to diff against")
    out_path.write_text(dump_snapshot({"about": _ABOUT, "reason": reason, "sample": actual}))
    print(f"wrote {out_path} (outside the repo; never commit this file)")


def _write(reason: str) -> None:
    """Regenerate the committed baseline for every bundled sample (``pipeline.SAMPLES``)."""
    from tests.e2e.pipeline import SAMPLES, run_pipeline

    previous = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    samples: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for sample in SAMPLES:
            result = run_pipeline(
                sample, Path(tmp) / "artifacts", job_id=f"routing-snapshot-{sample}"
            )
            samples[sample] = measure(result.job_dir())
    for sample, actual in samples.items():
        diff = compare(previous.get("samples", {}).get(sample, {}), actual) if previous else {}
        print(
            format_diff(sample, diff)
            if previous
            else f"{sample}: first snapshot, nothing to diff against"
        )
    history = [*previous.get("history", []), reason]
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        dump_snapshot({"about": _ABOUT, "reason": reason, "history": history, "samples": samples})
    )
    print(f"wrote {BASELINE}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="regenerate the snapshot")
    parser.add_argument("--reason", default="", help="why the routing shape moved")
    args = parser.parse_args()
    if not args.write:
        parser.error("nothing to do: pass --write --reason '...'")
    if not args.reason.strip():
        parser.error("--reason is required: say why the routing shape moved")
    reason = args.reason.strip()
    if os.environ.get("ROUTING_SNAPSHOT_EXTRA"):
        _write_extra(reason)
    else:
        _write(reason)


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    main()
