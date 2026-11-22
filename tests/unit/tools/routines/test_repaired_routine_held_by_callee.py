# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A repaired routine whose callee is not carried is held by that callee (#2566, ADR-034).

The repair seam's synthetic PostgreSQL 9.6 job with callers of ``public.wal_pos``, which
fails a check of its own: ``own_bad`` fails one too and calls ``wal_pos``; ``bad_top`` fails
one and calls ``own_bad`` (a chain); ``cyc_x`` and ``cyc_y`` each fail one and call each
other (a cycle). A repair of a routine whose callee is not carried does not carry it: the
facts, the deliverables, the deploy gate, the test and step 1 hold it with the same
reason, ``depends_on_needs_attention``, and the callees it still waits for. A repaired
routine whose callees are carried is carried as before (#2565)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.contracts.routine_repair import RoutineRepairContract
from src.contracts.synthesis_output import RoutineFacts
from src.report.routines import routines_md
from src.shared.routine_facts import read_routine_facts, repaired_routines, with_repairs
from src.storage.routine_repair_store import read_routine_repair, routine_repair_path
from src.tools.data_movement.routine_test_gate import deploy_gate
from src.tools.routines.migration import read_routine_migration
from src.tools.routines.proof_run import plan_proof
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.tools.routines import repaired_fixtures as rf
from tests.unit.tools.routines.test_carried_after_repair import _caller

OWN_BAD = "public.own_bad(integer)"
USES_OWN_BAD = "public.uses_own_bad(integer)"
BAD_TOP = "public.bad_top(integer)"
CYC_X, CYC_Y = "public.cyc_x(integer)", "public.cyc_y(integer)"
OLD = "pg_current_xlog_location()::text"
HELD = "depends_on_needs_attention"


def _collector() -> dict[str, Any]:
    col = fx.collector()
    col["database_schema"]["procedures"] += [
        _caller("own_bad", f"SELECT public.wal_pos(p) || {OLD}", [fx.WAL]),
        _caller("uses_own_bad", "SELECT public.own_bad(p)", [OWN_BAD]),
        _caller("bad_top", f"SELECT public.own_bad(p) || {OLD}", [OWN_BAD]),
        _caller("cyc_x", f"SELECT public.cyc_y(p) || {OLD}", [CYC_Y]),
        _caller("cyc_y", f"SELECT public.cyc_x(p) || {OLD}", [CYC_X]),
    ]  # fmt: skip
    return col


def _repair(store: Any, ids: list[str]) -> None:
    """``routine_repair.json`` with each of ``ids`` carried by the loop (``model_repaired``)."""
    rows = [
        {
            "kind": "function", "routine_id": rid, "state": "model_repaired",
            "reasons": ["postgresql-version-renamed-function"],
            "repaired_create": f"-- repaired {rid}\n",
            "change_note": "Calls the name PostgreSQL 10 gave the function.", "diff": "-a\n+b",
            "proof_source": "a disposable postgresql 9 engine",
            "attempts": [{"attempt": 1, "stage": "proof", "outcome": "proved"}],
        }  # fmt: skip
        for rid in ids
    ]
    counts = dict.fromkeys(
        ("repaired_pending_proof", "refused", "no_answer", "not_repaired", "model_repaired",
         "needs_attention"), 0)  # fmt: skip
    counts["model_repaired"] = len(rows)
    data = RoutineRepairContract.model_validate({
        "job_id": rf.JOB, "database_name": rf.DB, "assignment_version": 1,
        "llm_mode": "bedrock", "source_engine": "postgresql",
        "target_engine": "aurora_postgresql", "proof": "local", "rounds": 1,
        "counts": counts, "routines": rows,
    }).model_dump(mode="json")  # fmt: skip
    store.write_json(routine_repair_path(rf.DB, rf.JOB, 1), data)


def _job(tmp_path: Path, repaired: list[str]) -> Any:
    store = fx.job(tmp_path, col=_collector(), hide=True)
    _repair(store, repaired)
    return store


def _overlay(store: Any) -> dict[str, dict[str, Any]]:
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    repaired = repaired_routines(read_routine_repair(store, rf.DB, rf.JOB, 1), 1)
    rows = [r.model_dump(mode="json") for r in artifact.routines]
    return {r["routine_id"]: r for r in with_repairs(rows, repaired)}


def _facts(store: Any) -> dict[str, Any]:
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    RoutineFacts.model_validate(facts)
    return facts


def _step1(store: Any) -> dict[str, Any]:
    section = rf.contract(store).routines
    assert section is not None
    return {r.routine_id: r for r in section.routines}


def _refs(*ids: str) -> list[dict[str, str]]:
    return [{"kind": "function", "routine_id": i} for i in ids]


def _held_everywhere(store: Any, held: dict[str, list[str]]) -> None:
    """Each routine in ``held`` is held by the callees it lists: not carried in the facts
    (no category counts it as carried, its row names the reason and the callees), in step 1
    (the same reason), in the deploy gate and in the test's plan."""
    overlay, facts, step = _overlay(store), _facts(store), _step1(store)
    rows = {r["routine_id"]: r for r in facts["routines"]}
    gates = {i["routine_id"]: i for i in facts["interventions"]}
    for rid, callees in held.items():
        assert overlay[rid]["status"] == "needs_attention"
        assert HELD in overlay[rid]["reasons"]
        assert overlay[rid]["blocked_by"] == _refs(*callees)
        assert rows[rid]["category"] == "needs_repair"
        assert rows[rid]["blocked_by"] == _refs(*callees)
        assert step[rid].carry == "not_carried"
        assert step[rid].reasons == rows[rid]["reasons"]
        assert not step[rid].repaired
        assert gates[rid]["deploy_gate"] == HELD
        assert "not carried" in gates[rid]["line"]
    assert facts["repaired_count"] == 0
    assert set(held).isdisjoint(r["routine_id"] for r in facts.get("carried_after_repair", []))
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    gate = deploy_gate(store, rf.DB, rf.JOB, artifact)
    assert set(held).isdisjoint(rid for _, rid in gate.held)
    from src.agents.collector.routine_sources import read_routine_sources

    collector = store.read_json(f"{rf.DB}/{rf.JOB}/collector/output.json")
    plan = plan_proof(artifact, read_routine_sources(store, rf.DB, rf.JOB), collector,  # type: ignore[arg-type]
                      target_user="runner",
                      repaired={("function", rid): f"-- rebuilt {rid}\n" for rid in held})  # fmt: skip
    assert set(held).isdisjoint(r.routine_id for r in plan.routines)
    assert set(held).isdisjoint(plan.not_tested)


def test_a_repaired_routine_that_calls_an_unrepaired_one_is_held_by_it(tmp_path):
    """The issue's case: own_bad fails its own check and calls wal_pos, which no repair
    carries; own_bad's repair does not carry it, and what calls it waits too."""
    store = _job(tmp_path, [OWN_BAD])
    _held_everywhere(store, {OWN_BAD: [fx.WAL]})
    overlay = _overlay(store)
    assert overlay[OWN_BAD]["reasons"] == [HELD]  # its own failure is the repair's to fix
    assert overlay[USES_OWN_BAD]["status"] == "needs_attention"
    assert _step1(store)[USES_OWN_BAD].carry == "not_carried"
    facts = _facts(store)
    assert "repaired by a model" not in facts["executive_line"]
    assert OWN_BAD in {r["routine_id"] for r in facts["to_do_routines"]}


def test_a_chain_of_repairs_that_ends_at_an_unrepaired_routine_is_held(tmp_path):
    store = _job(tmp_path, [OWN_BAD, BAD_TOP])
    _held_everywhere(store, {OWN_BAD: [fx.WAL], BAD_TOP: [OWN_BAD]})


def test_a_cycle_of_repaired_routines_is_held(tmp_path):
    store = _job(tmp_path, [CYC_X, CYC_Y])
    _held_everywhere(store, {CYC_X: [CYC_Y], CYC_Y: [CYC_X]})
    rows = _overlay(store)
    assert rows[CYC_X]["reasons"] == ["dependency_cycle", HELD]


@pytest.mark.passes_on_main
def test_a_repaired_routine_whose_callee_is_carried_is_carried_as_before(tmp_path):
    """#2565's rule: wal_pos repaired too, so own_bad and bad_top are carried, repaired,
    and uses_own_bad after them."""
    store = _job(tmp_path, [fx.WAL, OWN_BAD, BAD_TOP])
    overlay, facts, step = _overlay(store), _facts(store), _step1(store)
    for rid in (fx.WAL, OWN_BAD, BAD_TOP):
        assert overlay[rid]["status"] == "model_repaired"
        assert step[rid].carry == "carried" and step[rid].repaired
    assert overlay[USES_OWN_BAD]["carried_after_repair"] == _refs(OWN_BAD)
    assert step[USES_OWN_BAD].carry == "carried"
    assert facts["repaired_count"] == 3
    assert {i["deploy_gate"] for i in facts["interventions"]} == {"not_tested"}
    assert "3 of the carried routines were repaired by a model" in facts["executive_line"]


def test_one_carried_repair_and_one_held_repair_count_only_the_carried_one(tmp_path):
    """wal_pos repaired (carried) and cyc_x repaired (held by cyc_y): the executive line
    counts the one carried repair, never the held one (``repaired_by_model``)."""
    store = _job(tmp_path, [fx.WAL, CYC_X])
    facts = _facts(store)
    gates = {i["routine_id"]: i["deploy_gate"] for i in facts["interventions"]}
    assert gates == {fx.WAL: "not_tested", CYC_X: HELD}
    assert facts["repaired_count"] == 1
    assert "1 of the carried routines was repaired by a model" in facts["executive_line"]
    step = _step1(store)
    assert step[fx.WAL].carry == "carried" and step[CYC_X].carry == "not_carried"


def test_the_deliverables_say_it_is_held_by_its_callee(tmp_path):
    from src.report.escaping import md_text
    from tests.unit.report.test_wave_readiness_deliverables import _report

    facts = _facts(_job(tmp_path, [OWN_BAD]))
    (item,) = [i for i in facts["interventions"] if i["routine_id"] == OWN_BAD]
    assert item["line"].startswith("A model repaired it.")
    assert "it calls a routine that still needs attention" in item["line"]
    assert facts["repaired_count"] == 0 and facts["carried_count"] == 1  # public.ok only
    report = _report("MEDIUM")
    report["facts"]["routines"] = facts
    md = "\n".join(routines_md(report))
    assert md_text(item["line"]) in md
    (row,) = [line for line in md.splitlines() if line.startswith(f"| `{OWN_BAD}`")]
    assert "It names a routine that is not translated." in row and fx.WAL in row


UI_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "src/ui/src/components/__tests__/fixtures/routineRepairHeld.json"
)


def test_the_ui_fixture_is_the_facts_the_python_deliverables_print(tmp_path):
    """RoutineMigrationSection.test.js renders this file: the same facts as here."""
    import json

    assert json.loads(UI_FIXTURE.read_text(encoding="utf-8")) == _facts(_job(tmp_path, [OWN_BAD]))
