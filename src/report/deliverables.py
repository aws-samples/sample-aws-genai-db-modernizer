"""Render every customer deliverable from one synthesis report.

Single entry point shared by the local scripts and the AWS Transform integration.
It only renders; the caller decides where items go (``stage`` = write next to the
report, ``publish`` = hand to a delivery channel). Deterministic: no LLM calls.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import analysis_report, pdf_report, pptx_report, renderers
from .analysis_report import GraphFetcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deliverable:
    name: str  # stable id, e.g. "decision-report"
    content: bytes
    file_type: str  # "HTML" | "MARKDOWN" | "JSON" | "PDF" | "PPTX"
    label: str  # human title, e.g. "Decision Report — discourse"
    filename: str  # download / on-disk filename
    stage: bool  # write a durable copy next to report.json
    publish: bool  # hand to the delivery channel


@dataclass
class DeliverableSet:
    items: list[Deliverable] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"analysis-report: {type(e).__name__}: {e}")

    try:
        deck, deck_pdf = pdf_report.render_executive_summary_pdf(report, export_data)
        out.items += [
            Deliverable(
                "executive-summary-pptx",
                deck,
                "PPTX",
                f"Executive Summary Deck — {database_name}",
                pptx_report.FILENAME,
                stage=True,
                publish=False,
            ),
            Deliverable(
                "executive-summary-pdf",
                deck_pdf,
                "PDF",
                f"Executive Summary Report — {database_name}",
                pdf_report.FILENAME,
                stage=True,
                publish=True,
            ),
        ]
    except Exception as e:  # noqa: BLE001 - optional deliverable
        out.errors.append(f"executive-summary: {type(e).__name__}: {e}")

    for err in out.errors:
        logger.warning("deliverable skipped: %s", err)
    return out
