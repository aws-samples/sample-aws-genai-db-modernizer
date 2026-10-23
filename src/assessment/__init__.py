"""The public library entry point: ``assess(request) -> result``.

>>> from src.assessment import AssessmentRequest, assess
>>> result = assess(AssessmentRequest(database_name="shop", collection=raw))  # doctest: +SKIP
>>> result.statements[0].engine  # doctest: +SKIP

The request is a database's raw collection (the collection scripts' output) and
options: ``run_through`` (how far to run), ``target_engines`` (engine scope),
``deterministic`` (no model) and ``include_artifacts``.

What the result is for: engine placement. ``run_through="analysis"`` scores every
statement on every analysed engine and places nothing; ``run_through="reality_check"``
adds where each statement runs, which statements move together and which engine owns
each table. Neither is a data model: partition and sort keys, indexes and target
tables come only from schema design (``run_through="schema_design"``, which needs a
model and is not available in this release). The deterministic analysis's own
data-model guesses are inputs to that design, not results, and are never part of the
stable surface.

Stable surface (contract ``1.0``, semantic versioning): ``contract_version``,
``label`` (``analysis_only``, ``recommendation`` or ``designed``: only a designed
result carries a data model), ``library_version``, ``ruleset_version``/``ruleset_hash``, ``options`` (as applied),
``scope`` (echoed), ``summary``, ``statements[]`` and ``groups[]``. Read a statement's
engine from ``statements[].engine`` and a table's owner from ``summary.tables[].owner``;
both are final for the run. ``reason_text`` and every ``reason`` (an engine's, a
consolidation's, a table's) are customer-facing sentences: use ``reason_code`` and
``role`` rather than parsing them.

Not stable: ``artifacts`` (only with ``include_artifacts=True``) carries the pipeline's
internal contracts verbatim, each with its own internal version, and has no
compatibility promise: they change as routing evolves.

Determinism: with ``deterministic=True`` (the default) no model is called, and the
same request on the same library version gives the same result.

This package's models are not the local API's request models
(``src.api.models.requests``); import them from here.
"""

from src.assessment.models import (
    CONTRACT_VERSION,
    SUPPORTED_RUN_THROUGH,
    Artifacts,
    AssessmentOptions,
    AssessmentRequest,
    AssessmentResult,
    Consolidation,
    EngineLine,
    EngineRole,
    Group,
    PatternRef,
    PublicReasonCode,
    RelationalEngine,
    ResultLabel,
    RunThrough,
    StatementResult,
    StatementType,
    Summary,
    TableLine,
    WorkloadScope,
)
from src.assessment.runner import assess

__all__ = [
    "CONTRACT_VERSION",
    "SUPPORTED_RUN_THROUGH",
    "Artifacts",
    "AssessmentOptions",
    "AssessmentRequest",
    "AssessmentResult",
    "Consolidation",
    "EngineLine",
    "EngineRole",
    "Group",
    "PatternRef",
    "PublicReasonCode",
    "RelationalEngine",
    "ResultLabel",
    "RunThrough",
    "StatementResult",
    "StatementType",
    "Summary",
    "TableLine",
    "WorkloadScope",
    "assess",
]
