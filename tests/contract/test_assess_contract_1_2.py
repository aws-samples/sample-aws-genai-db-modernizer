"""Contract 1.2 of the public ``assess()`` result (#1603), as a library caller reads it.

- ``statements[].fit_breakdown`` explains every score in ``statements[].fit``: recomputed
  here from its own fields with the rule docs/contracts/assess.md states, it gives the
  same number, for every statement and engine of the three samples, before and after
  placement; a gated engine has no score;
- the ``query_id`` rule per source engine: a row's ``digest`` (SQL Server's
  ``query_hash`` as the collection script writes it), else the first 16 hex characters
  of SHA-256 of the text, and rows sharing a digest are one statement, whose
  ``query_text`` is the costliest variant's;
- results written for contract 1.0 and 1.1 still validate against the published schema,
  load, and write back unchanged; the 1.2 rules do not apply to them.
"""

from __future__ import annotations

import copy
import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from scripts import render_contract_schemas as rcs
from src.assessment import AssessmentResult, assess, load_collection
from src.assessment.models import ADDED_IN_1_2

REPO = Path(__file__).resolve().parents[2]
SAMPLES = ("wordpress", "discourse", "trailgear")
FIXTURES = REPO / "tests" / "fixtures"

#: Results written by earlier versions' code: 1.0 (#1513's fixture) and 1.1 (main's code
#: before #1603, the wordpress sample with its statements cut to three).
EARLIER_RESULTS = {
    "1.0": FIXTURES / "assess_result_v1_0_opensearch_without_entry.json",
    "1.1": FIXTURES / "assess_result_v1_1_reality_check.json",
    "1.1-designed": FIXTURES / "assess_result_v1_1_designed.json",
}


@cache
def _result_json(sample: str, run_through: str) -> str:
    collection = load_collection(REPO / "docs" / "examples" / sample / f"{sample}.zip")
    result = assess(
        {"database_name": sample, "collection": collection, "options": {"run_through": run_through}}
    )
    return result.model_dump_json()


def _result(sample: str, run_through: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_result_json(sample, run_through))
    return loaded


@cache
def _schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(rcs.schema_path("assess_result").read_text("utf-8"))
    return loaded


# --------------------------------------------------------------------------- fit_breakdown


def recomputed_fit(entry: dict[str, Any]) -> int:
    """``fit`` from a breakdown's own fields, by the documented rule (assess.md, "The fit
    breakdown"): each table's score, the base as their mean with the fraction dropped,
    then the base less the largest penalty, never below 0."""
    if entry["method"] == "per_table_weighted_mean":
        scores = []
        for table in entry["tables"]:
            w = table["weights"]
            score = int(
                entry["pattern"]["score"] * w["pattern"]
                + table["complexity"] * w["complexity"]
                + table["performance"] * w["performance"]
                + entry["cost"]["score"] * w["cost"]
            )
            assert score == table["score"], table
            scores.append(score)
    else:
        scores = [table["score"] for table in entry["tables"]]
    base = int(sum(scores) / len(scores)) if scores else 0
    assert base == entry["base"], entry
    return max(0, base - max((p["points"] for p in entry["penalties"]), default=0))


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
@pytest.mark.parametrize("sample", SAMPLES)
def test_the_fit_breakdown_recomputes_to_fit(sample: str, run_through: str) -> None:
    result = _result(sample, run_through)
    checked = 0
    for statement in result["statements"]:
        fit, breakdown = statement["fit"], statement["fit_breakdown"]
        assert set(fit) <= set(breakdown), statement["query_id"]
        for engine, entry in breakdown.items():
            where = (statement["query_id"], engine)
            if entry["gate"] is not None:
                assert engine not in fit and engine in statement["excluded"], where
                assert entry["gate"]["kind"] and entry["gate"]["reason"], where
                assert entry["fit"] is entry["base"] is entry["method"] is None, where
                continue
            assert entry["fit"] == fit[engine] == recomputed_fit(entry), where
            checked += 1
    assert checked > len(result["statements"])


def test_the_samples_reach_every_method_and_both_gate_kinds() -> None:
    """The recomputation above meets every rule the scorer has, and its gates."""
    from src.agents.referee.query_scoring import METHODS

    methods: set[str] = set()
    gates: set[str] = set()
    penalties = {"intrinsic": 0, "fixable": 0}
    for sample in SAMPLES:
        for statement in _result(sample, "analysis")["statements"]:
            for entry in statement["fit_breakdown"].values():
                if entry["gate"]:
                    gates.add(entry["gate"]["kind"])
                    continue
                methods.add(entry["method"])
                for p in entry["penalties"]:
                    penalties["intrinsic" if p["intrinsic"] else "fixable"] += 1
                    assert p["points"] > 0 if p["intrinsic"] else p["points"] == 0, p
    assert methods == set(METHODS)
    assert {"capability", "hard_exclusion"} <= gates
    assert penalties["intrinsic"] and penalties["fixable"]


def test_a_breakdown_that_disagrees_with_fit_is_refused_from_1_2_on() -> None:
    document = _result("wordpress", "reality_check")
    statement = next(s for s in document["statements"] if s["fit"])
    engine = sorted(statement["fit"])[0]
    statement["fit_breakdown"][engine]["fit"] = (statement["fit"][engine] + 1) % 101
    with pytest.raises(ValidationError, match="fit_breakdown"):
        AssessmentResult.model_validate(document)
    del statement["fit_breakdown"][engine]
    with pytest.raises(ValidationError, match="missing"):
        AssessmentResult.model_validate(document)
    # The same document marked 1.1 is read as 1.1 read it: no breakdown rule.
    document["contract_version"] = "1.1"
    AssessmentResult.model_validate(document)


# --------------------------------------------------------------------------- query_id


def _digest_rows() -> list[dict[str, Any]]:
    """Three statements: one alone, one in two literal variants sharing a digest (the
    second costlier), and one with no digest."""
    return [
        {"query_text": "SELECT * FROM orders WHERE id = 1", "total_time_ms": 10.0},
        {"query_text": "SELECT * FROM customers WHERE id = 1", "total_time_ms": 5.0},
        {"query_text": "SELECT * FROM customers WHERE id = 2", "total_time_ms": 50.0},
        {"query_text": "SELECT * FROM products WHERE sku = 'a'", "total_time_ms": 1.0},
    ]


def _collection(sample: str, metadata: dict[str, Any], digests: list[str | None]) -> dict:
    collection = load_collection(REPO / "docs" / "examples" / sample / f"{sample}.zip")
    collection["metadata"] = {**collection["metadata"], **metadata}
    rows = []
    for row, digest in zip(_digest_rows(), digests, strict=True):
        row = {**row, "execution_count": 100, "avg_time_ms": 1.0}
        if digest is not None:
            row["digest"] = digest
        rows.append(row)
    collection["queries"] = rows
    return collection


#: Per source engine: the sample whose schema the collection borrows, the metadata that
#: names the engine, and the digest each row carries as that engine's collection script
#: writes it (MySQL and MariaDB: ``DIGEST``; PostgreSQL: ``queryid``; SQL Server:
#: ``query_hash`` as hex; Oracle: ``fms_<FORCE_MATCHING_SIGNATURE>`` or the ``SQL_ID``).
ENGINES: dict[str, tuple[str, dict[str, Any], list[str | None]]] = {
    "mysql": ("wordpress", {"version": "8.0.36"}, ["a" * 64, "b" * 64, "b" * 64, None]),
    "mariadb": (
        "wordpress",
        {"version": "10.11.6-MariaDB"},
        ["a1" * 16, "b2" * 16, "b2" * 16, None],
    ),
    "postgresql": (
        "discourse",
        {},
        ["-3141592653589793238", "2718281828459045235", "2718281828459045235", None],
    ),
    "sqlserver": (
        "trailgear",
        {"engine": "sqlserver"},
        ["0x1A2B3C4D5E6F7081", "0x0F1E2D3C4B5A6978", "0x0F1E2D3C4B5A6978", None],
    ),
    "oracle": (
        "trailgear",
        {"engine": "oracle", "version": "Oracle Database 19c"},
        ["fms_1234567890123", "fms_9876543210987", "fms_9876543210987", None],
    ),
}


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_query_id_is_the_digest_else_a_text_hash(engine: str) -> None:
    sample, metadata, digests = ENGINES[engine]
    result = assess(
        {
            "database_name": sample,
            "collection": _collection(sample, metadata, digests),
            "options": {"run_through": "analysis"},
        }
    )
    assert result.summary.source_engine == engine
    text = _digest_rows()[3]["query_text"]
    by_id = {s.query_id: s for s in result.statements}
    alone, merged = str(digests[0]), str(digests[1])
    # One statement per digest, in order of first appearance; a row with none is named
    # by the first 16 hex characters of SHA-256 of its text.
    assert list(by_id) == [alone, merged, hashlib.sha256(text.encode()).hexdigest()[:16]]
    # Rows sharing a digest merge; the text kept is the costliest variant's.
    assert by_id[merged].query_text == "SELECT * FROM customers WHERE id = 2"
    assert by_id[alone].query_text == "SELECT * FROM orders WHERE id = 1"
    assert list(by_id.values())[-1].query_text == text


def test_oracle_literal_variants_merge_under_the_costliest_digest() -> None:
    """Oracle only: rows whose texts differ only in literals merge even with different
    digests (a collection without ``FORCE_MATCHING_SIGNATURE``), named by the costliest
    variant's digest."""
    sample, metadata, _ = ENGINES["oracle"]
    collection = _collection(sample, metadata, ["sqlid_a", "sqlid_b", "sqlid_c", "sqlid_d"])
    result = assess(
        {"database_name": sample, "collection": collection, "options": {"run_through": "analysis"}}
    )
    by_id = {s.query_id: s for s in result.statements}
    assert list(by_id) == ["sqlid_a", "sqlid_c", "sqlid_d"]
    assert by_id["sqlid_c"].query_text == "SELECT * FROM customers WHERE id = 2"


# --------------------------------------------------------------------------- earlier versions


@pytest.mark.parametrize("version", sorted(EARLIER_RESULTS))
def test_an_earlier_result_validates_loads_and_writes_back_unchanged(version: str) -> None:
    text = EARLIER_RESULTS[version].read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["contract_version"] == version.split("-")[0]
    Draft202012Validator(_schema()).validate(document)
    loaded = AssessmentResult.model_validate_json(text)
    assert json.loads(loaded.model_dump_json()) == document
    # None of the 1.2 fields is written back for it, not even empty.
    dumped = json.loads(loaded.model_dump_json())
    for statement in dumped["statements"]:
        assert not set(ADDED_IN_1_2["StatementResult"]) & set(statement)
    assert not set(ADDED_IN_1_2["Summary"]) & set(dumped["summary"])


def test_a_1_2_result_validates_against_the_schema_that_reads_1_1() -> None:
    """One schema file reads every 1.x: the 1.2 fields are optional in it, so a 1.1
    result validates, and a 1.2 result with every field validates too."""
    document = _result("trailgear", "reality_check")
    assert document["contract_version"] == "1.2"
    Draft202012Validator(_schema()).validate(document)
    older = copy.deepcopy(document)
    older["contract_version"] = "1.1"
    loaded = AssessmentResult.model_validate(older)
    assert "fit_breakdown" not in json.loads(loaded.model_dump_json())["statements"][0]


def test_the_documented_sample_breakdown_recomputes() -> None:
    """The sample result in docs/contracts/assess.md is a valid 1.2 result whose fit
    breakdowns recompute to its fit by the documented rule."""
    import re

    text = (REPO / "docs" / "contracts" / "assess.md").read_text(encoding="utf-8")
    match = re.search(r"^### Sample result\n\n```json\n(.*?)\n```", text, re.M | re.S)
    assert match
    document = json.loads(match.group(1))
    assert document["contract_version"] == "1.2"
    AssessmentResult.model_validate(document)
    for statement in document["statements"]:
        assert statement["fit_breakdown"]
        for engine, entry in statement["fit_breakdown"].items():
            assert recomputed_fit(entry) == statement["fit"][engine] == entry["fit"]


# --------------------------------------------------------------------------- the request's version

#: The published result schema of each earlier version, as main shipped it: 1.1 before
#: #1603, 1.0 before #1513 (``git show <commit>:docs/contracts/schemas/...``).
EARLIER_SCHEMAS = {
    "1.0": FIXTURES / "assess_result_v1_0.schema.json",
    "1.1": FIXTURES / "assess_result_v1_1.schema.json",
}


def _pinned(
    version: str | None, run_through: str, engines: list[str] | None = None, db: str = "wordpress"
) -> str:
    from tests.assess_models import models_patched

    request: dict[str, Any] = {
        "database_name": db,
        "collection": load_collection(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip"),
        "options": {
            "run_through": run_through,
            "deterministic": run_through == "reality_check",
            "target_engines": engines,
        },
    }
    if version is not None:
        request["contract_version"] = version
    with models_patched():
        return assess(request).model_dump_json()


def _has_a_1_2_field(document: dict[str, Any]) -> bool:
    summary = document["summary"]
    rows = [
        (summary, "Summary"),
        *((e, "EngineLine") for e in summary["engines"]),
        *((c, "Consolidation") for c in summary["consolidations"]),
        *((s, "StatementResult") for s in document["statements"]),
        *(
            (t, "DynamoDBTable")
            for d in document["designs"] or []
            if d["engine"] == "dynamodb"
            for t in d["tables"]
        ),
    ]
    return any(set(ADDED_IN_1_2[model]) & set(row) for row, model in rows)


@pytest.mark.parametrize(
    ("version", "run_through", "engines"),
    [
        ("1.1", "reality_check", None),
        ("1.1", "schema_design", None),
        ("1.1", "schema_design", ["dynamodb", "opensearch"]),
        ("1.0", "reality_check", None),
        ("1.0", "schema_design", None),
        ("1.0", "schema_design", ["dynamodb", "opensearch"]),
    ],
)
def test_a_request_pinned_to_an_earlier_version_gets_that_versions_result(
    version: str, run_through: str, engines: list[str] | None
) -> None:
    """The result follows the request's contract_version (maintainer decision on #1603):
    a 1.1 (or 1.0) request's result is written as that version wrote it, with no 1.2
    field, and validates against the schema file that version shipped."""
    text = _pinned(version, run_through, engines)
    document = json.loads(text)
    assert document["contract_version"] == version
    assert not _has_a_1_2_field(document)
    schema = json.loads(EARLIER_SCHEMAS[version].read_text(encoding="utf-8"))
    assert schema["x-contract-version"] == version
    Draft202012Validator(schema).validate(document)
    # It reads back as the same document.
    assert AssessmentResult.model_validate_json(text).model_dump_json() == text
    if run_through == "schema_design":
        engines_designed = [d["engine"] for d in document["designs"]]
        assert ("opensearch" in engines_designed) == (version == "1.1" and engines is not None)


@pytest.mark.parametrize("version", ["1.2", None])
def test_a_1_2_request_or_one_with_no_version_gets_the_latest(version: str | None) -> None:
    from src.assessment import CONTRACT_VERSION

    document = json.loads(_pinned(version, "reality_check"))
    assert document["contract_version"] == CONTRACT_VERSION == "1.2"
    assert _has_a_1_2_field(document)
    assert all("fit_breakdown" in s for s in document["statements"])
    Draft202012Validator(_schema()).validate(document)


def test_an_engine_with_no_score_has_only_its_gate() -> None:
    """From 1.2 on, an entry for an engine with no `fit` holds its gate and nothing else:
    an ungated, unscored entry, or a gate with a score, is refused."""
    document = _result("trailgear", "reality_check")
    statement = next(
        s for s in document["statements"] if any(e["gate"] for e in s["fit_breakdown"].values())
    )
    gated = next(e for e, entry in statement["fit_breakdown"].items() if entry["gate"])
    AssessmentResult.model_validate(document)
    for change in (
        lambda entry: entry.update(gate=None),  # the reviewer's counterexample
        lambda entry: entry.update(fit=0),
        lambda entry: entry.update(base=0, method="per_table_weighted_mean"),
    ):
        changed = copy.deepcopy(document)
        target = next(s for s in changed["statements"] if s["query_id"] == statement["query_id"])
        change(target["fit_breakdown"][gated])
        with pytest.raises(ValidationError, match="fit_breakdown"):
            AssessmentResult.model_validate(changed)
    # An entry for an engine the scorer never saw is refused the same way.
    changed = copy.deepcopy(document)
    target = next(s for s in changed["statements"] if s["query_id"] == statement["query_id"])
    target["fit_breakdown"]["no_such_engine"] = {"gate": None}
    with pytest.raises(ValidationError, match="must be a gate"):
        AssessmentResult.model_validate(changed)
