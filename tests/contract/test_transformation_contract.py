# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step contract model (#1762, ADR-033 Decision 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.transformation_contract import Rule, StepContract
from src.shared.contract_versioning import contract_for_artifact, current_version


def _field(col: str = "ID") -> dict:
    return {"source_column": col, "target_column": col, "target_type": "BIGINT",
            "op": {"op": "copy", "column": {"table_id": "wp.posts", "column": col}}}  # fmt: skip


_ERR = {"code": "unknown_column", "severity": "error", "message": "no such column"}


def _rule(status: str = "valid", fields: list[dict] | None = None) -> dict:
    findings = [_ERR] if status == "blocked" else []
    return {"input_schema": "collector", "source_table": "wp.posts", "target_table": "posts",
            "primary_key": ["ID"], "fields": fields or [_field()], "status": status, "findings": findings}  # fmt: skip


def _contract(**over) -> dict:
    base = {"job_id": "j1", "assignment_version": 2, "step": 1, "wave": 1, "wave_shape": "homogeneous",
            "target": {"engine": "aurora_mysql"}, "status": "valid", "blocked_rule_count": 0,
            "unsupported_count": 0, "rules": [_rule()]}  # fmt: skip
    base.update(over)
    return base


def test_a_valid_step_contract_round_trips():
    c = StepContract.model_validate(_contract())
    assert (
        c.contract_version == current_version(StepContract) and c.depends_on == [] and c.views == []
    )
    assert StepContract.model_validate(c.model_dump(mode="json")) == c


def test_blocked_count_must_match_the_rules():
    with pytest.raises(ValidationError, match="blocked_rule_count"):
        StepContract.model_validate(_contract(rules=[_rule("blocked")], status="has_blocked_rules"))


def test_status_must_agree_with_blocked_rules():
    with pytest.raises(ValidationError, match="status"):
        StepContract.model_validate(
            _contract(rules=[_rule("blocked")], blocked_rule_count=1, status="valid")
        )
    with pytest.raises(ValidationError, match="status"):
        StepContract.model_validate(_contract(status="has_blocked_rules"))


def test_unsupported_count_must_match_the_fields():
    unsupported = {"source_column": "meta", "target_column": "meta", "target_type": "TEXT",
                   "op": {"op": "unsupported", "description": "PHP-serialized array"}}  # fmt: skip
    with pytest.raises(ValidationError, match="unsupported_count"):
        StepContract.model_validate(_contract(rules=[_rule(fields=[_field(), unsupported])]))
    ok = StepContract.model_validate(
        _contract(rules=[_rule(fields=[_field(), unsupported])], unsupported_count=1)
    )
    assert ok.unsupported_count == 1


def test_a_field_reading_another_table_is_refused():
    other = {"source_column": "ID", "target_column": "ID", "target_type": "BIGINT",
             "op": {"op": "copy", "column": {"table_id": "wp.users", "column": "ID"}}}  # fmt: skip
    with pytest.raises(ValidationError, match="own row"):
        Rule.model_validate(_rule(fields=[other]))


def test_a_contract_with_no_rules_states_why():
    c = StepContract.model_validate(_contract(rules=[], status="staged_move_pending",
                                              status_reason="staged relational move pending (#1702)"))  # fmt: skip
    assert c.rules == []
    with pytest.raises(ValidationError, match="status_reason"):
        StepContract.model_validate(_contract(rules=[], status="no_relational_anchor"))


def test_the_artifact_path_is_registered():
    assert contract_for_artifact("db/j1/transformation-contract/v2/step-1.json") is StepContract


def test_a_valid_rule_with_an_error_finding_is_refused():
    with pytest.raises(ValidationError, match="error finding"):
        Rule.model_validate(dict(_rule(), findings=[_ERR]))


def test_a_blocked_rule_without_an_error_finding_is_refused():
    warn = dict(_ERR, severity="warning")
    with pytest.raises(ValidationError, match="error finding"):
        Rule.model_validate(dict(_rule("blocked"), findings=[warn]))
    with pytest.raises(ValidationError, match="error finding"):
        Rule.model_validate(dict(_rule("blocked"), findings=[]))
    assert Rule.model_validate(dict(_rule(), findings=[warn])).status == "valid"


@pytest.mark.parametrize("bad", ["posts", ".posts", "wp."])
def test_a_source_table_needs_a_schema(bad):
    with pytest.raises(ValidationError, match="source_table"):
        Rule.model_validate(dict(_rule(), source_table=bad))


def test_a_source_table_name_may_hold_a_space():
    f = _field()
    f["op"]["column"]["table_id"] = "shop.Order Items"
    r = Rule.model_validate(dict(_rule(fields=[f]), source_table="shop.Order Items"))
    assert r.source_table == "shop.Order Items"


@pytest.mark.parametrize("status", ["no_relational_anchor", "staged_move_pending"])
def test_a_step_with_no_rules_status_refuses_rules(status):
    with pytest.raises(ValidationError, match="lists none"):
        StepContract.model_validate(_contract(status=status, status_reason="why"))


def test_a_field_source_column_names_the_op_column():
    f = _field("ID")
    f["source_column"] = "other"
    with pytest.raises(ValidationError, match="source_column"):
        Rule.model_validate(_rule(fields=[f]))


def test_depends_on_must_name_an_earlier_step():
    dep = {"step": 1, "contract_sha256": "a" * 64}
    with pytest.raises(ValidationError, match="earlier"):
        StepContract.model_validate(_contract(step=1, depends_on=[dep]))
    assert StepContract.model_validate(_contract(step=2, depends_on=[dep])).step == 2
