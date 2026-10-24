"""Every engine-named field the customer-text guard reads is declared (#1409).

A field named ``engine``, ``*_engine``, ``engines``, ``*_engines``, ``owner``,
``cached_by`` or ``also_read_by`` holds a raw engine id. ``scan_json`` flags a raw id
in any field not declared as an id field, so an undeclared one used to surface only
in an e2e shard or a real-model run, after a push (#1399's
``facts.cache_layer.engine``). This walks the contracts ``scan_json`` reads
(report.json, assignment.json, the reality-check output) and a real-model
report.json, with no pipeline run.
"""

from __future__ import annotations

import json
import types
import typing
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from src.contracts.assignment_models import Assignment
from src.contracts.reality_check_output import RealityCheckOutputContract
from src.contracts.synthesis_output import SynthesisOutputContract
from src.tools.validation import customer_text_guard as guard

SCANNED_CONTRACTS: tuple[type[BaseModel], ...] = (
    SynthesisOutputContract,
    Assignment,
    RealityCheckOutputContract,
)

# The saved real-model run (ci/llm/replay.py): its report.json carries the extra
# fields (``extra="allow"``) no model declares.
REAL_RUN = Path(__file__).resolve().parents[3] / "fixtures" / "real_runs" / "wordpress-both"

# A nested model reached through a dict is keyed by an id (a table, an engine): its
# fields' parent key is that id, so only a bare ID_FIELDS name can declare them.
_DICT_KEY = "<dict key>"


def _holds_strings(annotation: Any) -> bool:
    """Whether a value of this type can hold a string that ``scan_json`` reads."""
    if annotation is str:
        return True
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Literal:
        return any(isinstance(a, str) for a in args)
    if origin is dict:
        return len(args) == 2 and _holds_strings(args[1])
    if origin in (list, tuple, set, frozenset, typing.Union, types.UnionType):
        return any(_holds_strings(a) for a in args if a is not Ellipsis)
    if origin is typing.Annotated:
        return _holds_strings(args[0])
    return False


def _nested_models(annotation: Any, through_dict: bool = False) -> list[tuple[type, bool]]:
    """Each model a value of this type can hold, and whether it sits under a dict."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(annotation, through_dict)]
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is dict:
        return _nested_models(args[1], True) if len(args) == 2 else []
    if origin is typing.Annotated:
        return _nested_models(args[0], through_dict)
    if origin in (list, tuple, set, frozenset, typing.Union, types.UnionType):
        return [m for a in args if a is not Ellipsis for m in _nested_models(a, through_dict)]
    return []


def undeclared_model_fields(root: type[BaseModel]) -> list[str]:
    """The schema path of every engine-named string field under ``root`` that
    ``scan_json`` would scan as prose: not under a declared id field, not an id field
    itself, not a declared display field. Paths collapse list indexes; ``{key}``
    marks a dict level."""
    out: list[str] = []
    seen: set[tuple[type, str | None]] = set()

    def walk(model: type[BaseModel], parent: str | None, path: str) -> None:
        if (model, parent) in seen:
            return
        seen.add((model, parent))
        for name, info in model.model_fields.items():
            key = info.serialization_alias or info.alias or name
            if guard.is_id_field(None if parent == _DICT_KEY else parent, key):
                continue
            if (
                guard.ENGINE_FIELD_NAME_RE.match(key)
                and (parent, key) not in guard.DISPLAY_NAME_FIELD_PATHS
                and _holds_strings(info.annotation)
            ):
                out.append(f"{path}.{key}")
            for nested, through_dict in _nested_models(info.annotation):
                if through_dict:
                    walk(nested, _DICT_KEY, f"{path}.{key}{{key}}")
                else:
                    walk(nested, key, f"{path}.{key}")

    walk(root, None, "$")
    return sorted(set(out))


@pytest.mark.parametrize("contract", SCANNED_CONTRACTS, ids=lambda c: c.__name__)
def test_every_engine_field_of_a_scanned_contract_is_declared(contract: type[BaseModel]) -> None:
    undeclared = undeclared_model_fields(contract)
    assert undeclared == [], (
        "engine-named fields scan_json would read as prose: declare each in "
        "customer_text_guard.ID_FIELD_PATHS (an id, rendered through display_engine) "
        "or DISPLAY_NAME_FIELD_PATHS (a display name)",
        undeclared,
    )


def test_the_walk_reaches_the_fields_it_guards() -> None:
    """The walk sees nested, optional and list fields, so an empty answer above means
    every one is declared, not that the walk stopped early."""
    seen: list[str] = []
    real_is_id_field = guard.is_id_field

    def record(parent: str | None, key: str) -> bool:
        if guard.ENGINE_FIELD_NAME_RE.match(key):
            seen.append(f"{parent}.{key}")
        return real_is_id_field(parent, key)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(guard, "is_id_field", record)
        for contract in SCANNED_CONTRACTS:
            undeclared_model_fields(contract)
    for expected in (
        "cache_layer.engine",  # #1399
        "table_mappings.owner",
        "table_mappings.cached_by",
        "table_mappings.also_read_by",
        "table_lines.owner",
        "sweep_decisions.engine",
        "price_list.undated_engines",
        "unservable.no_engine_in_scope_closest_engines",
        # Engine ids under names without "engine" in them.
        "removed_engines.moved_to",
        "ranking.target",
        "cost_breakdown.database",
        "considered_not_recommended.absorbed_by",
    ):
        assert expected in seen, expected


def test_an_undeclared_engine_field_is_reported() -> None:
    class Leaf(BaseModel):
        engine: str
        target_engine: str | None = None
        owner: list[str] = []
        with_removed_engine: bool = False  # not a string: never scanned
        engine_count: int = 0  # does not end in _engine

    class Root(BaseModel):
        new_block: list[Leaf] = []
        cache_layer: Leaf | None = None  # ("cache_layer", "engine") is declared
        engines: list[Leaf] = []  # a bare ID_FIELDS name: its whole value is skipped
        by_table: dict[str, Leaf] = {}

    assert undeclared_model_fields(Root) == [
        "$.by_table{key}.engine",
        "$.by_table{key}.owner",
        "$.by_table{key}.target_engine",
        "$.cache_layer.owner",
        "$.cache_layer.target_engine",
        "$.new_block.engine",
        "$.new_block.owner",
        "$.new_block.target_engine",
    ]


def test_a_display_field_is_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    class Row(BaseModel):
        engine: str

    class Holder(BaseModel):
        new_block: list[Row] = []

    assert undeclared_model_fields(Holder) == ["$.new_block.engine"]
    monkeypatch.setattr(guard, "DISPLAY_NAME_FIELD_PATHS", frozenset({("new_block", "engine")}))
    assert undeclared_model_fields(Holder) == []


# ---------------------------------------------------------------------------
# undeclared_engine_fields: the same rule over a parsed document (extra fields)
# ---------------------------------------------------------------------------


def test_an_extra_field_in_a_document_is_reported() -> None:
    report = {
        "table_mappings": [
            {"owner": "dynamodb", "cached_by": None, "new_engine": "dynamodb", "notes": "x"}
        ],
        "engines": [{"engine": "dynamodb"}],  # under a declared id field
        "summary": {"engine": "dynamodb", "replaced_engines": ["aurora_mysql"]},
        "new_plan": [{"target": "opensearch", "source_of_truth": "dynamodb"}],
        "counts": {"engine": 3, "replica_engines_count": 1},  # not strings
        "empty": {"engines_listed": []},
    }
    assert guard.undeclared_engine_fields(report) == [
        "$.table_mappings[0].new_engine",
        "$.summary.engine",
        "$.summary.replaced_engines",
        "$.new_plan[0].target",
    ]


def test_a_document_display_field_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"block": [{"engine": "Amazon DynamoDB"}]}
    assert guard.undeclared_engine_fields(doc) == ["$.block[0].engine"]
    monkeypatch.setattr(guard, "DISPLAY_NAME_FIELD_PATHS", frozenset({("block", "engine")}))
    assert guard.undeclared_engine_fields(doc) == []


def test_the_newly_declared_fields_are_skipped_by_scan_json() -> None:
    sweep = {"engine": "opensearch", "reason": "Amazon OpenSearch kept"}
    doc: dict[str, Any] = {
        "sweep_decisions": [sweep],
        "facts": {
            "price_list": {"undated_engines": ["documentdb"]},
            "unservable": {"no_engine_in_scope_closest_engines": ["dynamodb"]},
        },
    }
    assert guard.scan_json(doc) == []
    assert guard.undeclared_engine_fields(doc) == []
    sweep["reason"] = "opensearch kept"
    assert [f.path for f in guard.scan_json(doc)] == ["$.sweep_decisions[0].reason"]


def test_the_real_model_report_json_declares_every_engine_field() -> None:
    """The saved real-model report.json carries extra fields no model declares
    (``table_mappings`` rows, ``schema_designs``); each engine-named one is declared."""
    report = json.loads((REAL_RUN / "report.json").read_text())
    assert report["facts"]["cache_layer"]["engine"]  # #1399's field, present
    assert guard.undeclared_engine_fields(report) == []
    assert guard.scan_json(report) == []
