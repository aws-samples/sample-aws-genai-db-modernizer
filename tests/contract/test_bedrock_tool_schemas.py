"""Every answer model we send to Bedrock as a tool schema is one Bedrock accepts (#1281).

A Strands agent built with ``structured_output_model=M`` sends ``M`` to Bedrock
as a tool, converted by ``convert_pydantic_to_tool_spec``. That conversion gives
every field that is not required a nullable type (``["string", "null"]``) and
leaves an ``enum`` as pydantic wrote it. Bedrock refuses a schema whose enum has
a value that does not match the declared type (``Enum value 'low' does not match
declared type '['string', 'null']'``), so a ``Literal`` field with a default, or
``Literal[...] | None``, made the whole call fail every time: the DocumentDB
analysis advisor never returned advice.

The rule checked here, on the converted schema of every model a Strands agent is
given (found in ``src/`` by name, so a new call site is covered without editing
this file): an enum node never sits under a list of types, and each of its
values matches its one declared type. So an enum field is either required (the
conversion leaves its type alone), or lists ``None`` among its values
(``Literal["a", "b", None]``: pydantic then writes the enum with no type, and
the conversion has no type to make nullable).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError
from strands.tools.structured_output.structured_output_utils import (
    convert_pydantic_to_tool_spec,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"

# The answer models this file must find; a discovery that finds fewer is broken.
EXPECTED_AT_LEAST = {
    "LlmDocumentDBOutput",
    "LlmAdvisorOutput",
    "DocumentDBModelOutputContract",
    "AuroraDesignDeltaContract",
    "DynamoDBModelOutputContract",
    "ElastiCacheModelOutputContract",
    "OpenSearchModelOutputContract",
    "PEReviewResult",
    "RealityCheckModelResponse",
    "ExecutiveSummaryResponse",
}

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "array": (list,),
    "object": (dict,),
}


def _value_matches(value: Any, json_type: str) -> bool:
    if isinstance(value, bool) and json_type in ("integer", "number"):
        return False
    return isinstance(value, _JSON_TYPES.get(json_type, ()))


def enum_type_mismatches(schema: Any, path: str = "$") -> list[str]:
    """Paths of enum nodes Bedrock refuses: a list of types, or a value outside the type."""
    found: list[str] = []
    if isinstance(schema, dict):
        if "enum" in schema and "type" in schema:
            declared = schema["type"]
            if isinstance(declared, list):
                found.append(f"{path}: enum {schema['enum']!r} under the type list {declared!r}")
            elif bad := [v for v in schema["enum"] if not _value_matches(v, declared)]:
                found.append(f"{path}: enum values {bad!r} do not match declared type {declared!r}")
        for key, value in schema.items():
            found.extend(enum_type_mismatches(value, f"{path}.{key}"))
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            found.extend(enum_type_mismatches(value, f"{path}[{i}]"))
    return found


def _tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema: dict[str, Any] = convert_pydantic_to_tool_spec(model)["inputSchema"]["json"]  # type: ignore[index]
    return schema


def _answer_model_names(tree: ast.AST) -> tuple[list[str], list[str]]:
    """``(names, unresolved)``: the bare names passed as ``structured_output_model=``
    or returned by ``_output_model``, and every other such expression (an attribute,
    a call, a variable built at run time), as ``line N: <source>``, which this file
    cannot check and so reports instead of skipping."""
    names: list[str] = []
    unresolved: list[str] = []

    def take(value: ast.expr | None, line: int) -> None:
        if isinstance(value, ast.Name):
            names.append(value.id)
        elif value is not None and ast.unparse(value) == "self._output_model()":
            pass  # every ``_output_model`` override's return is read below
        else:
            text = ast.unparse(value) if value is not None else "<bare return>"
            unresolved.append(f"line {line}: {text}")

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "structured_output_model":
            take(node.value, node.value.lineno)
        if isinstance(node, ast.FunctionDef) and node.name == "_output_model":
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return):
                    take(ret.value, ret.lineno)
    return names, unresolved


def _discover_answer_models() -> tuple[dict[str, type[BaseModel]], list[str]]:
    """``(models, unresolved)`` over every module in ``src/`` (the UI aside)."""
    models: dict[str, type[BaseModel]] = {}
    unresolved: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "ui" in path.relative_to(SRC).parts:
            continue
        names, odd = _answer_model_names(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(REPO).as_posix()
        unresolved.extend(f"{rel} {where}" for where in odd)
        if not names:
            continue
        module_name = ".".join(path.relative_to(REPO).with_suffix("").parts)
        # Names a module under this repo's src/ only, found by walking it above.
        module = importlib.import_module(module_name)  # nosemgrep: non-literal-import
        for name in names:
            model = getattr(module, name, None)
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                unresolved.append(f"{rel}: {name} is not a module-level pydantic model")
                continue
            models[f"{model.__module__}.{model.__qualname__}"] = model
    return models, unresolved


ANSWER_MODELS, UNRESOLVED = _discover_answer_models()


@pytest.mark.passes_on_main
def test_every_answer_model_call_site_names_a_checkable_model() -> None:
    """A call site this file cannot resolve to a module-level model fails here,
    instead of being skipped: rewrite it to pass the model class by name."""
    assert UNRESOLVED == [], "answer models this test cannot check:\n" + "\n".join(UNRESOLVED)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        ("Agent(structured_output_model=Answer)", []),
        ("Agent(structured_output_model=self.output_model)", ["line 1: self.output_model"]),
        ("Agent(structured_output_model=pick())", ["line 1: pick()"]),
        ("Agent(structured_output_model=self._output_model())", []),
        ("def _output_model(self):\n    return Answer", []),
        ("def _output_model(self):\n    return MODELS[0]", ["line 2: MODELS[0]"]),
        ("def _output_model(self):\n    return", ["line 2: <bare return>"]),
        ('def _output_model(self):\n    """Abstract."""', []),
    ],
)
def test_discovery_reports_what_it_cannot_resolve(source: str, unresolved: list[str]) -> None:
    names, odd = _answer_model_names(ast.parse(source))
    assert odd == unresolved
    assert names == (["Answer"] if "Answer" in source else [])


@pytest.mark.passes_on_main
def test_discovery_finds_every_known_answer_model() -> None:
    found = {model.__name__ for model in ANSWER_MODELS.values()}
    assert EXPECTED_AT_LEAST <= found, f"not found: {sorted(EXPECTED_AT_LEAST - found)}"


@pytest.mark.parametrize("key", sorted(ANSWER_MODELS))
def test_bedrock_tool_schema_has_no_enum_bedrock_refuses(key: str) -> None:
    mismatches = enum_type_mismatches(_tool_schema(ANSWER_MODELS[key]))
    assert not mismatches, (
        f"{key}: Bedrock refuses this tool schema. Make each enum field required, "
        "or list None among its values (Literal[..., None]):\n" + "\n".join(mismatches)
    )


@pytest.mark.passes_on_main
@pytest.mark.parametrize("key", sorted(ANSWER_MODELS))
def test_raw_json_schema_has_no_enum_bedrock_refuses(key: str) -> None:
    """Some call sites send ``model_json_schema()`` to Converse unconverted (the
    reality-check consolidation validator): that form must pass the same check."""
    assert enum_type_mismatches(ANSWER_MODELS[key].model_json_schema()) == []


# --- the checker itself (guards of the test, so they pass on main too) --------


class _Item(BaseModel):
    value: str


class _DefaultedEnum(BaseModel):
    items: list[_Item]
    level: Literal["low", "high"] = "low"


class _OptionalEnum(BaseModel):
    level: Literal["low", "high"] | None = None


class _RequiredEnum(BaseModel):
    level: Literal["low", "high"]


class _NullInEnum(BaseModel):
    level: Literal["low", "high", None] = None


class _NestedDefaultedEnum(BaseModel):
    rows: list[_DefaultedEnum]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("model", [_DefaultedEnum, _OptionalEnum, _NestedDefaultedEnum])
def test_checker_flags_an_enum_the_conversion_made_nullable(model: type[BaseModel]) -> None:
    assert enum_type_mismatches(_tool_schema(model))


_VALID: list[type[BaseModel]] = [_RequiredEnum, _NullInEnum, _Item]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("model", _VALID)
def test_checker_passes_a_required_enum_or_one_that_lists_null(model: type[BaseModel]) -> None:
    assert enum_type_mismatches(_tool_schema(model)) == []


@pytest.mark.passes_on_main
def test_checker_does_not_take_a_boolean_for_a_number() -> None:
    assert enum_type_mismatches({"type": "integer", "enum": [1, True]})
    assert enum_type_mismatches({"type": "integer", "enum": [1, 2]}) == []


# --- the fixed fields keep their meaning ------------------------------------------


def _strategy(**extra: Any) -> dict[str, Any]:
    return {
        "parent_table": "orders",
        "child_table": "order_items",
        "strategy": "embed",
        "rationale": "read together",
        "trade_offs": "larger documents",
        **extra,
    }


def test_documentdb_advisor_strategy_must_state_its_risk_level() -> None:
    from src.tools.analysis.documentdb_analysis_tools import DenormalizationStrategy

    assert DenormalizationStrategy(**_strategy(risk_level="high")).risk_level == "high"
    with pytest.raises(ValidationError):
        DenormalizationStrategy(**_strategy())


def _index(**extra: Any) -> dict[str, Any]:
    return {"index_name": "idx_a", "keys": {"a": 1}, "purpose": "AP-1", **extra}


def test_documentdb_index_without_index_type_still_loads_as_compound() -> None:
    """A design saved before #1281 (or an answer that leaves it out) reads as before."""
    from src.contracts.documentdb_model_output import IndexDefinition

    assert IndexDefinition(**_index()).index_type == "compound"
    assert IndexDefinition(**_index(index_type=None)).index_type == "compound"
    assert IndexDefinition(**_index(index_type="text")).index_type == "text"
    with pytest.raises(ValidationError):
        IndexDefinition(**_index(index_type="hashed"))


@pytest.mark.passes_on_main
def test_aurora_index_method_stays_optional() -> None:
    from src.contracts.aurora_design_delta import IndexSpec

    assert IndexSpec(index_name="idx_a", columns=["a"]).method is None
    assert IndexSpec(index_name="idx_a", columns=["a"], method="gin").method == "gin"
    with pytest.raises(ValidationError):
        IndexSpec(index_name="idx_a", columns=["a"], method="rtree")  # type: ignore[arg-type]
