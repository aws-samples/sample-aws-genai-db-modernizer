"""The public library entry point: ``assess(request) -> result``.

>>> from src.assessment import AssessmentRequest, assess, load_collection
>>> raw = load_collection(Path("collection.txt"))  # the scripts' output; text, bytes, .json or .zip  # doctest: +SKIP
>>> result = assess(AssessmentRequest(database_name="shop", collection=raw))  # doctest: +SKIP
>>> result.statements[0].engine  # doctest: +SKIP

The request is a database's raw collection (the collection scripts' output) and
options: ``run_through`` (how far to run), ``target_engines`` (engine scope),
``deterministic`` (no model) and ``include_artifacts``.

What the result is for: engine placement. ``run_through="analysis"`` scores every
statement on every analysed engine and places nothing; ``run_through="reality_check"``
adds where each statement runs, which statements move together and which engine owns
each table. Neither is a data model: partition and sort keys, indexes and target
tables come only from schema design (``run_through="schema_design"``, which designs
every engine the placement keeps with a model; ``"synthesis"`` adds the report). Both
need ``deterministic=False``; their designs and report are in ``artifacts`` (with
``include_artifacts=True``) until a stable design surface is agreed. The
deterministic analysis's own data-model guesses are inputs to that design, not
results, and are never part of the stable surface.

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

Determinism: with ``deterministic=True`` (the default) no model is called. For
``run_through="analysis"`` the same collection, library version and engine scope give
an identical result, in any process and under any ``PYTHONHASHSEED``. For
``run_through="reality_check"`` this is not guaranteed: everything up to the model
review is deterministic, but the review (``deterministic=False``) can differ between
runs and change consolidations; each change it makes is a Move with actor
``model_flag`` in ``artifacts.assignment.moves``, and the statement it moved has
``reason_code`` ``capability_flag``. ``run_through="schema_design"`` and
``"synthesis"`` always use a model: their placement has the reality check's guarantee,
and the designs and the report's executive summary can differ between runs. Results
are never stored or reused: a repeat call is a new run.

This package's models are not the local API's request models
(``src.api.models.requests``); import them from here.
"""

from src.assessment.loading import CollectionLoadError, load_collection
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
    "CollectionLoadError",
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
    "load_collection",
]
