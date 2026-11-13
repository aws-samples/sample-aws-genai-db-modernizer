# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A change made in the results page's assumptions panel (#1991): record it, then re-run
only the phases it affects, through the orchestrator's own resume.

The values are checked by the assumptions file's rules (``src.shared.assumptions_file``)
before this is called. A growth change re-runs synthesis and the deliverables; a peak
change also needs schema design (``src.shared.assumptions.RERUN_PHASES``). The change is
recorded under ``decisions/assumptions.json`` with origin ``ui``.

Only a finished job can change, one change to a job at a time (the job's lock,
``src.orchestrator.job_lock``, which ``/resume`` and the schema confirm routes take
too). A re-run that needs a model this server does not have is refused before anything
is written, saying why: the API's orchestrator runs with ``llm_mode="none"``, so it
can't rewrite an executive summary or a schema design a model wrote. On a job run
without a model, schema design is not run (it designs nothing without one, as in the
job's own run), and the result says so.

The result names the phases that ran (``reran``), the ones skipped and why
(``skipped``), and those a job the scripts made only had recorded as done
(``recorded_done``, ``LocalOrchestrator.adopt_finished_job``: nothing runs for them).
When any phase fails, the decision, the synthesis reports, the deliverables folder and
the progression are put back as they were, so the panel, the gate and the deliverables
never disagree (ADR-032).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.contracts.phase_models import Phase
from src.orchestrator.job_lock import job_lock
from src.shared.assumptions import (
    ASSUMPTIONS_FILE,
    RERUN_PHASES,
    SETTABLE,
    origin_label,
    record_assumptions,
    settable_ranges,
)
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: The phases a change can re-run, in pipeline order.
_ORDER = ("schema_design", "synthesis", "report")
#: How a refusal or a note names a phase.
_PHASE_NAMES = {
    "schema_design": "schema design",
    "synthesis": "synthesis",
    "report": "the deliverables",
}

NO_MODEL = "this local server runs without a model (llm-mode none)"
NEW_RUN = "To use a different value, start a new run with these assumptions."
SCHEMA_DESIGN_SKIPPED_NOTE = (
    "Schema design needs a model, and " + NO_MODEL + ": no design was written, as in the "
    "job's own run. The deliverables state the new peak, and the designs take it on a run "
    "with a model."
)


class AssumptionChangeRefused(Exception):
    """Why the change can't be made here; nothing was written."""


class AssumptionRerunFailed(Exception):
    """Re-running ``phase`` failed; the job was put back as it was."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(frozen=True)
class ChangePlan:
    """The phases a change needs, in order; ``refusal`` says why it can't run here."""

    phases: tuple[str, ...]
    refusal: str | None = None
    notes: list[str] = field(default_factory=list)
    skipped: tuple[str, ...] = ()


def _names(keys: Mapping[str, Any] | list[str]) -> str:
    return " and ".join(SETTABLE[k][0] for k in SETTABLE if k in keys)


def _latest_report(store: ArtifactStore, db: str, job_id: str) -> dict | None:
    from src.report.deliverables import synthesis_report_key

    try:
        key = synthesis_report_key(store, db, job_id, 0)
    except FileNotFoundError:
        return None
    report = store.read_json(key)
    return report if isinstance(report, dict) else None


def _has_schema_designs(store: ArtifactStore, db: str, job_id: str) -> bool:
    from src.shared.design_records import is_real_design_key

    prefix = f"{db}/{job_id}/"
    # A skip record (#2154) is no design: a run without a model writes one per engine.
    return any(
        key.startswith(f"{prefix}schema-") and is_real_design_key(key, store.read_json)
        for key in store.list_prefix(prefix)
    )


def change_plan(
    store: ArtifactStore,
    db: str,
    job_id: str,
    keys: list[str],
    llm_mode: str,
    job_status: str = "SUCCEEDED",
) -> ChangePlan:
    """What changing ``keys`` (recorded keys, :data:`SETTABLE`) needs for the job, and
    whether this server can run it (``llm_mode`` is its orchestrator's, ``job_status``
    the job's execution status)."""
    unknown = [k for k in keys if k not in RERUN_PHASES]
    if unknown or not keys:
        raise ValueError("no assumption a customer can set")
    wanted = {phase for k in keys for phase in RERUN_PHASES[k]}
    phases = tuple(p for p in _ORDER if p in wanted)
    report = _latest_report(store, db, job_id)
    if report is None:
        return ChangePlan(
            phases,
            refusal=(
                "The job has no results yet, so there is nothing to re-run. Nothing was "
                "changed. " + NEW_RUN
            ),
        )
    if job_status != "SUCCEEDED":
        return ChangePlan(
            phases,
            refusal=(
                f"The job is not finished (its status is {job_status.lower()}), so its "
                "assumptions can't change now. Nothing was changed."
            ),
        )
    without_model = llm_mode == "none"
    if without_model and "schema_design" in phases and _has_schema_designs(store, db, job_id):
        return ChangePlan(
            phases,
            refusal=(
                f"Changing the {_names(keys)} means redesigning the schemas, and only a model "
                f"can do that; {NO_MODEL}. Nothing was changed. " + NEW_RUN
            ),
        )
    summary_source = report.get("summary_source") or "deterministic"
    if without_model and "synthesis" in phases and summary_source != "deterministic":
        why = (
            "this job's executive summary was written with a model"
            if summary_source == "llm"
            else "this job's synthesis asked a model for its executive summary (the model's "
            "summary was set aside and the deterministic one kept)"
        )
        return ChangePlan(
            phases,
            refusal=(
                f"Changing the {_names(keys)} re-runs synthesis, and {why}; {NO_MODEL}, so "
                "it can't re-run that synthesis as it ran. Nothing was changed. " + NEW_RUN
            ),
        )
    skipped = ("schema_design",) if without_model and "schema_design" in phases else ()
    notes = [SCHEMA_DESIGN_SKIPPED_NOTE] if skipped else []
    return ChangePlan(phases, notes=notes, skipped=skipped)


def panel_state(
    store: ArtifactStore, db: str, job_id: str, llm_mode: str, job_status: str = "SUCCEEDED"
) -> dict[str, Any]:
    """What the results page's assumptions panel shows (#1991): each assumption a
    customer can set, with the value, origin and line the job's report states (its
    ``facts.assumptions`` item, as written: a report written before it has none), its
    range and re-run phases (``settable_ranges``), and whether this server can make the
    change (``available``; ``unavailable_reason`` says why not, ``notes`` what it can't
    re-run)."""
    report = _latest_report(store, db, job_id) or {}
    facts = report.get("facts") if isinstance(report.get("facts"), dict) else {}
    block = (facts or {}).get("assumptions") or {}
    items = {i.get("key"): i for i in block.get("assumptions") or [] if isinstance(i, Mapping)}
    rows = []
    for row in settable_ranges():
        item = items.get(row["name"]) or {}
        origin = item.get("origin")
        plan = change_plan(store, db, job_id, [row["key"]], llm_mode, job_status)
        rows.append(
            {
                **row,
                "value": item.get("setting"),
                "origin": origin,
                "origin_label": origin_label(origin) if origin else None,
                "line": item.get("line"),
                "available": plan.refusal is None,
                "unavailable_reason": plan.refusal,
                "notes": plan.notes,
            }
        )
    return {"job_id": job_id, "llm_mode": llm_mode, "assumptions": rows}


class _Snapshot:
    """A copy of what a change writes, put back when a phase fails: the decision, the
    synthesis reports, the deliverables folder and the job's progression."""

    def __init__(self, root: str, db: str, job_id: str) -> None:
        base = os.path.realpath(root)
        job_dir = os.path.realpath(os.path.join(base, db, job_id))
        if not job_dir.startswith(base + os.sep):
            raise ValueError("the job's folder is outside the artifact root")
        self.root = Path(base)
        self.paths = [
            Path(db, job_id, ASSUMPTIONS_FILE),
            Path(db, job_id, "synthesis"),
            Path(db, job_id, "deliverables"),
            Path(".meta", f"{job_id}.json"),
        ]
        self.dir = Path(tempfile.mkdtemp(prefix="assumption-change-"))
        self.had: dict[Path, bool] = {}
        for rel in self.paths:
            src = self.root / rel
            self.had[rel] = src.exists()
            if src.is_dir():
                shutil.copytree(src, self.dir / rel)
            elif src.exists():
                (self.dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, self.dir / rel)

    def restore(self) -> None:
        for rel in self.paths:
            live = self.root / rel
            if live.is_dir():
                shutil.rmtree(live)
            elif live.exists():
                live.unlink()
            if not self.had[rel]:
                continue
            saved = self.dir / rel
            if saved.is_dir():
                shutil.copytree(saved, live)
            else:
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(saved, live)

    def discard(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def apply_assumption_change(
    orchestrator: Any,
    store: ArtifactStore,
    db: str,
    job_id: str,
    values: Mapping[str, float],
    artifact_root: str,
    job_status: str = "SUCCEEDED",
) -> dict[str, Any]:
    """Record ``values`` (checked, by recorded key) with origin ``ui`` and re-run the
    phases they need: the orchestrator's resume for synthesis (and schema design when a
    model is there), then the deliverables folder. AssumptionChangeRefused (nothing
    written) when this server can't run them; AssumptionRerunFailed when a phase fails,
    with every file the change wrote put back; JobBusyError when another change to the
    job is running."""
    from src.report.deliverables_folder import render_job_deliverables
    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    # The same rule every route checks them by, before any path is built (#1991 review).
    db, job_id = validate_database_name(db), validate_job_id(job_id)
    llm_mode = str(getattr(orchestrator, "llm_mode", "none"))
    with job_lock(job_id):
        plan = change_plan(store, db, job_id, list(values), llm_mode, job_status)
        if plan.refusal:
            raise AssumptionChangeRefused(plan.refusal)
        snapshot = _Snapshot(artifact_root, db, job_id)
        ran: list[str] = []
        phase = "synthesis"
        try:
            adopt = getattr(orchestrator, "adopt_finished_job", None)
            recorded_done = [p.value for p in adopt(job_id, db)] if adopt else []
            met = getattr(orchestrator, "prerequisites_met", None)
            if met is not None and not met(job_id, Phase.SYNTHESIS):
                raise AssumptionChangeRefused(
                    "The job's phases before synthesis are not all done, so synthesis can't "
                    "re-run now. Nothing was changed."
                )
            record = record_assumptions(store, db, job_id, dict(values), via="ui")
            report: dict[str, Any] | None = None
            for phase in plan.phases:
                if phase in plan.skipped:
                    continue
                if phase == "report":
                    report = render_job_deliverables(job_id, db, artifact_root, None, graph=False)
                    if report.get("status") == "error":
                        raise RuntimeError(str(report.get("message")))
                else:
                    orchestrator.resume(job_id, Phase(phase))
                ran.append(phase)
        except AssumptionChangeRefused:
            snapshot.restore()
            raise
        except Exception as exc:  # noqa: BLE001 -- the reason goes to the log, a plain one out
            logger.warning("assumption change: re-running %s failed", phase, exc_info=True)
            snapshot.restore()
            raise AssumptionRerunFailed(
                phase,
                f"Re-running {_PHASE_NAMES[phase]} failed ({type(exc).__name__}; the API log "
                f"has the detail), so the {_names(list(values))} change was undone: the job "
                "is as it was. Apply the change again to retry.",
            ) from exc
        finally:
            snapshot.discard()
    return {
        "recorded": {SETTABLE[k][0]: record[k] for k in SETTABLE if k in values},
        "origins": {SETTABLE[k][0]: record["origins"][k] for k in SETTABLE if k in values},
        "reran": ran,
        "skipped": list(plan.skipped),
        "recorded_done": recorded_done,
        "notes": plan.notes,
        "deliverables": (report or {}).get("status"),
    }


__all__ = [
    "AssumptionChangeRefused",
    "AssumptionRerunFailed",
    "ChangePlan",
    "NEW_RUN",
    "SCHEMA_DESIGN_SKIPPED_NOTE",
    "apply_assumption_change",
    "change_plan",
    "panel_state",
]
