"""Every write of a contract file goes through its model (ADR-008 Decision 2, #1018).

A writer that reads a contract file as a dict, patches it and writes it back
keeps the ``contract_version`` it read, so after the next MINOR bump the file
would carry the newer MINOR's fields under the older number (#1018). The
version a contract file says must come from its model. This guard finds every
``<store>.write_json(path, data)`` call in ``src/`` and ``scripts/``, works out
the path's shapes (``tests/contract/test_reads_check_contract_version.py``'s
renderer, with an interpolated engine tried as every catalog engine), and, for
a path the contract file registry in ``src/shared/contract_versioning.py`` maps
to a contract, requires ``data`` to be one of:

- a dump of a model (``x.model_dump(...)``, ``json.loads(x.model_dump_json(...))``),
  whose class ``tests/contract/test_written_contracts_versioned.py`` checks;
- ``stamp_artifact_version(path, data)`` whose ``path`` renders to the same shapes
  as the path written, which writes the registered contract's current version; or
- a name whose last assignment before the write (by line) is one of those.

The path and data are read from the positional or the keyword arguments
(``write_json(key, data=raw)``). A path whose prefix comes from a caller
(``f"{base}/assignment.json"``) counts as a contract write when its file name is a
registered contract file's. Anything else is listed in ``NOT_THROUGH_MODEL`` with
the reason, keyed on the path written. A write whose path or data cannot be worked
out (``+`` concatenation, ``.format()``, a bare parameter, ``**kwargs``) is listed in
``UNRENDERED_PATHS`` with what it writes, unless its data is a dump or a stamp.

Known blind spots: only ``.write_json`` calls are scanned (not ``json.dump`` to a
file); "last assignment by line" ignores branches, so a raw value assigned in one
branch and a stamp in a later one passes; a stamp of an unrendered path is accepted
as written to the same unrendered path; and a stamped dict is not validated against
the model.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.shared import contract_versioning as versioning
from src.shared.catalog import engine_ids
from tests.contract.test_reads_check_contract_version import _PLACEHOLDER, _Ctx, _render
from tests.contract.test_written_contracts_versioned import _dumped_name

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts")
STAMP_HELPERS = {"stamp_artifact_version"}

_UPLOAD = (
    "the user's upload, kept as it was sent: usually a raw collection, which is not a "
    "contract and carries no contract_version; the collector output written from it is "
    "stamped"
)
_UPLOADS = "1/1/uploads/collector-output.json"

# (file, function, data expression, the path shapes written, " | "-joined) -> why
# this contract-file write does not go through the model. Keyed on the path too, so
# another raw write of the same name to another file is not covered. An entry that
# no longer matches such a write fails ``test_no_stale_entries``.
NOT_THROUGH_MODEL: dict[tuple[str, str, str, str], str] = {
    ("scripts/run_assessment.py", "phase_collect", "input_data", _UPLOADS): _UPLOAD,
    ("scripts/run_collect.py", "main", "input_data", _UPLOADS): _UPLOAD,
    ("scripts/test_local_phased.py", "main", "input_data", _UPLOADS): _UPLOAD,
    ("src/atx_orchestrator/core.py", "_discover_uploaded_input", "collection", _UPLOADS): (_UPLOAD),
    (
        "scripts/atx_subagent_test.py",
        "<module>",
        "json.loads(offline_input.read_text())",
        "discourse/subagent1/uploads/collector-output.json",
    ): _UPLOAD,
    (
        "scripts/atx_tool_test.py",
        "<module>",
        "json.loads(offline_input.read_text())",
        "discourse/atxtool1/uploads/collector-output.json",
    ): _UPLOAD,
    (
        "src/storage/assignment_versioning.py",
        "keep_completed_reality_check_output",
        "previous",
        "1/1/reality-check/output.json | 1/1/reality-check/v1/output.json",
    ): (
        "a verbatim copy of the completed reality-check/output.json (as read, an older "
        "MAJOR through its adapter) kept under its version before a new run replaces it "
        "(#193): the copy keeps the version its content was written at"
    ),
}

# (file, function, data expression, "") -> what the write stores, for a write whose
# path (or data) the renderer cannot work out.
UNRENDERED_PATHS: dict[tuple[str, str, str, str], str] = {
    ("src/api/stored_input_redaction.py", "record_marker", "{...}", ""): (
        "the redaction marker .meta/_stored-input-redaction-v1.json (not a contract)"
    ),
    (
        "src/orchestrator/local_orchestrator.py",
        "_copy_schema_artifacts",
        "self.store.read_json(key)",
        "",
    ): (
        "a verbatim copy of schema-<engine>/v<N>/* to the next version: the copy keeps "
        "the version its content was written at"
    ),
}


@dataclass(frozen=True)
class Write:
    path: str
    line: int
    function: str
    data: str
    shapes: tuple[str, ...]
    contracts: tuple[str, ...]
    through_model: bool

    @property
    def key(self) -> tuple[str, str, str, str]:
        """(file, function, data expression, the path shapes written): an exception
        covers this write to this path only, never another raw write of the same name."""
        return (self.path, self.function, self.data, " | ".join(self.shapes))

    def where(self) -> str:
        return f"{self.path}:{self.line} ({self.function}, writes {self.data})"


def _data_text(data: ast.expr) -> str:
    return "{...}" if isinstance(data, ast.Dict) else ast.unparse(data)


def _contract_of(shape: str) -> type | None:
    """The contract registered for ``shape``, trying an interpolated engine as each engine."""
    candidates = [shape] + [
        re.sub(rf"(?<=-){re.escape(_PLACEHOLDER)}(?=/)", engine, shape)
        for engine in sorted(engine_ids())
    ]
    for candidate in candidates:
        found = versioning.contract_for_artifact(candidate)
        if found is not None:
            return found
    return None


def _contract_file_names() -> frozenset[str]:
    """The literal file names of the registered contract files (``assignment.json``,
    ``schema_output.json``, ...), from the registry's own patterns."""
    names = set()
    for pattern, _ in versioning._registry():
        # The literal file name the pattern ends with (``schema_output\.json$``);
        # a pattern ending in a class (``[^/]+\.json``) names no single file.
        literal = re.search(r"(?:^|[/?)])([\w-]+)\\\.json\$$", pattern.pattern)
        if literal:
            names.add(f"{literal.group(1)}.json")
    return frozenset(names)


CONTRACT_FILE_NAMES = _contract_file_names()


def _contracts_of(shapes: tuple[str, ...]) -> tuple[str, ...]:
    """The contracts ``shapes`` are written to. A shape the registry does not match but
    whose file name is a contract file's (``f"{base}/assignment.json"`` with ``base`` a
    caller's prefix) counts as that file, unresolved: the prefix may be the contract's."""
    found: list[str] = []
    for shape in shapes:
        contract = _contract_of(shape)
        if contract is not None:
            found.append(contract.__qualname__)
        elif shape.rsplit("/", 1)[-1] in CONTRACT_FILE_NAMES and not versioning.is_model_io(shape):
            found.append(f"unresolved {shape.rsplit('/', 1)[-1]}")
    return tuple(dict.fromkeys(found))


def _last_assignment(scope: ast.AST, name: str, before: int) -> ast.expr | None:
    found: tuple[int, ast.expr] | None = None
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and node.lineno < before:
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                if found is None or node.lineno > found[0]:
                    found = (node.lineno, node.value)
    return found[1] if found else None


def _through_model(
    data: ast.expr, scope: ast.AST, line: int, shapes: set[str], ctx: _Ctx, depth: int = 0
) -> bool:
    """A model dump, or a stamp of the path being written (``shapes``): a stamp of
    another path stamps that path's contract, or nothing for an unregistered one."""
    if _dumped_name(data) is not None:
        return True
    if isinstance(data, ast.Call):
        func = data.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in STAMP_HELPERS:
            return False
        stamped = _call_arg(data, 0, ("path",))
        return stamped is not None and set(_render(stamped, ctx)) == shapes
    if isinstance(data, ast.Name) and depth < 4:
        value = _last_assignment(scope, data.id, line)
        return value is not None and _through_model(value, scope, line, shapes, ctx, depth + 1)
    return False


def _call_arg(call: ast.Call, index: int, names: tuple[str, ...]) -> ast.expr | None:
    """The ``index``-th positional argument, else the keyword named one of ``names``."""
    if len(call.args) > index and not any(isinstance(a, ast.Starred) for a in call.args):
        return call.args[index]
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    return None


def _writes_in(tree: ast.Module, path: str) -> list[Write]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_json"
        ):
            continue
        func: ast.AST | None = parents.get(node)
        while func is not None and not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = parents.get(func)
        scope = func if func is not None else tree
        ctx = _Ctx(tree, path, scope, {})
        # Positional or keyword (``write_json(key, data=raw)``); a call whose path or
        # data cannot be found (``**kwargs``) has no shapes, so it must be listed.
        target = _call_arg(node, 0, ("path", "key"))
        data = _call_arg(node, 1, ("data",))
        shapes = tuple(dict.fromkeys(_render(target, ctx))) if target is not None else ()
        found.append(
            Write(
                path=path,
                line=node.lineno,
                function=func.name if func is not None else "<module>",  # type: ignore[attr-defined]
                data=_data_text(data) if data is not None else "<unknown>",
                shapes=shapes if data is not None else (),
                contracts=_contracts_of(shapes) if data is not None else (),
                through_model=(
                    data is not None and _through_model(data, scope, node.lineno, set(shapes), ctx)
                ),
            )
        )
    return found


def _all_writes() -> list[Write]:
    writes = []
    for root in SCAN_ROOTS:
        for file in sorted((REPO / root).rglob("*.py")):
            if "node_modules" in file.parts:
                continue
            rel = str(file.relative_to(REPO))
            writes += _writes_in(ast.parse(file.read_text(), filename=rel), rel)
    return writes


WRITES = _all_writes()


def _bypasses(writes: list[Write]) -> list[Write]:
    """Contract-file writes that do not go through the model."""
    return [w for w in writes if w.contracts and not w.through_model]


def _unrendered(writes: list[Write]) -> list[Write]:
    return [w for w in writes if not w.shapes and not w.through_model]


def test_the_scan_finds_the_writes() -> None:
    assert len(WRITES) > 80
    assert any(w.contracts == ("Assignment",) and w.through_model for w in WRITES)


def test_every_contract_file_write_goes_through_its_model() -> None:
    offenders = [w for w in _bypasses(WRITES) if w.key not in NOT_THROUGH_MODEL]
    assert not offenders, (
        "These writes store a contract file from a raw dict, so the file keeps whatever "
        "contract_version the dict had (ADR-008 Decision 2, #1018). Write a model dump, or "
        "wrap the dict in stamp_artifact_version(path, data), or list the write in "
        "NOT_THROUGH_MODEL with the reason:\n"
        + "\n".join(f"  {w.where()} -> {', '.join(w.contracts)}" for w in offenders)
    )


def test_every_write_with_an_unknown_path_is_listed() -> None:
    offenders = [w for w in _unrendered(WRITES) if w.key not in UNRENDERED_PATHS]
    assert not offenders, (
        "The path of these writes cannot be worked out, so this guard cannot tell whether "
        "they store a contract file. Write through the model, or list them in "
        "UNRENDERED_PATHS with what they write:\n" + "\n".join(f"  {w.where()}" for w in offenders)
    )


def test_no_stale_entries() -> None:
    bypassing = {w.key for w in _bypasses(WRITES)}
    unrendered = {w.key for w in _unrendered(WRITES)}
    assert not set(NOT_THROUGH_MODEL) - bypassing, set(NOT_THROUGH_MODEL) - bypassing
    assert not set(UNRENDERED_PATHS) - unrendered, set(UNRENDERED_PATHS) - unrendered


# --- self-tests: each shape the scan must tell apart ------------------------------

_SAMPLE = """
def patched(store, db, job):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    data = store.read_json(key)
    data["status"] = "approved"
    store.write_json(key, data)

def stamped(store, db, job, data):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    store.write_json(key, stamp_artifact_version(key, data))

def stamped_by_name(store, db, job, data):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    data = stamp_artifact_version(key, data)
    store.write_json(key, data)

def patched_after_the_stamp(store, db, job, data):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    data = stamp_artifact_version(key, data)
    data = {**data, "status": "approved"}
    store.write_json(key, data)

def dumped(store, db, job, model):
    store.write_json(f"{db}/{job}/assignment/v1/assignment.json", model.model_dump(mode="json"))

def dumped_through_json(store, db, job, model):
    store.write_json(f"{db}/{job}/collector/output.json", json.loads(model.model_dump_json()))

def any_engine(store, db, job, engine, output):
    store.write_json(f"{db}/{job}/schema-{engine}/v1/schema_output.json", output)

def either_path(store, db, job, version, output):
    key = f"{db}/{job}/referee-synthesis/report.json" if version == 0 else f"{db}/{job}/x.json"
    store.write_json(key, output)

def not_a_contract(store, db, job, data):
    store.write_json(f"{db}/{job}/collector/sql_shadow.json", data)

def unknown_path(store, key, data):
    store.write_json(key, data)

def keyword_data(store, db, job, raw):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    store.write_json(key, data=raw)

def keyword_both(store, db, job, raw):
    store.write_json(path=f"{db}/{job}/assignment/v1/assignment.json", data=raw)

def keyword_stamped(store, db, job, raw):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    store.write_json(key, data=stamp_artifact_version(key, raw))

def caller_prefix(store, base, raw):
    store.write_json(f"{base}/assignment.json", raw)

def caller_prefix_schema(store, base_key, raw):
    store.write_json(f"{base_key}/schema_output.json", raw)

def caller_prefix_other_file(store, base, raw):
    store.write_json(f"{base}/notes.json", raw)

def caller_prefix_model_io(store, base, raw):
    store.write_json(f"{base}/partial_output.json", raw)

def stamp_of_another_path(store, db, job, raw):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    store.write_json(key, stamp_artifact_version(f"{db}/{job}/other.json", raw))

def stamp_by_name_of_another_path(store, db, job, raw):
    key = f"{db}/{job}/assignment/v1/assignment.json"
    raw = stamp_artifact_version(f"{db}/{job}/other.json", raw)
    store.write_json(key, raw)

def splatted(store, kwargs):
    store.write_json(**kwargs)
"""


_TWO_WRITES = """
def collect(store, db, job, input_data):
    store.write_json(f"{db}/{job}/uploads/collector-output.json", input_data)
    store.write_json(f"{db}/{job}/collector/output.json", input_data)
"""


@pytest.fixture(scope="module")
def sample() -> dict[str, Write]:
    return {w.function: w for w in _writes_in(ast.parse(_SAMPLE), "sample.py")}


@pytest.mark.parametrize(
    ("function", "bypasses"),
    [
        ("patched", True),
        ("stamped", False),
        ("stamped_by_name", False),
        ("patched_after_the_stamp", True),
        ("dumped", False),
        ("dumped_through_json", False),
        ("any_engine", True),
        ("either_path", True),
        ("not_a_contract", False),
        ("unknown_path", False),
        ("keyword_data", True),
        ("keyword_both", True),
        ("keyword_stamped", False),
        ("caller_prefix", True),
        ("caller_prefix_schema", True),
        ("caller_prefix_other_file", False),
        ("caller_prefix_model_io", False),
        ("stamp_of_another_path", True),
        ("stamp_by_name_of_another_path", True),
        ("splatted", False),
    ],
)
def test_the_scan_tells_a_bypass_from_a_write_through_the_model(
    sample: dict[str, Write], function: str, bypasses: bool
) -> None:
    assert (sample[function] in _bypasses(list(sample.values()))) is bypasses


def test_an_interpolated_engine_is_tried_as_every_engine(sample: dict[str, Write]) -> None:
    assert sample["any_engine"].contracts
    assert all(c.endswith("ModelOutputContract") for c in sample["any_engine"].contracts)


def test_a_path_the_scan_cannot_read_is_reported(sample: dict[str, Write]) -> None:
    assert _unrendered(list(sample.values())) == [sample["unknown_path"], sample["splatted"]]


def test_a_path_from_a_caller_counts_as_the_contract_file_it_names(
    sample: dict[str, Write],
) -> None:
    assert sample["caller_prefix"].contracts == ("unresolved assignment.json",)
    assert sample["caller_prefix_schema"].contracts == ("unresolved schema_output.json",)


def test_an_exception_covers_only_the_path_it_was_listed_for() -> None:
    """A second raw write of an exempt name, to another contract file, is still caught."""
    upload, output = _writes_in(ast.parse(_TWO_WRITES), "sample.py")
    assert upload.data == output.data == "input_data"
    assert upload.key != output.key
    exempt = {upload.key: "the upload, kept as sent"}
    assert [w for w in _bypasses([upload, output]) if w.key not in exempt] == [output]


def test_every_exception_names_the_path_it_covers() -> None:
    for key in NOT_THROUGH_MODEL:
        assert key[3], f"{key} names no path"
