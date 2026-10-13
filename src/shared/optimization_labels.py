"""Display strings for Aurora ``Optimization.category`` (#587 round 3).

A real-model run (schema design present) rendered the raw category code
directly in the interactive analysis report's "Aurora Design" section --
``"read_replica: Aurora cluster"``, ``"io_optimized: Aurora MySQL cluster"`` --
instead of a readable label. Shared between the three places that render an
Aurora optimization's category:

- ``src/ui/src/utils/optimizationLabels.js`` (its JS mirror, imported by
  ``src/ui/src/utils/ExportReport.js``'s standalone HTML export), kept in sync
  with this module by ``tests/unit/report/test_optimization_labels_js_sync.py``,
  the same way ``src/ui/src/utils/engineNames.js`` is kept in sync with
  ``src.shared.engine_names`` by ``test_engine_names_js_sync.py``;
- ``src/report/analysis_report.py``'s own ``OPTIMIZATION_LABELS`` constant,
  injected as a JS const into the ATX-rendered interactive report the same
  way ``ENGINE_LABELS`` is (``src/report/templates/analysis_report.js`` is
  lifted verbatim from ``ExportReport.js`` by
  ``scripts/sync_report_template.py``, so fixing the React source and
  re-running that script is enough to fix both).

Every ``category`` the Aurora schema-design contracts allow needs an entry
here: ``Optimization.category`` is declared identically in
``src.contracts.aurora_mysql_model_output`` and
``src.contracts.aurora_postgresql_model_output``
(``Literal["partitioning", "index", "read_replica", "io_optimized", "other"]``),
and ``src.contracts.aurora_design_delta.AuroraDesignDeltaContract.optimizations``
carries the same ``Optimization`` model (imported from the PostgreSQL
contract) for the LLM-delta path. ``"I/O-Optimized storage"`` matches the
wording ``src.tools.analysis.aurora_common_analysis_tools`` already uses for
the same AWS storage configuration.
"""

from __future__ import annotations

OPTIMIZATION_DISPLAY_NAMES: dict[str, str] = {
    "partitioning": "Partitioning",
    "index": "Index",
    "read_replica": "Read replica",
    "io_optimized": "I/O-Optimized storage",
    "other": "Other",
}


def optimization_label(category: str) -> str:
    """``read_replica`` -> ``Read replica``; an unknown category reads as its
    name with underscores turned to spaces, same fallback shape as
    ``src.shared.capability_labels.capability_label``."""
    return OPTIMIZATION_DISPLAY_NAMES.get(category, category.replace("_", " "))
