# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routine that needed attention only because it calls a routine the repair loop repaired
is carried after it (#2428, ADR-034).

The repair seam's synthetic PostgreSQL 9.6 job (``repair_fixtures``) with callers of
``public.wal_pos``, which a model repaired: ``uses_wal`` calls it and ``top`` calls
``uses_wal`` (a chain, carried in deploy order after the repair); ``own_bad`` calls it and
fails a check of its own (stays not carried); ``cyc_a`` and ``cyc_b`` call each other and
``cyc_a`` calls it (a cycle, stays not carried); ``uses_own_bad`` calls ``own_bad``. The rule
is deterministic and the same in the facts, step 1's section, the deploy gate and the test:
a routine carried this way is checked, not tested, until ``/test-routines`` runs it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.collector.routine_sources import routine_sources_digest
from src.contracts.routine_migration import RoutineProofEntry
from src.contracts.synthesis_output import RoutineFacts
from src.report.routines import routines_md
from src.shared.routine_facts import (
    NOT_TESTED_STATE,
    TEST_COMMAND,
    read_routine_facts,
    repair_sha256,
    repaired_routines,
    with_repairs,
)
from src.storage.routine_repair_store import read_routine_repair
from src.tools.data_movement.routine_scripts import DEPLOY_FILE, write_routine_scripts
from src.tools.data_movement.routine_test_gate import deploy_gate
from src.tools.routines.migration import read_routine_migration, routine_migration_path
from src.tools.routines.proof_run import apply_proof, plan_proof
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.tools.routines import repaired_fixtures as rf

USES = "public.uses_wal(integer)"
TOP = "public.top(integer)"
OWN_BAD = "public.own_bad(integer)"
USES_OWN_BAD = "public.uses_own_bad(integer)"
CYC_A, CYC_B = "public.cyc_a(integer)", "public.cyc_b(integer)"
SHA = "d" * 64


def _caller(name: str, body: str, calls: list[str]) -> dict[str, Any]:
    create = (
        f"CREATE OR REPLACE FUNCTION public.{name}(p integer)\n RETURNS text\n LANGUAGE sql\n"
        f"AS $function${body}$function$\n"
    )
    return {**fx._function(name, create), "depends_on": [{"kind": "function", "name": c}
                                                          for c in calls]}  # fmt: skip


def chain_collector() -> dict[str, Any]:
    col = fx.collector()
    col["database_schema"]["procedures"] += [
        _caller("uses_wal", "SELECT public.wal_pos(p)", [fx.WAL]),
        _caller("top", "SELECT public.uses_wal(p)", [USES]),
        _caller("own_bad", "SELECT public.wal_pos(p) || pg_current_xlog_location()::text",
                [fx.WAL]),
        _caller("uses_own_bad", "SELECT public.own_bad(p)", [OWN_BAD]),
        _caller("cyc_a", "SELECT public.cyc_b(p) || public.wal_pos(p)", [CYC_B, fx.WAL]),
        _caller("cyc_b", "SELECT public.cyc_a(p)", [CYC_A]),
    ]  # fmt: skip
    return col


def chain_job(tmp_path: Path, *, repaired: bool = True) -> Any:
    store = fx.job(tmp_path, col=chain_collector(), hide=True)
    if repaired:
        rf.write_repair(store)
    return store


def _rows(store: Any) -> list[dict[str, Any]]:
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    return [r.model_dump(mode="json") for r in artifact.routines]


def _overlay(store: Any) -> dict[str, dict[str, Any]]:
    repaired = repaired_routines(read_routine_repair(store, rf.DB, rf.JOB, 1), 1)
    return {r["routine_id"]: r for r in with_repairs(_rows(store), repaired)}


def _mark_tested(store: Any, outcomes: dict[str, str] | None = None) -> None:
    """The artifact as a test on a test Aurora leaves it: every routine the test runs
    (carried, the repair, the routines carried after it) with ``proved`` unless
    ``outcomes`` says otherwise; the repair's entry names the repair the test ran."""
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    entries = {
        r.routine_id: RoutineProofEntry(
            mode="side_by_side", outcome=(outcomes or {}).get(r.routine_id, "proved"),  # type: ignore[arg-type]
            reason="the test's result", calls_run=2, calls_succeeded=2,
        )  # fmt: skip
        for r in artifact.routines
    }
    done = apply_proof(
        artifact, entries, target="an Aurora PostgreSQL 16 test cluster", source=None,
        strict_check="not_available", seconds=1.0,
        sources_digest=routine_sources_digest(store, rf.DB, rf.JOB),
        repaired={rf.WAL_KEY: repair_sha256(fx.WAL_REPAIRED)},
    )  # fmt: skip
    store.write_json(routine_migration_path(rf.DB, rf.JOB, 1), done.model_dump(mode="json"))


def _refused(scripts: Any) -> dict[str, str]:
    return {i: c for _, i, c in scripts.refused}


# --------------------------------------------------------------------------- the rule


def test_a_chain_of_callers_of_a_repaired_routine_is_carried_after_it(tmp_path):
    rows = _overlay(chain_job(tmp_path))
    assert rows[fx.WAL]["status"] == "model_repaired"
    assert rows[USES]["status"] == "rewritten" and rows[TOP]["status"] == "rewritten"
    assert rows[USES]["carried_after_repair"] == [{"kind": "function", "routine_id": fx.WAL}]
    assert rows[TOP]["carried_after_repair"] == [{"kind": "function", "routine_id": USES}]
    assert rows[USES]["reasons"] == [] and rows[USES]["blocked_by"] == []
    order = {rid: r["deploy_order"] for rid, r in rows.items()}
    assert order[fx.WAL] < order[USES] < order[TOP]


def test_a_caller_with_a_failure_of_its_own_stays_not_carried(tmp_path):
    rows = _overlay(chain_job(tmp_path))
    assert rows[USES]["status"] == "rewritten"  # the same repair carries a clean caller
    assert rows[OWN_BAD]["status"] == "needs_attention"
    assert "postgresql-routine-v10-renamed-functions" in rows[OWN_BAD]["reasons"]
    assert "carried_after_repair" not in rows[OWN_BAD]
    # What calls it waits for it, so it stays not carried too.
    assert rows[USES_OWN_BAD]["status"] == "needs_attention"


def test_a_cycle_through_a_repaired_routine_stays_not_carried(tmp_path):
    rows = _overlay(chain_job(tmp_path))
    assert rows[USES]["status"] == "rewritten"  # the same repair carries a clean caller
    for rid in (CYC_A, CYC_B):
        assert rows[rid]["status"] == "needs_attention"
        assert "dependency_cycle" in rows[rid]["reasons"]
        assert "carried_after_repair" not in rows[rid]


def test_two_routines_that_wait_only_for_each_other_are_never_carried():
    """No cycle recorded, each blocked by the other only: the fixed point carries neither."""

    def row(rid: str, other: str) -> dict[str, Any]:
        return {"kind": "function", "routine_id": rid, "status": "needs_attention",
                "reasons": ["depends_on_needs_attention"], "source_text": "exact",
                "cycle": None, "blocked_by": [{"kind": "function", "routine_id": other}]}  # fmt: skip

    repaired = {"kind": "function", "routine_id": "z", "status": "needs_attention",
                "reasons": ["some-rule"], "source_text": "exact", "cycle": None}  # fmt: skip
    rows = [row("x", "y"), row("y", "x"), row("w", "z"), repaired]
    out = with_repairs(rows, {("function", "z"): "a" * 64})
    assert [r["status"] for r in out] == ["needs_attention", "needs_attention", "as_is",
                                          "model_repaired"]  # fmt: skip


def test_a_caller_of_a_routine_whose_repair_failed_stays_not_carried(tmp_path):
    from tests.unit.tools.routines.test_repaired_routine_deploy import _repair_gone

    store = chain_job(tmp_path)
    assert _overlay(store)[USES]["status"] == "rewritten"  # while the repair stands
    _repair_gone(store, "needs_attention")  # every attempt failed
    rows = _overlay(store)
    assert rows[fx.WAL]["status"] == "needs_attention"
    assert rows[USES]["status"] == "needs_attention" and rows[TOP]["status"] == "needs_attention"
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None and "carried_after_repair" not in facts
    assert {r["routine_id"] for r in facts["to_do_routines"]} >= {USES, TOP}


def test_a_caller_of_a_repaired_routine_that_still_waits_for_another_stays(tmp_path):
    """own_bad repaired, wal_pos not: own_bad still waits for wal_pos, so what calls it is
    not carried after it."""
    rows = with_repairs(_rows(chain_job(tmp_path, repaired=False)),
                        {("function", OWN_BAD): "a" * 64})  # fmt: skip
    by_id = {r["routine_id"]: r for r in rows}
    assert by_id[USES_OWN_BAD]["status"] == "needs_attention"
    assert by_id[USES]["status"] == "needs_attention"
    # Both repaired: own_bad waits for nothing, so its caller is carried after it.
    both = {("function", OWN_BAD): "a" * 64, rf.WAL_KEY: "b" * 64}
    by_id = {r["routine_id"]: r for r in with_repairs(_rows(chain_job(tmp_path / "b",
                                                                      repaired=False)), both)}  # fmt: skip
    assert by_id[USES_OWN_BAD]["carried_after_repair"] == [
        {"kind": "function", "routine_id": OWN_BAD}
    ]


# --------------------------------------------------------------------------- step 1


def test_step_1_carries_it_in_deploy_order_after_its_callee(tmp_path):
    section = rf.contract(chain_job(tmp_path)).routines
    assert section is not None
    carried = {r.routine_id: r for r in section.routines if r.carry == "carried"}
    assert carried[USES].carried_after_repair and carried[TOP].carried_after_repair
    assert carried[USES].migration_status == "rewritten"
    assert not carried[fx.WAL].carried_after_repair and carried[fx.WAL].repaired
    assert carried[fx.WAL].order < carried[USES].order < carried[TOP].order  # type: ignore[operator]
    assert {OWN_BAD, USES_OWN_BAD, CYC_A, CYC_B}.isdisjoint(carried)
    # Without the repair, nothing is carried after it.
    plain = rf.contract(chain_job(tmp_path / "plain", repaired=False)).routines
    assert plain is not None
    assert {r.routine_id for r in plain.routines if r.carry == "carried"} == {rf.OK}


def test_before_a_test_it_is_held_back_as_not_tested(tmp_path):
    store = chain_job(tmp_path)
    gate = deploy_gate(store, rf.DB, rf.JOB, read_routine_migration(store, rf.DB, rf.JOB, 1))
    assert gate.held[("function", USES)] == "not_tested"
    assert gate.held[("function", TOP)] == "not_tested"


def test_after_a_test_it_is_deployed_after_its_callee_and_the_file_says_so(tmp_path):
    store = chain_job(tmp_path)
    _mark_tested(store)
    scripts = write_routine_scripts(store, rf.DB, rf.JOB, rf.contract(store), SHA,
                                    tmp_path / "step")  # fmt: skip
    assert _refused(scripts) == {} and scripts.after_repair == 2
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert text.index("FUNCTION public.wal_pos") < text.index("FUNCTION public.uses_wal")
    assert text.index("FUNCTION public.uses_wal") < text.index("FUNCTION public.top")
    assert "-- 2 of the carried routines here were carried after a repair" in text
    assert f"function {USES} (carried after a repair of a routine it calls)" in text
    # routines.md says it under the carried count, never next to the routines not carried.
    md = (tmp_path / "step" / "routines.md").read_text().splitlines()
    (at,) = [n for n, line in enumerate(md) if "carried after a repair" in line]
    assert md[at].startswith("  - 2 of the carried routines here were carried after a repair")
    assert md[at - 1].startswith(f"- Carried by `{DEPLOY_FILE}`")
    assert md[at + 1].startswith("- Not carried:")
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    assert [(i["routine_id"], i["deploy_gate"]) for i in facts["carried_after_repair"]] == [
        (USES, "deployed"), (TOP, "deployed")
    ]  # fmt: skip


@pytest.mark.parametrize("how", ["deleted", "needs_attention"])
def test_a_contract_written_before_the_repair_failed_holds_what_was_carried_after_it(tmp_path, how):
    """Amendment 2's case for the caller: the saved contract carries uses_wal and top after
    wal_pos's repair, which is gone now. Neither is deployed; both are held as
    depends_on_held, and the repaired routine as rebuild_differs."""
    from tests.unit.tools.routines.test_repaired_routine_deploy import _repair_gone

    store = chain_job(tmp_path)
    _mark_tested(store)
    contract = rf.contract(store)  # written while the repair stood
    section = contract.routines
    assert section is not None
    assert {r.routine_id for r in section.routines if r.carried_after_repair} == {USES, TOP}
    _repair_gone(store, how)
    scripts = write_routine_scripts(store, rf.DB, rf.JOB, contract, SHA, tmp_path / "step")
    assert _refused(scripts) == {fx.WAL: "rebuild_differs", USES: "depends_on_held",
                                 TOP: "depends_on_held"}  # fmt: skip
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "uses_wal" not in text and "public.top" not in text


def test_while_its_repaired_callee_is_held_it_is_held_as_depends_on_held(tmp_path):
    store = chain_job(tmp_path)
    _mark_tested(store, {fx.WAL: "not_proved"})
    scripts = write_routine_scripts(store, rf.DB, rf.JOB, rf.contract(store), SHA,
                                    tmp_path / "step")  # fmt: skip
    assert _refused(scripts) == {fx.WAL: "not_proved", USES: "depends_on_held",
                                 TOP: "depends_on_held"}  # fmt: skip
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    uses, _top = facts["carried_after_repair"]
    assert uses["deploy_gate"] == "depends_on_held"
    assert "it calls a routine that is held back" in uses["line"]


# --------------------------------------------------------------------------- the test


def test_the_test_runs_it_with_its_repaired_callee(tmp_path):
    store = chain_job(tmp_path)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    from src.agents.collector.routine_sources import read_routine_sources

    collector = store.read_json(f"{rf.DB}/{rf.JOB}/collector/output.json")
    plan = plan_proof(artifact, read_routine_sources(store, rf.DB, rf.JOB), collector,
                      target_user="runner", repaired={rf.WAL_KEY: rf.REBUILT})  # fmt: skip
    planned = {r.routine_id: r for r in plan.routines}
    assert {fx.WAL, USES, TOP} <= planned.keys()
    assert fx.WAL in planned[USES].depends_on and USES in planned[TOP].depends_on
    assert {OWN_BAD, CYC_A, CYC_B, USES_OWN_BAD}.isdisjoint(planned)
    # A repair that does not rebuild: neither it nor what is carried after it is tested.
    plan = plan_proof(artifact, read_routine_sources(store, rf.DB, rf.JOB), collector,
                      target_user="runner", repaired={rf.WAL_KEY: None})  # fmt: skip
    assert {fx.WAL, USES, TOP} <= plan.not_tested.keys()
    assert {fx.WAL, USES, TOP}.isdisjoint(r.routine_id for r in plan.routines)
    # Each says why: uses_wal's callee is the repair; top's callee was not repaired itself.
    assert plan.not_tested[USES] == "a routine it calls that was repaired is not tested"
    assert plan.not_tested[TOP] == "a routine it calls is not tested"


def test_the_test_matches_what_it_waited_for_by_kind_and_id(tmp_path):
    """A procedure with the repaired function's id (MySQL lets a function and a procedure
    share a name): uses_wal waits for the procedure, which has no text to test, so uses_wal
    is not tested, though a function of that id is."""
    from src.agents.collector.routine_sources import read_routine_sources
    from src.contracts.routine_migration import RoutineRef

    store = chain_job(tmp_path)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    (wal,) = [r for r in artifact.routines if r.routine_id == fx.WAL]
    proc = wal.model_copy(update={"kind": "procedure", "status": "as_is", "reasons": [],
                                  "specific_name": "no_source"})  # fmt: skip
    ref = RoutineRef(kind="procedure", routine_id=fx.WAL)
    rows = [r.model_copy(update={"blocked_by": [ref]}) if r.routine_id == USES else r
            for r in artifact.routines]  # fmt: skip
    changed = artifact.model_copy(update={"routines": [proc, *rows]})
    collector = store.read_json(f"{rf.DB}/{rf.JOB}/collector/output.json")
    plan = plan_proof(changed, read_routine_sources(store, rf.DB, rf.JOB), collector,
                      target_user="runner", repaired={rf.WAL_KEY: rf.REBUILT})  # fmt: skip
    assert fx.WAL in {r.routine_id for r in plan.routines}  # the function is tested
    assert plan.not_tested[USES] == "a routine it calls is not tested"


# --------------------------------------------------------------------------- deliverables


def test_the_deliverables_list_it_as_checked_not_tested(tmp_path):
    store = chain_job(tmp_path)
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    RoutineFacts.model_validate(facts)
    items = facts["carried_after_repair"]
    assert facts["carried_after_repair_count"] == 2
    assert [i["routine_id"] for i in items] == [USES, TOP]
    assert items[0]["after"] == [{"kind": "function", "routine_id": fx.WAL}]
    for item in items:
        assert item["deploy_gate"] == "not_tested"
        assert NOT_TESTED_STATE in item["line"] and TEST_COMMAND in item["line"]
        assert "tested on a test Aurora" not in item["line"].replace("not tested", "")
    assert {USES, TOP}.isdisjoint(r["routine_id"] for r in facts["to_do_routines"])
    assert OWN_BAD in {r["routine_id"] for r in facts["to_do_routines"]}
    from src.report.escaping import md_text
    from tests.unit.report.test_wave_readiness_deliverables import _report

    report = _report("MEDIUM")
    report["facts"]["routines"] = facts
    md = "\n".join(routines_md(report))
    assert "### Carried after a repair" in md
    assert f"function `{USES}`, after function `{fx.WAL}`: {md_text(items[0]['line'])}" in md


UI_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "src/ui/src/components/__tests__/fixtures/routineAfterRepair.json"
)


def _chain_facts(tmp_path: Path) -> dict[str, Any]:
    facts = read_routine_facts(chain_job(tmp_path), rf.DB, rf.JOB, 1)
    assert facts is not None
    return facts


def test_the_ui_fixture_is_the_facts_the_python_deliverables_print(tmp_path):
    """RoutineMigrationSection.test.js renders this file: the same facts as here."""
    import json

    assert json.loads(UI_FIXTURE.read_text(encoding="utf-8")) == _chain_facts(tmp_path)


def test_test_routines_runs_it_on_the_cluster_and_then_step_1_deploys_it(tmp_path):
    """The whole test (#1945, a fake Data API): the repair and what is carried after it run
    together; the output names it carried after a repair, and the deploy gate holds none."""
    from tests.unit.tools.routines import test_repaired_routine_proof as rp
    from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets

    calls = {**rp.CALLS, '"public"."uses_wal"': [1], '"public"."top"': [1]}
    store = chain_job(tmp_path)
    out = rp._prove(store, FakeDataApi(secrets=FakeSecrets(), calls=calls),
                    FakeDataApi(calls=calls))  # fmt: skip
    by_id = {r["routine_id"]: r for r in out["routines"]}
    assert by_id[USES]["status"] == "rewritten" and by_id[USES]["carried_after_repair"]
    assert by_id[USES]["outcome"] == "proved" and by_id[TOP]["outcome"] == "proved"
    assert "carried_after_repair" not in by_id[fx.WAL]
    assert {OWN_BAD, CYC_A, CYC_B, USES_OWN_BAD}.isdisjoint(by_id)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    held = deploy_gate(store, rf.DB, rf.JOB, artifact).held
    assert ("function", USES) not in held and ("function", TOP) not in held
