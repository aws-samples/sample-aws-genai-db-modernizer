"""Results routes — analysis results, table mappings, deliverables and raw artifacts."""

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.api.dependencies import job_id_param, validated_filename
from src.api.services.local_execution import LocalExecutionService
from src.api.services.local_s3 import LocalS3Service
from src.report.deliverables import FOLDER_ENTRIES, INDEX_FILENAME
from src.report.facts import report_facts
from src.report.formatting import label_summary_counts
from src.report.renderers import resolve_migration_waves
from src.report.risks import NO_OPEN_RISKS_CAVEAT
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.contract_versioning import ContractVersionError
from src.shared.database_name import validate_database_name
from src.shared.migration_risk_rules import backfill_report_risks
from src.shared.prose_style import collector_identifiers
from src.shared.report_facts import (
    analysis_overview_facts,
    routed_cache_fit_facts,
    triage_signal_facts,
)

router = APIRouter(prefix="/api/v1/assessments", tags=["results"])

sfn_service: LocalExecutionService | None = None
s3_service: LocalS3Service | None = None


def _get_database_name(job_id: str) -> str:
    """Resolve database_name from Step Functions execution input."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    execution = sfn_service.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    db_name: str = execution.get("input", {}).get("database_name", "")
    return db_name


@router.get("/{job_id}/results")
async def get_results(job_id: str = Depends(job_id_param)):
    """Get full analysis results (executive summary, architecture, TCO, risks)."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    synthesis = s3_service.read_synthesis(db_name, job_id)
    if not synthesis:
        raise HTTPException(status_code=404, detail="Results not available yet")

    # #1148: the risks a report-only rule raises, for a report synthesized before
    # the rule existed, the same ones the deliverables show (before its facts).
    synthesis = backfill_report_risks(synthesis)
    # One wave source (#225): the UI reads this same payload, so a
    # report written before #225 must show the same fallback roadmap the
    # executive report and engineering report already derive, not an empty one.
    if not synthesis.get("migration_waves"):
        synthesis["migration_waves"] = resolve_migration_waves(synthesis) or None
    # #615 (ADR-032): the UI reads every figure from synthesis.facts; a report
    # synthesized before that field existed gets the same block here, from the
    # same function, so the UI never falls back to a formula of its own. A block
    # written before contract 1.26 gains its access-pattern totals the same way (#792),
    # one written before 1.28 its query move counts and resolved risks by severity (#1020),
    # one written before 1.38 its per-kind unservable counts (#888), one written before
    # the retired-engine statements existed gains them (#1076), and one written before
    # #1146 its query-pattern figures.
    facts = synthesis.get("facts")
    unservable = facts.get("unservable") if isinstance(facts, dict) else None
    if (
        not isinstance(facts, dict)
        or "access_pattern_totals" not in facts
        or "moving_query_count" not in facts
        or "resolved_risks_by_severity" not in facts
        or "retired_in_use_engines" not in facts
        or "query_pattern_counts" not in facts
        or (isinstance(unservable, dict) and "no_fit_query_count" not in unservable)
    ):
        synthesis["facts"] = report_facts(synthesis)

    # #996: the deterministic summary stored by an older run can name the other targets
    # by raw engine id and word its risk and access-pattern counts the old way. The
    # engineering report relabels it at render time with ``label_summary_counts``, only
    # when the summary is the deterministic one; this payload does the same, so the UI
    # only prints the text and the two agree. A generated summary is left as written.
    summary = synthesis.get("summary")
    if isinstance(summary, str) and summary == synthesis.get("summary_deterministic"):
        synthesis["summary"] = label_summary_counts(summary, synthesis)

    triage = s3_service.read_triage(db_name, job_id)

    return {
        "job_id": job_id,
        "status": "COMPLETED",
        "synthesis": synthesis,
        "triage_summary": triage,
        # #996: the one definition of the no-open-risks sentence the engineering and
        # executive reports print; the results page shows it when no risk is open.
        "no_open_risks_caveat": NO_OPEN_RISKS_CAVEAT,
    }


# The deliverables folder's own index (#955) is listed and downloadable next to
# the files it describes (src/report/deliverables.py FOLDER_ENTRIES).
_INDEX_TITLE = "Folder index (Markdown)"
_INDEX_AUDIENCE = "Everyone: what each file is and who it is for"

_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".json": "application/json",
}


def _deliverables_db_name(job_id: str) -> str:
    """The job's database name, validated like request input (#957 review):
    it comes from the job's ``_meta.json`` and reaches a filesystem path here."""
    try:
        return validate_database_name(_get_database_name(job_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Deliverable not available") from exc


def _deliverable_entries() -> list[tuple[str, str, str]]:
    """(filename, title, audience) for every file the folder can hold, in order."""
    return [(e.filename, e.title, e.audience) for e in FOLDER_ENTRIES] + [
        (INDEX_FILENAME, _INDEX_TITLE, _INDEX_AUDIENCE)
    ]


@router.get("/{job_id}/deliverables")
async def list_deliverables(job_id: str = Depends(job_id_param)):
    """The files in the job's ``deliverables/`` folder (#957), for the results
    page's download panel: each listed file the folder holds, with its title,
    audience, size and download path. Empty until the folder is rendered."""
    db_name = _deliverables_db_name(job_id)
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    files = []
    try:
        folder = sfn_service.deliverables_dir(db_name, job_id)
    except ValueError:
        folder = None
    if folder is not None:
        for filename, title, audience in _deliverable_entries():
            try:
                path = sfn_service.deliverable_file(db_name, job_id, filename)
            except ValueError:
                path = None
            if path is None:
                continue
            files.append(
                {
                    "filename": filename,
                    "title": title,
                    "audience": audience,
                    "size_bytes": os.path.getsize(path),
                    "path": f"assessments/{job_id}/deliverables/{filename}",
                }
            )
    return {"job_id": job_id, "files": files}


@router.get("/{job_id}/deliverables/{filename}")
async def download_deliverable(
    job_id: str = Depends(job_id_param), filename: str = Depends(validated_filename)
):
    """Download one file of the job's ``deliverables/`` folder (#957). Only a
    file the folder's definition names is served, from inside that folder,
    always as an attachment with ``nosniff``. A retired HTML report an older
    round left in the folder is not a name the definition carries, so it is
    never served (#987)."""
    db_name = _deliverables_db_name(job_id)
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    try:
        path = sfn_service.deliverable_file(db_name, job_id, filename)
    except ValueError:
        path = None
    if path is None:
        raise HTTPException(status_code=404, detail="Deliverable not available")
    name = os.path.basename(path)
    headers = {"X-Content-Type-Options": "nosniff"}
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(os.path.splitext(name)[1], "application/octet-stream"),
        filename=name,
        content_disposition_type="attachment",
        headers=headers,
    )


@router.get("/{job_id}/collector")
async def get_collector_output(job_id: str = Depends(job_id_param)):
    """Get raw collector output artifact."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_collector(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Collector output not available")
    return data


@router.get("/{job_id}/triage")
async def get_triage_output(job_id: str = Depends(job_id_param)):
    """Get raw triage decisions artifact."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_triage(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Triage output not available")
    # The signal figures the job summary page shows are computed here, once (#1319, ADR-032).
    return {**data, "facts": triage_signal_facts(data)}


@router.get("/{job_id}/analysis/{agent_type}")
async def get_analysis_output(agent_type: str, job_id: str = Depends(job_id_param)):
    """Get raw analysis output for a specific agent type."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_analysis(db_name, job_id, agent_type)
    if not data:
        raise HTTPException(
            status_code=404, detail=f"Analysis output for {agent_type} not available"
        )
    # The engine overview figures are computed here, once, for every job old or new; the
    # pattern analysis page only formats them (#1312, ADR-032).
    # Triage only adds the signal figures: a triage file this code cannot read must not
    # take the analysis away from the page (it did not read triage before #1312).
    signals = None
    withheld = None
    try:
        triage = s3_service.read_triage(db_name, job_id)
        found = triage.get("signals") if isinstance(triage, dict) else None
        signals = found if isinstance(found, list) else []
    except ContractVersionError:
        withheld = "the triage output is at another contract version"
    except Exception:
        withheld = "the triage output could not be read"
    facts = analysis_overview_facts(data, signals, agent_type)
    if agent_type in CACHE_ENGINES:
        # #1325: the cache layer's "Avg cache fit" is the fit of the reads routed to it, the
        # figure the reports state; the analysis average above stays for audit.
        facts.update(
            routed_cache_fit_facts(*s3_service.read_routed_cache_fit(db_name, job_id, agent_type))
        )
    if withheld:
        facts["signal_query_total"] = 0
        facts["signal_count"] = 0
        facts["signals_withheld_reason"] = withheld
    return {**data, "facts": facts}


@router.get("/{job_id}/reality-check")
async def get_reality_check(job_id: str = Depends(job_id_param)):
    """Get reality check output — engine consolidations and recommendations."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_reality_check(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Reality check output not available")
    return data


@router.get("/{job_id}/results/table-mappings")
async def get_table_mappings(
    job_id: str = Depends(job_id_param),
    limit: int = Query(default=25, ge=1, le=500, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
    recommended_db: str | None = Query(default=None, description="Filter by target engine"),
    sort: str = Query(
        default="confidence:desc",
        description="Sort field:direction — supported: confidence:asc, confidence:desc, source_table:asc, source_table:desc",
    ),
):
    """Get paginated table mappings from the synthesis report.

    Reads table_mappings from the synthesis artifact and returns a paginated,
    filterable slice. Returns 404 until synthesis has completed.
    """
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    synthesis = s3_service.read_synthesis(db_name, job_id)
    if not synthesis:
        raise HTTPException(
            status_code=404, detail="Results not available yet — synthesis has not completed"
        )

    all_mappings: list[dict] = synthesis.get("table_mappings", [])

    # Filter by recommended_db if provided
    if recommended_db:
        all_mappings = [
            m
            for m in all_mappings
            if m.get("recommended_database", "").lower() == recommended_db.lower()
        ]

    # Sort
    sort_field, _, sort_dir = sort.partition(":")
    reverse = sort_dir.lower() != "asc"
    if sort_field == "confidence":
        all_mappings = sorted(
            all_mappings, key=lambda m: m.get("confidence_score", 0), reverse=reverse
        )
    elif sort_field == "source_table":
        all_mappings = sorted(
            all_mappings, key=lambda m: m.get("source_table", "").lower(), reverse=reverse
        )

    total = len(all_mappings)
    page = all_mappings[offset : offset + limit]

    return {
        "table_mappings": page,
        "total_count": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{job_id}/schema-designs")
async def get_schema_designs(job_id: str = Depends(job_id_param)):
    """Get all schema design artifacts."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    designs = s3_service.read_all_schema_designs(db_name, job_id)
    # The UI's trade-offs tab and explorer read these designs directly, so the
    # model's customer prose goes through the same plain-prose list the report
    # uses (#1109); the design on disk keeps the model's text.
    collector = s3_service.read_collector(db_name, job_id)
    names = collector_identifiers(collector if isinstance(collector, dict) else None)
    cleaned = []
    for design in designs:
        content, _ = clean_schema_design_prose(
            str(design.get("target_type", "")), design.get("content"), names
        )
        cleaned.append({**design, "content": content})
    return {"schema_designs": cleaned}
