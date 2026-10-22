"""Every model written to disk carries a ``contract_version`` (ADR-008 Decision 1, #972).

ADR-008 Decision 1 says every contract an agent or script writes carries a
``contract_version`` in MAJOR.MINOR form, so a reader can tell which
structure it holds. ``tests/contract/test_schema_snapshot.py`` only sees
models that already declare the field, so a contract written without one
was invisible to it (``Assignment`` documented versions 1.0 to 1.14 and wrote
none).

This guard finds every ``<store>.write_json(path, data)`` call in ``src/`` and
``scripts/`` whose ``data`` is a Pydantic dump (``x.model_dump(...)`` or
``json.loads(x.model_dump_json(...))``), works out the class of ``x`` from
the enclosing function (a parameter annotation, ``x = Cls(...)``,
``x = Cls.model_validate(...)``, ``x = load_contract(Cls, ...)``,
``x = other.model_copy(...)``,
``x = func(...)`` or ``x = obj.method(...)`` with a return annotation), and
checks that class:

- is defined in ``src/contracts/`` (so the schema snapshot covers it), and
- declares ``contract_version`` with a literal MAJOR.MINOR default and the
  ``^\\d+\\.\\d+$`` pattern.

A call site whose variable cannot be typed that way (an unannotated
parameter, a loop variable) must be listed in ``UNTYPED_WRITE_SITES`` with
the class it writes, so a new writer can never slip past unlisted.

Nested models (``Move``, ``FeasibilityFinding``, ``QueryAssignment``...) are
versioned through the top-level contract that embeds them and are not
checked here.
"""

from __future__ import annotations

import ast
import importlib
import re
import typing
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from src.agents.load_test.models import SeedManifest
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.load_test_models import InfrastructureManifest, LoadTestOutput
from src.contracts.schema_design_trace import DesignTrace, DesignTraceGroup

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts")
VERSION_RE = re.compile(r"^\d+\.\d+$")
_VALIDATE_METHODS = {"model_validate", "model_validate_json", "model_construct"}


# (repo-relative path, enclosing function, variable) -> the model that call
# site writes, for variables the resolver below cannot type from the code
# (unannotated parameters). Each one is checked like a resolved site, and an
# entry that no longer matches a call site fails ``test_no_stale_entries``.
def _trace_contracts() -> tuple[type[BaseModel], ...]:
    """Every engine's registered decision-trace contract (#1187)."""
    from src.shared.catalog import engine_ids
    from src.shared.contract_versioning import contract_for_artifact

    found = (
        contract_for_artifact(f"db/job/analysis-{engine}/decision-trace.json")
        for engine in sorted(engine_ids(status="active"))
    )
    return tuple(model for model in found if model is not None)


UNTYPED_WRITE_SITES: dict[tuple[str, str, str], type[BaseModel] | tuple[type[BaseModel], ...]] = {
    ("src/agents/load_test/handler.py", "_write_artifacts", "manifest"): InfrastructureManifest,
    ("src/agents/load_test/handler.py", "_write_artifacts", "seed_manifest"): SeedManifest,
    ("src/agents/load_test/handler.py", "_write_artifacts", "output"): LoadTestOutput,
    ("scripts/run_analysis.py", "_write_analysis_artifacts", "contract"): AnalysisOutputContract,
    # ``contract, trace, mermaid = analyze_fn(...)``: the engine's analysis entry point.
    ("src/atx_orchestrator/core.py", "run_analysis_core", "contract"): AnalysisOutputContract,
    # The one decision-trace writer loads the trace through the contract registered for
    # its path: any engine's typed trace.
    ("src/agents/analysis/decision_trace.py", "write_decision_trace", "typed"): (
        _trace_contracts()
    ),
    # The one design-trace writer loads the trace through the contract registered for
    # its path: the merged or single trace, or one group's.
    ("src/agents/schema_design/design_trace.py", "write_design_trace", "typed"): (
        DesignTrace,
        DesignTraceGroup,
    ),
}

# Written contracts whose ``contract_version`` has a default but no pattern, with
# the issue that adds it. Empty since #1006 gave the last two theirs (the snapshot
# differ counts adopting exactly ``^\d+\.\d+$`` as MINOR). A model listed here
# must still have a literal MAJOR.MINOR default.
PATTERN_PENDING: dict[type[BaseModel], str] = {}


@dataclass(frozen=True)
class WriteSite:
    path: str
    line: int
    function: str
    variable: str
    func_node: ast.AST | None
    module_tree: ast.Module

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.function, self.variable)

    def where(self) -> str:
        return f"{self.path}:{self.line} ({self.function}, {self.variable})"


def _dumped_name(data: ast.expr) -> str | None:
    """``x`` for ``x.model_dump(...)`` or ``json.loads(x.model_dump_json(...))``."""
    if (
        isinstance(data, ast.Call)
        and isinstance(data.func, ast.Attribute)
        and data.func.attr == "loads"
        and data.args
    ):
        data = data.args[0]
    if (
        isinstance(data, ast.Call)
        and isinstance(data.func, ast.Attribute)
        and data.func.attr in {"model_dump", "model_dump_json"}
        and isinstance(data.func.value, ast.Name)
    ):
        return data.func.value.id
    return None


def _write_sites() -> list[WriteSite]:
    sites: list[WriteSite] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_json"
                    and len(node.args) >= 2
                ):
                    continue
                name = _dumped_name(node.args[1])
                if name is None:
                    continue
                func: ast.AST | None = parents.get(node)
                while func is not None and not isinstance(
                    func, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    func = parents.get(func)
                sites.append(
                    WriteSite(
                        path=str(path.relative_to(REPO)),
                        line=node.lineno,
                        function=func.name if func is not None else "<module>",  # type: ignore[union-attr]
                        variable=name,
                        func_node=func,
                        module_tree=tree,
                    )
                )
    return sites


def _module_name(path: str) -> str:
    return ".".join(Path(path).with_suffix("").parts)


def _import_bindings(tree: ast.Module, path: str) -> dict[str, tuple[str, str]]:
    """Local name -> (module, attribute) for every ``from X import Y`` in the file."""
    bindings: dict[str, tuple[str, str]] = {}
    package = _module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                module = ".".join(base + ([module] if module else []))
            for alias in node.names:
                bindings[alias.asname or alias.name] = (module, alias.name)
    return bindings


def _lookup(name: str, site: WriteSite) -> object | None:
    """The object a bare name refers to in the site's module, without importing scripts."""
    bindings = _import_bindings(site.module_tree, site.path)
    module_name: str | None = None
    if name in bindings:
        module_name, attr = bindings[name]
    else:
        attr = name
        for node in site.module_tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
                if site.path.startswith("src/"):
                    module_name = _module_name(site.path)
    if module_name is None:
        return None
    # module_name comes from the repo's own source files (an import statement
    # or a src/ path), never from input.
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    module = importlib.import_module(module_name)
    found: object | None = getattr(module, attr, None)
    return found


def _annotation_class(annotation: ast.expr | None, site: WriteSite) -> object | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        annotation = ast.parse(annotation.value, mode="eval").body
    if isinstance(annotation, ast.Name):
        return _lookup(annotation.id, site)
    return None


def _value_classes(value: ast.expr, site: WriteSite, seen: frozenset[str]) -> list[object]:
    if not isinstance(value, ast.Call):
        return []
    func = value.func
    if isinstance(func, ast.Name) and func.id == "load_contract":
        # ``x = load_contract(Cls, data, artifact=...)`` (#971) returns a ``Cls``.
        first = value.args[0] if value.args else None
        target = _lookup(first.id, site) if isinstance(first, ast.Name) else None
        return [target] if isinstance(target, type) else []
    if isinstance(func, ast.Name):
        target = _lookup(func.id, site)
        if isinstance(target, type):
            return [target]
        if callable(target):
            hint = typing.get_type_hints(target).get("return")
            return [hint] if isinstance(hint, type) else []
        return []
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.attr in _VALIDATE_METHODS:
            target = _lookup(func.value.id, site)
            return [target] if isinstance(target, type) else []
        if func.attr == "model_copy":
            return _resolve(func.value.id, site, seen)
        # ``x = obj.method(...)`` with ``obj`` typed in the same function: the
        # method's return annotation.
        found: list[object] = []
        for owner in _resolve(func.value.id, site, seen):
            method = getattr(owner, func.attr, None)
            if callable(method):
                hint = typing.get_type_hints(method).get("return")
                if isinstance(hint, type):
                    found.append(hint)
        return found
    return []


def _resolve(variable: str, site: WriteSite, seen: frozenset[str] = frozenset()) -> list[object]:
    """Every class the enclosing function binds ``variable`` to."""
    if variable in seen or site.func_node is None:
        return []
    seen = seen | {variable}
    func = site.func_node
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
    found: list[object] = []
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if arg.arg == variable:
            cls = _annotation_class(arg.annotation, site)
            if cls is not None:
                found.append(cls)
    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == variable:
                cls = _annotation_class(node.annotation, site)
                if cls is not None:
                    found.append(cls)
                elif node.value is not None:
                    found.extend(_value_classes(node.value, site, seen))
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == variable for t in node.targets):
                found.extend(_value_classes(node.value, site, seen))
    return found


def _site_models(site: WriteSite) -> list[type[BaseModel]]:
    if site.key in UNTYPED_WRITE_SITES:
        listed = UNTYPED_WRITE_SITES[site.key]
        return list(listed) if isinstance(listed, tuple) else [listed]
    return [c for c in _resolve(site.variable, site) if isinstance(c, type)]


def _version_problem(model: type[BaseModel]) -> str | None:
    if not model.__module__.startswith("src.contracts."):
        return f"{model.__qualname__} is defined in {model.__module__}, not src/contracts/"
    field = model.model_fields.get("contract_version")
    if field is None:
        return f"{model.__qualname__} has no contract_version field"
    if field.is_required() or not isinstance(field.default, str):
        return f"{model.__qualname__}.contract_version has no literal default"
    if not VERSION_RE.match(field.default):
        return f"{model.__qualname__}.contract_version default {field.default!r} is not MAJOR.MINOR"
    patterns = [getattr(m, "pattern", None) for m in field.metadata]
    if r"^\d+\.\d+$" not in patterns and model not in PATTERN_PENDING:
        return f"{model.__qualname__}.contract_version has no ^\\d+\\.\\d+$ pattern"
    return None


_SITES = _write_sites()


def test_the_scan_finds_the_known_writers() -> None:
    """The scan is not vacuous: it sees the assignment and synthesis writers."""
    paths = {site.path for site in _SITES}
    assert "src/agents/referee/assignment_handler.py" in paths
    assert "src/agents/referee/synthesis_handler.py" in paths
    assert len(_SITES) >= 20


@pytest.mark.parametrize("site", _SITES, ids=lambda s: f"{s.path}:{s.line}:{s.variable}")
def test_every_written_model_carries_contract_version(site: WriteSite) -> None:
    models = _site_models(site)
    assert models, (
        f"{site.where()} writes a model dump whose class this guard cannot work out. "
        "Annotate the variable with its contract class, or add the call site to "
        "UNTYPED_WRITE_SITES with the class it writes."
    )
    problems = [p for m in models if (p := _version_problem(m))]
    assert not problems, (
        f"{site.where()} writes a contract without a usable contract_version "
        f"(ADR-008 Decision 1): {problems}"
    )


def test_no_stale_untyped_entries() -> None:
    keys = {site.key for site in _SITES}
    stale = sorted(set(UNTYPED_WRITE_SITES) - keys)
    assert not stale, f"UNTYPED_WRITE_SITES entries with no matching call site: {stale}"


def test_untyped_entries_cannot_be_typed_by_the_resolver() -> None:
    """An entry is only for a site the resolver cannot type, so a typed site
    never hides behind a hand-written (possibly wrong) class."""
    typed = [
        site.key
        for site in _SITES
        if site.key in UNTYPED_WRITE_SITES and _resolve(site.variable, site)
    ]
    assert not typed, f"remove these from UNTYPED_WRITE_SITES, the resolver types them: {typed}"


def test_pattern_pending_entries_still_lack_the_pattern() -> None:
    """Once #1006 adds the pattern to a contract, its entry must go."""
    done = [
        m.__qualname__
        for m in PATTERN_PENDING
        if r"^\d+\.\d+$"
        in [getattr(x, "pattern", None) for x in m.model_fields["contract_version"].metadata]
    ]
    assert not done, f"remove from PATTERN_PENDING, the pattern is there now: {done}"


def test_the_version_check_rejects_an_unversioned_model() -> None:
    class Unversioned(BaseModel):
        job_id: str

    Unversioned.__module__ = "src.contracts.example"
    assert "no contract_version" in (_version_problem(Unversioned) or "")


def test_the_version_check_rejects_a_model_outside_src_contracts() -> None:
    class Outside(BaseModel):
        contract_version: str = "1.0"

    assert "not src/contracts/" in (_version_problem(Outside) or "")


def test_the_version_check_rejects_a_missing_default_and_a_missing_pattern() -> None:
    class NoDefault(BaseModel):
        contract_version: str

    class NoPattern(BaseModel):
        contract_version: str = "1.0"

    class BadDefault(BaseModel):
        contract_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+$")

    class Good(BaseModel):
        contract_version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")

    for model in (NoDefault, NoPattern, BadDefault, Good):
        model.__module__ = "src.contracts.example"
    assert "no literal default" in (_version_problem(NoDefault) or "")
    assert "no ^" in (_version_problem(NoPattern) or "")
    assert "not MAJOR.MINOR" in (_version_problem(BadDefault) or "")
    assert _version_problem(Good) is None


def test_the_resolver_types_an_annotated_parameter() -> None:
    sites = [
        s for s in _SITES if s.path == "src/agents/interaction.py" and s.variable == "questions"
    ]
    assert sites, "src/agents/interaction.py write_questions no longer writes questions"
    assert [m.__name__ for m in _site_models(sites[0])] == ["AgentQuestions"]


def test_the_resolver_types_a_validated_and_a_returned_model() -> None:
    """``x = Cls.model_validate(...)`` and ``x = func(...)`` (return annotation)."""
    by_var = {(s.path, s.variable): s for s in _SITES if s.path.startswith("src/agents/referee/")}
    triage = by_var[("src/agents/referee/triage_handler.py", "triage_output")]
    assert [m.__name__ for m in _site_models(triage)] == ["TriageOutputContract"]


def _versioned_contracts() -> dict[str, type[BaseModel]]:
    """Every versioned contract: each ``src/contracts`` model with a literal
    ``contract_version`` default (the schema snapshot's set, model answers included)
    and every model in the contract file registry."""
    from scripts.contract_schema_snapshot import versioned_contract_models
    from src.shared.contract_versioning import registered_contracts

    found = dict(versioned_contract_models())
    known = set(found.values())
    for model in registered_contracts():
        if model not in known:
            found[f"{model.__module__.rsplit('.', 1)[-1]}.{model.__qualname__}"] = model
    return found


VERSIONED = _versioned_contracts()


def test_the_versioned_set_includes_model_answers_and_registered_files() -> None:
    names = {m.__qualname__ for m in VERSIONED.values()}
    assert {"ExecutiveSummaryResponse", "RealityCheckModelResponse"} <= names
    assert {"OpenSearchModelOutputContract", "Assignment", "SeedManifest"} <= names


@pytest.mark.parametrize("key", sorted(VERSIONED))
def test_every_versioned_contract_declares_the_one_version_style(key: str) -> None:
    """``contract_version: str = Field(default="X.Y", pattern=r"^\\d+\\.\\d+$")`` on every
    versioned contract, written to disk or not (ADR-008 Decision 1, #1006)."""
    model = VERSIONED[key]
    field = model.model_fields["contract_version"]
    assert field.annotation is str, f"{key}.contract_version is {field.annotation}, not str"
    assert not field.is_required() and isinstance(
        field.default, str
    ), f"{key}.contract_version has no literal default"
    assert VERSION_RE.match(field.default), f"{key} default {field.default!r} is not MAJOR.MINOR"
    patterns = [getattr(m, "pattern", None) for m in field.metadata]
    assert (
        patterns.count(r"^\d+\.\d+$") == 1 and len([p for p in patterns if p]) == 1
    ), f"{key}.contract_version declares {patterns}, not exactly the ^\\d+\\.\\d+$ pattern"
