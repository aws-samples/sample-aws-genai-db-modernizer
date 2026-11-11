# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 preflight result (#1799)."""

from __future__ import annotations

import datetime as dt

import pytest

from src.contracts.dms_preflight import CHECK_ORDER, DmsPreflight
from src.shared.contract_versioning import contract_for_artifact, current_version

N = len(CHECK_ORDER)


def _rows(*statuses):
    return [
        {"id": i, "status": s, "summary": "x"} for i, s in zip(CHECK_ORDER, statuses, strict=True)
    ]


def _result(status, rows):
    return {"job_id": "job1", "database_name": "shop", "region": "us-east-1", "status": status,
            "checked_at": dt.datetime(2026, 11, 3, tzinfo=dt.UTC), "contract_sha256": "a" * 64,
            "instance_class": "dms.t3.medium", "checks": rows}  # fmt: skip


def test_the_nine_checks_in_their_fixed_order_lob_sizes_first():
    assert CHECK_ORDER == (
        "lob_sizes", "aurora_cluster", "aurora_parameters", "secrets", "instance_class_offered",
        "dms_service_roles", "source_cdc", "dms_certificates", "secrets_manager_reachable",
    )  # fmt: skip


def test_a_preflight_written_before_lob_sizes_still_loads():
    # #2123 added lob_sizes; a saved preflight.json has the eight earlier checks.
    from src.contracts.dms_preflight import LEGACY_CHECK_ORDER

    rows = [{"id": c, "status": "pass", "summary": "ok"} for c in LEGACY_CHECK_ORDER]
    old = DmsPreflight.model_validate({**_result("pass", rows), "contract_version": "1.0"})
    assert [c.id for c in old.checks] == list(CHECK_ORDER[1:])
    with pytest.raises(ValueError, match="one row per check"):
        DmsPreflight.model_validate(_result("pass", rows[1:]))


def test_a_full_pass_loads():
    p = DmsPreflight.model_validate(_result("pass", _rows(*["pass"] * N)))
    assert p.contract_version == current_version(DmsPreflight)
    assert [c.id for c in p.checks] == list(CHECK_ORDER)


def test_status_must_agree_with_the_rows():
    with pytest.raises(ValueError, match="status"):
        DmsPreflight.model_validate(_result("pass", _rows("pass", "fail", *["not_run"] * (N - 2))))
    with pytest.raises(ValueError, match="status"):
        DmsPreflight.model_validate(_result("pass", _rows(*["pass"] * (N - 1), "confirm")))
    with pytest.raises(ValueError, match="status"):
        DmsPreflight.model_validate(_result("fail", _rows(*["pass"] * N)))
    assert DmsPreflight.model_validate(
        _result("needs_confirmation", _rows(*["pass"] * (N - 1), "confirm"))
    )
    assert DmsPreflight.model_validate(
        _result("fail", _rows("pass", "fail", *["not_run"] * (N - 2)))
    )


def test_rows_after_a_failure_are_not_run_and_order_is_fixed():
    with pytest.raises(ValueError, match="not_run"):
        DmsPreflight.model_validate(_result("fail", _rows("fail", "pass", *["not_run"] * (N - 2))))
    with pytest.raises(ValueError, match="not_run"):
        DmsPreflight.model_validate(_result("pass", _rows("pass", "not_run", *["pass"] * (N - 2))))
    rows = _rows(*["pass"] * N)
    rows[0], rows[1] = rows[1], rows[0]
    with pytest.raises(ValueError, match="order"):
        DmsPreflight.model_validate(_result("pass", rows))


def test_every_row_is_always_present():
    with pytest.raises(ValueError, match="order"):
        DmsPreflight.model_validate(_result("pass", _rows(*["pass"] * N)[:-1]))


def test_the_path_is_registered():
    assert contract_for_artifact("shop/job1/data-movement/step-1/preflight.json") is DmsPreflight
