"""Render every customer deliverable from one synthesis report.

Single entry point shared by the local scripts and the AWS Transform integration.
It only renders; the caller decides where items go (``stage`` = write next to the
report, ``publish`` = hand to a delivery channel). No LLM calls; content is a pure
function of the artifacts apart from the export timestamp.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from . import analysis_report, pdf_report, pptx_report, renderers
from .analysis_report import GraphFetcher

logger = logging.getLogger(__name__)

FileType = Literal["HTML", "MARKDOWN", "JSON", "PDF", "PPTX"]


def _select_frame(
    frames: Sequence[traceback.FrameSummary],
) -> traceback.FrameSummary | None:
    """Pick the frame to report for a caught exception: the deepest one inside
    this codebase (a path containing ``src/``) when one exists, else the
    actual deepest frame.

    The actual deepest frame can be inside a third-party library (jinja2,
    python-pptx, ...), which isn't actionable -- our own frame nearest the
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
    name: str  # stable id, e.g. "decision-report"
    content: bytes
    file_type: FileType
    label: str  # human title, e.g. "Decision Report — discourse"
    filename: str  # download / on-disk filename
    stage: bool  # write a durable copy next to report.json
    publish: bool  # hand to the delivery channel


@dataclass
class DeliverableSet:
    items: list[Deliverable] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Query journeys embedded in the analysis report, or None when that report
    # itself failed to build (see ``errors``) and no count exists.
    journeys: int | None = None


def render_deliverables(
    store: Any,
    job_id: str,
    database_name: str,
    report_key: str,
    assignment_version: int = 1,
    graph_fetcher: GraphFetcher | None = None,
) -> DeliverableSet:
    """Render all deliverables for ``report_key``.

    Raises ``FileNotFoundError`` if the report itself is missing. The interactive
    analysis report and the executive summary are optional: a failure in either is
    recorded in ``errors`` and the rest are still returned.

    Each entry in ``errors`` and ``warnings`` is also logged here; callers should
    forward them, not re-log them. An ``errors`` entry wraps a caught exception's
    own message, which can echo the report's content (#146), so WARNING only gets
    the artifact name, the exception type and the raising frame -- never the
    message (#143); ``exc_info=True`` at DEBUG carries the full traceback.
    ``warnings`` are static, deterministic text, so they go to WARNING in full.
    """
    if not store.exists(report_key):
        raise FileNotFoundError(f"synthesis report not found: {report_key}")
    report = store.read_json(report_key)
    trust = any(r.get("schema_design_available") for r in (report.get("ranking") or []))

    def prov(artifact: str, ext: str) -> dict:
        return renderers.provenance(
            report, artifact, ext, job_id=job_id, source_artifact=report_key
        )

    out = DeliverableSet()

    decision = prov("decision-report", "html")
    engineering = prov("engineering-report", "md")
    data = prov("assessment-data", "json")
    out.items += [
        Deliverable(
            "decision-report",
            renderers.render_decision_report_html(
                report, trust_generated_summary=trust, prov=decision
            ).encode("utf-8"),
            "HTML",
            f"Decision Report — {database_name}",
            decision["filename"],
            stage=True,
            publish=True,
        ),
        Deliverable(
            "engineering-report",
            renderers.render_engineering_report_md(report, prov=engineering).encode("utf-8"),
            "MARKDOWN",
            f"Engineering Report — {database_name}",
            engineering["filename"],
            stage=True,
            publish=True,
        ),
        # The published JSON is wrapped with an identity envelope; the object at
        # report_key is NOT touched. That one is the system of record and is
        # validated against the synthesis contract on re-read, so injecting a key
        # into it would risk failing validation for the sake of a filename.
        Deliverable(
            "assessment-data",
            json.dumps({"_artifact": data, **report}, indent=2).encode("utf-8"),
            "JSON",
            f"Assessment Data (raw) — {database_name}",
            data["filename"],
            stage=False,
            publish=True,
        ),
    ]

    export_data: dict | None = None
    try:
        export_data = analysis_report.build_export_data(
            store,
            job_id,
            database_name,
            assignment_version=assignment_version,
            graph_fetcher=graph_fetcher,
        )
        analysis = prov("analysis-report", "html")
        html = analysis_report.render_analysis_report_html(
            export_data, filename=analysis["filename"]
        )
        out.items.append(
            Deliverable(
                "analysis-report",
                html.encode("utf-8"),
                "HTML",
                f"Interactive Analysis Report — {database_name}",
                analysis["filename"],
                stage=True,
                publish=True,
            )
        )
        out.journeys = (export_data.get("queryJourneys") or {}).get("total")
        if out.journeys == 0:
            out.warnings.append(
                "analysis-report: 0 query journeys embedded (graph unavailable or "
                "the job has no collector output)"
            )
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"analysis-report: {type(e).__name__}: {e}")
        logger.debug("deliverable skipped: analysis-report", exc_info=True)
        logger.warning(
            "deliverable skipped: analysis-report (%s at %s)",
            type(e).__name__,
            _raising_frame(e),
        )

    try:
        # export_data is passed even when the analysis report failed above (it is
        # then None): slide 3 needs the collector query patterns and degrades to a
        # stated gap without them, rather than losing the whole deck.
        deck, deck_pdf = pdf_report.render_executive_summary_pdf(report, export_data)
        out.items += [
            # Fixed names, unlike the other deliverables above: this is the reusable
            # executive deliverable, called the same thing in every engagement. The
            # job it belongs to is already in the key prefix (synthesis/v<N>/...), so
            # no per-round stem is needed on disk. The label still carries the round
            # (#146, #147 tracks the on-disk/download name too) so the panel can
            # tell rounds apart without opening the file.
            Deliverable(
                "executive-summary-pptx",
                deck,
                "PPTX",
                f"Executive Summary Deck — {database_name} (v{assignment_version})",
                pptx_report.FILENAME,
                stage=True,
                # The editable deck is staged but not published -- the PDF is the
                # delivery, the deck is the editable source for whoever presents it.
                publish=False,
            ),
            Deliverable(
                "executive-summary-pdf",
                deck_pdf,
                "PDF",
                f"Executive Summary Report — {database_name} (v{assignment_version})",
                pdf_report.FILENAME,
                stage=True,
                publish=True,
            ),
        ]
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"executive-summary: {type(e).__name__}: {e}")
        logger.debug("deliverable skipped: executive-summary", exc_info=True)
        logger.warning(
            "deliverable skipped: executive-summary (%s at %s)",
            type(e).__name__,
            _raising_frame(e),
        )

    for warn in out.warnings:
        logger.warning("deliverable warning: %s", warn)
    return out
