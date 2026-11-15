# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine proof on the test cluster (#1945): which routines go side by side, which load
and run, what lands in the routine-migration artifact and the routine facts.

Every Data API call goes to :class:`FakeDataApi`. The modules under test are imported inside
each test, so on a branch without them a test fails in its body."""

from __future__ import annotations

import importlib
import time
from typing import Any

import pytest

from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from tests.unit.tools.routines.fake_data_api import FakeDataApi

ARNS = {"cluster_arn": "arn:aws:rds:us-east-1:111122223333:cluster:c",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s"}  # fmt: skip  # pragma: allowlist secret
TABLES = [{"table_id": "public.t", "table_name": "t", "row_count": 3, "primary_key": ["id"],
           "columns": [{"column_name": "id", "data_type": "integer", "nullable": False}]}]  # fmt: skip
TEXT = {
    "f": "CREATE OR REPLACE FUNCTION public.f(p integer)\n RETURNS integer\n LANGUAGE sql\nAS $$ SELECT 1 $$",
    "h": "CREATE OR REPLACE FUNCTION public.h(p integer)\n RETURNS integer\n LANGUAGE sql\nAS $$ SELECT 2 $$",
}


def _mod(name: str) -> Any:
    """The module under test, imported here so a branch without it fails in the test."""
    if name == "proof_run":
        return importlib.import_module("src.tools.routines.proof_run")
    return importlib.import_module("src.tools.routines.data_api_target")


def _row(name: str, status: str, order: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "routine_id": f"public.{name}(integer)", "kind": "function", "schema_name": "public",
        "name": name, "specific_name": f"{name}_1", "signature": "integer", "returns": "integer",
        "source_text": "exact", "status": status, "deploy_order": order,
        "dependencies": [{"kind": "table", "name": "public.t", "found": True}],
        "dependencies_complete": True, "rules_passed": 3,
    }  # fmt: skip
    if status == "rewritten":
        row["rewrites"] = [{"rule_id": "pg-owner", "rewrite": "owner"}]
    if status == "needs_attention":
        row["reasons"] = ["dependency_cycle"]
    return row


def _migration(*rows: dict[str, Any]) -> RoutineMigrationContract:
    counts = {
        s: sum(r["status"] == s for r in rows)
        for s in ("as_is", "rewritten", "model_repaired", "needs_attention")
    }
    return RoutineMigrationContract(job_id="job1", database_name="shop", assignment_version=2,
                                    source_engine="postgresql", source_version="16.4",
                                    target_engine="aurora_postgresql", target_line="16",
                                    target_user="postgres", routine_sources=True, total=len(rows),
                                    counts=counts, routines=list(rows))  # type: ignore[arg-type]  # fmt: skip


def _sources() -> list[RoutineSource]:
    return [RoutineSource(kind="function", routine_id=n, schema_name="public", specific_name=f"{n}_1",
                          create_statement=t, origin="pg_get_functiondef") for n, t in TEXT.items()]  # fmt: skip


def _target(client: FakeDataApi, **kw: Any) -> Any:
    return _mod("data_api_target").data_api_target(client, **ARNS, engine="postgresql", version="16.15",
                                                   job_id="job1", admin_database="routine_proof", **kw)  # fmt: skip


class _Spy:
    def __init__(self, factory: Any) -> None:
        self.factory, self.opened = factory, 0

    def __call__(self) -> Any:
        self.opened += 1
        return self.factory()


def test_as_is_routines_load_and_run_and_rewritten_ones_are_compared_side_by_side():
    pr = _mod("proof_run")
    migration = _migration(
        _row("f", "as_is", 1), _row("h", "rewritten", 2), _row("x", "needs_attention", 3)
    )
    plan = pr.plan_proof(migration, _sources(), {}, target_user="proofadmin")
    assert plan.load_and_run == {"public.f(integer)"} and plan.side_by_side == {"public.h(integer)"}
    target_api = FakeDataApi(calls={'"public"."f"': [1], '"public"."h"': [2]})
    source = _Spy(_target(FakeDataApi(calls={'"public"."h"': [2]})))
    cleaned: list[bool] = []
    entries = pr.prove_routines(plan, TABLES, target=_target(target_api), source=source,
                                cleanup=lambda: cleaned.append(True), strict={}, strict_check=["not_available"])  # fmt: skip
    assert set(entries) == {"public.f(integer)", "public.h(integer)"}  # not the one not carried
    f, h = entries["public.f(integer)"], entries["public.h(integer)"]
    assert (f.mode, f.outcome) == ("load_and_run", "proved") and f.calls_succeeded >= 1
    assert (h.mode, h.outcome) == ("side_by_side", "proved") and "no differences found" in h.reason
    assert source.opened >= 1 and cleaned == [True] and not target_api.databases
    assert f.strict_check == "not_available"

    only_as_is = pr.plan_proof(
        _migration(_row("f", "as_is", 1)), _sources(), {}, target_user="proofadmin"
    )
    never = _Spy(_target(FakeDataApi()))
    pr.prove_routines(only_as_is, TABLES, target=_target(FakeDataApi(calls={'"public"."f"': [1]})),
                      source=never, cleanup=lambda: None, strict={}, strict_check=[])  # fmt: skip
    assert never.opened == 0  # a routine carried as it is runs on the test cluster only


def test_a_side_by_side_difference_needs_attention():
    pr = _mod("proof_run")
    plan = pr.plan_proof(
        _migration(_row("h", "rewritten", 1)), _sources(), {}, target_user="proofadmin"
    )
    entries = pr.prove_routines(plan, TABLES, target=_target(FakeDataApi(calls={'"public"."h"': [3]})),
                                source=_target(FakeDataApi(calls={'"public"."h"': [2]})),
                                cleanup=lambda: None, strict={}, strict_check=[])  # fmt: skip
    h = entries["public.h(integer)"]
    assert (h.outcome, h.mode) == ("needs_attention", "side_by_side") and h.mismatches >= 1


def test_the_job_scratch_databases_are_swept_even_when_the_proof_fails(monkeypatch):
    pr = _mod("proof_run")
    plan = pr.plan_proof(
        _migration(_row("h", "rewritten", 1)), _sources(), {}, target_user="proofadmin"
    )

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the comparison fell over")

    monkeypatch.setattr(pr, "compare_routines", boom)
    cleaned: list[bool] = []
    with pytest.raises(RuntimeError):
        pr.prove_routines(plan, TABLES, target=_target(FakeDataApi()), source=_target(FakeDataApi()),
                          cleanup=lambda: cleaned.append(True), strict={}, strict_check=[])  # fmt: skip
    assert cleaned == [True]


def test_a_strict_check_error_makes_a_routine_need_attention():
    pr = _mod("proof_run")
    plan = pr.plan_proof(
        _migration(_row("f", "as_is", 1)), _sources(), {}, target_user="proofadmin"
    )
    strict: dict[str, list[tuple[str, str]]] = {}
    ran: list[str] = []
    api = FakeDataApi(calls={'"public"."f"': [1]}, plpgsql_check=True,
                      strict={"public.f(integer)": [("error", "42703")]})  # fmt: skip
    entries = pr.prove_routines(plan, TABLES, target=_target(api, strict=strict, strict_check=ran),
                                source=None, cleanup=lambda: None, strict=strict, strict_check=ran)  # fmt: skip
    f = entries["public.f(integer)"]
    assert (f.outcome, f.strict_check, f.strict_check_errors) == (
        "needs_attention",
        "failed",
        ["42703"],
    )
    assert f.reason_code == "strict_check_failed"


def test_the_proof_lands_in_the_artifact_and_the_routine_facts():
    pr = _mod("proof_run")
    from src.shared.routine_facts import routine_facts

    migration = _migration(
        _row("f", "as_is", 1), _row("h", "rewritten", 2), _row("x", "needs_attention", 3)
    )
    assert routine_facts(migration, 2)["proof"] == "checked, not load-tested"
    entries = {
        "public.f(integer)": pr.RoutineProofEntry(mode="load_and_run", outcome="proved", reason="ran"),
        "public.h(integer)": pr.RoutineProofEntry(mode="side_by_side", outcome="needs_attention",
                                                  reason="different", mismatches=2),
    }  # fmt: skip
    written = pr.apply_proof(migration, entries, target="an Aurora PostgreSQL 16.15 test cluster",
                             source="a disposable postgresql 16 engine", strict_check="not_available",
                             started=time.monotonic())  # fmt: skip
    again = RoutineMigrationContract.model_validate(written.model_dump(mode="json"))
    assert again.proof_run is not None and again.proof_run.counts == {
        "proved": 1,
        "needs_attention": 1,
        "not_proved": 0,
    }
    assert [r.proof is not None for r in again.routines] == [True, True, False]
    facts = routine_facts(again, 2)
    assert facts["proof"] == "load-tested on a test Aurora"
    assert facts["proof_counts"] == {"proved": 1, "needs_attention": 1, "not_proved": 0}
    assert "On a test Aurora, of 2 carried routines: 1 loaded and ran" in facts["line"]
    assert (
        "1 needs attention" in facts["line"]
        and "load-tested" not in facts["line"].split("On a test")[0]
    )
    old = migration.model_dump(mode="json")
    old.pop("proof_run")
    for r in old["routines"]:
        r.pop("proof")
    assert (
        RoutineMigrationContract.model_validate(old).proof_run is None
    )  # a saved artifact still loads


def test_a_trigger_function_takes_its_triggers_outcome():
    pr = _mod("proof_run")
    entries = {"public.tf()": pr.RoutineProofEntry(mode="side_by_side", outcome="not_proved",
                                                    reason="no call", reason_code="no_calls"),
               "public.t.trg": pr.RoutineProofEntry(mode="side_by_side", outcome="proved", reason="same")}  # fmt: skip
    merged = pr._inherit(entries, {"public.tf()": "public.t.trg"})
    assert merged["public.tf()"].outcome == "proved"
    assert "through its trigger public.t.trg" in merged["public.tf()"].reason
