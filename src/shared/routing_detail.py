"""Read a job's saved routing records for the detailed engineering report (#1364).

The routing detail behind an assessment lives in the job's artifacts, next to its
synthesis report: the assignment versions and their Moves (``assignment/v<N>/
assignment.json``), the effective version's access-pattern record
(``assignment/v<N>/access_patterns.json``) and each engine's analysis
(``analysis-<engine>/analysis.json``). :func:`load_routing_detail` reads them from the
store, builds each query's decision trace from its Moves with the one trace builder
(``src.shared.query_trace``), and hands everything to the facts layer
(``src.shared.report_facts.routing_detail_facts``), which the renderer only formats.

Nothing is scored again: every score comes from a Move's ladder, the access-pattern
record or an engine's analysis. A job saved before one of those records existed, or a
store without them, gives an empty input; the facts say which, and the report states
the absence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from src.shared.catalog.engines import engine_ids
from src.shared.catalog.write_intents import carried_write_floor
from src.shared.engine_names import without_placeholder_engines
from src.shared.query_trace import (
    assignment_lineage,
    build_query_trace,
    ledger_moves,
    query_excerpt,
)
from src.shared.report_facts import routing_detail_facts

logger = logging.getLogger(__name__)

ACCESS_PATTERNS_FILE = "access_patterns.json"


def job_root_of(report_key: str) -> str:
    """``<db>/<job>`` of a synthesis report key."""
    return report_key.split("/synthesis/", 1)[0].split("/referee-synthesis/", 1)[0]


def _read(store: Any, key: str) -> dict | None:
    """The JSON object at ``key``, or None when it is missing or unreadable."""
    try:
        if store.exists(key):
            data = store.read_json(key)
            return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 - a missing record is stated, not raised
        logger.debug("routing detail: cannot read %s: %s", key, type(exc).__name__)
    return None


def effective_version_of(report: Mapping[str, Any], report_key: str) -> int:
    """The assignment version the report was synthesized from: ``assignment_summary``'s,
    else the report key's ``synthesis/v<N>``, else 0 (none)."""
    summary = report.get("assignment_summary")
    version = summary.get("version") if isinstance(summary, Mapping) else None
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        return version
    m = re.search(r"/synthesis/v(\d+)/", report_key)
    return int(m.group(1)) if m else 0


def _analyses(store: Any, root: str) -> dict[str, dict]:
    """Each catalog engine's ``analysis-<engine>/analysis.json`` the job saved, read at its
    known path (no listing of the job's objects, which on S3 is every key it wrote)."""
    out = {}
    for engine in sorted(engine_ids()):
        data = _read(store, f"{root}/analysis-{engine}/analysis.json")
        if data is not None:
            out[engine] = data
    return without_placeholder_engines(out)


def _excerpts(collector: Mapping[str, Any] | None) -> dict[str, str]:
    out = {}
    for q in ((collector or {}).get("queries") or {}).get("query_patterns") or []:
        if isinstance(q, Mapping) and q.get("query_id"):
            text = query_excerpt(q.get("query_text"))
            if text:
                out[str(q["query_id"])] = text
    return out


def load_routing_detail(
    store: Any,
    report_key: str,
    report: Mapping[str, Any],
    collector: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The routing detail (``RoutingDetail`` as JSON) of the job ``report_key`` belongs to."""
    root = job_root_of(report_key)
    version = effective_version_of(report, report_key)

    def read(v: int) -> dict | None:
        return _read(store, f"{root}/assignment/v{v}/assignment.json")

    lineage = assignment_lineage(read, version) if version else []
    effective = lineage[-1] if lineage else {}
    by_query = ledger_moves(lineage)
    record = (
        _read(store, f"{root}/assignment/v{version}/{ACCESS_PATTERNS_FILE}") if version else None
    )
    candidates = {
        str(r.get("query_id")): r.get("candidate_keys") or []
        for r in (record or {}).get("queries") or []
        if isinstance(r, Mapping) and r.get("query_id")
    }
    qas = [
        qa
        for qa in effective.get("query_assignments") or []
        if isinstance(qa, Mapping) and qa.get("query_id")
    ]
    plan = {str(qa["assigned_engine"]) for qa in qas if qa.get("assigned_engine")}
    traces = {
        str(qa["query_id"]): build_query_trace(
            str(qa["query_id"]),
            by_query.get(str(qa["query_id"]), []),
            qa.get("assigned_engine"),
            plan,
            version,
            candidates.get(str(qa["query_id"])),
        ).model_dump(mode="json", by_alias=True)
        for qa in qas
    }
    return routing_detail_facts(
        report,
        lineage,
        traces,
        record,
        _analyses(store, root),
        _excerpts(collector),
        poor_fit_floor=carried_write_floor(),
    )
