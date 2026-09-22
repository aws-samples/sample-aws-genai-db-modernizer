"""Interactive analysis report — the WebApp's exported report, rendered in-agent.

The React exporter (``src/ui/src/utils/ExportReport.js``) builds its report from
four REST endpoints. Neither endpoint set is reachable for an ATX job:

  * ``S3ArtifactsService.read_synthesis`` only looks at
    ``{db}/{job}/referee-synthesis/report.json``, but the orchestrated flow always
    passes ``assignment_version=1`` and therefore always writes
    ``{db}/{job}/synthesis/v1/report.json``.
  * every route resolves ``database_name`` through
    ``StepFunctionsService.describe_execution``, and an ATX job has no Step
    Functions execution at all -- it is orchestrated over A2A.

So this module reads the artifacts straight from the ``ArtifactStore``, assembles
the same ``DATA`` object the exporter would have received, and substitutes it into
the template extracted from ``ExportReport.js`` by
``scripts/sync_report_template.py``.

Nothing here re-implements the report's rendering. ``generateHTMLReport`` is a pure
function of one JSON object: its CSS is a static constant and its ~13 client-side
functions are static text, so the only thing that varies is ``const DATA``. Keeping
it that way is what makes the ATX report incapable of drifting from the WebApp's --
``tests/unit/atx_orchestrator/test_report_template_sync.py`` fails if the two
diverge.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.storage.parallel import map_parallel

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Colours are NOT defined here. The report palette lives in exactly one place -- the
# ":root" block of REPORT_CSS in src/ui/src/utils/ExportReport.js, synced into
# templates/analysis_report.css. Engine badges rendered on this side carry a
# data-engine attribute and let CSS pick the colour, so the two sides cannot drift.
ENGINE_LABELS: dict[str, str] = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "Elasticache",
    "aurora_postgresql": "AuroraPostgresql",
    "aurora_mysql": "AuroraMySQL",
    "neptune": "Neptune",
    "keyspaces": "Keyspaces",
    "aurora": "Aurora",
}

# Default cap on how many query journeys are embedded verbatim. Journeys are the
# largest part of the payload (~1.3 KB each after projection) and the browser has to
# parse all of them at load. The reference discourse job has 1654 and renders at
# ~3.9 MB, comfortably under the 6.33 MB the WebApp itself exports, so this cap only
# bites on pathologically large workloads. The flow view is fed from a
# full-population aggregate either way, so it is never silently sampled.
DEFAULT_JOURNEY_BUDGET = 3000

# Keys always embedded regardless of what the template references: flowAggregate is
# ATX-only (it does not exist in the WebApp's DATA) and jobId is used by the shell.
_ALWAYS_EMBED = ("jobId", "flowAggregate")

# Fields of a journey's ``source`` section the report actually reads.
_SOURCE_FIELDS = (
    "query_text",
    "query_type",
    "tables_accessed",
    "frequency_per_hour",
    "calls_per_second",
    "performance",
    "characteristics",
)
_ASSIGNMENT_FIELDS = ("assigned_engine", "confidence", "in_scope")


# ---------------------------------------------------------------------------
# Artifact reads
# ---------------------------------------------------------------------------


def _synthesis_key(store: Any, database_name: str, job_id: str, assignment_version: int) -> str:
    """Return the synthesis report key, preferring the highest version present.

    ``synthesis_handler`` writes ``synthesis/v{N}/report.json`` when
    ``assignment_version > 0`` and ``referee-synthesis/report.json`` at 0. The
    requested version is tried first, then any other version found under
    ``synthesis/``, then the unversioned path -- so this works for an ATX job, a
    Step Functions job, and a v0 local run without the caller having to know which.
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


def _read_schema_designs(store: Any, database_name: str, job_id: str) -> list[dict]:
    """Return ``[{target_type, artifact_path, content}]``, latest version per engine.

    Reproduces ``S3ArtifactsService.read_all_schema_designs``. The synthesis report's
    own ``schema_designs`` field cannot substitute for this: it is a dict of
    per-engine summaries (``status``, ``validation_passed``, ``tables``) with no
    ``access_patterns``, no ``unsupported_patterns`` and no per-engine
    ``trade_offs`` -- which is precisely what the Access Pattern Explorer and the
    Trade-offs tabs are built from.
    """
    prefix = f"{database_name}/{job_id}/"
    try:
        keys = store.list_prefix(prefix)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list schema designs under %s: %s", prefix, exc)
        return []

    # engine -> (version, key); version 0 means the legacy unversioned path.
    latest: dict[str, tuple[int, str]] = {}
    for key in keys:
        relative = key[len(prefix) :] if key.startswith(prefix) else key
        if not relative.startswith("schema-"):
            continue
        parts = relative.split("/")
        if len(parts) < 2:
            continue
        engine = parts[0].removeprefix("schema-")

        if len(parts) == 3 and parts[1].startswith("v") and parts[2] == "schema_output.json":
            try:
                version = int(parts[1][1:])
            except ValueError:
                continue
            if version > latest.get(engine, (-1, ""))[0]:
                latest[engine] = (version, key)
        elif len(parts) == 2 and parts[1] == "schema_output.json":
            latest.setdefault(engine, (0, key))

    designs: list[dict] = []
    for engine, (_, key) in sorted(latest.items()):
        try:
            designs.append(
                {"target_type": engine, "artifact_path": key, "content": store.read_json(key)}
            )
        except Exception as exc:  # noqa: BLE001 - one unreadable design must not lose the rest
            logger.warning("Skipping schema design %s: %s", key, exc)
    return designs


def _project_collector(collector: dict) -> dict:
    """Keep only the collector fields the report reads.

    The full artifact is large and mostly unused by the report; the source-table
    browse mode needs the query patterns and the schema table list.
    """
    queries = collector.get("queries") or {}
    patterns = []
    for p in queries.get("query_patterns") or []:
        patterns.append(
            {
                "query_id": p.get("query_id"),
                "query_text": p.get("query_text"),
                "query_type": p.get("query_type"),
                "tables_accessed": p.get("tables_accessed") or [],
                "calls_per_second": p.get("calls_per_second"),
                "frequency_per_hour": p.get("frequency_per_hour"),
            }
        )
    projected: dict[str, Any] = {
        "database_name": collector.get("database_name"),
        "queries": {"query_patterns": patterns},
    }
    schema = collector.get("schema")
    if isinstance(schema, dict):
        projected["schema"] = {"tables": schema.get("tables") or []}
    return projected


def _project_journey(journey: dict) -> dict:
    """Keep only the journey sections the report reads.

    Drops ``load_test``, ``sdk_code`` and the design's ``access_pattern`` /
    ``trade_offs`` -- the latter two are already carried per engine in
    ``schemaDesigns[].content``, so embedding them per query duplicates them 1654
    times over.
    """
    source = journey.get("source") or {}
    assignment = journey.get("assignment") or {}
    design = journey.get("design") or {}
    out: dict[str, Any] = {
        "query_id": journey.get("query_id"),
        "source": {k: source.get(k) for k in _SOURCE_FIELDS if k in source},
    }
    if assignment:
        out["assignment"] = {k: assignment.get(k) for k in _ASSIGNMENT_FIELDS if k in assignment}
    else:
        out["assignment"] = None
    if design:
        out["design"] = {"engine": design.get("engine"), "status": design.get("status")}
    else:
        out["design"] = None
    return out


def _read_journeys_from_graph(store: Any, database_name: str, job_id: str) -> list[dict] | None:
    """Serve the per-query journeys from the published context graph.

    Returns the journey list (same projected shape as the JSON path), or ``None``
    when the graph is unavailable for this job (never published, or the graph
    module/deps are absent) so the caller falls back to the per-query artifacts.
    The graph is the read-model under JOURNEY_MODE=graph; the JSON artifacts do
    not exist there, so this is the primary path, not an optimization.
    """
    import tempfile

    try:
        from src.atx_orchestrator.runtime import graph_transport
        from src.graph import GraphStore
        from src.graph.queries import query_journeys
    except Exception:  # noqa: BLE001 - graph deps unavailable; use the JSON path
        return None

    with tempfile.TemporaryDirectory(prefix=f"graph-read-{job_id}-") as tmpdir:
        local_path = str(Path(tmpdir) / "context.lbug")
        if not graph_transport.download_graph(store, database_name, job_id, local_path):
            return None
        graph_store = None
        try:
            graph_store = GraphStore(local_path)
            return query_journeys(graph_store)
        except Exception:  # noqa: BLE001 - a corrupt/unreadable graph -> JSON fallback
            logger.warning(
                "context graph unreadable for %s/%s; falling back to journey artifacts",
                database_name,
                job_id,
                exc_info=True,
            )
            return None
        finally:
            if graph_store is not None:
                with contextlib.suppress(Exception):
                    graph_store.close()


def _read_journeys(store: Any, database_name: str, job_id: str) -> list[dict]:
    # Prefer the published context graph (the read-model under JOURNEY_MODE=graph).
    # Falls through to the per-query JSON artifacts when the graph isn't available
    # (JOURNEY_MODE=json, or a job that predates the graph), so both modes render.
    from_graph = _read_journeys_from_graph(store, database_name, job_id)
    if from_graph is not None:
        return from_graph

    prefix = f"{database_name}/{job_id}/query-journeys/"
    try:
        keys = sorted(store.list_prefix(prefix))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list query journeys under %s: %s", prefix, exc)
        return []

    json_keys = [k for k in keys if k.endswith(".json")]

    # One journey read per query (1,654 on the reference discourse run) — fan
    # out through the shared store-IO helper, preserving order and skipping any
    # unreadable journey.
    return map_parallel(lambda k: _project_journey(store.read_json(k)), json_keys)


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


def _derive_reality_check(report: dict) -> dict:
    """Build the ``reality_check`` block the React exporter expects.

    The synthesis contract has no ``reality_check`` key and ATX writes no
    ``reality-check/output.json`` (Reality Check is a consolidation step, not a
    subagent), so ``synthesis.reality_check.after_distribution`` is ``undefined``
    for an ATX job. ``ranking[]`` carries the same information under different
    names, so derive it rather than shipping a report with an empty engine list.
    """
    after: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in report.get("ranking") or []:
        target = row.get("target")
        if not target:
            continue
        pct = row.get("workload_percent")
        if isinstance(pct, (int, float)) and pct > 0:
            after[target] = pct
        assigned = row.get("assigned_queries")
        if isinstance(assigned, int) and assigned > 0:
            counts[target] = assigned
    return {
        "after_distribution": after,
        "assigned_query_counts": counts,
        "derived_from": "ranking[].workload_percent",
    }


def _flow_aggregate(journeys: list[dict]) -> list[dict]:
    """``(query_type, assigned_engine) -> count`` over *every* journey.

    The Sankey only needs these counts, so aggregating here keeps the flow view
    complete even when the embedded journey list is capped by the budget. The
    browser export gets this wrong in the other direction: it asks for
    ``page_size=1000``, the API silently clamps to 200, and the resulting diagram
    covers 12% of the workload while presenting itself as the whole thing.
    """
    buckets: dict[tuple[str, str], int] = {}
    for j in journeys:
        qtype = (j.get("source") or {}).get("query_type") or "UNKNOWN"
        engine = ((j.get("assignment") or {}) or {}).get("assigned_engine") or "unassigned"
        buckets[(qtype, engine)] = buckets.get((qtype, engine), 0) + 1
    return [
        {"query_type": qt, "assigned_engine": eng, "count": n}
        for (qt, eng), n in sorted(buckets.items(), key=lambda kv: -kv[1])
    ]


def _apply_budget(journeys: list[dict], budget: int) -> tuple[list[dict], dict | None]:
    """Cap the embedded journeys, keeping the busiest queries."""
    total = len(journeys)
    if budget <= 0 or total <= budget:
        return journeys, None

    def _cps(j: dict) -> float:
        v = (j.get("source") or {}).get("calls_per_second")
        return float(v) if isinstance(v, (int, float)) else 0.0

    kept = sorted(journeys, key=_cps, reverse=True)[:budget]
    return kept, {
        "kept": len(kept),
        "total": total,
        "criterion": "calls_per_second desc",
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_export_data(
    store: Any,
    job_id: str,
    database_name: str,
    assignment_version: int = 1,
    journey_budget: int = DEFAULT_JOURNEY_BUDGET,
) -> dict:
    """Assemble the ``DATA`` object the interactive report's client code reads.

    Mirrors what ``AnalysisResults-02.js`` builds from four REST calls, but sourced
    from the ``ArtifactStore`` directly. Raises only if the synthesis report itself
    is missing; every other artifact degrades to an empty section.
    """
    report_key = _synthesis_key(store, database_name, job_id, assignment_version)
    report = store.read_json(report_key)

    if "reality_check" not in report:
        report = {**report, "reality_check": _derive_reality_check(report)}

    triage: dict | None = None
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if store.exists(triage_key):
        try:
            triage = store.read_json(triage_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read triage %s: %s", triage_key, exc)

    collector: dict = {}
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    if store.exists(collector_key):
        try:
            collector = _project_collector(store.read_json(collector_key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read collector %s: %s", collector_key, exc)

    schema_designs = _read_schema_designs(store, database_name, job_id)
    if not schema_designs:
        logger.warning(
            "No schema-design artifacts for job %s: the Access Pattern Explorer and "
            "Trade-offs sections will render as unavailable. This is the documented "
            "gap when schema design has not run, not a failure.",
            job_id,
        )

    all_journeys = _read_journeys(store, database_name, job_id)
    flow = _flow_aggregate(all_journeys)
    journeys, truncated = _apply_budget(all_journeys, journey_budget)

    query_journeys = {
        "job_id": job_id,
        "total": len(all_journeys),
        "page": 1,
        "page_size": len(journeys),
        "total_pages": 1,
        "items": journeys,
    }
    if truncated:
        query_journeys["truncated"] = truncated

    return {
        "results": {
            "job_id": job_id,
            "status": "COMPLETED",
            "synthesis": report,
            "triage_summary": triage,
        },
        "schemaDesigns": schema_designs,
        "collector": collector,
        "jobId": job_id,
        "exportDate": datetime.now(UTC).isoformat(),
        "queryJourneys": query_journeys,
        "flowAggregate": flow,
        "sourceArtifact": report_key,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _read_template(name: str) -> str:
    path = _TEMPLATE_DIR / name
    text = path.read_text(encoding="utf-8")
    # Strip the generated-by banner; it belongs in the repo, not the deliverable.
    return re.sub(r"^\s*(<!--|//|/\*).*?(-->|\*/)?\s*\n", "", text, count=1)


def _data_keys_used_by_template() -> set[str]:
    """Which ``DATA.<key>`` the extracted client code actually reads.

    ``ExportReport.js`` hands its client code more than it consumes, so this is read
    off the template rather than assumed. Only optional-chained and plain property
    access are used in that file (``DATA.results``, ``DATA.queryJourneys?.items``), so
    a property-name scan is sufficient; there is no computed ``DATA[expr]`` access.
    """
    js = _read_template("analysis_report.js")
    if re.search(r"\bDATA\s*\??\s*\[", js):  # pragma: no cover - defensive
        raise ValueError(
            "the report template uses computed DATA[...] access, so the embedded key "
            "set can no longer be determined statically. Embed every key explicitly."
        )
    return set(re.findall(r"\bDATA\s*\??\.\s*([A-Za-z_$][A-Za-z0-9_$]*)", js))


def _engine_badges(after_distribution: dict) -> str:
    return "".join(
        f'<span class="badge" data-engine="{html.escape(str(engine), quote=True)}">'  # nosemgrep: string-concat-in-list -- intentional multi-line string
        f"{html.escape(ENGINE_LABELS.get(engine, str(engine)))}</span>"
        for engine in after_distribution
    )


def _banner_html(export_data: dict) -> str:
    truncated = (export_data.get("queryJourneys") or {}).get("truncated")
    if not truncated:
        return ""
    kept, total = truncated["kept"], truncated["total"]
    return (
        '    <div class="section" style="border-left: 4px solid var(--color-accent);">\n'
        f"      <strong>Partial query detail.</strong> This report embeds the "
        f"{kept:,} busiest of {total:,} query journeys "
        f"({truncated['criterion']}) to keep the file openable. The Query Flow "
        f"diagram and every engine, cost and trade-off figure above cover all "
        f"{total:,} queries; only the per-query drill-down table is sampled.\n"
        "    </div>\n"
    )


def _provenance_html(meta: dict) -> str:
    """Provenance as a comment, not a visible footer card.

    The report is a customer deliverable; the file name, job id and S3 source key are
    plumbing, and an S3 key must never be shown to the customer. They stay in the file
    -- here and in the ``x-dbmod-*`` meta tags -- so any report can still be traced
    back to the job that produced it.
    """
    fields = (
        f"{meta['filename']} | job {meta['job_id']} | database {meta['database']} | "
        f"generated {meta['generated']} | source {meta['source_artifact']}"
    )
    # A "--" inside a comment ends it early in some parsers; none of these values
    # normally carry one, but a database or file name could.
    return f"    <!-- dbmod provenance: {fields.replace('--', '-')} -->\n"


def _meta_html(meta: dict) -> str:
    rows = [
        ("artifact", meta["artifact"]),
        ("database", meta["database"]),
        ("job-id", meta["job_id"]),
        ("generated", meta["generated"]),
        ("filename", meta["filename"]),
        ("source-artifact", meta["source_artifact"]),
    ]
    return "\n".join(
        f'  <meta name="x-dbmod-{name}" content="{html.escape(str(value))}">'
        for name, value in rows
    )


def _chart_js_tag() -> str:
    """Inline Chart.js when vendored, else fall back to the CDN.

    The browser export loads Chart.js from ``cdn.jsdelivr.net``. An ATX report is
    mailed around and opened on networks that may not reach a public CDN, where the
    CDN version renders two blank canvases with no error at all. Inlining trades
    ~210 KB for a report that always renders.
    """
    vendored = _TEMPLATE_DIR / "vendor" / "chart.umd.min.js"
    if vendored.exists():
        return "<script>\n" + vendored.read_text(encoding="utf-8") + "\n  </script>"
    logger.info("Chart.js not vendored; falling back to the CDN tag")
    return (
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/'
        'chart.umd.min.js"></script>'
    )


def render_analysis_report_html(export_data: dict, filename: str = "") -> str:
    """Render the interactive analysis report from an assembled ``DATA`` object."""
    synthesis = (export_data.get("results") or {}).get("synthesis") or {}
    database_name = synthesis.get("database_name") or "N/A"
    job_id = export_data.get("jobId") or ""
    generated = export_data.get("exportDate") or datetime.now(UTC).isoformat()

    after = (synthesis.get("reality_check") or {}).get("after_distribution") or {}
    cost_rows = (synthesis.get("tco_analysis") or {}).get("cost_breakdown") or []
    projected = sum(
        r.get("monthly_cost_usd") or 0
        for r in cost_rows
        if isinstance(r.get("monthly_cost_usd"), (int, float))
    )
    total_patterns = sum(
        len((d.get("content") or {}).get("access_patterns") or [])
        for d in export_data.get("schemaDesigns") or []
    )

    meta = {
        "artifact": "analysis-report",
        "database": database_name,
        "job_id": job_id,
        "generated": generated,
        "filename": filename or f"{database_name}_analysis-report_{job_id[:8]}.html",
        "source_artifact": export_data.get("sourceArtifact") or "",
    }

    # Embed only what the client code actually reads. The WebApp's exporter passes
    # `collector` through unused -- 1.06 MB of dead JSON in a 4.9 MB artifact for the
    # reference job. Deriving the key set from the template rather than hardcoding it
    # means a future UI change that starts reading DATA.collector picks it up on the
    # next sync, with no matching edit needed here.
    used = _data_keys_used_by_template() | set(_ALWAYS_EMBED)
    available = {
        "results": export_data.get("results"),
        "schemaDesigns": export_data.get("schemaDesigns"),
        "collector": export_data.get("collector"),
        "jobId": job_id,
        "queryJourneys": export_data.get("queryJourneys"),
        "flowAggregate": export_data.get("flowAggregate"),
    }
    unknown = used - set(available)
    if unknown:
        raise ValueError(
            "the report template reads DATA keys this module does not assemble: "
            + ", ".join(sorted(unknown))
            + ". Add them to build_export_data."
        )
    data_payload = {k: v for k, v in available.items() if k in used}

    script = (
        "  <script>\n"
        f"    const DATA = {_json(data_payload)};\n"
        f"    const ENGINE_LABELS = {_json(ENGINE_LABELS, compact=True)};\n"
        + _read_template("analysis_report.js")
        + "\n  </script>\n"
    )

    exported_human = generated.replace("T", " ").split(".")[0].replace("+00:00", "") + " UTC"

    replacements = {
        "__CHART_JS__": _chart_js_tag(),
        "__CSS__": _read_template("analysis_report.css"),
        "__META__": _meta_html(meta),
        "__BANNER__": _banner_html(export_data),
        "__PROVENANCE__": _provenance_html(meta),
        "__SCRIPT__": script,
        # The template's <title> already reads "Analysis Report - __TITLE__"; upstream
        # substitutes the bare job id there, so this must not repeat the prefix.
        "__TITLE__": html.escape(f"{database_name} \u2014 {job_id[:8]}"),
        "__JOB_ID__": html.escape(job_id),
        "__EXPORT_DATE__": html.escape(exported_human),
        "__DATABASE_NAME__": html.escape(str(database_name)),
        "__SUMMARY__": html.escape(synthesis.get("summary") or "No summary available."),
        "__ENGINE_BADGES__": _engine_badges(after),
        "__PROJECTED_COST__": f"{projected:,.2f}",
        "__TOTAL_PATTERNS__": str(total_patterns),
    }

    template = _read_template("analysis_report.html.tpl")

    # Check the template, not the rendered output: real customer data legitimately
    # contains __UPPER__ tokens (a captured query referencing __NULL__, a generated
    # key pattern), and scanning the finished document for them fails on valid input.
    # The invariant that matters is that every placeholder the sync script emitted has
    # a value here.
    unfilled = sorted(set(re.findall(r"__[A-Z_]+__", template)) - set(replacements))
    if unfilled:
        raise ValueError(
            "analysis report template has unfilled placeholders: "
            + ", ".join(unfilled)
            + ". Re-run scripts/sync_report_template.py and add the new placeholders "
            "to render_analysis_report_html."
        )

    out = template
    for placeholder, value in replacements.items():
        out = out.replace(placeholder, value)
    return out


def _json(obj: Any, compact: bool = False) -> str:
    """Serialise for embedding in a ``<script>`` block.

    ``</script>`` appearing inside a JSON string -- entirely possible in a captured
    SQL statement or a free-text trade-off -- would otherwise terminate the script
    element early and break the whole report. Escaping ``<`` is the standard fix and
    stays valid JSON.
    """
    text = (
        json.dumps(obj, separators=(",", ":"), default=str)
        if compact
        else json.dumps(obj, indent=2, default=str)
    )
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
