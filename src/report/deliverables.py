"""Render every customer deliverable from one synthesis report.

Single entry point shared by the local scripts and the AWS Transform integration.
It only renders; the caller decides where items go: every item is a file of the
job's ``deliverables/`` folder (``FOLDER_ENTRIES``), and ``publish`` marks the ones
handed to a delivery channel. No LLM calls; content is a pure function of the
report apart from the provenance timestamp and the code commit of the render (#1166).
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from src.shared.migration_risk_rules import backfill_report_risks
from src.shared.report_facts import build_commit, build_commit_line
from src.shared.routing_detail import load_routing_detail
from src.tools.validation.customer_text_guard import STYLE_AUDIT_FIELDS

from . import detailed_report, executive_report, renderers

logger = logging.getLogger(__name__)

FileType = Literal["MARKDOWN", "JSON", "PDF"]


def _select_frame(
    frames: Sequence[traceback.FrameSummary],
) -> traceback.FrameSummary | None:
    """Pick the frame to report for a caught exception: the deepest one inside
    this codebase (a path containing ``src/``) when one exists, else the
    actual deepest frame.

    The actual deepest frame can be inside a third-party library (jinja2,
    reportlab, ...), which isn't actionable -- our own frame nearest the
    failure is. Split out from ``_raising_frame`` so the selection logic can
    be tested against plain ``FrameSummary`` values, with no real traceback
    (and no ``exec``/``compile``) needed.
    """
    if not frames:
        return None
    for f in reversed(frames):
        path = f.filename.replace("\\", "/")
        if "/src/" in path or path.startswith("src/"):
            return f
    return frames[-1]


def _raising_frame(exc: BaseException) -> str:
    """``file:line in function`` for the frame ``_select_frame`` picks for
    ``exc``.

    No message text: the caller logs this at WARNING, and the message can echo
    the report's own content (#146) -- same reasoning as #143. The frame alone
    is still enough to find the bug without a debug-level log line.
    """
    frame = _select_frame(traceback.extract_tb(exc.__traceback__))
    if frame is None:
        return "<no traceback>"
    return f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"


@dataclass(frozen=True)
class Deliverable:
    name: str  # stable id, e.g. "engineering-report"
    content: bytes
    file_type: FileType
    label: str  # human title, e.g. "Engineering Report: discourse"
    filename: str  # download filename on the delivery channel
    publish: bool  # hand to the delivery channel


@dataclass(frozen=True)
class FolderEntry:
    """One customer file in ``artifacts/<db>/<job>/deliverables/``."""

    name: str  # the Deliverable.name it is copied from
    filename: str  # stable on-disk name, the same in every job
    title: str
    audience: str
    # The routing record behind the reports (#1364): the index lists it apart.
    audit: bool = False


# The customer deliverables folder: one per job, holding the latest assignment
# version's files under stable names. This list is the single definition the
# local render step, the folder index and every docs/command list follow
# (tests/unit/docs/test_deliverables_lists.py). Intermediate artifacts (model
# I/O, traces, versioned folders) never go here.
DELIVERABLES_DIR = "deliverables"
INDEX_FILENAME = "index.md"
FOLDER_ENTRIES: tuple[FolderEntry, ...] = (
    FolderEntry(
        "executive-report",
        "executive-report.pdf",
        "Executive report (PDF)",
        "Executives and stakeholders deciding whether to proceed",
    ),
    FolderEntry(
        "engineering-report",
        "engineering-report.md",
        "Engineering report (Markdown)",
        "The team that will build it: schemas, migration waves, risks",
    ),
    FolderEntry(
        "assessment-data",
        "assessment-data.json",
        "Assessment Data (JSON)",
        "Tools and scripts: the complete machine-readable assessment",
    ),
    FolderEntry(
        "engineering-report-detailed",
        detailed_report.FILENAME,
        "Detailed engineering report (Markdown)",
        "Engineers auditing or debugging the result: the full routing detail per table, "
        "query and group",
        audit=True,
    ),
)
FOLDER_FILENAMES: tuple[str, ...] = (
    *(e.filename for e in FOLDER_ENTRIES),
    INDEX_FILENAME,
)
# Files an older round wrote into the folder for deliverables since retired (#987:
# the decision and interactive analysis HTML reports; #1020: the multi-page PDF
# report the executive report replaced). The render step removes them, so a
# re-rendered folder never offers a report the index no longer lists.
RETIRED_FOLDER_FILENAMES: tuple[str, ...] = (
    "decision-report.html",
    "analysis-report.html",
    "assessment.pdf",
)


def render_folder_index(
    database_name: str,
    job_id: str,
    assignment_version: int,
    written: Sequence[FolderEntry],
    commit: str = "",
) -> str:
    """The ``index.md`` of the deliverables folder: each file, what it is, who
    it is for, the assignment version they were rendered from and, when given, the
    code commit of the render (``DeliverableSet.commit``, #1166)."""

    def rows(entries: Sequence[FolderEntry]) -> str:
        return "\n".join(f"| `{e.filename}` | {e.title} | {e.audience} |" for e in entries)

    audit = [e for e in written if e.audit]
    lines = [
        f"# Database Modernizer Assessment Tool: deliverables for {database_name}",
        "",
        f"Job: {job_id}",
        f"Assignment version: v{assignment_version}",
        *([f"Code commit: {commit}"] if commit else []),
        "",
        "| File | What it is | Who it is for |",
        "|---|---|---|",
        rows([e for e in written if not e.audit]),
        "",
        "Every file is rendered from the same assessment data, so a figure in one "
        "matches the same figure in the others.",
    ]
    if audit:
        lines += [
            "",
            "## For auditing the routing",
            "",
            "The record behind the reports: why each query is on its engine, step by step. "
            "You don't need it to read the assessment.",
            "",
            "| File | What it is | Who it is for |",
            "|---|---|---|",
            rows(audit),
        ]
    missing = [e for e in FOLDER_ENTRIES if e not in written]
    if missing:
        lines += [
            "",
            "Not rendered this time (see the render step's errors): "
            + ", ".join(f"`{e.filename}`" for e in missing)
            + ".",
        ]
    lines += [
        "",
        "The other folders of the job hold the pipeline's working files (per-phase "
        "outputs, versioned assignments, model inputs and traces). You don't need "
        "them to read the assessment.",
        "",
    ]
    return "\n".join(lines)


def synthesis_report_key(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> str:
    """Return the synthesis report key, preferring the highest version present.

    ``synthesis_handler`` writes ``synthesis/v{N}/report.json`` when
    ``assignment_version > 0`` and ``referee-synthesis/report.json`` at 0. The
    requested version is tried first, then any other version found under
    ``synthesis/``, then the unversioned path -- so this works for an AWS Transform
    job and a v0 local run without the caller having to know which.
    """
    candidates: list[str] = []
    if assignment_version > 0:
        candidates.append(f"{database_name}/{job_id}/synthesis/v{assignment_version}/report.json")

    prefix = f"{database_name}/{job_id}/synthesis/"
    try:
        found: list[tuple[int, str]] = []
        for raw_key in store.list_prefix(prefix):
            m = re.search(r"/synthesis/v(\d+)/report\.json$", raw_key)
            if m:
                found.append((int(m.group(1)), raw_key))
        candidates.extend(k for _, k in sorted(found, reverse=True))
    except Exception as exc:  # noqa: BLE001 - listing is best-effort
        logger.debug("Could not list %s: %s", prefix, exc)

    candidates.append(f"{database_name}/{job_id}/referee-synthesis/report.json")

    for key in candidates:
        if store.exists(key):
            return key
    raise FileNotFoundError(
        f"No synthesis report found for job {job_id!r} under "
        f"'{database_name}/{job_id}/' (tried {', '.join(dict.fromkeys(candidates))})."
    )


def folder_key(database_name: str, job_id: str, filename: str) -> str:
    """Store key of one file of the job's deliverables folder."""
    return f"{database_name}/{job_id}/{DELIVERABLES_DIR}/{filename}"


@dataclass
class DeliverableSet:
    items: list[Deliverable] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # The code commit every item was rendered from (#1166), for the folder index.
    commit: str = ""

    def folder_files(self) -> list[tuple[FolderEntry, Deliverable]]:
        """Each rendered item with the folder entry it is written as, in
        ``FOLDER_ENTRIES`` order; an entry that failed to render is absent."""
        by_name = {d.name: d for d in self.items}
        return [(e, by_name[e.name]) for e in FOLDER_ENTRIES if e.name in by_name]


def _collector_output(store: Any, report_key: str) -> dict[str, Any] | None:
    """The job's collector output next to its synthesis report, when the store has it:
    the executive report keeps every collected name out of its text (#1020)."""
    job_root = report_key.split("/synthesis/", 1)[0].split("/referee-synthesis/", 1)[0]
    key = f"{job_root}/collector/output.json"
    try:
        if store.exists(key):
            data = store.read_json(key)
            return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 - the names are best-effort; the report renders
        logger.debug("No collector output at %s: %s", key, type(exc).__name__)
    return None


def render_deliverables(
    store: Any,
    job_id: str,
    database_name: str,
    report_key: str,
    assignment_version: int = 1,
) -> DeliverableSet:
    """Render all deliverables for ``report_key``.

    Raises ``FileNotFoundError`` if the report itself is missing. The executive
    report PDF is optional: a failure is recorded in ``errors`` and the rest are still
    returned.

    Each entry in ``errors`` is also logged here; callers should forward them,
    not re-log them. An ``errors`` entry wraps a caught exception's own message,
    which can echo the report's content (#146), so WARNING only gets the artifact
    name, the exception type and the raising frame -- never the message (#143);
    ``exc_info=True`` at DEBUG carries the full traceback.
    """
    if not store.exists(report_key):
        raise FileNotFoundError(f"synthesis report not found: {report_key}")
    # #1148: a report synthesized before a report-only risk rule existed shows
    # the same risk a new synthesis writes (the same function, ADR-032).
    report = backfill_report_risks(store.read_json(report_key))
    # #1166: one commit for the whole set; each renderer only prints it.
    commit = build_commit()

    def prov(artifact: str, ext: str) -> dict:
        return renderers.provenance(
            report,
            artifact,
            ext,
            job_id=job_id,
            source_artifact=report_key,
            version=assignment_version,
            commit=commit,
        )

    out = DeliverableSet(commit=commit)

    engineering = prov("engineering-report", "md")
    data = prov("assessment-data", "json")
    out.items += [
        Deliverable(
            "engineering-report",
            renderers.render_engineering_report_md(report, prov=engineering).encode("utf-8"),
            "MARKDOWN",
            f"Engineering Report: {database_name} (v{assignment_version})",
            engineering["filename"],
            publish=True,
        ),
        # The published JSON is wrapped with an identity envelope; the object at
        # report_key is NOT touched. That one is the system of record and is
        # validated against the synthesis contract on re-read, so injecting a key
        # into it would risk failing validation for the sake of a filename.
        Deliverable(
            "assessment-data",
            # Audit fields (a model's rejected text, its warnings) are diagnostics:
            # a report written before they left report.json still carries them (#1211).
            json.dumps(
                {
                    "_artifact": data,
                    **{k: v for k, v in report.items() if k not in STYLE_AUDIT_FIELDS},
                },
                indent=2,
            ).encode("utf-8"),
            "JSON",
            f"Assessment Data (raw): {database_name} (v{assignment_version})",
            data["filename"],
            publish=True,
        ),
    ]

    collector = _collector_output(store, report_key)
    try:
        # #1364: the routing record behind the reports, read from the job's saved
        # assignments, access-pattern record and analyses (never scored again). In the
        # folder, not handed to the delivery channel: it is an audit file, and runs to
        # megabytes on a large workload.
        detailed = prov("engineering-report-detailed", "md")
        detail = load_routing_detail(store, report_key, report, collector)
        out.items.append(
            Deliverable(
                "engineering-report-detailed",
                detailed_report.render_detailed_engineering_report_md(
                    report, detail, prov=detailed
                ).encode("utf-8"),
                "MARKDOWN",
                f"Detailed Engineering Report: {database_name} (v{assignment_version})",
                detailed["filename"],
                publish=False,
            )
        )
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"engineering-report-detailed: {type(e).__name__}: {e}")
        logger.debug("deliverable skipped: engineering-report-detailed", exc_info=True)
        logger.warning(
            "deliverable skipped: engineering-report-detailed (%s at %s)",
            type(e).__name__,
            _raising_frame(e),
        )

    try:
        pdf = executive_report.render_executive_report_pdf(
            report,
            executive_report.collected_identifiers(collector),
            commit_line=build_commit_line(commit),
        )
        out.items.append(
            # The folder copy keeps the stable name; the published download name
            # and label state the round (#146, #147) so the flat panel tells
            # rounds apart.
            Deliverable(
                "executive-report",
                pdf,
                "PDF",
                f"Executive Report: {database_name} (v{assignment_version})",
                f"{executive_report.FILENAME.removesuffix('.pdf')}_v{assignment_version}.pdf",
                publish=True,
            )
        )
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"executive-report: {type(e).__name__}: {e}")
        logger.debug("deliverable skipped: executive-report", exc_info=True)
        logger.warning(
            "deliverable skipped: executive-report (%s at %s)",
            type(e).__name__,
            _raising_frame(e),
        )

    return out
