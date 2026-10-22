"""Readers check the stored ``contract_version`` (ADR-008 Decision 4, #971).

A reader accepts any MINOR of the MAJOR its model declares and refuses a
different MAJOR with an error naming the artifact, the stored version and the
expected one. A file with no version (written before #972) is accepted as
legacy. A model loaded through the helper says the current version, so a
load-then-write-back (``.meta`` progression, an approved assignment) writes
today's version, not the stored one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.phase_models import PhaseProgression
from src.shared.contract_versioning import (
    ContractVersionError,
    carries_contract,
    check_artifact,
    check_contract_version,
    contract_for_artifact,
    current_version,
    load_contract,
)
from src.storage.local_store import LocalArtifactStore


class _Example(BaseModel):
    contract_version: str = Field(default="2.3", pattern=r"^\d+\.\d+$")
    name: str
    added_in_2_3: str = "default"


# --------------------------------------------------------------------- helper


def test_the_same_major_at_a_higher_minor_is_accepted() -> None:
    loaded = load_contract(_Example, {"contract_version": "2.9", "name": "x"}, artifact="a.json")
    assert loaded.name == "x"


def test_the_same_major_at_a_lower_minor_is_accepted() -> None:
    assert check_contract_version({"contract_version": "2.0"}, _Example, artifact="a") == "2.0"


@pytest.mark.parametrize("stored", ["3.0", "1.9", "10.3"])
def test_a_different_major_is_refused_naming_artifact_and_both_versions(stored: str) -> None:
    with pytest.raises(ContractVersionError) as err:
        load_contract(
            _Example, {"contract_version": stored, "name": "x"}, artifact="db/job/thing.json"
        )
    message = str(err.value)
    assert "db/job/thing.json" in message
    assert repr(stored) in message
    assert "'2.3'" in message
    assert "_Example" in message
    assert err.value.stored == stored
    assert err.value.expected == "2.3"
    assert err.value.artifact == "db/job/thing.json"


def test_a_major_that_only_shares_a_prefix_is_refused() -> None:
    """``"20.1"`` is not MAJOR 2: the comparison is numeric, not a string prefix."""
    with pytest.raises(ContractVersionError):
        check_contract_version({"contract_version": "20.1"}, _Example, artifact="a")


@pytest.mark.parametrize("stored", ["2", "2.3.1", "v2.3", "", 2.3, None, ["2.3"]])
def test_a_version_that_is_not_major_minor_is_refused(stored: object) -> None:
    payload: dict[str, object] = {"name": "x"}
    payload["contract_version"] = stored
    with pytest.raises(ContractVersionError, match="MAJOR.MINOR"):
        check_contract_version(payload, _Example, artifact="a")


def test_a_missing_version_is_accepted_as_legacy_with_a_debug_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="src.shared.contract_versioning")
    assert check_contract_version({"name": "x"}, _Example, artifact="old.json") is None
    assert any(
        r.levelno == logging.DEBUG and "old.json" in r.getMessage() and "legacy" in r.getMessage()
        for r in caplog.records
    )
    assert load_contract(_Example, {"name": "x"}, artifact="old.json").contract_version == "2.3"


def test_the_loaded_model_says_the_current_version() -> None:
    loaded = load_contract(_Example, {"contract_version": "2.0", "name": "x"}, artifact="a")
    assert loaded.contract_version == "2.3"
    assert loaded.model_dump()["contract_version"] == "2.3"


def test_load_contract_does_not_change_the_callers_payload() -> None:
    payload = {"contract_version": "2.0", "name": "x"}
    load_contract(_Example, payload, artifact="a")
    assert payload == {"contract_version": "2.0", "name": "x"}


def test_a_shape_error_is_still_a_validation_error() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        load_contract(_Example, {"contract_version": "2.3"}, artifact="a")


def test_a_model_without_a_version_default_is_a_programming_error() -> None:
    class NoDefault(BaseModel):
        contract_version: str

    class NoField(BaseModel):
        name: str

    for model in (NoDefault, NoField):
        with pytest.raises(TypeError, match="contract_version"):
            current_version(model)


def test_carries_contract_tells_a_contract_from_a_raw_collection() -> None:
    assert carries_contract({"contract_version": "3.0"}, CollectorOutputContract, artifact="f")
    assert not carries_contract(
        {"collection_version": "1.0"}, CollectorOutputContract, artifact="f"
    )
    with pytest.raises(ContractVersionError):
        carries_contract({"contract_version": "4.0"}, CollectorOutputContract, artifact="f")


# ------------------------------------------------------------------- registry


@pytest.mark.parametrize(
    ("path", "model"),
    [
        ("db/job/collector/output.json", "CollectorOutputContract"),
        ("db/job/uploads/collector-output.json", "CollectorOutputContract"),
        ("db/job/referee-triage/triage.json", "TriageOutputContract"),
        ("db/job/analysis-dynamodb/analysis.json", "AnalysisOutputContract"),
        ("db/job/assignment/v3/assignment.json", "Assignment"),
        ("db/job/assignment/v3/validation.json", "ValidationResult"),
        ("db/job/reality-check/output.json", "RealityCheckOutputContract"),
        ("db/job/synthesis/v2/report.json", "SynthesisOutputContract"),
        ("db/job/deliverables/assessment-data.json", "SynthesisOutputContract"),
        ("db/job/schema-dynamodb/v1/schema_output.json", "DynamoDBModelOutputContract"),
        ("db/job/schema-dynamodb/schema_output.json", "DynamoDBModelOutputContract"),
        ("db/job/referee-synthesis/report.json", "SynthesisOutputContract"),
        ("db/job/schema-aurora_mysql/v2/schema_output.json", "AuroraMySQLModelOutputContract"),
        ("db/job/schema-opensearch/v1/groups_manifest.json", "SchemaDesignGroupsManifest"),
        ("db/job/schema-opensearch/v2/version_meta.json", "SchemaVersionMeta"),
        ("db/job/schema-opensearch/v2/revision_request.json", "SchemaRevisionRequest"),
        ("db/job/schema-opensearch/confirmed.json", "SchemaConfirmation"),
        ("db/job/schema-dynamodb/questions.json", "AgentQuestions"),
        ("db/job/schema-dynamodb/answers.json", "AgentAnswers"),
        (".meta/job-1.json", "PhaseProgression"),
        ("db/job/post-schema-router/router_output.json", "RouterOutput"),
        ("db/job/post-schema-router/pass-2/router_output.json", "RouterOutput"),
        ("db/job/load-test-dynamodb/v1/infrastructure.json", "InfrastructureManifest"),
        ("db/job/load-test-dynamodb/v1/seed-manifest.json", "SeedManifest"),
        ("db/job/load-test-dynamodb/v1/results/summary.json", "LoadTestOutput"),
        ("db/job/load-test-dynamodb/v1/results/q-7.json", "PatternResult"),
        ("db/job/analysis-dynamodb/decision-trace.json", "DynamoDBDecisionTrace"),
        ("db/job/analysis-documentdb/decision-trace.json", "DocumentDBDecisionTrace"),
        ("db/job/analysis-opensearch/decision-trace.json", "OpenSearchDecisionTrace"),
        ("db/job/analysis-aurora_postgresql/decision-trace.json", "AuroraDecisionTrace"),
        ("db/job/analysis-aurora_mysql/decision-trace.json", "AuroraDecisionTrace"),
    ],
)
def test_each_contract_file_maps_to_its_model(path: str, model: str) -> None:
    found = contract_for_artifact(path)
    assert found is not None and found.__name__ == model


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "path",
    [
        "db/job/collector/sql_shadow.json",
        "db/job/assignment/v1/access_patterns.json",
        "db/job/schema-unknown_engine/v1/schema_output.json",
        "db/job/llm_responses/schema_design_dynamodb.json",
        "db/job/schema-dynamodb/v1/schema_draft_group_2.json",
        ".meta/_stored-input-redaction-v1.json",
        "artifact://abc123",
    ],
)
def test_other_files_have_no_contract(path: str) -> None:
    assert contract_for_artifact(path) is None


def test_check_artifact_refuses_a_different_major_for_a_registered_file() -> None:
    with pytest.raises(ContractVersionError, match="analysis-dynamodb/analysis.json"):
        check_artifact("db/job/analysis-dynamodb/analysis.json", {"contract_version": "3.0"})


def test_check_artifact_ignores_unregistered_files_and_non_dicts() -> None:
    check_artifact("db/job/collector/sql_shadow.json", {"contract_version": "99.0"})
    check_artifact("db/job/collector/output.json", ["not", "a", "dict"])


# ---------------------------------------------------------------------- store


def _write(root: Path, rel: str, data: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_the_local_store_refuses_a_contract_file_at_another_major(tmp_path: Path) -> None:
    _write(tmp_path, "db/job/analysis-dynamodb/analysis.json", {"contract_version": "3.0"})
    store = LocalArtifactStore(str(tmp_path))
    with pytest.raises(ContractVersionError, match=r"'3\.0'.*'2\.\d+'"):
        store.read_json("db/job/analysis-dynamodb/analysis.json")


def test_the_local_store_reads_a_higher_minor_and_a_legacy_file(tmp_path: Path) -> None:
    major = current_version(AnalysisOutputContract).split(".")[0]
    _write(tmp_path, "db/job/analysis-dynamodb/analysis.json", {"contract_version": f"{major}.99"})
    _write(tmp_path, "db/job/collector/output.json", {"job_id": "j"})
    store = LocalArtifactStore(str(tmp_path))
    assert store.read_json("db/job/analysis-dynamodb/analysis.json")["contract_version"].endswith(
        ".99"
    )
    assert store.read_json("db/job/collector/output.json") == {"job_id": "j"}


def test_the_s3_store_refuses_a_contract_file_at_another_major() -> None:
    import io

    from src.storage.s3_store import S3ArtifactStore

    class _Client:
        def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 names
            return {"Body": io.BytesIO(json.dumps({"contract_version": "9.0"}).encode())}

    store = S3ArtifactStore("bucket", s3_client=_Client())
    with pytest.raises(ContractVersionError, match="referee-triage/triage.json"):
        store.read_json("db/job/referee-triage/triage.json")


# --------------------------------------------------------- load then write back


def test_a_progression_loaded_at_an_older_minor_is_written_back_at_the_current_one(
    tmp_path: Path,
) -> None:
    """The ``.meta`` rewrite from the note on #971: load, change, save."""
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    _write(
        tmp_path,
        ".meta/job-1.json",
        {
            "contract_version": "1.0",
            "job_id": "job-1",
            "current_phase": "analysis",
            "phases": {"analysis": {"phase": "analysis", "status": "completed"}},
        },
    )
    orch = LocalOrchestrator(store=LocalArtifactStore(str(tmp_path)))
    progression = orch._load_progression("job-1")
    assert progression.contract_version == current_version(PhaseProgression)
    orch._save_progression(progression)
    written = json.loads((tmp_path / ".meta/job-1.json").read_text())
    assert written["contract_version"] == current_version(PhaseProgression) != "1.0"


def test_a_progression_at_another_major_is_refused(tmp_path: Path) -> None:
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    _write(
        tmp_path,
        ".meta/job-1.json",
        {
            "contract_version": "2.0",
            "job_id": "job-1",
            "current_phase": "analysis",
            "phases": {"analysis": {"phase": "analysis", "status": "completed"}},
        },
    )
    orch = LocalOrchestrator(store=LocalArtifactStore(str(tmp_path)))
    with pytest.raises(ContractVersionError, match=r"\.meta/job-1\.json"):
        orch._load_progression("job-1")


def test_answers_loaded_at_an_older_minor_say_the_current_version(tmp_path: Path) -> None:
    from src.agents.interaction import read_answers
    from src.contracts.agent_interaction_models import AgentAnswers

    _write(
        tmp_path,
        "db/job/schema-dynamodb/answers.json",
        {
            "contract_version": "1.0",
            "agent_type": "schema-dynamodb",
            "answers": {"q-1": "A"},
            "timestamp": "2026-10-01T12:00:00Z",
        },
    )
    answers = read_answers(LocalArtifactStore(str(tmp_path)), "db", "job", "schema-dynamodb")
    assert answers is not None
    assert answers.model_dump(mode="json")["contract_version"] == current_version(AgentAnswers)


# ------------------------------------------------------------------------ api


def test_the_api_answers_409_in_plain_words_and_logs_the_detail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.api.routes import agent_interaction

    _write(
        tmp_path,
        "wordpress/job1/schema-dynamodb/questions.json",
        {"contract_version": "2.0", "agent_type": "schema-dynamodb", "questions": []},
    )
    agent_interaction.artifact_store = LocalArtifactStore(str(tmp_path))
    caplog.set_level(logging.WARNING, logger="src.api.main")
    try:
        resp = TestClient(app).get(
            "/api/v1/assessments/job1/agents/schema-dynamodb/questions",
            params={"database_name": "wordpress"},
        )
    finally:
        agent_interaction.artifact_store = None
    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "This assessment was saved by an incompatible version of the tool "
        "(contract 2.0, expected 1.*)."
    )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "wordpress/job1/schema-dynamodb/questions.json" in logged
    assert "AgentQuestions" in logged


def test_the_public_message_names_no_path_class_or_policy() -> None:
    with pytest.raises(ContractVersionError) as err:
        check_contract_version({"contract_version": "3.0"}, _Example, artifact="db/job/x.json")
    public = err.value.public_message
    assert "3.0" in public and "2.*" in public
    for internal in ("db/job/x.json", "_Example", "ADR", "MAJOR"):
        assert internal not in public
    with pytest.raises(ContractVersionError) as err:
        check_contract_version({"contract_version": "<b>x</b>"}, _Example, artifact="a")
    assert "<b>" not in err.value.public_message


def test_a_refusal_is_not_a_value_error() -> None:
    """``except ValueError`` around a read must not turn a refusal into "not available"."""
    assert not issubclass(ContractVersionError, ValueError)


def test_a_long_stored_value_is_cut_short_in_the_message() -> None:
    with pytest.raises(ContractVersionError) as err:
        check_contract_version({"contract_version": "x" * 500}, _Example, artifact="a")
    assert len(str(err.value)) < 400


# ------------------------------------------------------------------- adapters

_RC_FIXTURES = Path(__file__).resolve().parents[2] / "contract" / "fixtures"


@pytest.mark.parametrize(
    "name", ["reality_check_output_v1_13.json", "reality_check_output_v1_13_empty.json"]
)
def test_a_reality_check_saved_at_1_x_loads_through_its_adapter(name: str, tmp_path: Path) -> None:
    """RealityCheckOutputContract 2.0 (#790) dropped a field; 1.x files still load."""
    from src.contracts.reality_check_output import RealityCheckOutputContract

    old = json.loads((_RC_FIXTURES / name).read_text())
    assert old["contract_version"] == "1.13" and "lightweight_recommendations" in old
    loaded = load_contract(RealityCheckOutputContract, old, artifact=name)
    assert loaded.contract_version == current_version(RealityCheckOutputContract)
    assert loaded.source_assignment_version == 1

    _write(tmp_path, "db/job/reality-check/output.json", old)
    raw = LocalArtifactStore(str(tmp_path)).read_json("db/job/reality-check/output.json")
    assert raw["contract_version"] == current_version(RealityCheckOutputContract)
    assert "lightweight_recommendations" not in raw
    assert raw["before_distribution"] == old["before_distribution"]


@pytest.mark.parametrize("stored", ["0.9", "3.0"])
def test_a_reality_check_at_a_major_with_no_adapter_is_refused(stored: str) -> None:
    from src.contracts.reality_check_output import RealityCheckOutputContract

    with pytest.raises(ContractVersionError, match=repr(stored)):
        check_contract_version(
            {"contract_version": stored}, RealityCheckOutputContract, artifact="a"
        )


def test_a_payload_at_the_current_major_is_returned_unchanged(tmp_path: Path) -> None:
    data = {"contract_version": "2.0", "source_assignment_version": 1}
    assert check_artifact("db/job/reality-check/output.json", data) is data


# ----------------------------------------------------- collector output uploads


def _collector_upload(version: str) -> dict:
    return {
        "contract_version": version,
        "database_schema": {"tables": []},
        "queries": {"query_patterns": []},
    }


def test_ingest_refuses_an_uploaded_collector_output_at_another_major() -> None:
    """A 4.0 collector output was once taken for a raw collection (``startswith("3")``)."""
    from src.atx_orchestrator import core

    class _Store:
        def write_json(self, path: str, data: dict) -> None:
            raise AssertionError("nothing is written for a refused upload")

    with pytest.raises(ContractVersionError, match=r"testdb/job-1 upload.*'4\.0'"):
        core.ingest_offline_collection(_Store(), "job-1", "testdb", _collector_upload("4.0"))


@pytest.mark.parametrize("script", ["run_collect", "run_assessment"])
def test_the_collect_scripts_refuse_an_uploaded_collector_output_at_another_major(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import sys

    from scripts import run_assessment, run_collect

    module = {"run_collect": run_collect, "run_assessment": run_assessment}[script]
    upload = tmp_path / "upload.json"
    upload.write_text(json.dumps(_collector_upload("4.0")))
    if script == "run_collect":
        argv = ["run_collect.py", "--file", str(upload), "--db", "testdb"]
        argv += ["--artifact-root", str(tmp_path / "artifacts")]
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            module.main()
    else:
        store = LocalArtifactStore(str(tmp_path / "artifacts"))
        with pytest.raises(SystemExit):
            module.phase_collect(str(upload), "testdb", store)
    out = capsys.readouterr()
    assert "'4.0'" in out.out + out.err
    assert not (tmp_path / "artifacts" / "testdb").exists()


# ------------------------------------------------ model-written schema outputs


@pytest.mark.parametrize("echoed", ["2.0", "1.0", None])
def test_a_schema_design_answer_is_written_at_the_contracts_version(
    echoed: str | None, tmp_path: Path
) -> None:
    """A model echoing another MAJOR must never leave a file the store then refuses."""
    from src.agents.schema_design.handler import finalize_schema_design
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from tests.unit.agents.schema_design.test_schema_llm_seam import _VALID_DYNAMODB_OUTPUT

    answer = {k: v for k, v in _VALID_DYNAMODB_OUTPUT.items() if k != "contract_version"}
    if echoed is not None:
        answer["contract_version"] = echoed
    _write(tmp_path, "mydb/job-001/llm_responses/schema_design_dynamodb.json", answer)
    store = LocalArtifactStore(str(tmp_path))
    result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
    assert result["status"] == "complete", result
    written = store.read_json(result["output_path"])  # refused if another MAJOR
    assert written["contract_version"] == current_version(DynamoDBModelOutputContract)


def test_stamp_artifact_version_leaves_unregistered_files_alone() -> None:
    from src.shared.contract_versioning import stamp_artifact_version

    data = {"contract_version": "7.0"}
    assert stamp_artifact_version("db/job/llm_responses/x.json", data) is data
    stamped = stamp_artifact_version("db/job/schema-dynamodb/v1/schema_output.json", data)
    assert stamped["contract_version"] != "7.0" and data["contract_version"] == "7.0"


# ------------------------------------------------------------------ model I/O


@pytest.mark.parametrize(
    "path",
    [
        "db/job/llm_responses/schema_design_elasticache.json",
        "db/job/llm_responses/reality_check.json",
        "db/job/llm_responses/analysis_dynamodb.json",
        "db/job/llm_responses/synthesis.json",
        "db/job/llm_requests/schema_design_dynamodb.json",
        "db/job/reality-check/llm_input.json",
        "db/job/synthesis/v2/llm_input.json",
        "db/job/schema-dynamodb/v1/llm_input.json",
        "db/job/synthesis/llm_response.json",
        "db/job/schema-dynamodb/partial_output.json",
        "db/job/schema-dynamodb/v1/schema_draft_group_3.json",
        "db/job/schema-dynamodb/v1/input_group_3.json",
    ],
)
def test_a_model_io_file_is_never_checked(path: str, tmp_path: Path) -> None:
    """A model may echo any version; its own files are read as they are."""
    from src.shared.contract_versioning import is_model_io

    assert is_model_io(path)
    assert contract_for_artifact(path) is None
    _write(tmp_path, path, {"contract_version": "2.0", "x": 1})
    assert LocalArtifactStore(str(tmp_path)).read_json(path) == {"contract_version": "2.0", "x": 1}


@pytest.mark.parametrize(
    "path",
    [
        "db/job/collector/output.json",
        "db/job/schema-dynamodb/v1/schema_output.json",
        "db/job/synthesis/v2/report.json",
        "db/job/reality-check/output.json",
    ],
)
def test_a_stored_contract_is_not_model_io(path: str) -> None:
    from src.shared.contract_versioning import is_model_io

    assert not is_model_io(path)
    assert contract_for_artifact(path) is not None


def test_a_model_response_at_another_major_finalizes_at_the_current_version(
    tmp_path: Path,
) -> None:
    """The external-mode failure: the model's response says ``2.0``; finalize reads it
    through the store, and the stored schema output says the contract's version."""
    from src.agents.schema_design.handler import (
        finalize_schema_design,
        validate_schema_design_output,
    )
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from tests.unit.agents.schema_design.test_schema_llm_seam import _VALID_DYNAMODB_OUTPUT

    _write(
        tmp_path,
        "mydb/job-001/llm_responses/schema_design_dynamodb.json",
        {**_VALID_DYNAMODB_OUTPUT, "contract_version": "2.0"},
    )
    store = LocalArtifactStore(str(tmp_path))
    result = finalize_schema_design("job-001", "mydb", "dynamodb", store)
    assert result["status"] == "complete", result
    written = store.read_json(result["output_path"])
    assert written["contract_version"] == current_version(DynamoDBModelOutputContract) != "2.0"
    assert validate_schema_design_output(written, "dynamodb")["valid"]
