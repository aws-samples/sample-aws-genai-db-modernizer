"""Consumer contract tests on the public ``assess()`` result (#1184).

A library caller (AWS Transform is the first) builds on the result's documented fields
(docs/contracts/assess.md). These tests run ``assess()`` the way a caller does, on the
fixtures in ``fixtures/assess_consumer_fixtures.json``, for every ``run_through`` this
release runs (``SUPPORTED_RUN_THROUGH``, so a phase added later is covered without an
edit here), and check what a caller relies on:

- the result validates against the checked-in JSON Schema
  (``docs/contracts/schemas/assess_result.v1.schema.json``);
- every field a fixture guards is present, with the type the schema gives it, and
  reaches at least one value across the fixture's runs (a guard never passes vacuously);
- every field of the result schema is guarded by at least one fixture;
- the documented phase rules hold: fields a later phase fills are null or empty
  before it, and filled from it on.

Removing, renaming or retyping a field, or changing an enum value, without a MAJOR
bump is caught by ``test_assess_released_contract.py``, against the last released shape.

Model phases are patched (``deterministic=False`` runs the reality check's review,
schema design and synthesis's summary through the entry points in
``tests/assess_models.py``); a real model is never called. The phases that always use
a model (``MODEL_PHASES``) run only with the model patched; their refusal with
``deterministic=True`` is asserted instead.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator

from scripts import render_contract_schemas as rcs
from src.assessment import (
    SUPPORTED_RUN_THROUGH,
    AssessmentResult,
    RunThrough,
    assess,
    load_collection,
)
from src.assessment.models import LABEL_BY_PHASE, MODEL_PHASES, result_label
from tests.assess_models import MODEL_ENTRY_POINTS, models_patched

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "assess_consumer_fixtures.json"
#: A fixture's ``sample`` is a bundled sample, read as a caller reads it: the zip the
#: collection scripts' output ships in, through the public ``load_collection``.
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "docs" / "examples"


#: The phases in contract order; a field a phase fills is filled from it on.
PHASE_ORDER: tuple[str, ...] = get_args(RunThrough)
PLACEMENT_PHASE = "reality_check"

_JSON_TYPES: dict[type, str] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _fixtures() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))[
        "fixtures"
    ]
    return loaded


FIXTURES = _fixtures()


def _modes(run_through: str) -> tuple[bool, ...]:
    """The ``deterministic`` values a phase runs with: a model phase only with a model."""
    return (False,) if run_through in MODEL_PHASES else (True, False)


RUNS = [
    pytest.param(
        name,
        run_through,
        deterministic,
        id=f"{name}-{run_through}-{'deterministic' if deterministic else 'model-patched'}",
    )
    for name in FIXTURES
    for run_through in SUPPORTED_RUN_THROUGH
    for deterministic in _modes(run_through)
]


@cache
def _result_schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    return loaded


def _request(name: str, run_through: str, deterministic: bool) -> dict[str, Any]:
    fixture = FIXTURES[name]
    return {
        "database_name": fixture["database_name"],
        "collection": load_collection(SAMPLES_DIR / fixture["sample"] / f"{fixture['sample']}.zip"),
        "options": {
            **fixture["options"],
            "run_through": run_through,
            "deterministic": deterministic,
        },
        "scope": fixture["scope"],
    }


@cache
def _run_json(name: str, run_through: str, deterministic: bool) -> str:
    with models_patched():
        result = assess(_request(name, run_through, deterministic))
    # What a caller in another process reads: the result as JSON.
    return result.model_dump_json()


def _run(name: str, run_through: str, deterministic: bool) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_run_json(name, run_through, deterministic))
    return loaded


# --------------------------------------------------------------------------- paths


def _segments(path: str) -> list[str]:
    """``"statements[].fit{}"`` -> ``["statements", "[]", "fit", "{}"]``."""
    out: list[str] = []
    for part in path.split("."):
        name = part.rstrip("[]{}")
        out.append(name)
        suffix = part[len(name) :]
        while suffix:
            out.append(suffix[:2])
            suffix = suffix[2:]
    return out


def _deref(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = _result_schema()["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _non_null(node: dict[str, Any]) -> dict[str, Any]:
    """The non-null member of an ``Optional`` union (the node itself otherwise)."""
    node = _deref(node)
    members = [_deref(m) for m in node.get("anyOf", []) if _deref(m).get("type") != "null"]
    return members[0] if len(members) == 1 else node


def _schema_at(path: str) -> dict[str, Any] | None:
    """The result schema's node for ``path``, or None when the schema has no such field."""
    node: dict[str, Any] = _result_schema()
    for segment in _segments(path):
        node = _non_null(node)
        if segment == "[]":
            if node.get("type") != "array":
                return None
            node = node.get("items", {})
        elif segment == "{}":
            if not isinstance(node.get("additionalProperties"), dict):
                return None
            node = node["additionalProperties"]
        else:
            props = node.get("properties", {})
            if segment not in props:
                return None
            node = props[segment]
    return _deref(node)


def _allowed_types(node: dict[str, Any]) -> set[str]:
    node = _deref(node)
    if "anyOf" in node:
        return set().union(*(_allowed_types(m) for m in node["anyOf"]))
    if "const" in node:
        return {_JSON_TYPES[type(node["const"])]}
    if "enum" in node:
        return {_JSON_TYPES[type(v)] for v in node["enum"]}
    kind = node.get("type")
    if kind is None:
        return set(_JSON_TYPES.values())
    types = set(kind) if isinstance(kind, list) else {kind}
    return types | ({"integer"} if "number" in types else set())


def _schema_leaf_paths(node: dict[str, Any], path: str = "") -> set[str]:
    """Every field path of the result schema, down to its leaves."""
    node = _non_null(node)
    paths = {path} if path else set()
    if node.get("type") == "array":
        items = _non_null(node.get("items", {}))
        if items.get("properties"):
            # An item object's own path is its list's path; its fields are below it.
            paths |= _schema_leaf_paths(items, f"{path}[]") - {f"{path}[]"}
    elif isinstance(node.get("additionalProperties"), dict):
        paths.add(f"{path}{{}}")
    for name, child in node.get("properties", {}).items():
        paths |= _schema_leaf_paths(child, f"{path}.{name}" if path else name)
    return paths


def _values_at(document: Any, path: str) -> tuple[list[Any], list[str]]:
    """Every value ``path`` reaches in ``document``, and every place a field is missing.

    A null parent (``scope`` with no scope sent) reaches nothing below it: the parent's
    own guard checks that null is allowed there."""
    level: list[tuple[str, Any]] = [("", document)]
    missing: list[str] = []
    for segment in _segments(path):
        nxt: list[tuple[str, Any]] = []
        for where, value in level:
            if value is None:
                continue
            if segment == "[]":
                nxt.extend((f"{where}[{i}]", item) for i, item in enumerate(value))
            elif segment == "{}":
                nxt.extend((f"{where}[{key!r}]", item) for key, item in value.items())
            elif segment in value:
                nxt.append((f"{where}.{segment}" if where else segment, value[segment]))
            else:
                missing.append(f"{where}.{segment}" if where else segment)
        level = nxt
    return [value for _, value in level], missing


def _guard_problems(document: dict[str, Any], guards: list[str]) -> list[str]:
    """Each guarded field missing from ``document``, or of a type the schema refuses."""
    problems: list[str] = []
    for path in guards:
        node = _schema_at(path)
        assert node is not None, path
        allowed = _allowed_types(node)
        values, missing = _values_at(document, path)
        problems.extend(f"{where}: missing" for where in missing)
        for value in values:
            if _JSON_TYPES[type(value)] not in allowed:
                problems.append(f"{path}: {type(value).__name__} where {sorted(allowed)} expected")
    return problems


# --------------------------------------------------------------------------- fixtures


@pytest.mark.passes_on_main
def test_every_guard_names_a_field_of_the_published_schema() -> None:
    unknown = {
        name: [path for path in fixture["guards"] if _schema_at(path) is None]
        for name, fixture in FIXTURES.items()
    }
    assert not any(unknown.values()), f"guards with no field in the result schema: {unknown}"


@pytest.mark.passes_on_main
def test_every_result_field_is_guarded_by_a_fixture() -> None:
    guarded = {path for fixture in FIXTURES.values() for path in fixture["guards"]}
    unguarded = sorted(_schema_leaf_paths(_result_schema()) - guarded)
    assert not unguarded, (
        "result fields no fixture in fixtures/assess_consumer_fixtures.json guards (add "
        f"each to the guards of a fixture that reaches it): {unguarded}"
    )


@pytest.mark.passes_on_main
def test_the_fixtures_cover_a_sql_server_source_and_an_engine_scope() -> None:
    run_through = SUPPORTED_RUN_THROUGH[-1]
    results = {name: _run(name, run_through, _modes(run_through)[0]) for name in FIXTURES}
    assert "wordpress" in FIXTURES
    assert any(r["summary"]["source_engine"] == "sqlserver" for r in results.values())
    scoped = [r for r in results.values() if r["options"]["target_engines"] is not None]
    assert scoped, "no fixture sets an engine scope"
    assert any(r["summary"]["unservable_count"] > 0 for r in scoped)


@pytest.mark.passes_on_main
def test_every_supported_run_through_has_a_label() -> None:
    assert set(SUPPORTED_RUN_THROUGH) <= set(LABEL_BY_PHASE) <= set(PHASE_ORDER)


# --------------------------------------------------------------------------- results


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("name", "run_through", "deterministic"), RUNS)
def test_the_result_validates_against_the_published_schema(
    name: str, run_through: str, deterministic: bool
) -> None:
    document = _run(name, run_through, deterministic)
    errors = sorted(
        Draft202012Validator(_result_schema()).iter_errors(document), key=lambda e: e.json_path
    )
    assert not errors, "\n".join(f"{e.json_path}: {e.message}" for e in errors[:20])
    # The typed model reads back what it wrote, unchanged.
    text = _run_json(name, run_through, deterministic)
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("name", "run_through", "deterministic"), RUNS)
def test_every_guarded_field_is_present_with_its_documented_type(
    name: str, run_through: str, deterministic: bool
) -> None:
    problems = _guard_problems(_run(name, run_through, deterministic), FIXTURES[name]["guards"])
    assert not problems, "\n".join(problems[:30])


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d["statements"][0].pop("reason_code"), "statements[0].reason_code: missing"),
        (lambda d: d["summary"].pop("engines"), "summary.engines: missing"),
        (
            lambda d: d["statements"][0].update(join_count="1"),
            "statements[].join_count: str where ['integer', 'null'] expected",
        ),
        (
            lambda d: d["summary"]["statements_per_engine"].update(dynamodb=1.5),
            "summary.statements_per_engine{}: float where ['integer'] expected",
        ),
    ],
)
def test_a_removed_or_retyped_field_is_reported(change: Any, expected: str) -> None:
    """The guard check itself: a field a caller reads, gone or of another type, fails."""
    document = _run("wordpress", PLACEMENT_PHASE, True)
    assert _guard_problems(document, FIXTURES["wordpress"]["guards"]) == []
    change(document)
    assert expected in _guard_problems(document, FIXTURES["wordpress"]["guards"])


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", FIXTURES)
def test_every_guard_reaches_a_value(name: str) -> None:
    """A guard on a list item or a scope field only checks something when the fixture
    has one: across its runs, every guarded path reaches at least one value."""
    runs = [_run(name, rt, det) for rt in SUPPORTED_RUN_THROUGH for det in _modes(rt)]
    vacuous = [
        path for path in FIXTURES[name]["guards"] if not any(_values_at(r, path)[0] for r in runs)
    ]
    assert not vacuous, f"{name}: guards that reach no value in any run: {vacuous}"


@pytest.mark.passes_on_main
@pytest.mark.parametrize(("name", "run_through", "deterministic"), RUNS)
def test_the_documented_phase_rules_hold(name: str, run_through: str, deterministic: bool) -> None:
    """docs/contracts/assess.md: fields a later phase fills are null or empty before it."""
    result = _run(name, run_through, deterministic)
    summary, statements = result["summary"], result["statements"]
    assert summary["run_through_reached"] == run_through
    assert result["label"] == result_label(run_through, summary["designed_engines"])
    if run_through in MODEL_PHASES:
        assert (summary["design_note"] is None) == bool(summary["designed_engines"])
    else:
        assert summary["designed_engines"] == [] and summary["design_note"] is None
    assert result["options"]["run_through"] == run_through
    assert statements and summary["statement_count"] == len(statements)
    assert any(s["fit"] for s in statements)
    if PHASE_ORDER.index(run_through) < PHASE_ORDER.index(PLACEMENT_PHASE):
        assert summary["assignment_version"] is None
        assert summary["statements_per_engine"] == {}
        assert summary["engines"] == summary["consolidations"] == summary["tables"] == []
        for s in statements:
            assert s["engine"] is s["confidence"] is s["reason_code"] is None, s["query_id"]
            assert s["matched_patterns"] == [], s["query_id"]
        assert all(g["engine"] is None for g in result["groups"])
        return
    assert isinstance(summary["assignment_version"], int)
    assert summary["engines"] and summary["tables"]
    placed = [s for s in statements if s["reason_code"] != "unservable"]
    unservable = [s for s in statements if s["reason_code"] == "unservable"]
    assert len(unservable) == summary["unservable_count"]
    assert sum(summary["statements_per_engine"].values()) == len(placed)
    for s in placed:
        assert isinstance(s["engine"], str) and s["unservable_reason"] is None, s["query_id"]
    for s in unservable:
        assert s["engine"] is None and s["matched_patterns"] == [], s["query_id"]
        assert isinstance(s["unservable_reason"], str), s["query_id"]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("run_through", SUPPORTED_RUN_THROUGH)
def test_the_options_and_scope_are_echoed(run_through: str) -> None:
    fixture = FIXTURES["wordpress_scoped"]
    result = _run("wordpress_scoped", run_through, _modes(run_through)[0])
    assert result["options"]["target_engines"] == sorted(fixture["options"]["target_engines"])
    assert result["scope"] == fixture["scope"]
    rewritten = set(fixture["scope"]["rewritten_query_ids"])
    marked = {s["query_id"] for s in result["statements"] if s["rewritten_by_caller"]}
    assert marked == rewritten
    assert result["artifacts"]["compatibility"].startswith("none")


@pytest.mark.parametrize("name", FIXTURES)
@pytest.mark.parametrize("run_through", MODEL_PHASES)
def test_a_model_phase_is_refused_without_a_model(name: str, run_through: str) -> None:
    """A phase that always uses a model is refused with ``deterministic=True`` before
    anything runs, and no model entry point is reached."""
    from pydantic import ValidationError

    with models_patched() as mocks:
        with pytest.raises(ValidationError, match="needs a model"):
            assess(_request(name, run_through, True))
    assert not any(m.called for m in mocks.values())


def test_every_model_phase_reaches_its_patched_entry_points() -> None:
    """The designer and synthesis's summary are in MODEL_ENTRY_POINTS and are what a
    model phase calls (a model call missing from the list would reach the Bedrock guard
    in tests/conftest.py and fail the run)."""
    targets = {target for target, _ in MODEL_ENTRY_POINTS}
    with models_patched() as mocks:
        assess(_request("wordpress", "synthesis", False))
    for name in (
        "run_schema_design_auto",
        "generate_executive_summary",
        "validate_consolidations",
    ):
        called = [t for t in targets if t.endswith(f".{name}") and mocks[t].called]
        assert called, name
