"""The DynamoDB designer is given each key-scoped write's key requirement (#1061).

#1038 records, for a write that changes the rows one value of a key anchor addresses,
that the column must become a partition key (or a GSI's) so the write reads its items
by key. It lived only in the decision trace, which the DynamoDB designer never reads.
The DynamoDB analysis now carries it in its contract (``key_scoped_writes``), from the
same ``transaction_shape`` the trace reads, so every designer input path has it: the
external request (``prepare_schema_design_input``), the Bedrock projection
(``load_agent_input``) and each design group's input (``build_group_input``), which
keeps the group's own writes only. No test calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.schema_design.group_input import build_group_input
from src.agents.schema_design.handler import prepare_schema_design_input
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.dynamodb_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-1061"
NOTE = (
    "order_items.order_id must become the partition key of an item collection "
    "(or a GSI's) so this write reads its items by key"
)


def _write(qid: str, text: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "DELETE",
        "frequency_per_hour": 60.0,
        "calls_per_second": 0.0167,
        "tables_accessed": [f"ecommerce.{table}"],
    }


# By ``order_id``, a foreign key that is not unique: key scoped.
BY_ORDER = _write("w-items-by-order", "DELETE FROM order_items WHERE order_id = ?", "order_items")
# By the primary key: one item, no requirement.
BY_PK = _write("w-item-by-pk", "DELETE FROM order_items WHERE item_id = ?", "order_items")


def _collector() -> dict:
    collector = copy.deepcopy(get_ecommerce_collector_output())
    collector["queries"]["query_patterns"] += [BY_ORDER, BY_PK]
    return collector


def _analysis(collector: dict) -> tuple[dict, dict]:
    contract, trace, _ = analyze_for_dynamodb_deterministic(
        AnalysisInput(
            job_id=JOB, collector_output=collector, target_database=TargetDatabase.DYNAMODB
        )
    )
    return contract.model_dump(mode="json"), trace


def test_the_analysis_carries_each_key_scoped_write_and_the_key_it_needs() -> None:
    analysis, _ = _analysis(_collector())
    assert analysis["contract_version"] == "2.2"
    assert analysis["key_scoped_writes"] == [
        {
            "query_id": "w-items-by-order",
            "table_id": "ecommerce.order_items",
            "columns": ["order_id"],
            "schema_note": NOTE,
            # A foreign key with collected statistics: its cardinality, and no lookups
            # (a schema key needs none to be one).
            "anchors": [{"column": "order_id", "cardinality": 500000, "lookup_query_ids": []}],
        }
    ]


# ``quantity`` has no index, no foreign key and no statistics; two queries look
# ``order_items`` up by it, so it is a key (#1061) and the delete is key scoped.
BY_QUANTITY = _write(
    "w-items-by-quantity", "DELETE FROM order_items WHERE quantity = ?", "order_items"
)
READ_BY_QUANTITY = {
    **_write(
        "r-items-by-quantity", "SELECT item_id FROM order_items WHERE quantity = ?", "order_items"
    ),
    "query_type": "SELECT",
}


def test_a_workload_key_carries_its_lookups_and_cardinality_unknown() -> None:
    """The maintainer decision on #1104: the designer is given the key-value lookups
    behind a workload key and its cardinality, or an explicit "unknown"."""
    collector = _collector()
    collector["queries"]["query_patterns"] += [BY_QUANTITY, READ_BY_QUANTITY]
    analysis, _ = _analysis(collector)
    (write,) = [w for w in analysis["key_scoped_writes"] if w["query_id"] == "w-items-by-quantity"]
    assert write["anchors"] == [
        {
            "column": "quantity",
            "cardinality": "unknown",
            "lookup_query_ids": ["r-items-by-quantity"],
        }
    ]


def test_a_cardinality_the_collector_did_not_measure_is_unknown() -> None:
    collector = _collector()
    for table in collector["database_schema"]["tables"]:
        for column in table["columns"]:
            column.pop("cardinality", None)
    analysis, _ = _analysis(collector)
    (write,) = analysis["key_scoped_writes"]
    assert write["anchors"] == [
        {"column": "order_id", "cardinality": "unknown", "lookup_query_ids": []}
    ]


def test_it_is_exactly_what_the_decision_trace_records() -> None:
    analysis, trace = _analysis(_collector())
    noted = {
        m["query_id"]: m["transaction_shape"]["schema_note"]
        for m in trace["query_matches"]
        if (m.get("transaction_shape") or {}).get("schema_note")
    }
    assert {w["query_id"]: w["schema_note"] for w in analysis["key_scoped_writes"]} == noted


def test_no_key_scoped_write_writes_no_field() -> None:
    analysis, _ = _analysis(get_ecommerce_collector_output())
    assert analysis.get("key_scoped_writes") is None


def test_a_saved_2_1_analysis_still_loads() -> None:
    analysis, _ = _analysis(get_ecommerce_collector_output())
    old = {k: v for k, v in analysis.items() if k != "key_scoped_writes"}
    old["contract_version"] = "2.1"
    assert AnalysisOutputContract.model_validate(old).key_scoped_writes is None


def _store(tmp_path: Path, collector: dict, analysis: dict) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json", analysis)
    return store


def _bedrock_input(tmp_path: Path, collector: dict, analysis: dict) -> dict:
    paths = []
    for name, data in (("collector", collector), ("analysis", analysis)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data, default=str))
        paths.append(str(path))
    return load_agent_input(AgentInputPaths(collector=paths[0], analysis=paths[1]))


def test_the_external_request_and_the_bedrock_projection_carry_the_same_list(tmp_path) -> None:
    collector = _collector()
    analysis, _ = _analysis(collector)
    external = prepare_schema_design_input(
        JOB, DB, "dynamodb", _store(tmp_path, collector, analysis)
    )
    bedrock = _bedrock_input(tmp_path, collector, analysis)
    assert external["analysis_output"]["key_scoped_writes"] == analysis["key_scoped_writes"]
    assert bedrock["analysis"]["key_scoped_writes"] == analysis["key_scoped_writes"]


def _group(collector: dict, analysis: dict, query_ids: set[str]) -> dict:
    queries = [q for q in collector["queries"]["query_patterns"] if q["query_id"] in query_ids]
    tables = [
        t
        for t in collector["database_schema"]["tables"]
        if any(t["table_id"] in q["tables_accessed"] for q in queries)
    ]
    return build_group_input(
        job_id=JOB,
        database_name=DB,
        engine="dynamodb",
        group_index=0,
        group_name="g",
        primary_tables=[t["table_id"] for t in tables],
        group_queries=queries,
        group_tables=tables,
        collector_output=collector,
        analysis_output=analysis,
        total_queries=len(collector["queries"]["query_patterns"]),
    )


def test_a_design_group_keeps_its_own_key_scoped_writes_only(tmp_path) -> None:
    collector = _collector()
    analysis, _ = _analysis(collector)
    mine = _group(collector, analysis, {"w-items-by-order", "w-item-by-pk"})
    assert mine["analysis_output"]["key_scoped_writes"] == analysis["key_scoped_writes"]
    other = _group(collector, analysis, {"w-item-by-pk"})
    assert "key_scoped_writes" not in other["analysis_output"]
    # The Bedrock path validates and projects the group's own files: the same list.
    bedrock = _bedrock_input(tmp_path, mine["collector_output"], mine["analysis_output"])
    assert bedrock["analysis"]["key_scoped_writes"] == analysis["key_scoped_writes"]


# ---------------------------------------------------------------------------
# Each design call sees only its own writes and its own queries (#1104 review)
# ---------------------------------------------------------------------------


def _with_quantity() -> tuple[dict, dict]:
    collector = _collector()
    collector["queries"]["query_patterns"] += [BY_QUANTITY, READ_BY_QUANTITY]
    analysis, _ = _analysis(collector)
    return collector, analysis


def _anchors(payload: dict, qid: str) -> list[dict]:
    (write,) = [w for w in payload["key_scoped_writes"] if w["query_id"] == qid]
    anchors: list[dict] = write["anchors"]
    return anchors


def test_a_group_names_only_lookups_in_the_group() -> None:
    collector, analysis = _with_quantity()
    both = _group(collector, analysis, {"w-items-by-quantity", "r-items-by-quantity"})
    assert _anchors(both["analysis_output"], "w-items-by-quantity")[0]["lookup_query_ids"] == [
        "r-items-by-quantity"
    ]
    alone = _group(collector, analysis, {"w-items-by-quantity"})
    (anchor,) = _anchors(alone["analysis_output"], "w-items-by-quantity")
    # The read is in another group: the designer cannot see it, so it is not named.
    assert anchor == {"column": "quantity", "cardinality": "unknown", "lookup_query_ids": []}


def _assignment(dynamodb: set[str], collector: dict) -> dict:
    return {
        "query_assignments": [
            {
                "query_id": q["query_id"],
                "assigned_engine": "dynamodb" if q["query_id"] in dynamodb else "aurora_mysql",
                "in_scope": True,
                "source_tables": q["tables_accessed"],
            }
            for q in collector["queries"]["query_patterns"]
        ]
    }


def test_the_external_request_carries_only_this_engines_writes_and_lookups(tmp_path) -> None:
    """With an assignment, the request's collector holds only the queries assigned to
    DynamoDB, and its key-scoped writes are only theirs, naming only them."""
    collector, analysis = _with_quantity()
    store = _store(tmp_path, collector, analysis)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        _assignment({"w-items-by-quantity", "w-item-by-pk"}, collector),
    )
    request = prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=1)
    writes = request["analysis_output"]["key_scoped_writes"]
    assert [w["query_id"] for w in writes] == ["w-items-by-quantity"]
    assert writes[0]["anchors"][0]["lookup_query_ids"] == []
    # The whole analysis on disk is unchanged.
    assert store.read_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json") == analysis


def test_the_bedrock_projection_carries_only_the_filtered_collectors_writes(tmp_path) -> None:
    collector, analysis = _with_quantity()
    mine = {"w-items-by-quantity", "r-items-by-quantity"}
    filtered = copy.deepcopy(collector)
    filtered["queries"]["query_patterns"] = [
        q for q in collector["queries"]["query_patterns"] if q["query_id"] in mine
    ]
    projected = _bedrock_input(tmp_path, filtered, analysis)["analysis"]
    assert [w["query_id"] for w in projected["key_scoped_writes"]] == ["w-items-by-quantity"]
    assert _anchors(projected, "w-items-by-quantity")[0]["lookup_query_ids"] == [
        "r-items-by-quantity"
    ]


def test_no_write_in_scope_projects_none(tmp_path) -> None:
    collector, analysis = _with_quantity()
    filtered = copy.deepcopy(collector)
    filtered["queries"]["query_patterns"] = [
        q for q in collector["queries"]["query_patterns"] if q["query_id"] == "w-item-by-pk"
    ]
    assert _bedrock_input(tmp_path, filtered, analysis)["analysis"]["key_scoped_writes"] is None
