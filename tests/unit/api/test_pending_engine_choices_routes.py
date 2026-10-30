# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The assignment routes present pending engine choices per table and accept a
pick per table, or "decide for me" (#1003)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import assignments
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)
DB, JOB = "shop", "job-1"
PAIR = ["documentdb", "dynamodb"]


def _qa(qid: str, table: str, tied: list[str]) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine="dynamodb",
        confidence=78,
        source_tables=[table],
        assignment_reason="test",
        tied_engines=tied,
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            _qa("q1", "shop.theme_fields", PAIR),
            _qa("q2", "shop.theme_fields", PAIR),
            _qa("q3", "shop.orders", []),
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    queries = [
        {
            "query_id": q,
            "query_text": f"SELECT * FROM {t} WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": [f"shop.{t}"],
        }
        for q, t in (("q1", "theme_fields"), ("q2", "theme_fields"), ("q3", "orders"))
    ]
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"queries": {"query_patterns": queries}, "database_schema": {"tables": []}},
    )
    for engine in PAIR:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    monkeypatch.setattr(assignments, "artifact_store", store)
    return store


def _url() -> str:
    return f"/api/v1/assessments/{JOB}/assignments?database_name={DB}"


def test_get_presents_one_choice_per_table(store) -> None:
    body = client.get(_url()).json()
    (choice,) = body["pending_choices"]
    assert choice["choice_id"] == "shop.theme_fields"
    assert choice["query_ids"] == ["q1", "q2"]
    assert choice["tied_engines"] == PAIR
    assert choice["decide_for_me_engine"] in PAIR
    assert [e["engine"] for e in choice["options"]] == PAIR


def test_a_table_pick_moves_every_query_of_the_table(store) -> None:
    resp = client.put(
        _url(),
        json={"table_choices": [{"choice_id": "shop.theme_fields", "engine": "documentdb"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    qas = {qa["query_id"]: qa for qa in body["assignment"]["query_assignments"]}
    assert [qas[q]["assigned_engine"] for q in ("q1", "q2", "q3")] == [
        "documentdb",
        "documentdb",
        "dynamodb",
    ]
    assert qas["q1"]["customer_override"] and not qas["q1"]["tied_engines"]
    assert body["pending_choices"] == []
    assert body["assignment"]["auto_decided_choices"] == []
    assert {m["reason_code"] for m in body["assignment"]["moves"]} == {"customer.pick"}
    assert (body["assignment"]["status"], body["assignment"]["source"]) == (
        "customer_modified",
        "customer_gate",
    )


def test_decide_for_me_records_each_decision_as_the_rule_s(store) -> None:
    resp = client.put(_url(), json={"decide_for_me": True})
    assert resp.status_code == 200, resp.text
    assignment = resp.json()["assignment"]
    # The rule decided, not the customer: the version says so (#1003).
    assert (assignment["status"], assignment["source"]) == ("auto_decided", "auto_decided")
    (decided,) = assignment["auto_decided_choices"]
    assert decided["choice_id"] == "shop.theme_fields"
    assert decided["query_ids"] == ["q1", "q2"]
    assert decided["alternatives"] == [e for e in PAIR if e != decided["engine"]]
    moves = assignment["moves"]
    assert {(m["pass"], m["reason_code"], m["actor"]) for m in moves} == {
        ("auto_pick", "tie.decided_for_you", "rule")
    }
    assert all(m["evidence"]["choice_id"] == "shop.theme_fields" for m in moves)
    qas = {qa["query_id"]: qa for qa in assignment["query_assignments"]}
    assert not qas["q1"]["customer_override"]
    reason = qas["q1"]["assignment_reason"]
    assert "Decided automatically, review if needed" in reason
    # The API's "decide for me" is not --auto, and the table drops its schema prefix.
    assert "--auto" not in reason and "on table theme_fields." in reason


def test_a_pick_for_no_choice_or_an_untied_engine_is_refused_and_writes_nothing(store) -> None:
    for pick in (
        {"choice_id": "shop.nope", "engine": "dynamodb"},
        {"choice_id": "shop.theme_fields", "engine": "aurora_mysql"},
    ):
        resp = client.put(_url(), json={"table_choices": [pick]})
        assert resp.status_code == 400, resp.text
    assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")


def test_decide_for_me_with_nothing_pending_writes_nothing(store) -> None:
    assert client.put(_url(), json={"decide_for_me": True}).status_code == 200
    resp = client.put(_url(), json={"decide_for_me": True})
    assert resp.status_code == 200
    assert resp.json()["assignment"]["version"] == 2
    assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")


def test_a_customer_pick_on_a_decided_table_drops_the_decision(store) -> None:
    decided = client.put(_url(), json={"decide_for_me": True}).json()["assignment"]
    (decision,) = decided["auto_decided_choices"]
    other = decision["alternatives"][0]
    resp = client.put(
        _url(),
        json={"overrides": [{"query_id": q, "assigned_engine": other} for q in ("q1", "q2")]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assignment"]["auto_decided_choices"] == []


def test_confirming_the_decided_engine_makes_it_the_customer_s_choice(store) -> None:
    decided = client.put(_url(), json={"decide_for_me": True}).json()["assignment"]
    (decision,) = decided["auto_decided_choices"]
    resp = client.put(
        _url(), json={"overrides": [{"query_id": "q1", "assigned_engine": decision["engine"]}]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assignment"]["auto_decided_choices"] == []


def test_an_unrelated_edit_keeps_the_decision(store) -> None:
    client.put(_url(), json={"decide_for_me": True})
    resp = client.put(_url(), json={"overrides": [{"query_id": "q3", "in_scope": False}]})
    assert resp.status_code == 200, resp.text
    (kept,) = resp.json()["assignment"]["auto_decided_choices"]
    assert kept["decided_in_version"] == 2


def _write_group_store(tmp_path) -> LocalArtifactStore:
    """Two writes of ``carts`` (one tied, one settled on DynamoDB) and two settled
    DocumentDB reads elsewhere, so --auto's settled-workload rule picks DocumentDB."""
    s = LocalArtifactStore(base_dir=str(tmp_path))
    qas = [_qa("w1", "shop.carts", PAIR), _qa("w2", "shop.carts", [])]
    for qid in ("r1", "r2"):
        qa = _qa(qid, "shop.orders", [])
        qa.assigned_engine = "documentdb"
        qas.append(qa)
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    s.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json"))
    texts = {
        "w1": ("INSERT INTO carts (id) VALUES (?)", "INSERT", "shop.carts"),
        "w2": ("UPDATE carts SET n = 1 WHERE id = ?", "UPDATE", "shop.carts"),
        "r1": ("SELECT * FROM orders WHERE id = ?", "SELECT", "shop.orders"),
        "r2": ("SELECT * FROM orders WHERE user_id = ?", "SELECT", "shop.orders"),
    }
    queries = [
        {"query_id": q, "query_text": t, "query_type": k, "tables_accessed": [tbl]}
        for q, (t, k, tbl) in texts.items()
    ]
    s.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {"queries": {"query_patterns": queries}, "database_schema": {"tables": []}},
    )
    for engine in PAIR:
        s.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})
    return s


@pytest.mark.parametrize(("decide", "actor"), [(False, "customer"), (True, "rule")])
def test_a_write_pulled_along_by_a_pick_is_moved_by_whoever_picked(
    tmp_path, decide: bool, actor: str
) -> None:
    """A table's writes stay on one engine (#992): when a tied write is placed, the
    table's other write follows it. Under --auto that Move is the rule's, never the
    customer's (#1003)."""
    from src.agents.referee.assignment_overrides import apply_engine_choices

    s = _write_group_store(tmp_path)
    picks = {} if decide else {"carts": "documentdb"}
    result = apply_engine_choices(s, DB, JOB, picks, decide_for_me=decide)
    assert result is not None
    moves = {m.query_id: m for m in result.override.assignment.moves}
    assert set(moves) == {"w1", "w2"}
    assert moves["w2"].to_engine == "documentdb"
    assert moves["w2"].reason_code.value == "group.write_customer_propagation"
    assert {m.actor.value for m in moves.values()} == {actor}


def test_decide_for_me_with_a_customer_edit_in_the_same_batch_is_a_customer_edit(store) -> None:
    resp = client.put(
        _url(),
        json={"decide_for_me": True, "overrides": [{"query_id": "q3", "in_scope": False}]},
    )
    assert resp.status_code == 200, resp.text
    assignment = resp.json()["assignment"]
    assert (assignment["status"], assignment["source"]) == ("customer_modified", "customer_gate")
    assert {m["actor"] for m in assignment["moves"]} == {"rule"}
    assert len(assignment["auto_decided_choices"]) == 1


def test_in_a_mixed_batch_each_pulled_query_follows_whoever_pulled_it(tmp_path) -> None:
    """--choose on one table plus decide-for-me on another (#1003 review): the write
    pulled by the rule's pick is the rule's Move, never "the customer re-routed"."""
    from src.agents.referee.assignment_overrides import apply_engine_choices

    s = _write_group_store(tmp_path)
    raw = s.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
    raw["query_assignments"].append(_qa("q9", "shop.theme_fields", PAIR).model_dump(mode="json"))
    s.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", raw)
    collector = s.read_json(f"{DB}/{JOB}/collector/output.json")
    collector["queries"]["query_patterns"].append(
        {
            "query_id": "q9",
            "query_text": "SELECT * FROM theme_fields WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": ["shop.theme_fields"],
        }
    )
    s.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    result = apply_engine_choices(s, DB, JOB, {"theme_fields": "dynamodb"}, decide_for_me=True)
    assert result is not None
    moves = {m.query_id: m for m in result.override.assignment.moves}
    assert moves["q9"].actor.value == "customer"
    assert moves["w1"].pass_.value == "auto_pick"
    assert moves["w2"].reason_code.value == "group.write_customer_propagation"
    assert moves["w2"].actor.value == "rule"
    qas = {qa.query_id: qa for qa in result.override.assignment.query_assignments}
    assert "customer" not in qas["w2"].assignment_reason
    assert "decided automatically" in qas["w2"].assignment_reason
    # A customer pick in the batch makes the version a customer edit.
    assert result.override.assignment.source.value == "customer_gate"
