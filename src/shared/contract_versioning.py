"""Read-side contract version check (ADR-008 Decision 4, #971).

Every stored contract carries a ``contract_version`` in MAJOR.MINOR form
(Decision 1). A reader accepts any MINOR of the MAJOR its model declares (a
MINOR only adds optional fields, Decision 5) and refuses a different MAJOR
with a :class:`ContractVersionError` that names the artifact, the stored
version and the version this code reads, unless ``_ADAPTERS`` below holds an
adapter from that MAJOR (one should ship in the same change as the bump, per the
amendment to Decision 4; the only one, RealityCheckOutputContract 1.x, is a
retrofit in #971 for the 2.0 bump of #790). A payload with no ``contract_version`` was written before #972
gave every contract one; it is read as legacy, with a debug log.

- :func:`load_contract` checks the version and parses the model. The loaded
  model says the current version, so writing it back (a ``.meta`` progression
  update, an approved assignment) stores today's version, not the one read.
- :func:`check_contract_version` checks a raw dict against a model.
- :func:`check_artifact` checks a raw dict by its artifact path, through the
  contract file registry below, and returns it (through its adapter for an
  older MAJOR). Every ``ArtifactStore.read_json`` returns what it returns, so
  the raw-dict reads of contract files are checked in one place.
- :func:`carries_contract` tells a stored contract from a raw upload (a
  collection has no ``contract_version``) and checks the contract's version.

``tests/contract/test_reads_check_contract_version.py`` keeps every read on
these helpers.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable, Mapping
from functools import cache
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_VERSION = re.compile(r"^(\d+)\.(\d+)$")


class ContractVersionError(Exception):
    """A stored contract's ``contract_version`` is not one this code can read.

    Not a ``ValueError``, so the many ``except ValueError`` around artifact reads
    do not turn a refusal into "not available". ``str(exc)`` is the full detail
    for logs (artifact, contract, versions); :attr:`public_message` is what a
    user is shown, with no path or code name in it.
    """

    def __init__(self, *, artifact: str, contract: str, stored: object, expected: str) -> None:
        self.artifact = artifact
        self.contract = contract
        self.stored = stored
        self.expected = expected
        major = expected.split(".", 1)[0]
        if isinstance(stored, str) and _VERSION.match(stored):
            reason = (
                f"this code reads {contract} MAJOR {major} (any {major}.x; current "
                f"{expected!r}), and a different MAJOR has no adapter"
            )
        else:
            reason = f"it is not MAJOR.MINOR; this code reads {contract} {expected!r}"
        shown = repr(stored)
        if len(shown) > 40:  # a stored value is file content: never echo much of it
            shown = shown[:37] + "..."
        super().__init__(
            f"{artifact}: stored {contract} contract_version {shown} cannot be read: "
            f"{reason} (ADR-008 Decision 4)"
        )

    @property
    def public_message(self) -> str:
        """The user-facing refusal: no artifact path, class name or policy reference."""
        major = self.expected.split(".", 1)[0]
        stored = (
            self.stored if isinstance(self.stored, str) and _VERSION.match(self.stored) else None
        )
        found = f"contract {stored}" if stored else "an unreadable contract version"
        return (
            "This assessment was saved by an incompatible version of the tool "
            f"({found}, expected {major}.*)."
        )


def current_version(model: type[BaseModel]) -> str:
    """The ``contract_version`` default ``model`` writes today."""
    field = model.model_fields.get("contract_version")
    default = None if field is None or field.is_required() else field.default
    if not isinstance(default, str) or not _VERSION.match(default):
        raise TypeError(
            f"{model.__qualname__} has no MAJOR.MINOR contract_version default to check against"
        )
    return default


# A MAJOR bump ships its adapter in the same change (the 2026-10-16 amendment to
# Decision 4): "module:Class" -> {stored MAJOR: function that turns a payload of
# that MAJOR into one of the model's current MAJOR}. A different MAJOR with no
# adapter here is refused.
Adapter = Callable[[dict[str, Any]], dict[str, Any]]


def _reality_check_from_1(payload: dict[str, Any]) -> dict[str, Any]:
    """RealityCheckOutputContract 2.0 (#790) removed ``lightweight_recommendations``,
    which was always empty; every other 1.x field reads as it was."""
    return {k: v for k, v in payload.items() if k != "lightweight_recommendations"}


_ADAPTERS: dict[str, dict[int, Adapter]] = {
    "src.contracts.reality_check_output:RealityCheckOutputContract": {1: _reality_check_from_1},
}


def _adapter(model: type[BaseModel], major: int) -> Adapter | None:
    return _ADAPTERS.get(f"{model.__module__}:{model.__qualname__}", {}).get(major)


def _stored_major(
    payload: Mapping[str, Any], model: type[BaseModel], *, artifact: str
) -> int | None:
    """The stored MAJOR (``None`` for a legacy payload), or raise for an unreadable one."""
    expected = current_version(model)
    if "contract_version" not in payload:
        logger.debug(
            "%s: no contract_version, read as legacy %s (written before #972)",
            artifact,
            model.__qualname__,
        )
        return None
    stored = payload["contract_version"]
    match = _VERSION.match(stored) if isinstance(stored, str) else None
    major = int(match.group(1)) if match else None
    if major is None or (
        major != int(expected.split(".", 1)[0]) and _adapter(model, major) is None
    ):
        raise ContractVersionError(
            artifact=artifact, contract=model.__qualname__, stored=stored, expected=expected
        )
    return major


def check_contract_version(
    payload: Mapping[str, Any], model: type[BaseModel], *, artifact: str
) -> str | None:
    """Check ``payload``'s stored version against ``model``; return it (``None`` if legacy).

    Raises :class:`ContractVersionError` for a different MAJOR that has no adapter,
    or a value that is not MAJOR.MINOR.
    """
    _stored_major(payload, model, artifact=artifact)
    stored = payload.get("contract_version")
    return None if stored is None else str(stored)


def read_payload(
    payload: Mapping[str, Any], model: type[BaseModel], *, artifact: str
) -> Mapping[str, Any]:
    """Check ``payload`` and return it in the model's current MAJOR.

    The same object for a payload of the current MAJOR (or a legacy one); for an
    older MAJOR with an adapter, a new dict in the current shape, whose
    ``contract_version`` is the model's current one.
    """
    major = _stored_major(payload, model, artifact=artifact)
    current_major = int(current_version(model).split(".", 1)[0])
    if major is None or major == current_major:
        return payload
    adapter = _adapter(model, major)
    if adapter is None:  # pragma: no cover - _stored_major refused a MAJOR without one
        raise AssertionError(f"no adapter from MAJOR {major}")
    logger.debug(
        "%s: %s %s read through its MAJOR %d adapter",
        artifact,
        model.__qualname__,
        payload["contract_version"],
        major,
    )
    return {**adapter(dict(payload)), "contract_version": current_version(model)}


def load_contract[M: BaseModel](model: type[M], payload: Mapping[str, Any], *, artifact: str) -> M:
    """Check the stored version, then parse ``payload`` as ``model`` at the current version.

    The returned model's ``contract_version`` is the model's current default: the
    stored MINOR's fields were read into today's shape, so writing the model back
    stores today's version. A file at a newer MINOR is relabelled to this code's
    MINOR too. A model that keeps unknown fields (``extra="allow"``) then carries
    them under the older label, and one that ignores them (``Assignment``) drops
    them: a writer that must keep a newer MINOR's fields patches the stored dict
    and stamps it with ``stamp_artifact_version`` instead (approving an assignment
    as-is, #1056).
    ``payload`` itself is not changed, and a ``ValidationError`` shows it as stored.
    """
    data = read_payload(payload, model, artifact=artifact)
    loaded = model.model_validate(data)
    return loaded.model_copy(update={"contract_version": current_version(model)})


def carries_contract(payload: Mapping[str, Any], model: type[BaseModel], *, artifact: str) -> bool:
    """``True`` when ``payload`` is a stored ``model`` (it has a ``contract_version``).

    Used where an upload is either a contract or a raw collection (which carries
    no ``contract_version``). A contract at another MAJOR raises
    :class:`ContractVersionError` rather than being taken for a raw collection.
    """
    if "contract_version" not in payload:
        return False
    check_contract_version(payload, model, artifact=artifact)
    return True


# ------------------------------------------------------- contract file registry

# Model I/O: files a model session reads or writes (requests, inputs, responses,
# group drafts and their inputs), not contracts this tool stores. Their
# ``contract_version`` is whatever the model echoed; the writer that turns a
# response into a stored contract (``finalize_schema_design``, the reality check
# and synthesis ``--finalize``, the group merge) validates it and writes the
# contract's own version. They are never checked on read, so finalizing works
# whatever version a model put in its answer. One list, reused by the registry
# guard and the e2e check.
MODEL_IO_PATTERNS: tuple[str, ...] = (
    r"llm_responses/[^/]+\.json",
    r"llm_requests/[^/]+\.json",
    r"llm_input\.json",
    r"llm_response\.json",
    r"partial_output\.json",
    r"schema-[^/]+/v\d+/schema_draft_group_\d+\.json",
    r"schema-[^/]+/v\d+/input_group_\d+\.json",
)
_MODEL_IO = tuple(re.compile(rf"(?:^|/){pattern}$") for pattern in MODEL_IO_PATTERNS)


def is_model_io(path: str) -> bool:
    """``True`` for a model session's own file (see ``MODEL_IO_PATTERNS``)."""
    return any(pattern.search(path) for pattern in _MODEL_IO)


# Internal artifacts (ADR-008 Decision 1, amendment of 2026-10-20, #1192): every other
# JSON file a run writes that is neither a registered contract (below) nor model I/O
# (above), with why it is not a contract. Callers may not rely on their shape. A
# file other phases read back across runs is typed in its own issue and then moves
# to the registry. (path pattern, reason), compiled by ``_internal`` below.
# ``tests/contract/test_declared_artifacts.py`` fails on a written JSON file in none
# of the three lists, and on an entry of any of them that no write or run produces.
INTERNAL_ARTIFACTS: tuple[tuple[str, str], ...] = (
    # Collect
    (
        r"collector/sql_shadow\.json",
        "parser comparison (sqlglot facade vs regex table references, #557): a "
        "diagnostic rewritten every collect, read by no phase",
    ),
    # Analysis
    (
        r"analysis-[^/]+/er-diagram\.json",
        "the ER diagram's Mermaid text in a JSON wrapper, rebuilt from the analysis",
    ),
    (
        r"analysis-[^/]+/partial\.json",
        "external-mode analysis held for the model's advisor pass, replaced at finalize",
    ),
    # Assignment and reality check
    (
        r"assignment/v\d+/access_patterns\.json",
        "each query's access-pattern class and scores as routing scored them (#728, "
        "#936): an audit record written with its assignment version",
    ),
    (
        r"assignment/review/pending_hitl\.json",
        "AWS Transform transport state: the pending review task to resume",
    ),
    # Schema design and post-schema routing
    (
        r"schema-[^/]+/v\d+/merge_record\.json",
        "the last --merge of the DynamoDB group drafts (draft hashes, errors)",
    ),
    (
        r"schema-[^/]+/v\d+/finalize_record\.json",
        "the last --finalize of a single-pass engine (status, errors, response and output hashes)",
    ),
    (
        r"schema-[^/]+/v\d+/changelog\.json",
        "a schema revision's changelog, shown with the revision",
    ),
    (
        r"schema-[^/]+/v\d+/revision_context\.json",
        "a schema revision's context for the designer, rewritten per revision",
    ),
    (
        r"post-schema-router/external_run\.json",
        "external-mode post-schema routing pass record (depth, engines to design again)",
    ),
    # Load test
    (r"load-test-[^/]+/v\d+/config\.json", "the load test's configuration as run"),
    (r"load-test-[^/]+/v\d+/result\.json", "the load test runner's raw result"),
    (r"load-test-[^/]+/v\d+/k6_diagnostics\.json", "the load test runner's diagnostics"),
    # Job bookkeeping
    (
        r"^[^/]+/[^/]+/_meta\.json",
        "the local API runner's own job record (start time, request input), read only by "
        "the local API and its stored-input redaction, never by a phase",
    ),
    (
        r"^[^/]+/[^/]+/_status\.json",
        "the local API runner's job status marker (cancelled, failed)",
    ),
    (
        r"^[^/]+/[^/]+/(?:collector|referee-triage|referee-synthesis|assignment-resolver"
        r"|reality-check|schema-design|schema-split|schema-merge|load-test|{engines})"
        r"/error\.json",
        "a container agent's failure record, under its agent type",
    ),
    (
        r"_logs/prose_style_rejections-v\d+\.json",
        "a log of the synthesis prose a style check rejected, per assignment version",
    ),
    (r"uploads/input_key\.json", "AWS Transform transport state: the resolved input location"),
    (
        r"_pending/[^/]+/upload\.json",
        "AWS Transform transport state: a pending upload task, before the job folder exists",
    ),
    (
        r"\.meta/_stored-input-redaction-v\d+\.json",
        "the stored-input redaction marker (not a job's progression)",
    ),
)


@cache
def _internal() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Each internal entry's pattern, compiled. A pattern starting with ``^`` is pinned
    to a path relative to the artifact root (``<db>/<job>/...``); the others match the
    end of a path at a ``/`` boundary, like the registry's. ``{engines}`` stands for
    every catalog engine id."""
    from src.shared.catalog import engine_ids

    engines = "|".join(re.escape(e) for e in sorted(engine_ids()))
    compiled = []
    for pattern, _ in INTERNAL_ARTIFACTS:
        regex = pattern.replace("{engines}", engines)
        compiled.append(
            (pattern, re.compile(rf"{regex}$" if regex.startswith("^") else rf"(?:^|/){regex}$"))
        )
    return tuple(compiled)


def internal_artifact_entry(path: str) -> str | None:
    """The ``INTERNAL_ARTIFACTS`` pattern ``path`` matches, or ``None``."""
    for pattern, regex in _internal():
        if regex.search(path):
            return pattern
    return None


def is_internal_artifact(path: str) -> bool:
    """``True`` for a declared internal artifact (see ``INTERNAL_ARTIFACTS``)."""
    return internal_artifact_entry(path) is not None


_COLLECTOR = "src.contracts.collector_output:CollectorOutputContract"
_SYNTHESIS = "src.contracts.synthesis_output:SynthesisOutputContract"

# (path suffix pattern, "module:Class"). A pattern matches the end of an
# artifact key at a ``/`` boundary, so it holds for ``<db>/<job>/...`` keys and
# for filesystem paths alike. Classes are named, not imported, so the store
# modules can import this one without importing every contract.
_ARTIFACT_CONTRACTS: tuple[tuple[str, str], ...] = (
    (r"collector/output\.json", _COLLECTOR),
    # Usually a raw collection (no contract_version, read as legacy); checked for the
    # case where the upload is a stored collector output.
    (r"uploads/collector-output\.json", _COLLECTOR),
    (r"referee-triage/triage\.json", "src.contracts.triage_output:TriageOutputContract"),
    (r"analysis-[^/]+/analysis\.json", "src.contracts.analysis_output:AnalysisOutputContract"),
    # Each engine's typed trace (#973, #1187): ``write_decision_trace`` refuses the trace
    # of an engine not registered here, and
    # ``tests/unit/contracts/test_decision_trace_base.py`` fails on an analysis engine
    # with no entry.
    (
        r"analysis-dynamodb/decision-trace\.json",
        "src.contracts.dynamodb_decision_trace:DynamoDBDecisionTrace",
    ),
    (
        r"analysis-documentdb/decision-trace\.json",
        "src.contracts.documentdb_decision_trace:DocumentDBDecisionTrace",
    ),
    (
        r"analysis-elasticache/decision-trace\.json",
        "src.contracts.elasticache_decision_trace:ElastiCacheDecisionTrace",
    ),
    (
        r"analysis-opensearch/decision-trace\.json",
        "src.contracts.opensearch_decision_trace:OpenSearchDecisionTrace",
    ),
    (
        r"analysis-aurora_postgresql/decision-trace\.json",
        "src.contracts.aurora_decision_trace:AuroraDecisionTrace",
    ),
    (
        r"analysis-aurora_mysql/decision-trace\.json",
        "src.contracts.aurora_decision_trace:AuroraDecisionTrace",
    ),
    (r"assignment/v\d+/assignment\.json", "src.contracts.assignment_models:Assignment"),
    (r"assignment/v\d+/validation\.json", "src.contracts.assignment_models:ValidationResult"),
    (r"reality-check/awaiting_llm\.json", "src.contracts.reality_check_marker:RealityCheckMarker"),
    (r"decisions/[^/]+\.json", "src.contracts.decision_record:DecisionRecord"),
    (
        r"reality-check/output\.json",
        "src.contracts.reality_check_output:RealityCheckOutputContract",
    ),
    (r"synthesis/(?:v\d+/)?report\.json", _SYNTHESIS),
    # Written instead of synthesis/v<N>/ when there is no assignment version (0).
    (r"referee-synthesis/report\.json", _SYNTHESIS),
    (r"deliverables/assessment-data\.json", _SYNTHESIS),
    (
        r"schema-[^/]+/v\d+/groups_manifest\.json",
        "src.contracts.schema_design_input:SchemaDesignGroupsManifest",
    ),
    (
        r"schema-[^/]+/v\d+/version_meta\.json",
        "src.contracts.schema_revision_models:SchemaVersionMeta",
    ),
    (
        r"schema-[^/]+/v\d+/revision_request\.json",
        "src.contracts.schema_revision_models:SchemaRevisionRequest",
    ),
    (r"schema-[^/]+/confirmed\.json", "src.contracts.schema_revision_models:SchemaConfirmation"),
    (
        r"schema-[^/]+/(?:v\d+/)?design_trace\.json",
        "src.contracts.schema_design_trace:DesignTrace",
    ),
    (
        r"schema-[^/]+/v\d+/design_trace_group_\d+\.json",
        "src.contracts.schema_design_trace:DesignTraceGroup",
    ),
    (r"[^/]+/questions\.json", "src.contracts.agent_interaction_models:AgentQuestions"),
    (r"[^/]+/answers\.json", "src.contracts.agent_interaction_models:AgentAnswers"),
    # ``.meta/<job_id>.json``; job ids never start with ``_`` (marker files do).
    (r"\.meta/[^_/][^/]*\.json", "src.contracts.phase_models:PhaseProgression"),
    (
        r"post-schema-router/(?:pass-\d+/)?router_output\.json",
        "src.contracts.post_schema_router_output:RouterOutput",
    ),
    (
        r"load-test-[^/]+/v\d+/infrastructure\.json",
        "src.contracts.load_test_models:InfrastructureManifest",
    ),
    (r"load-test-[^/]+/v\d+/seed-manifest\.json", "src.agents.load_test.models:SeedManifest"),
    (
        r"load-test-[^/]+/v\d+/results/summary\.json",
        "src.contracts.load_test_models:LoadTestOutput",
    ),
    (
        r"load-test-[^/]+/v\d+/results/(?!summary\.json)[^/]+\.json",
        "src.contracts.load_test_models:PatternResult",
    ),
)

# Each engine's schema design output (``schema-<engine>/[v<N>/]schema_output.json``)
# is the ``*ModelOutputContract`` of ``src/contracts/<engine>_model_output.py``, for
# every catalog engine that has one. Group drafts (``schema_draft_group_<G>.json``)
# are a model's own files, not registered: the merge writes the contract version.
_SCHEMA_OUTPUT = r"schema-{engine}/(?:v\d+/)?schema_output\.json"


def _schema_output_targets() -> list[tuple[str, str]]:
    from importlib.util import find_spec

    from src.shared.catalog import engine_ids

    found = []
    for engine in sorted(engine_ids()):
        module = f"src.contracts.{engine}_model_output"
        if find_spec(module) is None:
            continue
        # The module name is built from a catalog engine id, never from input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        names = [
            n for n in vars(importlib.import_module(module)) if n.endswith("ModelOutputContract")
        ]
        if len(names) == 1:
            found.append((_SCHEMA_OUTPUT.format(engine=re.escape(engine)), f"{module}:{names[0]}"))
    return found


@cache
def _registry() -> tuple[tuple[re.Pattern[str], str], ...]:
    entries = [*_ARTIFACT_CONTRACTS, *_schema_output_targets()]
    return tuple((re.compile(rf"(?:^|/){pattern}$"), target) for pattern, target in entries)


@cache
def _import(target: str) -> type[BaseModel]:
    module, name = target.split(":")
    # ``target`` is a registry entry above, never input.
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    cls: type[BaseModel] = getattr(importlib.import_module(module), name)
    return cls


def contract_for_artifact(path: str) -> type[BaseModel] | None:
    """The contract stored at ``path``, or ``None`` for a file that holds none
    (model I/O included, whatever its name)."""
    if is_model_io(path):
        return None
    for pattern, target in _registry():
        if pattern.search(path):
            return _import(target)
    return None


# A ``status: "skipped"`` payload at these paths is a placeholder, not the design
# or report the path holds otherwise (#1023): its version is checked against the
# placeholder's model, for any engine (one with no design contract too).
_SKIPPED = "skipped"
_PLACEHOLDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:^|/)schema-[^/]+/(?:v\d+/)?schema_output\.json$"),
        "src.contracts.skipped_placeholders:SkippedSchemaOutput",
    ),
    (
        re.compile(r"(?:^|/)(?:synthesis/(?:v\d+/)?|referee-synthesis/)report\.json$"),
        "src.contracts.skipped_placeholders:SkippedSynthesisReport",
    ),
)


def placeholder_for_artifact(path: str) -> type[BaseModel] | None:
    """The skipped placeholder that may be stored at ``path`` instead of its contract."""
    if is_model_io(path):
        return None
    for pattern, target in _PLACEHOLDERS:
        if pattern.search(path):
            return _import(target)
    return None


def contract_for_payload(path: str, data: object) -> type[BaseModel] | None:
    """The contract ``data`` stored at ``path`` is: the path's contract, or its skipped
    placeholder when the payload says ``status: "skipped"`` (the status is read first)."""
    model = contract_for_artifact(path)
    if isinstance(data, Mapping) and data.get("status") == _SKIPPED:
        return placeholder_for_artifact(path) or model
    return model


def registered_contracts() -> list[type[BaseModel]]:
    """Every contract the registry knows (skipped placeholders included), once each."""
    return list(
        dict.fromkeys(
            [_import(target) for _, target in _registry()]
            + [_import(target) for _, target in _PLACEHOLDERS]
        )
    )


def stamp_artifact_version(path: str, data: dict[str, Any]) -> dict[str, Any]:
    """``data`` with the registered contract's current ``contract_version`` for ``path``.

    For writers of a model's answer (a schema design): the version written comes
    from the contract, never from what the model echoed (ADR-008 Decision 2), so the
    file is never one the store then refuses to read. An unregistered path is
    returned unchanged.
    """
    model = contract_for_payload(path, data)
    if model is None:
        return data
    return {**data, "contract_version": current_version(model)}


def check_artifact[T](path: str, data: T) -> T:
    """Check a raw dict read from ``path`` when ``path`` is a registered contract file.

    Returns ``data`` (a payload of an older MAJOR comes back through its adapter, in
    the current shape), so a store returns what this returns.
    """
    if not isinstance(data, Mapping):
        return data
    model = contract_for_payload(path, data)
    if model is None:
        return data
    return read_payload(data, model, artifact=path)  # type: ignore[return-value]
