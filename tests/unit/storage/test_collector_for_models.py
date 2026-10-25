"""Model-facing steps read the collector output only through one choke point (#1180).

``read_collector_for_models`` removes the caller's query tags. Every module that
builds or feeds a model input (``ci/needs_llm_run.py``'s E2E_LLM and BEDROCK_SMOKE
lists, plus the modules that hand them the collector output) must read the
collector output through it, never with its own ``read_json``: a module added to
those lists later is checked too. ``tests/unit/agents/test_query_tags_reach_no_model.py``
runs the model inputs themselves with a marker tag.
"""

from __future__ import annotations

import ast
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from src.storage.collector_for_models import collector_output_key, read_collector_for_models
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ci"))
from needs_llm_run import MODEL_FACING  # noqa: E402

# Modules outside those lists that hand the collector output to a model-facing step.
FEEDERS = (
    "src/agents/analysis/handler.py",  # builds the AnalysisInput the advisors read
    "src/agents/referee/synthesis_data.py",  # the synthesis data its payload is built from
)
_PATH = "collector/output.json"
# Calls that name the collector output without reading it: existence checks, writes,
# error messages and logging.
_NOT_READS = {
    "exists",
    "write_json",
    "stamp_artifact_version",
    "FileNotFoundError",
    "RuntimeError",
    "warning",
    "info",
    "debug",
    "error",
    "print",
    "say",
    "_path",
    "_log_artifact",
}


def _target_names(target: ast.AST) -> set[str]:
    """The names a binding target binds: ``key``, ``self.key`` (as ``key``), tuples."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(e) for e in target.elts))
    return set()


# Calls that build a path from their arguments; a value made by any other call is not a
# path, so a name bound to it is not one either.
_PATH_BUILDERS = {"join", "Path", "PurePosixPath", "str", "format", "collector_output_key"}


def _is_path_expr(node: ast.AST) -> bool:
    """Whether ``node`` is built only from strings, names, f-strings and path-building
    calls. An f-string is a string whatever its fields compute (``f"{key.split(...)[0]}/
    collector/output.json"``), so calls inside its fields do not count."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _PATH_BUILDERS:
            return False
    return all(_is_path_expr(child) for child in ast.iter_child_nodes(node))


def _collector_keys(scope: ast.AST, inherited: frozenset[str] = frozenset()) -> set[str]:
    """Names bound in ``scope`` to a path naming the collector output: plain, annotated
    and attribute assignments and loop variables, and paths built from such names (a
    module constant used in an f-string), until nothing new is found."""
    names: set[str] = set(inherited)
    while True:
        found = set(names)
        for node in ast.walk(scope):
            value, targets = None, []
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            elif isinstance(node, (ast.For, ast.comprehension)):
                value, targets = node.iter, [node.target]
            if value is not None and _is_path_expr(value) and _names_collector(value, found):
                for target in targets:
                    found |= _target_names(target)
        if found == names:
            return names
        names = found


def _names_collector(node: ast.AST, keys: set[str]) -> bool:
    """Whether ``node`` names the collector output: a string holding the path, a name
    bound to one, the choke point's own ``collector_output_key(...)``, or a path joined
    from ``"collector"`` and ``"output.json"`` (``os.path.join``, ``Path / ...``)."""
    parts = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if _PATH in n.value:
                return True
            parts.add(n.value.strip("/"))
        elif isinstance(n, ast.Name) and n.id in keys:
            return True
        elif isinstance(n, ast.Attribute) and n.attr in keys:
            return True
        elif isinstance(n, ast.Call):
            func = n.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "collector_output_key":
                return True
    return {"collector", "output.json"} <= parts


def direct_reads(source: str) -> list[int]:
    """Lines that read the collector output by any call other than an existence check, a
    write, an error message or a log line (``_NOT_READS``). Names are bound per function,
    on top of the module's own constants."""
    tree = ast.parse(source)
    module_level = ast.Module(
        body=[
            n
            for n in tree.body
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ],
        type_ignores=[],
    )
    module_keys = frozenset(_collector_keys(module_level))
    lines: set[int] = set()
    scopes: list[ast.AST] = [module_level] + [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        keys = _collector_keys(scope, module_keys)
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _NOT_READS or name in _PATH_BUILDERS:
                continue
            args = [*node.args, *(k.value for k in node.keywords)]
            if any(_names_collector(arg, keys) for arg in args):
                lines.add(node.lineno)
    return sorted(lines)


def _guarded() -> list[Path]:
    return [
        path
        for root in ("src", "scripts")
        for path in sorted((REPO / root).rglob("*.py"))
        if any(fnmatchcase(str(path.relative_to(REPO)), p) for p in (*MODEL_FACING, *FEEDERS))
    ]


def test_model_facing_modules_read_the_collector_only_through_the_choke_point() -> None:
    guarded = _guarded()
    assert len(guarded) >= 40, len(guarded)  # the lists still match files
    offenders = [
        f"{path.relative_to(REPO)}:{line}"
        for path in guarded
        for line in direct_reads(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "read the collector output with read_collector_for_models (it removes the "
        f"caller's query tags): {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        'x = store.read_json(f"{db}/{job}/collector/output.json")',
        'key = f"{db}/{job}/collector/output.json"\nx = store.read_json(key)',
        'key = f"{p}/collector/output.json"\nx = _read(key) if store.exists(key) else {}',
        'x = _read_artifact(store, f"{db}/{job}/collector/output.json", required=False)',
        "x = store.read_json(collector_output_key(db, job))",
        # #1323's form: the job prefix computed inside the f-string
        "k = f\"{key.split('/schema-', 1)[0]}/collector/output.json\"\nx = store.read_json(k)",
        'key: str = f"{db}/{job}/collector/output.json"\nx = store.read_json(key)',
        'self.key = f"{p}/collector/output.json"\nx = self.store.read_json(self.key)',
        'x = json.load(open(os.path.join(prefix, "collector", "output.json")))',
        'x = store.read_json(str(Path(prefix) / "collector" / "output.json"))',
        'OUT = "collector/output.json"\n\ndef f(db, job):\n    return store.read_json(f"{db}/{job}/{OUT}")',
        'for key in (f"{p}/collector/output.json", t):\n    data = store.read_json(key)',
    ],
)
def test_the_guard_catches_a_direct_read(source: str) -> None:
    assert direct_reads(source)


@pytest.mark.parametrize(
    "source",
    [
        'key = f"{db}/{job}/collector/output.json"\nif not store.exists(key):\n    raise FileNotFoundError(f"missing {key}")',
        'key = f"{db}/{job}/collector/output.json"\nstore.write_json(key, stamp_artifact_version(key, data))',
        "x = read_collector_for_models(store, db, job)",
    ],
)
def test_the_guard_allows_checks_writes_and_the_choke_point(source: str) -> None:
    assert not direct_reads(source)


def test_the_choke_point_removes_the_tags(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    collector = {
        "queries": {
            "query_patterns": [
                {"query_id": "q1", "query_tags": ["controller='x'"], "query_tags_dropped": 2}
            ]
        }
    }
    store.write_json(collector_output_key("db", "job"), collector)
    assert read_collector_for_models(store, "db", "job") == {
        "queries": {"query_patterns": [{"query_id": "q1"}]}
    }
    assert store.read_json(collector_output_key("db", "job")) == collector  # kept on disk
    assert read_collector_for_models(store, "db", "other", missing_ok=True) == {}
    with pytest.raises(FileNotFoundError):
        read_collector_for_models(store, "db", "other")
