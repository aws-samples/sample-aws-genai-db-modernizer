"""Every artifact read checks the stored ``contract_version`` (ADR-008 Decision 4, #971).

``src/shared/contract_versioning.py`` holds the one check: same MAJOR (any MINOR)
is read, a different MAJOR is refused (or read through its adapter), a file with
no version is legacy. This guard keeps every read going through it:

- **Model reads.** A call that parses a payload into a model with a
  ``contract_version`` (``Cls.model_validate(d)`` / ``model_validate_json``,
  ``Cls(**d)``, ``TypeAdapter(Cls)``) in ``src/`` or ``scripts/`` must be
  ``load_contract(Cls, ...)`` instead, unless it is listed in
  ``NOT_ARTIFACT_READS`` with the reason it reads no stored artifact. ``Cls`` is
  resolved through ``from X import Cls``, ``import X as m`` / ``m.Cls``, a class
  in the same ``src/`` module and a local alias (``cls = Cls``). A receiver only
  known at run time (``self.output_model``, a dict lookup) is not resolved: those
  parse a model's answer.
- **Store reads.** Raw-dict reads go through ``ArtifactStore.read_json``. Every
  class in ``src/`` that implements ``read_json`` must call
  ``check_artifact(path, data)``, which looks the path up in the contract file
  registry. Every path a contract is written to (the writers
  ``tests/contract/test_written_contracts_versioned.py`` finds, their path
  expressions rendered with ``1`` for each value) must map to that contract.
- **File reads.** ``json.load(f)`` and ``json.loads(...)`` of ``x.read_text()``,
  ``x.read_bytes()`` or ``f.read()`` (directly or through a name bound to one)
  read a file outside the store. The parsed value itself must reach a helper
  call, directly or through the name it is bound to; a helper elsewhere in the
  function does not count. Otherwise the read is listed in ``FILE_READS`` with
  what it reads.
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.shared import contract_versioning as versioning

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts")
VALIDATE_METHODS = {"model_validate", "model_validate_json"}
HELPERS = {"load_contract", "check_contract_version", "check_artifact", "carries_contract"}

# (path, enclosing function, class) -> why the call reads no stored artifact.
_BUILT = "built in memory from the offline upload's fields, not read from a file"
_ANSWER = "a model's answer, parsed before it becomes an artifact"
NOT_ARTIFACT_READS: dict[tuple[str, str, str], str] = {
    ("src/agents/collector/handler.py", "run_collector", "CollectorInput"): (
        "built in memory from the task's environment, not read from a file"
    ),
    # The one offline builder every ingest path calls (#1177): the library ingest,
    # run_assessment.py, run_collect.py and test_local_phased.py each built it before.
    (
        "src/agents/collector/offline_ingest.py",
        "build_offline_collector_output",
        "CollectorInput",
    ): _BUILT,
    (
        "src/agents/referee/reality_check_handler.py",
        "write_reality_check_result",
        "RealityCheckOutputContract",
    ): "builds the new output in memory before writing it",
    # The public assess() request (#1182): a caller's argument, never a stored artifact;
    # its contract_version is a Literal of the one version it accepts.
    ("src/assessment/runner.py", "assess", "AssessmentRequest"): (
        "validates the caller's request argument, not a file"
    ),
    (
        "src/agents/referee/synthesis_handler.py",
        "_write_synthesis_report",
        "SynthesisOutputContract",
    ): "builds the new output in memory before writing it",
    (
        "src/contracts/executive_summary_response.py",
        "parse_executive_summary_response",
        "ExecutiveSummaryResponse",
    ): _ANSWER,
    (
        "src/contracts/reality_check_model_response.py",
        "parse_reality_check_response",
        "RealityCheckModelResponse",
    ): _ANSWER,
    (
        "src/tools/schema/opensearch_schema_agent.py",
        "_invoke_designer",
        "OpenSearchModelOutputContract",
    ): _ANSWER,
    (
        "src/tools/schema/aurora_common/bedrock_delta.py",
        "prepare_delta_design",
        "AgentCollectorInput",
    ): (
        "a projection with no version default of its own; the collector output it is "
        "projected from was checked when it was read"
    ),
    (
        "src/tools/schema/aurora_common/bedrock_delta.py",
        "prepare_delta_design",
        "AgentAnalysisInput",
    ): (
        "a projection with no version default of its own; the analysis output it is "
        "projected from was checked when it was read"
    ),
}

# (path, enclosing function, name the parsed JSON is bound to) -> what the
# direct file read reads, for a read whose result reaches none of HELPERS.
_REVISION = "revision_context.json: revision instructions, not a contract"
_K6 = "the load generator's own summary files"
_REFERENCE = "the raw offline upload (a collection, no contract) the script seeds"
FILE_READS: dict[tuple[str, str, str], str] = {
    ("src/atx_orchestrator/core.py", "_discover_uploaded_input", "collection"): (
        "the customer upload, copied verbatim to the seed key; the store checks it when "
        "it is read back"
    ),
    ("src/atx_orchestrator/runtime/hitl.py", "_download_artifact_json", "<return>"): (
        "a human-in-the-loop answer file, not a contract"
    ),
    ("src/agents/answer_validation.py", "_bedrock_validate", "result"): (
        "a model's HTTP response body"
    ),
    ("src/graph/raw_query_worker.py", "_child_main", "request"): "the worker's stdin request",
    ("src/tools/schema/dynamodb_schema_agent.py", "run_dynamodb_schema_agent", "context_tables"): (
        "a group input's group_context_tables list, written by the handler (#1243)"
    ),
    ("src/tools/aws/s3_storage.py", "load_json", "<return>"): (
        "collector checkpoints; each collector reads its cached output with load_contract"
    ),
    **{
        (
            f"src/tools/schema/{engine}_schema_agent.py",
            f"run_{engine}_schema_agent",
            "revision_ctx",
        ): (_REVISION)
        for engine in (
            "aurora_mysql",
            "aurora_postgresql",
            "documentdb",
            "dynamodb",
            "elasticache",
            "opensearch",
        )
    },
    ("src/agents/load_test/dynamodb/runner.py", "run", "summary"): _K6,
    ("src/agents/load_test/dynamodb/runner.py", "run", "raw"): _K6,
    ("src/agents/load_test/elasticache/runner.py", "run", "summary"): _K6,
    ("src/agents/load_test/elasticache/runner.py", "run", "raw"): _K6,
    ("src/agents/load_test/opensearch/runner.py", "run", "summary"): _K6,
    ("src/agents/load_test/opensearch/runner.py", "run", "raw"): _K6,
    ("src/agents/load_test/opensearch/runner.py", "validate_queries", "entries"): (
        "the runner's query manifest (a list, not a contract)"
    ),
    ("scripts/atx_bootstrap_reference.py", "<module>", "raw"): _REFERENCE,
    ("scripts/atx_subagent_test.py", "<module>", "<expr>"): _REFERENCE,
    ("scripts/atx_tool_test.py", "<module>", "<expr>"): _REFERENCE,
    ("scripts/build_docs_sample.py", "update_scan_baseline", "data"): (
        "the repository's detect-secrets baseline (.secrets.baseline), not a contract"
    ),
    (
        "scripts/run_assessment.py",
        "_read_state",
        "<return>",
    ): "the .modernizer-state.json job cursor",
    ("scripts/run_schema_design.py", "_group_design_metrics", "group_input"): (
        "a group input's design_metrics, not a contract"
    ),
    ("scripts/run_schema_design.py", "run_check_costs", "payload"): (
        "a model's group draft (not registered: the merge writes the contract version)"
    ),
    ("scripts/run_schema_design.py", "_draft_state", "<expr>"): (
        "a model's group draft, only checked to be a JSON object"
    ),
    ("scripts/contract_schema_snapshot.py", "check_released_against", "<expr>"): (
        "the public contract's released snapshot (a generated test baseline, not an "
        "artifact contract), on the branch and at the base ref"
    ),
    ("scripts/render_debezium_connector.py", "_load", "<expr>"): (
        "a Debezium connector input, validated by DebeziumConnectorInput; it has no "
        "contract version until the DataMovementPlan contract (#1413) feeds it (#1414)"
    ),
    ("scripts/validate_schemas.py", "validate_schema_file", "schema"): "JSON Schema files",
    ("scripts/contract_schema_snapshot.py", "main", "<expr>"): "the contract schema snapshot",
    ("scripts/contract_value_snapshot.py", "main", "<expr>"): "the contract value snapshot",
    ("scripts/start_local_ui.py", "read_pids", "data"): "the local UI's pid file",
    ("scripts/start_local_ui.py", "ui_build_is_current", "stamp"): "the UI build stamp",
    ("scripts/hooks/enforce_tool_use_rule.py", "allowed_rules", "<expr>"): "Claude Code settings",
    ("scripts/hooks/hook_common.py", "read_event", "data"): "a hook event on stdin",
    ("scripts/check_commit_authors.py", "read_event", "<expr>"): "the GitHub Actions event",
}


def _module_name(path: str) -> str:
    return ".".join(Path(path).with_suffix("").parts)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    func = parents.get(node)
    while func is not None and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        func = parents.get(func)
    return func


def _calls_a_helper(scope: ast.AST) -> bool:
    for node in ast.walk(scope):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            )
            if name in HELPERS:
                return True
    return False


@dataclass(frozen=True)
class Source:
    path: str
    tree: ast.Module
    parents: dict[ast.AST, ast.AST]


def _sources() -> list[Source]:
    out = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            out.append(Source(str(path.relative_to(REPO)), tree, _parents(tree)))
    return out


SOURCES = _sources()


def _import_module(name: str) -> object | None:
    try:
        # The module name comes from the repo's own import statements.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        return importlib.import_module(name)
    except ImportError:
        return None


def _assignments(scope: ast.AST, name: str) -> list[ast.expr]:
    """Right-hand sides of ``name = ...`` directly in ``scope`` (function or module)."""
    found = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            found.append(node.value)
    return found


def _resolve(expr: ast.expr, src: Source, scope: ast.AST | None, depth: int = 0) -> object | None:
    """The object an expression names: a ``from X import name`` binding, an
    ``import X as m`` module, a class defined in the same ``src/`` module, a local
    alias (``cls = Cls``), or an attribute of any of those (``mod.Cls``). Scripts
    are never imported."""
    if depth > 4:
        return None
    if isinstance(expr, ast.Attribute):
        owner = _resolve(expr.value, src, scope, depth + 1)
        attr: object | None = getattr(owner, expr.attr, None) if owner is not None else None
        return attr
    if not isinstance(expr, ast.Name):
        return None
    name = expr.id
    for node in ast.walk(src.tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    module = _import_module(node.module)
                    found: object | None = (
                        getattr(module, alias.name, None) if module is not None else None
                    )
                    if found is None:  # ``from package import module``
                        found = _import_module(f"{node.module}.{alias.name}")
                    return found
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname == name:
                    return _import_module(alias.name)
    for where in [w for w in (scope, src.tree) if w is not None]:
        for value in _assignments(where, name):
            target = _resolve(value, src, scope, depth + 1)
            if target is not None:
                return target
    if src.path.startswith("src/"):
        for node in src.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                own = _import_module(_module_name(src.path))
                local: object | None = getattr(own, name, None) if own is not None else None
                return local
    return None


def _is_versioned(cls: object) -> bool:
    return (
        isinstance(cls, type)
        and issubclass(cls, BaseModel)
        and "contract_version" in cls.model_fields
    )


@dataclass(frozen=True)
class ValidateSite:
    path: str
    line: int
    function: str
    cls: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.cls)


def _parsed_class(node: ast.Call) -> ast.expr | None:
    """The class a call parses a payload into, for the shapes that read one:
    ``Cls.model_validate(d)`` / ``model_validate_json``, ``Cls(**d)`` and
    ``TypeAdapter(Cls)``."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in VALIDATE_METHODS:
        return func.value
    if any(kw.arg is None for kw in node.keywords):  # ``Cls(**d)``
        return func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "TypeAdapter" and node.args:
        return node.args[0]
    return None


def _validate_sites(sources: list[Source] | None = None) -> list[ValidateSite]:
    sites = []
    for src in SOURCES if sources is None else sources:
        if src.path == "src/shared/contract_versioning.py":
            continue
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call):
                continue
            target = _parsed_class(node)
            if target is None:
                continue
            func = _enclosing(node, src.parents)
            cls = _resolve(target, src, func)
            if not _is_versioned(cls):
                continue
            fname = func.name if func is not None else "<module>"  # type: ignore[attr-defined]
            sites.append(ValidateSite(src.path, node.lineno, fname, getattr(cls, "__name__", "")))
    return sites


VALIDATE_SITES = _validate_sites()


_READ_METHODS = {"read_text", "read_bytes", "read"}


def _reads_a_file(expr: ast.expr, scope: ast.AST, depth: int = 0) -> bool:
    """``x.read_text()`` / ``x.read_bytes()`` / ``f.read()`` (optionally ``.decode()``),
    or a name bound to one in the same scope (``text = p.read_text()``)."""
    if depth > 4:
        return False
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        if expr.func.attr == "decode":
            return _reads_a_file(expr.func.value, scope, depth + 1)
        return expr.func.attr in _READ_METHODS
    if isinstance(expr, ast.Name):
        return any(_reads_a_file(v, scope, depth + 1) for v in _assignments(scope, expr.id))
    return False


def _is_file_read(node: ast.Call, scope: ast.AST) -> bool:
    f = node.func
    if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
        return False
    if f.value.id not in {"json", "_json"}:
        return False
    if f.attr == "load":
        return True
    return f.attr == "loads" and bool(node.args) and _reads_a_file(node.args[0], scope)


def _helper_args(scope: ast.AST) -> tuple[set[int], set[str]]:
    """Node ids and bare names passed (anywhere in the arguments) to a helper call."""
    ids: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name not in HELPERS:
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            for sub in ast.walk(arg):
                ids.add(id(sub))
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return ids, names


@dataclass(frozen=True)
class FileRead:
    path: str
    line: int
    function: str
    target: str  # the name the parsed JSON is bound to, or "<expr>"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.target)


def _unchecked_file_reads(sources: list[Source] | None = None) -> list[FileRead]:
    """Each direct JSON file read whose own result does not reach a helper call.

    A helper elsewhere in the function does not count: the parsed value itself
    must be a helper argument, directly or through the name it is bound to."""
    reads = []
    for src in SOURCES if sources is None else sources:
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call):
                continue
            func = _enclosing(node, src.parents)
            scope: ast.AST = func if func is not None else src.tree
            if not _is_file_read(node, scope):
                continue
            ids, names = _helper_args(scope)
            parent = src.parents.get(node)
            target = "<expr>"
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
                if isinstance(parent.targets[0], ast.Name):
                    target = parent.targets[0].id
            elif isinstance(parent, ast.Return):
                target = "<return>"
            if id(node) in ids or target in names:
                continue
            fname = func.name if func is not None else "<module>"  # type: ignore[attr-defined]
            reads.append(FileRead(src.path, node.lineno, fname, target))
    return reads


FILE_READ_SITES = _unchecked_file_reads()


# ------------------------------------------------------------------ model reads


@pytest.mark.passes_on_main
def test_the_model_read_scan_is_not_vacuous() -> None:
    """It sees the reads this issue routed and the built-in-memory exemptions (nine
    since #1177 folded the four offline CollectorInput builders into one)."""
    assert len(VALIDATE_SITES) >= 9
    assert {s.cls for s in VALIDATE_SITES} >= {"CollectorInput", "SynthesisOutputContract"}


@pytest.mark.parametrize("site", VALIDATE_SITES, ids=lambda s: f"{s.path}:{s.line}:{s.cls}")
def test_no_artifact_read_validates_a_versioned_contract_directly(site: ValidateSite) -> None:
    assert site.key in NOT_ARTIFACT_READS, (
        f"{site.path}:{site.line} ({site.function}) parses {site.cls}, which carries a "
        "contract_version, without checking it. Read it with "
        f"load_contract({site.cls}, data, artifact=<path>) from src/shared/contract_versioning.py "
        "(ADR-008 Decision 4), or, if it reads no stored artifact, add it to "
        "NOT_ARTIFACT_READS with the reason."
    )


def test_no_stale_not_artifact_entries() -> None:
    stale = sorted(set(NOT_ARTIFACT_READS) - {s.key for s in VALIDATE_SITES})
    assert not stale, f"NOT_ARTIFACT_READS entries with no matching call: {stale}"


# ------------------------------------------------------------------ store reads


def _read_json_implementations() -> list[tuple[str, str, ast.FunctionDef]]:
    found = []
    for src in SOURCES:
        if not src.path.startswith("src/"):
            continue
        for cls in ast.walk(src.tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for item in cls.body:
                if isinstance(item, ast.FunctionDef) and item.name == "read_json":
                    body = [
                        s
                        for s in item.body
                        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                    ]
                    if body:  # a Protocol / abstract stub is docstring and ``...`` only
                        found.append((src.path, cls.name, item))
    return found


READ_JSON_IMPLS = _read_json_implementations()


def test_the_store_scan_is_not_vacuous() -> None:
    names = {cls for _, cls, _ in READ_JSON_IMPLS}
    assert {"LocalArtifactStore", "S3ArtifactStore", "AtxArtifactStore"} <= names


@pytest.mark.parametrize("impl", READ_JSON_IMPLS, ids=lambda i: f"{i[0]}::{i[1]}")
def test_every_store_read_json_checks_the_artifact(
    impl: tuple[str, str, ast.FunctionDef],
) -> None:
    path, cls, func = impl
    calls = {
        n.func.id
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "check_artifact" in calls, (
        f"{path}::{cls}.read_json returns stored JSON without check_artifact(path, data) "
        "(src/shared/contract_versioning.py), so a contract file at another MAJOR would be read "
        "as if it were current (ADR-008 Decision 4)."
    )


def test_the_registry_covers_every_written_contract() -> None:
    """Each contract a writer puts on disk has a registry entry, so the store checks it."""
    from tests.contract.test_written_contracts_versioned import _SITES, _site_models

    def name(m: type) -> str:  # by name: a test may have reloaded a contract module
        return f"{m.__module__}.{m.__qualname__}"

    written = {name(m) for site in _SITES for m in _site_models(site)}
    registered = {name(m) for m in versioning.registered_contracts()}
    missing = sorted(written - registered)
    assert not missing, (
        f"written to disk but not in src/shared/contract_versioning.py's contract file registry: "
        f"{missing}. Add the file's path pattern, so the store checks its version."
    )


_PLACEHOLDER = "1"  # stands in for every value a path f-string interpolates


def _trace_paths() -> tuple[str, ...]:
    """Every analysis engine's decision-trace path."""
    from src.shared.catalog import engine_ids

    return tuple(
        f"db/job/analysis-{engine}/decision-trace.json"
        for engine in sorted(engine_ids(status="active"))
    )


def _design_trace_paths() -> tuple[str, ...]:
    """Every engine's design-trace path: the single and merged trace, and a group's."""
    from src.shared.catalog import engine_ids

    return tuple(
        f"db/job/schema-{engine}/{tail}"
        for engine in sorted(engine_ids(status="active"))
        for tail in ("design_trace.json", "v1/design_trace.json", "v1/design_trace_group_1.json")
    )


# (file, name) -> the shape (or shapes) a path-prefix name holds where the renderer
# cannot see it bound (a parameter set by the caller).
PATH_HINTS: dict[tuple[str, str], str | tuple[str, ...]] = {
    ("src/agents/load_test/handler.py", "base"): "db/job/load-test-dynamodb/v1",
    # The one decision-trace writer takes the key from its callers (the analysis
    # handler, scripts/run_analysis.py, the transform core); it writes a trace through
    # the contract registered for its path and refuses an untyped one (#973, #1187): any
    # engine's trace path.
    ("src/agents/analysis/decision_trace.py", "key"): _trace_paths(),
    # The one design-trace writer takes the key from its callers (the schema design
    # handler, the group merger, the external PE review, the ATX carry-forward) and
    # writes it through the contract registered for its path (#1258).
    ("src/agents/schema_design/design_trace.py", "key"): _design_trace_paths(),
    # The group merger writes the merged design trace under its caller's version
    # folder (#1192's declared-artifact scan).
    ("src/agents/schema_design/group_merger.py", "base_key"): "db/job/schema-dynamodb/v1",
    # The container entrypoint's agent type, a module global set from the environment.
    ("src/agents/entrypoint.py", "AGENT_TYPE"): "collector",
}

# (file, function) of a writer whose model is the one registered for the path it is
# given (``contract_for_artifact(key)``), so each model is written only to its own
# paths, not to every path the writer can be given.
PATH_CHOSEN_MODEL: frozenset[tuple[str, str]] = frozenset(
    {
        ("src/agents/analysis/decision_trace.py", "write_decision_trace"),
        ("src/agents/schema_design/design_trace.py", "write_design_trace"),
    }
)


def _helper(name: str, tree: ast.Module) -> tuple[ast.FunctionDef, ast.Module, str] | None:
    """A function named ``name`` with one ``return``, in this module or the ``src/``
    module it is imported from: (definition, its module tree, its file)."""
    candidates: list[tuple[ast.Module, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if any((a.asname or a.name) == name for a in node.names):
                rel = Path(*node.module.split(".")).with_suffix(".py")
                if (REPO / rel).is_file():
                    candidates.append((ast.parse((REPO / rel).read_text()), str(rel)))
    candidates.append((tree, ""))
    for t, file in candidates:
        for node in t.body if isinstance(t, ast.Module) else []:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value]
                if len(returns) == 1:
                    return node, t, file
        for node in ast.walk(t):  # methods (``self._progression_path``)
            if isinstance(node, ast.FunctionDef) and node.name == name:
                returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value]
                if len(returns) == 1:
                    return node, t, file
    return None


@dataclass
class _Ctx:
    tree: ast.Module
    file: str
    scope: ast.AST
    bound: dict[str, list[str]]


def _render(expr: ast.expr, ctx: _Ctx, depth: int = 0) -> list[str]:
    """Every literal shape a write-path expression can take, values as ``1``.

    A name assigned in several branches (``key = ...v{N}...`` / ``key =
    ...referee-synthesis...``) gives one shape per assignment; a one-return path
    helper (``assignment_artifact_path``) is inlined with its arguments."""
    if depth > 8:
        return []
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [expr.value]
    if isinstance(expr, ast.JoinedStr):
        shapes = [""]
        for part in expr.values:
            if isinstance(part, ast.Constant):
                shapes = [x + str(part.value) for x in shapes]
                continue
            inner = _render(part.value, ctx, depth + 1)  # type: ignore[attr-defined]
            shapes = [x + i for x in shapes for i in (inner or [_PLACEHOLDER])]
        return shapes
    if isinstance(expr, ast.IfExp):  # ``a if cond else b``: either shape (#1018)
        return _render(expr.body, ctx, depth + 1) + _render(expr.orelse, ctx, depth + 1)
    if isinstance(expr, ast.Name):
        if expr.id in ctx.bound:
            return ctx.bound[expr.id]
        if (ctx.file, expr.id) in PATH_HINTS:
            hint = PATH_HINTS[(ctx.file, expr.id)]
            return [hint] if isinstance(hint, str) else list(hint)
        found: list[str] = []
        for value in _assignments(ctx.scope, expr.id):
            found += _render(value, ctx, depth + 1)
        return found
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        helper = _helper(name, ctx.tree)
        if helper is None:
            return []
        node, tree, file = helper
        params = [a.arg for a in node.args.args if a.arg != "self"]
        bound = {
            param: (_render(arg, ctx, depth + 1) or [_PLACEHOLDER])
            for param, arg in zip(params, expr.args, strict=False)
        }
        for kw in expr.keywords:
            if kw.arg:
                bound[kw.arg] = _render(kw.value, ctx, depth + 1) or [_PLACEHOLDER]
        for param in params:
            bound.setdefault(param, [_PLACEHOLDER])
        body = next(n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value)
        return _render(body.value, _Ctx(tree, file or ctx.file, node, bound), depth + 1)  # type: ignore[arg-type]
    return []


def _write_paths() -> list[tuple[str, int, str, list[str]]]:
    """(file, line, written model, rendered path shapes) for each #972 write site."""
    from tests.contract.test_written_contracts_versioned import _SITES, _site_models

    out = []
    for site in _SITES:
        scope = site.func_node if site.func_node is not None else site.module_tree
        call = next(
            n
            for n in ast.walk(scope)
            if isinstance(n, ast.Call)
            and getattr(n, "lineno", 0) == site.line
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "write_json"
        )
        shapes = _render(call.args[0], _Ctx(site.module_tree, site.path, scope, {}))
        for model in _site_models(site):
            own = shapes
            if (site.path, site.function) in PATH_CHOSEN_MODEL:
                own = [
                    shape for shape in shapes if versioning.contract_for_artifact(shape) is model
                ]
            out.append((site.path, site.line, f"{model.__module__}.{model.__qualname__}", own))
    return out


WRITE_PATHS = _write_paths()


@pytest.mark.parametrize("site", WRITE_PATHS, ids=lambda w: f"{w[0]}:{w[1]}")
def test_every_written_path_is_registered_for_its_contract(
    site: tuple[str, int, str, list[str]],
) -> None:
    """Paths, not only classes: a contract written to an unregistered path (the
    ``referee-synthesis/report.json`` of a job with no assignment version) is read
    back unchecked."""
    file, line, model, shapes = site
    assert shapes, f"{file}:{line}: cannot work out the path this write uses"
    for shape in shapes:
        found = versioning.contract_for_artifact(shape)
        name = f"{found.__module__}.{found.__qualname__}" if found else None
        # A skipped placeholder is stored at its contract's path (#1023).
        skipped = versioning.placeholder_for_artifact(shape)
        allowed = {name} | ({f"{skipped.__module__}.{skipped.__qualname__}"} if skipped else set())
        assert model in allowed, (
            f"{file}:{line} writes {model} to {shape!r}, which the contract file registry in "
            f"src/shared/contract_versioning.py maps to {name}. Add the path pattern."
        )


def test_the_path_scan_renders_both_synthesis_paths() -> None:
    shapes = {
        shape
        for file, _, _, found in WRITE_PATHS
        if file == "src/agents/referee/synthesis_handler.py"
        for shape in found
    }
    assert "1/1/referee-synthesis/report.json" in shapes
    assert "1/1/synthesis/v1/report.json" in shapes


def test_every_registered_contract_has_a_version_default() -> None:
    for model in versioning.registered_contracts():
        assert versioning.current_version(model)


# ------------------------------------------------------------------- file reads


@pytest.mark.parametrize("read", FILE_READ_SITES, ids=lambda r: f"{r.path}:{r.line}")
def test_every_direct_json_file_read_checks_its_version(read: FileRead) -> None:
    assert read.key in FILE_READS, (
        f"{read.path}:{read.line} ({read.function}) reads a JSON file directly, outside the "
        "artifact store, and calls none of load_contract / check_contract_version / "
        "check_artifact / carries_contract. Check the contract it reads (ADR-008 "
        "Decision 4), or, if the file is no contract, add it to FILE_READS with what it is."
    )


def test_no_stale_file_read_entries() -> None:
    stale = sorted(set(FILE_READS) - {r.key for r in FILE_READ_SITES})
    assert not stale, f"FILE_READS entries with no unchecked read left: {stale}"


def test_the_file_read_scan_sees_store_and_script_reads() -> None:
    """Not vacuous: an unchecked read is found, and a checked one is not reported."""
    keys = {r.key for r in FILE_READ_SITES}
    assert ("scripts/run_assessment.py", "_read_state", "<return>") in keys
    assert not any(r.path == "src/storage/local_store.py" for r in FILE_READ_SITES)


# ------------------------------------------------------------- scan self-tests


def _source(code: str, path: str = "src/agents/example.py") -> Source:
    tree = ast.parse(code)
    return Source(path, tree, _parents(tree))


_SHAPES = """
import json
from pydantic import TypeAdapter
from src.contracts import assignment_models
import src.contracts.assignment_models as am
from src.contracts.assignment_models import Assignment


def by_module_attribute(d):
    return assignment_models.Assignment.model_validate(d)


def by_import_alias(d):
    return am.Assignment.model_validate(d)


def by_local_alias(d):
    cls = Assignment
    return cls.model_validate(d)


def by_keywords(d):
    return Assignment(**d)


def by_type_adapter(d):
    return TypeAdapter(Assignment).validate_python(d)
"""


def test_the_model_scan_sees_every_parse_shape() -> None:
    found = {s.function for s in _validate_sites([_source(_SHAPES)])}
    assert found == {
        "by_module_attribute",
        "by_import_alias",
        "by_local_alias",
        "by_keywords",
        "by_type_adapter",
    }


_FILE_SHAPES = """
import json
from src.shared.contract_versioning import check_artifact


def read_then_decode(f):
    return json.loads(f.read())


def read_text_first(p):
    text = p.read_text()
    return json.loads(text)


def helper_elsewhere(p, q, path):
    checked = check_artifact(path, json.loads(q.read_text()))
    unchecked = json.loads(p.read_text())
    return checked, unchecked


def checked_through_name(p, path):
    data = json.loads(p.read_text())
    return check_artifact(path, data)


def not_a_file(text):
    return json.loads(text)
"""


def test_the_file_scan_sees_every_read_shape_and_checks_the_value_itself() -> None:
    reads = {(r.function, r.target) for r in _unchecked_file_reads([_source(_FILE_SHAPES)])}
    assert reads == {
        ("read_then_decode", "<return>"),
        ("read_text_first", "<return>"),
        ("helper_elsewhere", "unchecked"),
    }
