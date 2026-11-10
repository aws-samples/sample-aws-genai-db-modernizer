# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``/wave-edit <N>``: revisit one wave's design and its review warnings (#2067).

A wave can be suggested with design review warnings the customer accepts by approving
it (maintainer decision on #2051). This is how the customer says which they do not
accept, and sends only that wave's engines back to their design with them:

1. :func:`wave_view`: wave N's readiness line from the report's facts, its engines and
   queries, and each warning and blocker, numbered (warnings first). A held wave's
   warnings are listed too (a review with a blocker can carry warnings), so they can be
   rejected in the same round as its blockers.
2. :func:`decide`: records which warnings the customer accepts and which they do not
   (``decisions/wave-edit/wave-<N>/v<k>.json``, a new version each time, never
   overwritten), each by its number in that list and its own text. Then it starts a
   round of the #1992 loop on wave N's engines only: their designs move aside, and the
   request each design input carries (``redesign_request.json``) holds the rejected
   warnings (``rejected_warning``, the reviewer's text framed as untrusted data) with
   the wave's blockers a redesign can fix. Nothing to send back (every warning
   accepted, no such blocker): no round, and synthesis runs once so the report states
   the decision.
3. :func:`check`, after the engines are designed and reviewed again and synthesis ran:
   closes the round with the loop's own rule (``wave_redesign._close``). A design is
   kept only when its review ran and it is strictly better, here on ``[holds,
   unsupported queries, warnings the new review lists]``; otherwise the earlier design
   is put back. The new review's warnings are a new list: they are never matched to the
   ones the customer rejected (a reviewer rewords them), so the customer decides on them
   in the next ``/wave-edit``, or accepts them by approving the wave. A further round
   runs only for a blocker still left. At most :data:`MAX_REDESIGN_ROUNDS` rounds per
   decision, counted apart from the automatic loop's; a design that fails to run is
   rolled back with ``run_wave_redesign.py --rollback``, as in the loop.

Routing never changes: no assignment version is written and no query moves (moving
queries stays with ``/assign``). Without a model no design runs, so no redesign can.
The rounds go into the version's ``wave-redesign/v<N>/rounds.json`` with the loop's,
marked ``wave_edit``; synthesis copies the decisions into ``report.json``
(``wave_warning_decisions``), and each wave's readiness line states the customer's
decision (``src.shared.wave_readiness``), one fact for every deliverable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.agents.schema_design import wave_redesign
from src.agents.schema_design.wave_redesign import (
    _FINAL,
    AWAITING_REVIEW,
    FIXED,
    MAX_REDESIGN_ROUNDS,
    REJECTED_WARNING,
    _clear_requests,
    _close,
    _designed,
    _open_round,
    _set_aside,
    engine_score,
    fixable_requests,
    log_key,
    pending_reviews,
    read_log,
    request_key,
    review_warnings_of,
)
from src.contracts.synthesis_output import DecidedWarning
from src.shared.engine_names import display_engine
from src.shared.wave_readiness import _warning_phrase
from src.storage.artifact_store import ArtifactStore

_DECISION = re.compile(r"decisions/wave-edit/wave-(\d+)/v(\d+)\.json$")

NO_MODEL = (
    "No redesign can run without a model (--llm-mode none): every schema designer needs "
    "one. Run the assessment with a model to revisit a wave's design."
)

_WARNING = ("engine", "category", "target", "change")


def decision_key(db: str, job_id: str, wave: int, n: int) -> str:
    return f"{db}/{job_id}/decisions/wave-edit/wave-{wave}/v{n}.json"


def read_decisions(
    store: ArtifactStore, db: str, job_id: str, version: int, wave: int | None = None
) -> list[dict[str, Any]]:
    """The wave edit decisions recorded on assignment ``version`` (of ``wave`` only, when
    given), oldest first, each checked against its contract."""
    from src.contracts.wave_edit_decision import WaveEditDecision
    from src.shared.contract_versioning import load_contract

    found: list[dict[str, Any]] = []
    for key in store.list_prefix(f"{db}/{job_id}/decisions/wave-edit/"):
        m = _DECISION.search(key.replace("\\", "/"))
        if not m or (wave is not None and int(m.group(1)) != wave):
            continue
        record = load_contract(WaveEditDecision, store.read_json(key), artifact=key)
        if record.assignment_version == version:
            found.append(record.model_dump(mode="json"))
    return sorted(found, key=lambda d: (d["wave"], d["decision_version"]))


def _row(report: Mapping[str, Any], wave: int) -> Mapping[str, Any] | None:
    for row in (report.get("facts") or {}).get("waves") or []:
        if isinstance(row, Mapping) and int(row.get("wave") or 0) == wave:
            return row
    return None


def _query_count(report: Mapping[str, Any], wave: int) -> int | None:
    for w in report.get("migration_waves") or []:
        if isinstance(w, Mapping) and int(w.get("wave") or 0) == wave:
            count = w.get("query_count")
            return int(count) if isinstance(count, int) else None
    return None


def _warnings(report: Mapping[str, Any], row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every warning of the last review of each of the wave's designed engines, in the
    wave's engine order: the list the customer decides on, by number."""
    designed = _designed(report)
    return [
        {k: str(w.get(k) or "") for k in _WARNING}
        for e in row.get("engines") or []
        if str(e) in designed
        for w in review_warnings_of(report, str(e))
    ]


def _fixable(report: Mapping[str, Any], wave: int, engines: list[str]) -> dict[str, Any]:
    return {e: r for e, r in fixable_requests(report, waves={wave}).items() if e in engines}


def wave_view(report: Mapping[str, Any], wave: int) -> dict[str, Any]:
    """Wave ``wave`` as the command shows it: its readiness line, engines and queries, and
    each warning and blocker, numbered (warnings first). A blocker says whether a
    redesign can fix it; one it cannot is in the readiness line's next steps."""
    row = _row(report, wave)
    if row is None:
        return {"status": "error", "message": f"the report has no wave {wave}"}
    engines = [str(e) for e in row.get("engines") or []]
    fixable = _fixable(report, wave, engines)
    sent = {(e, c.get("line")) for e, r in fixable.items() for c in r["checks"]}
    items: list[dict[str, Any]] = []
    for w in _warnings(report, row):
        items.append(
            {
                "n": len(items) + 1,
                "kind": "warning",
                "engine": w["engine"],
                "line": _warning_phrase(w),
            }
        )
    for h in row.get("holds") or []:
        if not isinstance(h, Mapping):
            continue
        items.append(
            {
                "n": len(items) + 1,
                "kind": "blocker",
                "engine": h.get("engine"),
                "line": h.get("line"),
                "redesign": (h.get("engine"), h.get("line")) in sent,
            }
        )
    return {
        "status": "shown",
        "wave": wave,
        "readiness": row.get("readiness"),
        "engines": engines,
        "query_count": _query_count(report, wave),
        "readiness_line": row.get("readiness_line"),
        "items": items,
        "warnings": sum(1 for i in items if i["kind"] == "warning"),
        "blockers": sum(1 for i in items if i["kind"] == "blocker"),
    }


def _rejected_check(w: Mapping[str, Any]) -> dict[str, Any]:
    engine = str(w.get("engine") or "")
    target = f" on {w['target']}" if w.get("target") else ""
    return {
        "check": REJECTED_WARNING,
        "risk_id": None,
        "severity": None,
        "line": (
            f"the customer does not accept {display_engine(engine)} design review warning "
            f"{w.get('n')}: revise the design so its review no longer raises it"
        ),
        "detail": f"{w.get('category') or 'warning'}{target}: {w.get('change') or ''}".strip(),
        "mitigation": None,
        "query_ids": [],
        "warning": {k: str(w.get(k) or "") for k in _WARNING},
    }


def _requests(
    report: Mapping[str, Any],
    wave: int,
    rejected: Sequence[Mapping[str, Any]],
    exclude: set[str],
) -> dict[str, dict[str, Any]]:
    """Per engine of wave ``wave`` (outside ``exclude``): its blockers a redesign can fix
    and the warnings the customer rejected, each exactly as that review listed it."""
    row = _row(report, wave) or {}
    engines = [str(e) for e in row.get("engines") or [] if str(e) not in exclude]
    out = _fixable(report, wave, engines)
    for w in rejected:
        engine = str(w.get("engine") or "")
        if engine not in engines:
            continue
        entry = out.setdefault(engine, {"engine": engine, "waves": [wave], "checks": []})
        entry["checks"].append(_rejected_check(w))
    return {e: out[e] for e in sorted(out)}


def _start(
    store: ArtifactStore,
    db: str,
    job_id: str,
    report: Mapping[str, Any],
    version: int,
    wave: int,
    decision: int,
    requests: dict[str, dict[str, Any]],
    log: dict[str, Any],
    edit_round: int,
) -> dict[str, Any]:
    """Start round ``edit_round`` of the wave edit: the #1992 loop's set-aside and
    request, on ``requests``' engines only."""
    n = len(log["rounds"]) + 1
    affected: dict[str, list[int] | None] = {}
    for engine, request in requests.items():
        affected[engine] = _set_aside(store, db, job_id, version, n, engine, request, False)
        store.write_json(
            request_key(db, job_id, engine, version),
            {"round": edit_round, "max_rounds": MAX_REDESIGN_ROUNDS, "wave_edit": wave, **request},
        )
    log["rounds"].append(
        {
            "round": n,
            "assignment_version": version,
            "engines": sorted(requests),
            "requests": [requests[e] for e in sorted(requests)],
            "before": {
                e: [*engine_score(report, e), len(review_warnings_of(report, e))]
                for e in sorted(requests)
            },
            "parts": affected,
            "outcome": None,
            "wave_edit": wave,
            "decision_version": decision,
        }
    )
    store.write_json(log_key(db, job_id, version), log)
    return {
        "status": "awaiting_redesign",
        "wave": wave,
        "round": edit_round,
        "max_rounds": MAX_REDESIGN_ROUNDS,
        "engines": sorted(requests),
        "decision_version": decision,
        "assignment_version": version,
    }


def _next_version(store: ArtifactStore, db: str, job_id: str, wave: int) -> int:
    n = 1
    while store.exists(decision_key(db, job_id, wave, n)):
        n += 1
    return n


def _decided(w: Mapping[str, Any], n: int) -> DecidedWarning:
    return DecidedWarning(n=n, **{k: str(w.get(k) or "") for k in _WARNING})


def decide(
    store: ArtifactStore,
    db: str,
    job_id: str,
    report: Mapping[str, Any],
    version: int,
    *,
    wave: int,
    reject: list[int],
    auto: bool = False,
    llm_mode: str = "external",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the customer's decision on wave ``wave``'s warnings (``reject``: the item
    numbers of :func:`wave_view` they do not accept; every other warning is accepted)
    and start the wave edit's first round. One status line (see the module docstring)."""
    if llm_mode == "none":
        return {"status": "no_model", "message": NO_MODEL}
    view = wave_view(report, wave)
    if view["status"] != "shown":
        return view
    log = read_log(store, db, job_id, version)
    running = _open_round(log)
    if running is not None:
        return {
            "status": "error",
            "message": (
                f"redesign round {running.get('round')} is still running: finish it, or roll "
                "it back with run_wave_redesign.py --rollback, first"
            ),
        }
    by_n = {i["n"]: i for i in view["items"]}
    for n in reject:
        if n not in by_n:
            return {"status": "error", "message": f"wave {wave} has no item {n}"}
        if by_n[n]["kind"] != "warning":
            return {
                "status": "error",
                "message": f"item {n} is a blocker: it always goes back to the design",
            }
    listed = _warnings(report, _row(report, wave) or {})
    chosen = set(reject)
    rejected = [_decided(w, i) for i, w in enumerate(listed, 1) if i in chosen]
    accepted = [_decided(w, i) for i, w in enumerate(listed, 1) if i not in chosen]
    requests = _requests(report, wave, [w.model_dump() for w in rejected], set())
    if not listed and not requests:
        return {
            "status": "nothing_to_edit",
            "wave": wave,
            "message": (
                f"wave {wave} has no design review warning and no blocker a redesign can fix"
            ),
        }
    from src.contracts.wave_edit_decision import WaveEditDecision

    k = _next_version(store, db, job_id, wave)
    record = WaveEditDecision(
        wave=wave,
        decision_version=k,
        assignment_version=version,
        accepted=accepted,
        rejected=rejected,
        blockers=[
            str(c.get("line"))
            for r in requests.values()
            for c in r["checks"]
            if c.get("check") != REJECTED_WARNING
        ],
        auto=auto,
        recorded_at=now or datetime.now(UTC),
    )
    store.write_json(decision_key(db, job_id, wave, k), record.model_dump(mode="json"))
    counts = {"accepted": len(accepted), "rejected": len(rejected), "decision_version": k}
    if not requests:
        return {"status": "awaiting_synthesis", "wave": wave, **counts}
    return {**_start(store, db, job_id, report, version, wave, k, requests, log, 1), **counts}


def _engine_outcome(report: Mapping[str, Any], engine: str, outcome: str | None) -> dict:
    """One engine's result, stated without claiming any rejected warning is fixed: the new
    review's warnings are counted, never matched to the old ones."""
    warnings = len(review_warnings_of(report, engine))
    if outcome in ("fixed", "improved"):
        said = (
            "redesigned; the new review approved it with no warnings"
            if outcome == FIXED
            else (
                f"redesigned; the new review lists {warnings} design review "
                f"{'warning' if warnings == 1 else 'warnings'} to accept"
                if warnings
                else "redesigned; a blocker is still left"
            )
        )
    elif outcome == "not_reviewed":
        said = "the review did not run, so the earlier design is kept"
    elif outcome == "did_not_improve":
        said = "the redesign was not better, so the earlier design is kept"
    elif outcome == "failed":
        said = "the redesign failed, so the earlier design is kept"
    else:
        said = "not redesigned"
    return {"outcome": outcome, "review_warnings": warnings, "summary": said}


def check(
    store: ArtifactStore,
    db: str,
    job_id: str,
    report: Mapping[str, Any],
    version: int,
    *,
    wave: int,
) -> dict[str, Any]:
    """After synthesis: close the wave edit's running round from ``report`` (keep the
    design when its review ran and it is strictly better, else put the earlier one back),
    then start its next round while a blocker a redesign can fix is left and rounds
    remain, or end ``complete``."""
    decisions = read_decisions(store, db, job_id, version, wave)
    if not decisions:
        return {
            "status": "error",
            "message": f"no wave edit decision for wave {wave}: record one with --reject first",
        }
    k = int(decisions[-1]["decision_version"])
    log = read_log(store, db, job_id, version)
    running = _open_round(log)
    if running is not None and (running.get("wave_edit"), running.get("decision_version")) != (
        wave,
        k,
    ):
        return {
            "status": "error",
            "message": f"redesign round {running.get('round')} is not this wave edit's",
        }
    pending = pending_reviews(store, db, job_id, version, log)
    if pending:
        # The redesign's review is its own step here (#2081): never close without it.
        return {"status": AWAITING_REVIEW, "missing_reviews": pending, "wave": wave}
    restored = _close(store, db, job_id, version, log, report)
    edit = [
        r for r in log["rounds"] if r.get("wave_edit") == wave and r.get("decision_version") == k
    ]
    if restored:
        store.write_json(log_key(db, job_id, version), log)
        return {"status": "awaiting_synthesis", "restored": restored, "rounds": len(edit)}
    if edit:
        # Through the module, so the loop's one writer is the one patched in tests.
        wave_redesign._write_report_log(store, db, job_id, version, log)
    done = {
        str(e)
        for r in edit
        for e, out in (r.get("outcome") or {}).items()
        if out in _FINAL or out == FIXED
    }
    # A further round only for a blocker left: the new review's warnings are the
    # customer's to decide on again, never sent back unasked.
    requests = _requests(report, wave, [], done)
    if requests and edit and len(edit) < MAX_REDESIGN_ROUNDS:
        return _start(store, db, job_id, report, version, wave, k, requests, log, len(edit) + 1)
    _clear_requests(store, db, job_id, version, log)
    store.write_json(log_key(db, job_id, version), log)
    row = _row(report, wave) or {}
    last: dict[str, str] = {}
    for r in edit:
        last.update(r.get("outcome") or {})
    return {
        "status": "complete",
        "wave": wave,
        "rounds": len(edit),
        "max_rounds": MAX_REDESIGN_ROUNDS,
        "outcome": dict((edit[-1].get("outcome") or {}) if edit else {}),
        "engines": {e: _engine_outcome(report, e, out) for e, out in sorted(last.items())},
        "readiness": row.get("readiness"),
        "readiness_line": row.get("readiness_line"),
        "decision_version": k,
    }
