# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 deploys a model-repaired routine only once the test on a test Aurora ran that
repair on the job's routines file and found no difference (#2384, ADR-034 Decisions 4 and 5).

The deploy script holds the repair's exact text, rebuilt in memory from the routines file and
``routine_repair.json``; no artifact holds it. Every other case holds the routine back with its
reason: not tested, not proved, a test of another repair or of a rebuild that no longer passes
its checks, a test on an earlier routines file, and artifacts written before #2384."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.data_movement.routine_scripts import DEPLOY_FILE, write_routine_scripts
from src.tools.data_movement.routine_test_gate import HELD_WHY, deploy_gate, held_routines
from src.tools.routines.migration import read_routine_migration
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.tools.routines import repaired_fixtures as rf

SHA = "c" * 64


def _write(tmp_path: Path, store):
    return write_routine_scripts(store, rf.DB, rf.JOB, rf.contract(store), SHA, tmp_path / "step")


def _refused(scripts) -> dict[str, str]:
    return {i: c for _, i, c in scripts.refused}


def _artifacts_with(tmp_path: Path, needle: str) -> list[str]:
    """Every file under the job folder that holds ``needle``, by its path."""
    found = []
    for path in tmp_path.rglob("*"):
        if path.is_file() and needle in path.read_text(errors="replace"):
            found.append(str(path.relative_to(tmp_path)))
    return sorted(found)


def test_a_repaired_routine_the_test_proved_is_deployed_with_its_rebuilt_exact_text(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    section = rf.contract(store).routines
    assert section is not None
    carried = {r.routine_id: r.migration_status for r in section.routines if r.carry == "carried"}
    assert carried == {rf.OK: "rewritten", rf.WAL: "model_repaired"}
    scripts = _write(tmp_path, store)
    assert (scripts.in_script, scripts.held, scripts.refused) == (2, 0, [])
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert rf.REBUILT.strip() in text  # the repair, with its literal back
    assert "pg_current_xlog_location" not in text
    # The rebuilt text reaches the deploy script only: no JSON artifact, log or other file.
    assert _artifacts_with(tmp_path, "pg_current_wal_lsn()::text || 'LiteralMark1940'") == [
        f"step/{DEPLOY_FILE}"
    ]


def test_a_repaired_routine_no_test_ran_on_is_held_back_as_not_tested(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store, tested=None)  # the test ran before the repair, or without it
    scripts = _write(tmp_path, store)
    assert _refused(scripts) == {rf.WAL: "not_tested"}
    assert "pg_current_wal_lsn" not in (tmp_path / "step" / DEPLOY_FILE).read_text()


@pytest.mark.parametrize("outcome", ["not_proved", "needs_attention"])
def test_a_repaired_routine_the_test_did_not_prove_is_held_back(tmp_path, outcome):
    store = rf.job(tmp_path)
    rf.mark_tested(store, {rf.WAL: outcome})
    assert _refused(_write(tmp_path, store)) == {rf.WAL: outcome}


def test_a_test_of_another_repair_holds_the_routine_back(tmp_path):
    """The repair loop ran again after the test and carried another candidate: the test
    proved a text step 1 would no longer deploy."""
    store = rf.job(tmp_path)
    rf.mark_tested(store, tested=rf.OTHER)
    scripts = _write(tmp_path, store)
    assert _refused(scripts) == {rf.WAL: "rebuild_differs"}
    assert "rebuild_differs" in HELD_WHY and "/test-routines" in HELD_WHY["rebuild_differs"]


def test_a_saved_repair_that_no_longer_passes_its_checks_is_never_deployed(tmp_path):
    """The #2387 review: step 1 never trusts routine_repair.json. A row that says
    model_repaired, with a test that says it ran it, is rebuilt and checked again here; the
    candidate still calls the renamed function, so it fails its checks and is held back."""
    store = rf.job(tmp_path)
    forged = fx.WAL_REDACTED.replace("SELECT ", "SELECT  ")  # still pg_current_xlog_location
    rf.write_repair(store, forged)
    rf.mark_tested(store, tested=forged)
    gate = deploy_gate(store, rf.DB, rf.JOB, read_routine_migration(store, rf.DB, rf.JOB, 1))
    assert gate.held == {rf.WAL_KEY: "rebuild_differs"} and not gate.texts
    assert _refused(_write(tmp_path, store)) == {rf.WAL: "rebuild_differs"}


def test_a_test_on_an_earlier_routines_file_holds_the_repair_back(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store, digest="e" * 64)
    refused = _refused(_write(tmp_path, store))
    assert refused == {rf.OK: "not_tested", rf.WAL: "not_tested"}


def test_artifacts_written_before_2384_load_and_hold_the_repair_back(tmp_path):
    """A repair result with no proof target and a test whose entries name no repair: they
    load, and the repaired routine is held back as not tested (the test ran no repair)."""
    store = rf.job(tmp_path)
    rf.mark_tested(store, tested=None)
    old = store.read_json(f"{rf.DB}/{rf.JOB}/routines/v1/routine_migration.json")
    for row in old["routines"]:
        if row.get("proof"):
            row["proof"].pop("repair_sha256", None)
    store.write_json(f"{rf.DB}/{rf.JOB}/routines/v1/routine_migration.json", old)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    assert held_routines(store, rf.DB, rf.JOB, artifact) == {rf.WAL_KEY: "not_tested"}
    # A job with no repair result at all: nothing repaired, the routine is not carried.
    plain = rf.job(tmp_path / "plain", repaired=False)
    rf.mark_tested(plain, tested=None)
    section = rf.contract(plain).routines
    assert section is not None
    assert {r.routine_id for r in section.routines if r.carry == "carried"} == {rf.OK}


def test_a_test_of_a_repair_never_deploys_the_original(tmp_path):
    """The repair is gone from routine_repair.json (the loop ran again and did not carry
    it): the test ran the repair, not the original, so nothing of it is deployed."""
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    store.delete(f"{rf.DB}/{rf.JOB}/routines/v1/routine_repair.json")
    assert held_routines(store, rf.DB, rf.JOB, artifact) == {}  # not carried: nothing held
    rows = [r.model_dump(mode="json") for r in artifact.routines]
    from src.shared.routine_facts import proof_hold

    (wal,) = [r for r in rows if r["routine_id"] == rf.WAL]
    assert proof_hold({**wal, "status": "as_is"}, True) == "rebuild_differs"


def test_the_gate_holds_the_repair_text_in_memory_only(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    gate = deploy_gate(store, rf.DB, rf.JOB, read_routine_migration(store, rf.DB, rf.JOB, 1))
    assert gate.texts == {rf.WAL_KEY: rf.REBUILT}
    assert "LiteralMark1940" not in repr(gate)
    written = json.dumps([store.read_json(k) for k in store.list_prefix(f"{rf.DB}/{rf.JOB}/")
                          if k.endswith(".json")])  # fmt: skip
    assert "pg_current_wal_lsn()::text || 'LiteralMark1940'" not in written


def test_a_mysql_repair_is_deployed_with_the_rewrites_applied_again(tmp_path):
    """The rebuilt repair goes through the same rewrites as any carried MySQL routine: its
    DEFINER is the user who runs the deploy script."""
    store = rf.mysql_job(tmp_path)
    scripts = write_routine_scripts(store, rf.DB, rf.JOB, rf.mysql_contract(store), SHA,
                                    tmp_path / "step", target_user="admin")  # fmt: skip
    assert (scripts.in_script, scripts.refused) == (1, [])
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "CREATE DEFINER=`admin`@`%` PROCEDURE `hashpw`" in text and "`app`" not in text
    assert "DECLARE rnk INT" in text and "CONCAT(p, rnk, 'salt')" in text
    assert _artifacts_with(tmp_path, "CONCAT(p, rnk, 'salt')") == [f"step/{DEPLOY_FILE}"]


def test_the_triggers_enable_script_writes_the_gates_repair_text_too(tmp_path, monkeypatch):
    """A MySQL trigger the repair loop carried is created by the triggers-enable script from
    the text the deploy gate rebuilt, with the rewrites applied again; never its original."""
    from src.tools.data_movement import routine_test_gate as gate_module
    from src.tools.data_movement.triggers_scripts import TRIGGERS_FILE
    from tests.unit.tools.data_movement import test_triggers_scripts as ts

    store, contract = ts._mysql_case(tmp_path)
    key = ("trigger", "shop.orders_bi")
    repaired = ("CREATE DEFINER=`app`@`%` TRIGGER `orders_bi` BEFORE INSERT ON `orders` "
                "FOR EACH ROW SET NEW.note = 'repaired-2384'")  # fmt: skip
    real = gate_module.deploy_gate

    def gate(*args, **kwargs):
        done = real(*args, **kwargs)
        done.repaired.add(key)
        done.texts[key] = repaired
        return done

    monkeypatch.setattr(gate_module, "deploy_gate", gate)
    from src.contracts.transformation_contract import StepRoutines

    section = contract.routines
    assert section is not None
    rows = [r.model_copy(update={"repaired": True}) if (r.kind, r.routine_id) == key else r
            for r in section.routines]  # fmt: skip
    contract = contract.model_copy(update={"routines": StepRoutines.model_validate(
        {**section.model_dump(), "routines": [r.model_dump() for r in rows]})})  # fmt: skip
    ts._write_scripts(tmp_path, store, contract)
    text = ts._read(tmp_path, TRIGGERS_FILE)
    assert "SET NEW.note = 'repaired-2384'" in text and "CREATE DEFINER=`admin`@`%` TRIGGER" in text
    assert "s3cr3t-1947" not in text


# --------------------------------------------------------------------------- #2427 review


def _repair_gone(store, how: str) -> None:
    """After the contract was written: the repair loop ran again and ended needs_attention,
    or its result was deleted."""
    path = f"{rf.DB}/{rf.JOB}/routines/v1/routine_repair.json"
    if how == "deleted":
        store.delete(path)
        return
    data = rf.repair_result()
    row = data["routines"][0]
    for key in ("repaired_create", "change_note", "diff", "proof_source"):
        row[key] = None
    row.update(state="needs_attention", attention="attempts_used",
               attention_detail="every one of 3 attempts failed",
               attempts=[{"attempt": 1, "stage": "proof", "outcome": "failed"}])  # fmt: skip
    data["counts"].update(model_repaired=0, needs_attention=1)
    data["refused_candidates"] = 1  # the one failed attempt (#2444)
    store.write_json(path, data)


@pytest.mark.parametrize("how", ["deleted", "needs_attention"])
def test_a_contract_written_before_the_repair_was_lost_never_deploys_the_original(tmp_path, how):
    """The saved contract carries wal_pos as model_repaired; routine_repair.json no longer
    does. The deploy script holds it back as rebuild_differs, never its original text, and
    the facts call it not carried, so the deliverables and the script agree."""
    from src.shared.routine_facts import read_routine_facts

    store = rf.job(tmp_path)
    rf.mark_tested(store)
    contract = rf.contract(store)  # written while the repair stood
    _repair_gone(store, how)
    scripts = write_routine_scripts(store, rf.DB, rf.JOB, contract, SHA, tmp_path / "step")
    assert _refused(scripts) == {rf.WAL: "rebuild_differs"}
    text = (tmp_path / "step" / DEPLOY_FILE).read_text()
    assert "pg_current_xlog_location" not in text and "pg_current_wal_lsn" not in text
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None and facts["repaired_count"] == 0
    assert "step 1 deploys it" not in json.dumps(facts)


def test_a_contract_written_before_the_repair_holds_the_repaired_routine_back(
    tmp_path, monkeypatch
):
    """The reverse: the gate carries a repair of a routine the saved contract carries as it
    was. Neither text is deployed: the test ran the repair, the contract names the original."""
    from src.tools.data_movement import routine_test_gate as gate_module

    store = rf.job(tmp_path)
    rf.mark_tested(store)
    contract = rf.contract(store)
    real = gate_module.deploy_gate

    def gate(*args, **kwargs):
        done = real(*args, **kwargs)
        done.repaired.add(("function", rf.OK))
        return done

    monkeypatch.setattr(gate_module, "deploy_gate", gate)
    refused = _refused(write_routine_scripts(store, rf.DB, rf.JOB, contract, SHA, tmp_path / "s"))
    assert refused == {rf.OK: "rebuild_differs"}


def test_the_triggers_enable_script_holds_a_stale_repaired_row_back(tmp_path):
    """The triggers-enable writer: a row its contract marks as a repair with no
    rebuilt text in the gate is held back, never written from the original."""
    from src.contracts.transformation_contract import StepRoutines
    from tests.unit.tools.data_movement import test_triggers_scripts as ts

    store, contract = ts._mysql_case(tmp_path)
    section = contract.routines
    assert section is not None
    rows = [r.model_copy(update={"repaired": True})
            if r.routine_id == "shop.orders_bi" else r for r in section.routines]  # fmt: skip
    stale = contract.model_copy(update={"routines": StepRoutines.model_validate(
        {**section.model_dump(), "routines": [r.model_dump() for r in rows]})})  # fmt: skip
    scripts = ts._write_scripts(tmp_path, store, stale)
    assert ("trigger", "shop.orders_bi", "rebuild_differs") in scripts.excluded
    assert "s3cr3t-1947" not in (tmp_path / "step").joinpath("triggers-enable.sql").read_text()


def test_a_repair_row_never_replaces_a_carried_routine_no_test_refused(tmp_path):
    """A repair result naming a routine carried as it is, which no test refused, carries
    nothing: only a routine the loop could have sent may be replaced."""
    from src.shared.routine_facts import with_repairs

    store = rf.job(tmp_path)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    rows = [r.model_dump(mode="json") for r in artifact.routines]
    out = with_repairs(rows, {("function", rf.OK): "a" * 64, rf.WAL_KEY: "b" * 64})
    by_id = {r["routine_id"]: r["status"] for r in out}
    assert by_id[rf.OK] != "model_repaired" and by_id[rf.WAL] == "model_repaired"


def test_the_facts_reflect_the_test_and_step_1s_rebuild_still_decides(tmp_path):
    """A repair the test ran, whose rebuild step 1 now refuses. Synthesis never runs the
    routine checks (ADR-034 Decision 8), so the facts reflect the test: their line says step
    1 deploys it only once the repair passes its checks again there, and the scripts hold it
    back."""
    from src.shared.routine_facts import read_routine_facts

    store = rf.job(tmp_path)
    forged = fx.WAL_REDACTED.replace("SELECT ", "SELECT  ")  # still the renamed function
    rf.write_repair(store, forged)
    rf.mark_tested(store, tested=forged)
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    (item,) = [i for i in facts["interventions"] if i["routine_id"] == rf.WAL]
    assert item["deploy_gate"] == "deployed"
    assert "deploys it once it rebuilds the repair" in item["line"]
    assert "passes its checks again there" in item["line"]
    assert _refused(_write(tmp_path, store)) == {rf.WAL: "rebuild_differs"}


def test_what_calls_a_routine_held_for_a_stale_contract_is_held_as_depends_on_held(
    tmp_path, monkeypatch
):
    """Round 2 review of #2427: a routine the writer holds as rebuild_differs (its contract
    row and the deploy gate disagree on a repair) holds back what calls it in the same
    script as depends_on_held, through the one rule (routine_holds) both writers read
    (``stale_holds``)."""
    from src.tools.data_movement import routine_test_gate as gate_module
    from tests.unit.tools.data_movement import test_routine_scripts as rs

    store, contract = rs._pg_case(tmp_path)  # audit, a trigger and later.fn that call it
    real = gate_module.deploy_gate

    def gate(*args, **kwargs):
        done = real(*args, **kwargs)
        done.repaired.add(("function", "public.audit()"))  # a repair the contract lacks
        return done

    monkeypatch.setattr(gate_module, "deploy_gate", gate)
    refused = _refused(rs._write(tmp_path, store, contract))
    assert refused == {
        "public.audit()": "rebuild_differs",
        "public.orders.trg": "depends_on_held",
        "public.later(integer)": "depends_on_held",
    }
