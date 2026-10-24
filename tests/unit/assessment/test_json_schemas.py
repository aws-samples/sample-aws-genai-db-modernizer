"""The checked-in JSON Schemas of the public ``assess()`` request and result (#1183).

They are rendered from the Pydantic models, so a committed file must equal a fresh
render; real results and the documented samples must validate against them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from scripts import render_contract_schemas as rcs
from src.assessment import CONTRACT_VERSION, AssessmentRequest, AssessmentResult
from tests.unit.assessment.conftest import REPO, assessed

DOC = REPO / "docs" / "contracts" / "assess.md"


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(rcs.schema_path(name).read_text(encoding="utf-8"))
    return loaded


def _validator(name: str) -> Draft202012Validator:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _doc_sample(heading: str) -> dict[str, Any]:
    """The JSON block right under ``### <heading>`` in docs/contracts/assess.md."""
    text = DOC.read_text(encoding="utf-8")
    match = re.search(rf"^### {re.escape(heading)}\n\n.*?```json\n(.*?)\n```", text, re.M | re.S)
    assert match, f"no JSON block under '### {heading}' in {DOC.name}"
    loaded: dict[str, Any] = json.loads(match.group(1))
    return loaded


# --------------------------------------------------------------------------- the files


def test_the_committed_schemas_are_current() -> None:
    assert rcs.main(["--check"]) == 0
    for path, text in rcs.render():
        assert path.read_text(encoding="utf-8") == text, path.name


def test_one_file_per_model_named_by_the_major_version() -> None:
    assert CONTRACT_VERSION == "1.0"
    assert [p.name for p, _ in rcs.render()] == [
        "assess_request.v1.schema.json",
        "assess_result.v1.schema.json",
    ]
    for name, title in (
        ("assess_request", "AssessmentRequest"),
        ("assess_result", "AssessmentResult"),
    ):
        schema = _schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["title"] == title
        assert schema["x-contract-version"] == CONTRACT_VERSION
        assert schema["additionalProperties"] is False


def test_check_fails_on_a_stale_or_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A model change without a regeneration fails ``--check`` (and so lint)."""
    monkeypatch.setattr(rcs, "SCHEMA_DIR", tmp_path)
    assert rcs.main(["--check"]) == 1
    assert "render_contract_schemas.py --write" in capsys.readouterr().out
    assert rcs.main(["--write"]) == 0
    assert rcs.main(["--check"]) == 0
    stale = tmp_path / "assess_result.v1.schema.json"
    stale.write_text(stale.read_text(encoding="utf-8").replace('"label"', '"lable"', 1))
    assert rcs.main(["--check"]) == 1
    assert "assess_result.v1.schema.json" in capsys.readouterr().out


def test_a_model_change_changes_the_render(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {p.name: text for p, text in rcs.render()}

    class AssessmentResult(rcs.AssessmentResult):  # a result model with one more field
        new_field: int = 0

    monkeypatch.setattr(rcs, "AssessmentResult", AssessmentResult)
    after = {p.name: text for p, text in rcs.render()}
    assert '"new_field"' in after["assess_result.v1.schema.json"]
    assert after["assess_result.v1.schema.json"] != before["assess_result.v1.schema.json"]
    assert after["assess_request.v1.schema.json"] == before["assess_request.v1.schema.json"]


def test_the_schema_validator_hook_accepts_them() -> None:
    """The pre-commit schema check (``scripts/validate_schemas.py``) passes both files."""
    from scripts.validate_schemas import validate_schema_file

    for path, _ in rcs.render():
        assert validate_schema_file(path) == (True, []), path.name


def test_an_action_is_required() -> None:
    with pytest.raises(SystemExit):
        rcs.main([])


# --------------------------------------------------------------------------- payloads


@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
@pytest.mark.parametrize("name", ["wordpress", "trailgear"])
def test_real_results_validate(name: str, run_through: str) -> None:
    result = assessed(name, run_through)
    _validator("assess_result").validate(json.loads(result.model_dump_json()))


def test_a_result_with_artifacts_validates() -> None:
    from src.assessment import assess
    from tests.unit.assessment.conftest import sample_collection

    result = assess(
        {
            "database_name": "wordpress",
            "collection": sample_collection("wordpress"),
            "options": {"run_through": "analysis", "include_artifacts": True},
        }
    )
    _validator("assess_result").validate(json.loads(result.model_dump_json()))


def test_the_documented_samples_validate() -> None:
    request = _doc_sample("Sample request")
    result = _doc_sample("Sample result")
    _validator("assess_request").validate(request)
    _validator("assess_result").validate(result)
    # And the models agree (the rules a schema does not state, such as the label).
    AssessmentRequest.model_validate(request)
    parsed = AssessmentResult.model_validate(result)
    assert parsed.contract_version == CONTRACT_VERSION
    assert parsed.label == "recommendation"


def test_the_request_schema_accepts_the_minimal_request() -> None:
    _validator("assess_request").validate({"database_name": "shop", "collection": {"x": 1}})


@pytest.mark.parametrize(
    ("change", "what"),
    [
        (lambda r: r.update(unknown=1), "an unknown top-level field"),
        (lambda r: r["summary"].update(unknown=1), "an unknown summary field"),
        (lambda r: r.update(label="data_model"), "a label outside the list"),
        (lambda r: r.update(contract_version="2.0"), "another contract version"),
        (lambda r: r["summary"]["engines"][0].update(role="primary"), "an unknown engine role"),
        (lambda r: r["statements"][0].update(reason_code="best"), "an unknown reason code"),
        (lambda r: r["statements"][0].update(query_type="select"), "a statement type off the list"),
        (
            lambda r: r["statements"][0]["matched_patterns"][0].pop("name"),
            "a pattern without a name",
        ),
        (lambda r: r["statements"][0].update(join_count=-1), "a negative join count"),
        (lambda r: r.pop("summary"), "no summary"),
    ],
)
def test_the_result_schema_refuses(change: Any, what: str) -> None:
    result = _doc_sample("Sample result")
    change(result)
    with pytest.raises(SchemaError):
        _validator("assess_result").validate(result)


@pytest.mark.parametrize(
    ("request_", "what"),
    [
        ({"collection": {"x": 1}}, "no database_name"),
        ({"database_name": "shop"}, "no collection"),
        ({"database_name": "../shop", "collection": {"x": 1}}, "a path in database_name"),
        ({"database_name": "-shop", "collection": {"x": 1}}, "a leading hyphen"),
        ({"database_name": "s" * 129, "collection": {"x": 1}}, "a name over 128 characters"),
        ({"database_name": "shop", "collection": {}, "colour": 1}, "an unknown field"),
        (
            {"database_name": "shop", "collection": {}, "options": {"run_through": "all"}},
            "an unknown run_through",
        ),
    ],
)
def test_the_request_schema_refuses(request_: dict[str, Any], what: str) -> None:
    with pytest.raises(SchemaError):
        _validator("assess_request").validate(request_)


# --------------------------------------------------------------------------- required fields


def test_every_always_written_result_field_is_required() -> None:
    """``model_dump_json`` writes every field, so the result schema requires each one
    (generated types are not all-optional); the request keeps its defaults optional."""
    from src.assessment.models import StatementResult

    result = _schema("assess_result")
    statement = result["$defs"]["StatementResult"]
    assert len(StatementResult.model_fields) == 18
    assert set(statement["required"]) == set(StatementResult.model_fields)
    for name, model in result["$defs"].items():
        assert set(model.get("required", [])) == set(model["properties"]), name
    assert set(result["required"]) == set(AssessmentResult.model_fields)
    request = _schema("assess_request")
    assert request["required"] == ["database_name", "collection"]
    assert all("required" not in m for m in request["$defs"].values())


def test_a_result_missing_an_always_written_field_is_refused() -> None:
    result = _doc_sample("Sample result")
    del result["statements"][0]["tied_engines"]
    with pytest.raises(SchemaError, match="tied_engines"):
        _validator("assess_result").validate(result)


def test_each_id_is_the_files_published_url() -> None:
    for path, _ in rcs.render():
        assert _schema(path.name.split(".")[0])["$id"] == (
            "https://raw.githubusercontent.com/aws-samples/sample-aws-genai-db-modernizer/"
            f"main/docs/contracts/schemas/{path.name}"
        )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda s: s.pop("description"), "description"),
        (lambda s: s.update({"$schema": "http://json-schema.org/draft-04/schema#"}), "Draft 07"),
        (lambda s: s.pop("x-contract-version"), "x-contract-version"),
    ],
)
def test_the_schema_validator_keeps_its_bar(tmp_path: Path, change: Any, error: str) -> None:
    from scripts.validate_schemas import validate_schema_file

    schema = _schema("assess_result")
    change(schema)
    path = tmp_path / "schemas" / "x.json"
    path.parent.mkdir()
    path.write_text(json.dumps(schema), encoding="utf-8")
    ok, errors = validate_schema_file(path)
    assert not ok and any(error in e for e in errors), errors


@pytest.mark.parametrize(
    "where",
    ["result_docstring", "package_docstring", "schema", "doc"],
)
def test_the_determinism_limits_are_stated(where: str) -> None:
    """The result's docstring, its schema description and the library docs state what
    is identical run to run and what is not (#1259)."""
    import src.assessment as package

    if where == "result_docstring":
        text = " ".join((AssessmentResult.__doc__ or "").split())
    elif where == "package_docstring":
        text = " ".join((package.__doc__ or "").split())
    elif where == "schema":
        text = " ".join(_schema("assess_result")["description"].split())
    else:
        text = " ".join(DOC.read_text(encoding="utf-8").split())
    for phrase in ("PYTHONHASHSEED", "not guaranteed", "model_flag", "capability_flag"):
        assert phrase in text, (where, phrase)
    assert "analysis" in text and "reality_check" in text
    assert "new run" in text
