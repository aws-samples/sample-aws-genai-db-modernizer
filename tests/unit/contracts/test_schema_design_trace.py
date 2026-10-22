"""The schema design trace is a versioned contract (ADR-008 Decision 1, #1258).

``schema-<engine>/[v<N>/]design_trace.json`` (and DynamoDB's per-group
``design_trace_group_<G>.json``) was a plain dict. The post-schema router reads the
PE reviewer's routing flags from it and synthesis reads it for the report, so it is
now ``DesignTrace`` / ``DesignTraceGroup``: every writer goes through the model,
every reader checks the version, and a trace saved before the contract still loads.

The files under ``tests/fixtures/design_trace_pre_1258/`` are traces the writers
produced before this change: a Bedrock designer run with a revision round and
routing flags, an external PE review recorded on a single design and on a DynamoDB
group, and the two groups merged by ``--merge``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.agents.referee.post_schema_router import routing_flags_from_trace
from src.agents.referee.synthesis_data import _read_design_trace
from src.agents.schema_design import pe_review_external as pe
from src.agents.schema_design.design_trace import (
    DesignTraceShapeError,
    parse_design_trace,
    read_design_trace,
    write_design_trace,
)
from src.agents.schema_design.group_merger import _consolidate_design_traces
from src.contracts.schema_design_trace import DesignTrace, DesignTraceGroup
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_artifact,
    current_version,
    is_internal_artifact,
)
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.schema_design.test_pe_review_external import (  # noqa: F401 (fixture)
    JOB,
    PREFIX,
    V,
    _flag,
    _review,
    store,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "design_trace_pre_1258"
DB = "shop"
BASE = f"{DB}/{JOB}/schema-dynamodb/v{V}"
SINGLE = f"{DB}/{JOB}/schema-dynamodb/v{V}/design_trace.json"
LEGACY_SINGLE = f"{DB}/{JOB}/schema-dynamodb/design_trace.json"
GROUP = f"{BASE}/design_trace_group_1.json"
CURRENT = current_version(DesignTrace)
CURRENT_GROUP = current_version(DesignTraceGroup)


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def _raw(root: Path, key: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / key).read_text())
    return data


def _put(root: Path, key: str, data: dict[str, Any]) -> None:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _fresh(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path))


# --- registration -------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        SINGLE,
        LEGACY_SINGLE,
        f"{DB}/{JOB}/schema-opensearch/v3/design_trace.json",
        f"{DB}/{JOB}/schema-aurora_mysql/v1/design_trace.json",
    ],
)
def test_a_design_trace_path_is_a_registered_contract(key: str) -> None:
    assert contract_for_artifact(key) is DesignTrace
    assert not is_internal_artifact(key)


def test_a_group_trace_path_is_its_own_registered_contract() -> None:
    assert contract_for_artifact(GROUP) is DesignTraceGroup
    assert not is_internal_artifact(GROUP)


# --- old traces load ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["single_bedrock", "single_external_review", "group_external_review", "merged_dynamodb"]
)
def test_a_pre_contract_trace_has_no_version_and_loads_unchanged(name: str, tmp_path: Path) -> None:
    legacy = _fixture(name)
    assert "contract_version" not in legacy
    key = GROUP if name.startswith("group") else SINGLE
    _put(tmp_path, key, legacy)
    read = _fresh(tmp_path).read_json(key)
    assert read == legacy
    model = DesignTraceGroup if name.startswith("group") else DesignTrace
    loaded = model.model_validate(read)
    # Loaded and dumped again it is the old trace plus the version: no key lost, no null added.
    assert loaded.model_dump(mode="json") == {
        "contract_version": current_version(model),
        **legacy,
    }


def test_the_merged_fixture_is_the_shape_the_router_reads_flags_from() -> None:
    merged = DesignTrace.model_validate(_fixture("merged_dynamodb"))
    assert merged.total_groups == 2
    assert [g.routing_flags is not None for g in merged.groups or []] == [True, True]
    assert len(routing_flags_from_trace(merged)) == 2


def test_an_old_trace_in_the_legacy_unversioned_folder_loads(tmp_path: Path) -> None:
    _put(tmp_path, LEGACY_SINGLE, _fixture("single_bedrock"))
    loaded = read_design_trace(_fresh(tmp_path), LEGACY_SINGLE)
    assert loaded.contract_version == CURRENT
    assert len(loaded.trace or []) == 4


def test_a_trace_with_only_the_minimal_keys_loads() -> None:
    olds: list[dict[str, Any]] = [{"trace": []}, {"groups": []}, {}, {"iterations": []}]
    for old in olds:
        assert (
            DesignTrace.model_validate(old).model_dump(mode="json")["contract_version"] == CURRENT
        )


# --- the shapes and what they must not gain ----------------------------------------


def test_an_absent_section_is_never_written_as_null() -> None:
    dumped = DesignTrace.model_validate({"trace": []}).model_dump(mode="json")
    assert dumped == {"contract_version": CURRENT, "trace": []}


def test_the_aurora_delta_merge_section_round_trips() -> None:
    trace = {
        "trace": [{"type": "designer", "iteration": 0, "elapsed_seconds": 1.5}],
        "delta_merge": {
            "summary": {"tables_added": 2, "columns_added": 7},
            "errors": [],
            "warnings": ["a column type was widened"],
        },
    }
    dumped = DesignTrace.model_validate(trace).model_dump(mode="json")
    assert dumped == {"contract_version": CURRENT, **trace}


def test_a_designer_may_record_a_key_the_contract_does_not_know() -> None:
    trace = {"trace": [], "x_added_later": {"a": 1}}
    assert DesignTrace.model_validate(trace).model_dump(mode="json")["x_added_later"] == {"a": 1}


@pytest.mark.parametrize(
    "bad",
    [
        {"trace": "none"},
        {"trace": [{"iteration": 0}]},
        {"routing_flags": "q-1"},
        {"routing_flags": ["q-1"]},
        {"groups": "all"},
        {"total_groups": "two"},
        {"merge_reconciliation": "kept"},
        {"delta_merge": {"errors": []}},
    ],
)
def test_a_section_off_the_contract_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DesignTrace.model_validate(bad)


# --- reading ------------------------------------------------------------------------


def test_the_store_refuses_a_trace_at_another_major(tmp_path: Path) -> None:
    _put(tmp_path, SINGLE, {**_fixture("single_bedrock"), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="DesignTrace"):
        _fresh(tmp_path).read_json(SINGLE)
    with pytest.raises(ContractVersionError, match="DesignTrace"):
        read_design_trace(_fresh(tmp_path), SINGLE)


def test_the_store_refuses_a_group_trace_at_another_major(tmp_path: Path) -> None:
    _put(tmp_path, GROUP, {**_fixture("group_external_review"), "contract_version": "2.0"})
    with pytest.raises(ContractVersionError, match="DesignTraceGroup"):
        _fresh(tmp_path).read_json(GROUP)


def test_a_trace_at_a_newer_minor_is_read_and_keeps_its_fields(tmp_path: Path) -> None:
    newer = {**_fixture("single_bedrock"), "contract_version": "1.9", "x_added_later": True}
    _put(tmp_path, SINGLE, newer)
    loaded = read_design_trace(_fresh(tmp_path), SINGLE)
    assert loaded.model_dump(mode="json")["x_added_later"] is True
    assert loaded.contract_version == CURRENT


def test_the_router_refuses_a_flag_list_off_the_contract() -> None:
    """A renamed or retyped ``routing_flags`` used to drop the flags silently."""
    with pytest.raises(DesignTraceShapeError, match="routing_flags"):
        routing_flags_from_trace({"routing_flags": {"q-1": "full_scan"}})
    with pytest.raises(ContractVersionError):
        routing_flags_from_trace({"contract_version": "2.0", "routing_flags": []})


def test_the_router_reads_the_flags_of_every_shape() -> None:
    flag = _flag(["q5"], "dynamodb")
    assert routing_flags_from_trace(_fixture("single_bedrock")) == [flag]
    assert routing_flags_from_trace(_fixture("group_external_review")) == [flag]
    merged = _fixture("merged_dynamodb")
    assert routing_flags_from_trace(merged) == [flag, flag]
    assert routing_flags_from_trace(DesignTrace.model_validate(merged)) == [flag, flag]


def test_synthesis_reads_the_trace_as_the_contract(tmp_path: Path) -> None:
    _put(tmp_path, SINGLE, _fixture("single_bedrock"))
    trace = _read_design_trace(_fresh(tmp_path), SINGLE)
    assert isinstance(trace, DesignTrace)
    assert trace.token_usage == {
        "calls": 4,
        "inputTokens": 1200,
        "outputTokens": 340,
        "cacheReadInputTokens": 50,
    }
    assert _read_design_trace(_fresh(tmp_path), f"{BASE}/missing.json") is None


def test_synthesis_does_not_hide_a_version_it_refuses(tmp_path: Path) -> None:
    _put(tmp_path, SINGLE, {"contract_version": "2.0"})
    with pytest.raises(ContractVersionError):
        _read_design_trace(_fresh(tmp_path), SINGLE)


# --- writing: every writer goes through the model ---------------------------------


def test_write_design_trace_writes_the_contracts_version_not_the_dicts(tmp_path: Path) -> None:
    store_ = _fresh(tmp_path)
    write_design_trace(store_, SINGLE, {**_fixture("single_bedrock"), "contract_version": "1.7"})
    assert _raw(tmp_path, SINGLE)["contract_version"] == CURRENT


def test_write_design_trace_refuses_a_trace_off_the_contract_and_writes_nothing(
    tmp_path: Path,
) -> None:
    store_ = _fresh(tmp_path)
    with pytest.raises(DesignTraceShapeError, match="routing_flags"):
        write_design_trace(store_, SINGLE, {"routing_flags": "q-1"})
    assert not store_.exists(SINGLE)


def test_write_design_trace_refuses_a_path_that_is_not_a_design_trace(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="not a schema design trace"):
        write_design_trace(_fresh(tmp_path), f"{BASE}/schema_output.json", {})


def test_the_group_merge_writes_a_versioned_trace_and_keeps_the_groups_plain(
    tmp_path: Path,
) -> None:
    store_ = _fresh(tmp_path)
    _put(tmp_path, f"{BASE}/design_trace_group_0.json", _fixture("single_bedrock"))
    # One group trace was written after the contract, with its own version.
    _put(
        tmp_path,
        f"{BASE}/design_trace_group_1.json",
        {**_fixture("group_external_review"), "contract_version": "1.0"},
    )
    _consolidate_design_traces(store_, BASE, {}, ["kept a unique key on orders"])
    merged = _raw(tmp_path, f"{BASE}/design_trace.json")
    assert merged["contract_version"] == CURRENT
    assert merged["total_groups"] == 2
    assert merged["merge_reconciliation"] == ["kept a unique key on orders"]
    assert [g.get("contract_version") for g in merged["groups"]] == [None, None]
    # Same content as before the contract, plus the version.
    before = _fixture("merged_dynamodb")
    assert merged == {"contract_version": CURRENT, **before}
    assert read_design_trace(store_, f"{BASE}/design_trace.json").total_groups == 2


def test_the_group_merge_fails_on_a_group_trace_it_cannot_read(tmp_path: Path) -> None:
    store_ = _fresh(tmp_path)
    _put(tmp_path, f"{BASE}/design_trace_group_0.json", _fixture("single_bedrock"))
    _put(
        tmp_path,
        f"{BASE}/design_trace_group_1.json",
        {**_fixture("group_external_review"), "contract_version": "2.0"},
    )
    with pytest.raises(ContractVersionError):
        _consolidate_design_traces(store_, BASE, {})
    assert not store_.exists(f"{BASE}/design_trace.json")


def test_the_group_merge_with_nothing_to_write_still_writes_nothing(tmp_path: Path) -> None:
    store_ = _fresh(tmp_path)
    _consolidate_design_traces(store_, BASE, {})
    assert not store_.exists(f"{BASE}/design_trace.json")


def test_an_external_review_is_recorded_as_a_versioned_trace(store) -> None:  # noqa: F811
    pe.prepare_pe_review_llm_input(store, DB, JOB, V)
    store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review([_flag(["q2"])]))
    pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
    key = f"{PREFIX}/schema-documentdb/v{V}/design_trace.json"
    trace = store.read_json(key)
    assert trace["contract_version"] == CURRENT
    assert trace["pe_review"]["source"] == "external"
    assert routing_flags_from_trace(trace) == [_flag(["q2"])]


def test_an_external_group_review_is_recorded_as_a_versioned_group_trace(
    store,  # noqa: F811
) -> None:
    pe.prepare_pe_review_llm_input(store, DB, JOB, V)
    store.write_json(
        f"{PREFIX}/llm_responses/pe_review_dynamodb_group_1.json",
        _review([_flag(["q5"], "dynamodb")]),
    )
    pe.apply_pe_review_llm_output(store, DB, JOB, V, "dynamodb", 1)
    trace = store.read_json(f"{PREFIX}/schema-dynamodb/v{V}/design_trace_group_1.json")
    assert trace["contract_version"] == CURRENT_GROUP
    assert DesignTraceGroup.model_validate(trace).routing_flags == [_flag(["q5"], "dynamodb")]


def test_a_failed_external_review_is_recorded_as_a_versioned_trace(
    store,  # noqa: F811
    tmp_path: Path,
) -> None:
    pe.prepare_pe_review_llm_input(store, DB, JOB, V)
    status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb", failed=True)
    assert status["review_failed"] is True
    trace = store.read_json(f"{PREFIX}/schema-documentdb/v{V}/design_trace.json")
    assert trace["contract_version"] == CURRENT
    assert trace["pe_review"]["error"]
    assert trace["trace"][0]["type"] == "pe_error"


def test_the_carry_forward_restamps_a_copied_trace_to_today(tmp_path: Path) -> None:
    """ATX copies an unaffected engine's design forward; the trace is written at today's
    version, not the one it was copied at."""
    from src.atx_orchestrator.tools import _copy_schema_forward
    from tests.unit.agents.schema_design.valid_designs import ddb_design, ddb_pattern, ddb_table

    store_ = _fresh(tmp_path)
    prev = f"{DB}/{JOB}/schema-dynamodb/v1"
    design = ddb_design(
        [ddb_table("Orders", "orders")], [ddb_pattern("DDB-AP-1", "Orders", "orders", "q1")]
    )
    store_.write_json(f"{prev}/schema_output.json", design)
    old = copy.deepcopy(_fixture("single_bedrock"))
    _put(tmp_path, f"{prev}/design_trace.json", old)  # saved before the contract
    assert _copy_schema_forward(store_, DB, JOB, "dynamodb", 1, 2) is True
    copied = _raw(tmp_path, f"{DB}/{JOB}/schema-dynamodb/v2/design_trace.json")
    assert copied == {"contract_version": CURRENT, **old}


def test_the_group_designer_writes_each_groups_trace_versioned(tmp_path: Path) -> None:
    from unittest.mock import patch

    from src.agents.schema_design.handler import run_schema_design_auto
    from tests.unit.agents.schema_design import test_group_decision_trace as groups

    store_ = groups._seed(tmp_path, "documentdb", groups._trace())
    trace = json.dumps(_fixture("single_bedrock"))

    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        return json.dumps({"ok": True}), trace

    with (
        patch(
            "src.agents.schema_design.handler.run_schema_split",
            side_effect=groups._fake_split({0: groups.GROUP_A, 1: groups.GROUP_B}),
        ),
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        patch("src.agents.schema_design.handler.run_schema_merge"),
    ):
        run_schema_design_auto(groups.JOB, groups.DB, "documentdb", store_, assignment_version=1)
    for idx in (0, 1):
        key = f"{groups.DB}/{groups.JOB}/schema-documentdb/v1/design_trace_group_{idx}.json"
        written = _raw(tmp_path, key)
        assert written == {"contract_version": CURRENT_GROUP, **_fixture("single_bedrock")}


def test_a_group_designer_answer_off_the_contract_is_not_written(tmp_path: Path) -> None:
    from unittest.mock import patch

    from src.agents.schema_design.handler import run_schema_design_auto
    from tests.unit.agents.schema_design import test_group_decision_trace as groups

    store_ = groups._seed(tmp_path, "documentdb", groups._trace())

    def dispatch(target_type, collector_path=None, analysis_path=None, **kw):
        return json.dumps({"ok": True}), json.dumps({"routing_flags": "q-1"})

    with (
        patch(
            "src.agents.schema_design.handler.run_schema_split",
            side_effect=groups._fake_split({0: groups.GROUP_A}),
        ),
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        patch("src.agents.schema_design.handler.run_schema_merge"),
        pytest.raises(DesignTraceShapeError),
    ):
        run_schema_design_auto(groups.JOB, groups.DB, "documentdb", store_, assignment_version=1)
    key = f"{groups.DB}/{groups.JOB}/schema-documentdb/v1/design_trace_group_0.json"
    assert not store_.exists(key)


# --- one behaviour on a bad shape ---------------------------------------------------

BAD_SHAPE = {"trace": [], "routing_flags": "q-1"}


def test_the_shape_error_names_the_artifact_and_field_and_not_the_value() -> None:
    with pytest.raises(DesignTraceShapeError) as caught:
        parse_design_trace(
            {"routing_flags": "secret-looking-value"}, artifact="a/design_trace.json"
        )
    assert caught.value.artifact == "a/design_trace.json"
    assert caught.value.errors and caught.value.errors[0].startswith("routing_flags")
    assert "secret-looking-value" not in str(caught.value)
    assert not isinstance(caught.value, ValueError)


def test_every_reader_raises_the_same_error_on_a_bad_shape(tmp_path: Path) -> None:
    """Router, store reader (the orchestrator's), synthesis and the AWS Transform
    carry-forward: none reads a bad shape as no flags or no trace."""
    from src.atx_orchestrator.tools import _copy_schema_forward
    from tests.unit.agents.schema_design.valid_designs import ddb_design, ddb_pattern, ddb_table

    _put(tmp_path, SINGLE, BAD_SHAPE)
    store_ = _fresh(tmp_path)
    with pytest.raises(DesignTraceShapeError):
        routing_flags_from_trace(BAD_SHAPE)
    with pytest.raises(DesignTraceShapeError):
        read_design_trace(store_, SINGLE)
    with pytest.raises(DesignTraceShapeError):
        _read_design_trace(store_, SINGLE)
    prev = f"{DB}/{JOB}/schema-dynamodb/v1"
    design = ddb_design(
        [ddb_table("Orders", "orders")], [ddb_pattern("DDB-AP-1", "Orders", "orders", "q1")]
    )
    store_.write_json(f"{prev}/schema_output.json", design)
    _put(tmp_path, f"{prev}/design_trace.json", BAD_SHAPE)
    with pytest.raises(DesignTraceShapeError):
        _copy_schema_forward(store_, DB, JOB, "dynamodb", 1, 3)
    assert not store_.exists(f"{DB}/{JOB}/schema-dynamodb/v3/design_trace.json")


def test_a_model_answer_is_not_thrown_away_on_a_type_quirk() -> None:
    """The numbers a model client or answer can shape are kept as given."""
    trace = {
        "trace": [{"type": "designer", "iteration": "1", "elapsed_seconds": "2.5s"}],
        "token_usage": {"calls": "3", "inputTokens": None, "latency": {"p50": 1}},
    }
    dumped = DesignTrace.model_validate(trace).model_dump(mode="json")
    assert dumped == {"contract_version": CURRENT, **trace}


def test_a_quirky_trace_is_repaired_when_the_paid_review_is_recorded(store) -> None:  # noqa: F811
    """A shape quirk already in the trace must not lose the review the model was paid
    for, nor leave a file the router then refuses: what does not fit is dropped and
    noted, and the written trace validates."""
    pe.prepare_pe_review_llm_input(store, DB, JOB, V)
    key = f"{PREFIX}/schema-documentdb/v{V}/design_trace.json"
    store.write_json(
        key,
        {
            "trace": [{"iteration": 0}, {"type": "designer", "iteration": 1}],
            "token_usage": "n/a",
            "delta_merge": {"errors": []},
            "x_kept": {"a": 1},
        },
    )
    store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review([_flag(["q2"])]))
    status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
    assert status["status"] == "complete"
    written = store.read_json(key)
    assert written["contract_version"] == CURRENT
    assert written["pe_review"]["source"] == "external"
    assert written["x_kept"] == {"a": 1}
    assert [e["type"] for e in written["trace"]] == ["designer", "pe_review"]
    assert "token_usage" not in written and "delta_merge" not in written
    assert [n.split(":")[0] for n in written["dropped_fields"]] == [
        "trace.0",
        "token_usage",
        "delta_merge",
    ]
    assert all("n/a" not in note for note in written["dropped_fields"])
    # The router accepts what was written, and finds the flag.
    assert routing_flags_from_trace(read_design_trace(store, key)) == [_flag(["q2"])]
    assert routing_flags_from_trace(store.read_json(key)) == [_flag(["q2"])]


def test_a_review_whose_trace_has_unusable_routing_flags_is_refused_at_the_step(
    store,  # noqa: F811
) -> None:
    """The router acts on ``routing_flags``, so it is never repaired: the step reports the
    error (the command's retry rule applies) and the trace on disk is left as it was."""
    pe.prepare_pe_review_llm_input(store, DB, JOB, V)
    key = f"{PREFIX}/schema-documentdb/v{V}/design_trace.json"
    before = {"trace": [], "groups": [{"routing_flags": "q-1"}]}
    store.write_json(key, before)
    store.write_json(f"{PREFIX}/llm_responses/pe_review_documentdb.json", _review([_flag(["q2"])]))
    status = pe.apply_pe_review_llm_output(store, DB, JOB, V, "documentdb")
    assert status["status"] == "validation_failed"
    assert any("routing_flags" in e for e in status["errors"])
    assert store.read_json(key) == before


def test_a_lenient_write_never_writes_an_invalid_trace(tmp_path: Path) -> None:
    messy = {
        "trace": [{"iteration": 1}, "not-an-entry", {"type": "designer", "iteration": 2}],
        "token_usage": [1, 2],
        "pe_review": {"error": "x"},
        "groups": [{"trace": "none"}, {"token_usage": {"calls": 1}}],
        "total_groups": "two",
        "merge_reconciliation": "kept",
    }
    store_ = _fresh(tmp_path)
    write_design_trace(store_, SINGLE, messy, lenient=True)
    written = _raw(tmp_path, SINGLE)
    assert DesignTrace.model_validate(written)
    assert read_design_trace(store_, SINGLE).trace is not None
    assert [e["type"] for e in written["trace"]] == ["designer"]
    assert written["groups"] == [{}, {"token_usage": {"calls": 1}}]
    assert [n.split(":")[0] for n in written["dropped_fields"]] == [
        "trace.0",
        "trace.0",
        "token_usage",
        "pe_review",
        "total_groups",
        "groups.0.trace",
        "merge_reconciliation",
    ]


def test_a_lenient_write_of_a_clean_trace_adds_no_note(tmp_path: Path) -> None:
    write_design_trace(_fresh(tmp_path), SINGLE, _fixture("single_bedrock"), lenient=True)
    assert "dropped_fields" not in _raw(tmp_path, SINGLE)


def test_a_lenient_write_still_refuses_bad_routing_flags(tmp_path: Path) -> None:
    store_ = _fresh(tmp_path)
    for bad in (BAD_SHAPE, {"groups": [{"routing_flags": {"q": 1}}]}):
        with pytest.raises(DesignTraceShapeError, match="routing_flags"):
            write_design_trace(store_, SINGLE, bad, lenient=True)
    assert not store_.exists(SINGLE)


def test_the_strict_write_still_refuses_what_the_lenient_one_repairs(tmp_path: Path) -> None:
    with pytest.raises(DesignTraceShapeError):
        write_design_trace(_fresh(tmp_path), SINGLE, {"token_usage": "n/a"})
