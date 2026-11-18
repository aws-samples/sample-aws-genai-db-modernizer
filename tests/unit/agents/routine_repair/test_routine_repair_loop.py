# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine repair loop (#1941): every candidate rebuilt onto the exact text, checked
again, proved side by side, at most three attempts per routine.

The model is a fake: the Bedrock entry point (``handler._ask_bedrock``) is patched to
return set answers and record each request, and the external session's answers are
written to the response file by the test. The side-by-side proof runs on two disposable
PostgreSQL 16 engines (``pgserver``, a dev dependency, on a Unix socket only) where the
test needs it; every other test proves nothing (``--proof none``). The routines are
synthetic fixtures.
"""

from __future__ import annotations

import json
import os
import signal
from typing import Any

import pytest

from src.agents.prompt_framing import is_framed
from src.contracts.routine_migration import RoutineProofEntry
from src.contracts.routine_repair import MAX_ATTEMPTS, RoutineRepairContract
from src.shared.contract_versioning import load_contract
from src.tools.database import disposable_engine as de
from tests.unit.agents.routine_repair import repair_fixtures as fx

NOTE = "Calls pg_current_wal_lsn, the name PostgreSQL 10 gave the function."
WAL_EXACT = fx.WAL_CREATE  # ... || 'LiteralMark1940'


def _answer(create: str, note: str = NOTE, rid: str = fx.WAL) -> dict[str, Any]:
    return {
        "repairs": [
            {"kind": "function", "routine_id": rid, "repaired_create": create, "change_note": note}
        ]
    }


class _FakeModel:
    """The Bedrock entry point: returns the next set answer and records each request."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> Any:
        from src.contracts.routine_repair import RoutineRepairResponse

        self.requests.append(request)
        answer = self.answers.pop(0)
        return None if answer is None else RoutineRepairResponse.model_validate(answer)


@pytest.fixture
def model(monkeypatch):
    def install(answers: list[Any]) -> _FakeModel:
        from src.agents.routine_repair import handler

        fake = _FakeModel(answers)
        monkeypatch.setattr(handler, "_ask_bedrock", fake)
        return fake

    return install


def _result(store, status) -> RoutineRepairContract:
    return load_contract(RoutineRepairContract, store.read_json(status["artifact"]), artifact="x")


def _row(result: RoutineRepairContract, rid: str = fx.WAL):
    return next(r for r in result.routines if r.routine_id == rid)


def _blocks(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Each routine block of a request, out of its frame."""
    out = []
    for framed in request["routines"]:
        assert is_framed(framed, "routine_id")
        body = framed.split("<untrusted_customer_data>\n", 1)[1].rsplit(
            "\n</untrusted_customer_data>", 1
        )[0]
        out.append(json.loads(body))
    return out


# --------------------------------------------------------------------------- the job

#: wal_pos with two literals: a setting read with current_setting, and a plain string.
#: Its only failed rule is the PostgreSQL 10 rename; the setting it reads is current.
TWO_LITERALS = (
    "CREATE OR REPLACE FUNCTION public.wal_pos(p integer)\n RETURNS text\n LANGUAGE sql\n"
    "AS $function$SELECT pg_current_xlog_location()::text || current_setting('server_version')"
    " || 'wal_keep_segments'$function$\n"
)


def _job(tmp_path, wal_create: str = WAL_EXACT):
    col = fx.collector()
    col["database_schema"]["procedures"][0]["definition"] = wal_create
    return fx.job(tmp_path, col=col, hide=True)


def _seen(text: str) -> str:
    from src.tools.sql.redact import redact_definition

    return redact_definition(text, "postgresql")


# --------------------------------------------------------------------------- rebuild


def test_a_routine_whose_literals_cannot_be_put_back_is_never_sent(tmp_path, model):
    """A source literal that reads '?' itself: no repair could be rebuilt by position, so
    the routine needs attention with the reason, and no model is asked."""
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path, WAL_EXACT.replace(f"'{fx.LITERAL_MARK}'", "'?'"))
    fake = model([])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    assert fake.requests == []
    row = _row(_result(store, status))
    assert (row.state, row.attention) == ("needs_attention", "not_rebuildable")
    assert "reads '?' itself" in (row.attention_detail or "")
    external = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    assert external["status"] == "complete"
    assert not store.exists(f"{fx.DB}/{fx.JOB}/routines/v1/repair/llm_input.json")


def test_the_recheck_on_the_rebuilt_text_catches_what_the_redacted_screen_missed(tmp_path, model):
    """The candidate moves a placeholder into current_setting(...). Every placeholder
    reads '?', so the redacted screen passes it; rebuilt, it reads a setting PostgreSQL 13
    removed, and the routine checks run again on the exact text refuse it."""
    from src.agents.routine_repair.handler import (
        repair_refusal,
        run_routine_repair,
        run_routine_repair_deterministic,
    )
    from src.storage.collector_for_models import read_collector_for_models
    from src.storage.routine_migration_store import read_routine_migration

    store = _job(tmp_path, TWO_LITERALS)
    seen = _seen(TWO_LITERALS)
    good = seen.replace("pg_current_xlog_location()", "pg_current_wal_lsn()")
    moved = good.replace("current_setting('?') || '?'", "'?' || current_setting('?')")
    assert moved != good
    migration = read_routine_migration(store, fx.DB, fx.JOB, 1)
    assert migration is not None
    plan = run_routine_repair_deterministic(
        read_collector_for_models(store, fx.DB, fx.JOB), migration
    )
    (candidate,) = plan.candidates
    assert repair_refusal(candidate, moved, "postgresql") is None  # the screen misses it
    fake = model([_answer(moved), _answer(good)])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    row = _row(_result(store, status))
    first, second = row.attempts
    assert (first.stage, first.outcome, first.code) == ("recheck", "failed", "check_failed")
    assert "postgresql-routine-v13-removed-settings" in (first.error or "")
    assert (second.stage, second.outcome) == ("proof", "pending")
    assert row.state == "repaired_pending_proof"
    # The second request carries the first attempt's error, framed, and no literal.
    (block,) = _blocks(fake.requests[1])
    assert block["attempt"] == 2 and block["previous_attempt_error"] == first.error
    # (The catalog's reason names the removed setting; the routine's literals never.)
    for request in fake.requests:
        text = json.dumps(request)
        assert "'server_version'" not in text and "'wal_keep_segments'" not in text


def test_a_candidate_the_rebuilt_text_screen_refuses_is_tried_again(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    good = fx.WAL_REPAIRED
    renamed = good.replace("public.wal_pos", "public.wal_pos2")
    fake = model([_answer(renamed), _answer(good)])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    row = _row(_result(store, status))
    assert [a.stage for a in row.attempts] == ["screen", "proof"]
    assert row.attempts[0].code == "renamed"
    assert len(fake.requests) == 2


# --------------------------------------------------------------------------- the bound


def test_at_most_three_attempts_each_fed_the_previous_error(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair
    from src.shared.routine_facts import read_routine_facts

    store = _job(tmp_path)
    renamed = fx.WAL_REPAIRED.replace("public.wal_pos", "public.wal_pos2")
    # Keeps the renamed function: the redacted screen passes it, the recheck does not.
    kept = fx.WAL_REDACTED.replace("SELECT pg_current", "SELECT (pg_current").replace(
        "()::text", "())::text"
    )
    fake = model([_answer(renamed), _answer(kept), _answer(renamed), _answer(fx.WAL_REPAIRED)])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    assert len(fake.requests) == MAX_ATTEMPTS == 3
    row = _row(_result(store, status))
    assert row.state == "needs_attention" and row.attention == "attempts_used"
    assert [(a.attempt, a.stage) for a in row.attempts] == [
        (1, "screen"),
        (2, "recheck"),
        (3, "screen"),
    ]
    for n in (1, 2):
        (block,) = _blocks(fake.requests[n])
        assert block["attempt"] == n + 1
        assert block["previous_attempt_error"] == row.attempts[n - 1].error
    (first,) = _blocks(fake.requests[0])
    assert first["previous_attempt_error"] is None
    assert all(fx.LITERAL_MARK not in json.dumps(r) for r in fake.requests)
    # Every attempt and its error is in the artifact and in the risk's words.
    facts = read_routine_facts(store, fx.DB, fx.JOB, 1)
    (item,) = facts["interventions"]
    assert item["state"] == "needs_attention" and len(item["attempt_lines"]) == 3
    assert "refused by the repair screen" in item["attempt_lines"][0]
    assert "postgresql-routine-v10-renamed-functions" in item["attempt_lines"][1]


def test_a_routine_left_out_of_the_answer_is_not_asked_again(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    fake = model([{"repairs": []}])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    assert len(fake.requests) == 1
    row = _row(_result(store, status))
    assert row.state == "no_answer" and [a.outcome for a in row.attempts] == ["no_answer"]


def test_a_candidate_a_round_refused_is_counted_when_the_model_then_gives_none(tmp_path, model):
    """#2444: round 1's candidate is refused, round 2 leaves the routine out. The routine
    ends no_answer, and the result and its status line still count the refused candidate;
    a result written before the count reads it from its rows."""
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    renamed = fx.WAL_REPAIRED.replace("public.wal_pos", "public.wal_pos2")
    model([_answer(renamed), {"repairs": []}])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    result = _result(store, status)
    row = _row(result)
    assert row.state == "no_answer"
    assert [a.outcome for a in row.attempts] == ["failed", "no_answer"]
    assert result.refused_candidates == 1 and status["refused_candidates"] == 1
    assert status["counts"]["refused"] == 0
    saved = store.read_json(status["artifact"])
    assert saved["refused_candidates"] == 1
    saved.pop("refused_candidates")
    assert RoutineRepairContract.model_validate(saved).refused_candidates == 1
    saved["refused_candidates"] = 0
    with pytest.raises(ValueError, match="refused_candidates"):
        RoutineRepairContract.model_validate(saved)


# --------------------------------------------------------------------------- external


def test_external_mode_round_trips_each_attempt_through_the_request_file(tmp_path):
    from src.agents.routine_repair.handler import (
        StaleRepairRequest,
        finalize_routine_repair,
        run_routine_repair,
    )

    store = _job(tmp_path)
    renamed = fx.WAL_REPAIRED.replace("public.wal_pos", "public.wal_pos2")
    first = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    assert (first["status"], first["round"], first["max_rounds"]) == ("awaiting_llm", 1, 3)
    store.write_json(first["llm_response"], _answer(renamed))
    second = finalize_routine_repair(store, fx.DB, fx.JOB)
    assert (second["status"], second["round"]) == ("awaiting_llm", 2)
    # The earlier round's answer is gone, so it can never be applied to round 2.
    assert not store.exists(second["llm_response"])
    with pytest.raises(FileNotFoundError):
        finalize_routine_repair(store, fx.DB, fx.JOB)
    (block,) = _blocks(store.read_json(second["llm_request"]))
    assert block["attempt"] == 2 and "renames" not in block
    assert "another name" in block["previous_attempt_error"]
    assert fx.LITERAL_MARK not in json.dumps(store.read_json(second["llm_request"]))
    # A request edited since it was written is refused.
    stale = store.read_json(second["llm_request"])
    stale["routine_count"] = 5
    store.write_json(second["llm_request"], stale)
    store.write_json(second["llm_response"], _answer(fx.WAL_REPAIRED))
    with pytest.raises(StaleRepairRequest):
        finalize_routine_repair(store, fx.DB, fx.JOB)
    again = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    assert again["round"] == 1  # a new run starts over
    store.write_json(again["llm_response"], _answer(fx.WAL_REPAIRED))
    done = finalize_routine_repair(store, fx.DB, fx.JOB)
    assert done["status"] == "complete" and done["rounds"] == 1
    row = _row(_result(store, done))
    # No proof target: never carried.
    assert row.state == "repaired_pending_proof" and row.attempts[-1].code == "no_proof_target"


def test_external_mode_ends_after_the_third_round(tmp_path):
    from src.agents.routine_repair.handler import finalize_routine_repair, run_routine_repair

    store = _job(tmp_path)
    renamed = fx.WAL_REPAIRED.replace("public.wal_pos", "public.wal_pos2")
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    for n in range(1, MAX_ATTEMPTS + 1):
        assert status["status"] == "awaiting_llm" and status["round"] == n
        store.write_json(status["llm_response"], _answer(renamed))
        status = finalize_routine_repair(store, fx.DB, fx.JOB)
    assert status["status"] == "complete" and status["counts"]["needs_attention"] == 1
    assert _row(_result(store, status)).attention == "attempts_used"


def test_mode_none_runs_no_loop(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    fake = model([])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="none", proof="local")
    assert status["status"] == "skipped" and fake.requests == []
    assert [r.state for r in _result(store, status).routines] == ["not_repaired"]


def test_an_opted_out_body_never_reaches_any_round(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    renamed = fx.WAL_REPAIRED.replace("public.wal_pos", "public.wal_pos2")
    fake = model([_answer(renamed)] * 3)
    run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    assert len(fake.requests) == 3
    for request in fake.requests:
        assert fx.OPTED_OUT_MARK not in json.dumps(request) and "hidden" not in json.dumps(request)


def test_without_an_engine_of_the_sources_major_the_repair_waits(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = _job(tmp_path)
    model([_answer(fx.WAL_REPAIRED)])
    status = run_routine_repair(
        store, fx.DB, fx.JOB, llm_mode="bedrock", proof="local",
        proof_options={"engine_available": lambda engine, major: False},
    )  # fmt: skip
    row = _row(_result(store, status))
    assert row.state == "repaired_pending_proof"
    assert row.attempts[-1].code == "source_engine_unavailable"
    assert "postgresql 9" in (row.attempts[-1].error or "")


# --------------------------------------------------------------------------- the proof

needs_pg = pytest.mark.skipif(
    "16" not in de.available_majors("postgresql"), reason="pgserver (PostgreSQL 16) is needed"
)
SIGN_ID = "public.sign_word(integer)"
SIGN_EXACT = (
    "CREATE OR REPLACE FUNCTION public.sign_word(p integer)\n RETURNS text\n LANGUAGE sql\n"
    "AS $function$SELECT CASE WHEN p > 0 THEN 'pos' ELSE 'neg' END$function$\n"
)


@pytest.fixture
def no_server_left(monkeypatch):
    """Every PostgreSQL the test starts is stopped by the proof: each one's postmaster
    pid is recorded as it starts, and one still running afterwards is stopped by that
    pid and fails the test."""
    started: list[Any] = []
    real_start = de.start

    def recording_start(*args: Any, **kwargs: Any) -> Any:
        handle = real_start(*args, **kwargs)
        started.append(handle)
        return handle

    monkeypatch.setattr(de, "start", recording_start)
    try:
        yield started
    finally:
        left = [h.pid for h in started if h.pid is not None and de._running(h.pid)]
        for pid in left:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        assert not left, f"PostgreSQL left running: {left}"


def _sign_job(tmp_path):
    """A PostgreSQL 16 job whose sign_word the test cluster refused (#1945): carried, so
    its own checks passed, with a proof that needs attention."""
    from src.tools.routines.migration import run_routine_migration_deterministic
    from src.tools.routines.proof_run import apply_proof

    col = fx.collector()
    col["metadata"]["source_database"]["version"] = "16.4"
    proc = dict(col["database_schema"]["procedures"][1])
    proc.update(procedure_id="sign_word", procedure_name="sign_word", specific_name="sign_word_1",
                definition=SIGN_EXACT)  # fmt: skip
    col["database_schema"]["procedures"] = [proc]
    built = run_routine_migration_deterministic(
        col, None, fx.sources(col), job_id=fx.JOB, database_name=fx.DB, assignment_version=1
    )
    assert built is not None
    (row,) = built.routines
    assert row.status in ("as_is", "rewritten")
    refused = RoutineProofEntry(mode="load_and_run", outcome="needs_attention",
                                reason="a call failed on the test cluster", reason_code="calls_failed")  # fmt: skip
    built = apply_proof(built, {row.routine_id: refused}, target="an Aurora PostgreSQL 16 test cluster",
                        source=None, strict_check="not_available", seconds=1.0)  # fmt: skip
    return fx.job(tmp_path, col=col, hide=False, built=built)


@needs_pg
def test_the_proof_catches_a_swapped_literal_and_carries_a_faithful_repair(
    tmp_path, model, no_server_left
):
    """Flipping the condition swaps which literal each branch returns. The screens see
    '?' and '?' either way; side by side on the rebuilt exact text the results differ,
    so the attempt fails and the next is fed that error. The faithful repair gives the
    same results and is carried as model_repaired, with its diff."""
    from src.agents.routine_repair.handler import run_routine_repair
    from src.shared.routine_facts import read_routine_facts

    if os.geteuid() == 0:
        pytest.skip("the disposable engines refuse to start as root")
    store = _sign_job(tmp_path)
    seen = _seen(SIGN_EXACT)
    assert "'pos'" not in seen and seen.count("'?'") == 2
    swapped = seen.replace("p > 0", "p <= 0")
    faithful = seen.replace("p > 0", "0 < p")
    fake = model([_answer(swapped, "Wrote the test the other way round.", SIGN_ID),
                  _answer(faithful, "Wrote the comparison with the constant first.", SIGN_ID)])  # fmt: skip
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock", proof="local")
    assert len(no_server_left) >= 2  # one engine per side
    result = _result(store, status)
    row = _row(result, SIGN_ID)
    first, second = row.attempts
    assert (first.stage, first.outcome) == ("proof", "failed"), first
    assert "difference" in (first.error or "") and "'pos'" not in (first.error or "")
    assert (second.stage, second.outcome) == ("proof", "proved"), second
    assert row.state == "model_repaired" and result.proof == "local"
    assert row.proof_source == "a disposable postgresql 16 engine"
    assert "-" in (row.diff or "") and "0 < p" in (row.diff or "")
    assert "'pos'" not in (row.diff or "") and "'neg'" not in (row.diff or "")
    (block,) = _blocks(fake.requests[1])
    assert block["previous_attempt_error"] == first.error
    assert "'pos'" not in json.dumps(fake.requests) and "neg" not in json.dumps(fake.requests)
    facts = read_routine_facts(store, fx.DB, fx.JOB, 1)
    (item,) = facts["interventions"]
    assert item["state"] == "model_repaired"
    assert "Wrote the comparison with the constant first." in item["line"]
    # Carried, repaired (#2384); step 1 deploys it only once a test runs that repair.
    assert facts["repaired_count"] == 1 and item["deploy_gate"] == "not_tested"


# --------------------------------------------------------------------------- #2387 review


def _state_path() -> str:
    return f"{fx.DB}/{fx.JOB}/routines/v1/repair/loop_state.json"


def _forge(
    store, status, *, proof: str = "local", candidate: str = "CREATE anything", reseal: bool = True
) -> None:
    """The review's forgery: a session rewrites the loop state between rounds so that the
    routine reads as proved, with an empty request and answer to match."""
    from src.agents.routine_repair import handler

    state = store.read_json(_state_path())
    state["proof"] = proof
    state["open"] = {}
    state["settled"] = {
        f"function\u0000{fx.WAL}": {
            "state": "model_repaired",
            "repaired_create": candidate,
            "change_note": NOTE,
            "diff": "-a\n+b",
            "proof_source": "a disposable postgresql 9 engine",
            "attempts": [{"attempt": 1, "stage": "proof", "outcome": "proved", "code": None,
                          "error": "no differences found"}],  # fmt: skip
        }
    }
    empty = handler.RoutineRepairPlan(
        job_id=fx.JOB, database_name=fx.DB, assignment_version=1, source_engine="postgresql",
        target_engine="aurora_postgresql", target_line="14", candidates=(),
    )  # fmt: skip
    request = handler.prepare_routine_repair_llm_input(empty)
    if reseal:  # a writer that knows how the seal is made: only re-verification stops it
        from src.agents.routine_repair.loop import _State

        state.pop("seal", None)
        state["seal"] = _State(state).seal(fx.DB, fx.JOB, request)
    store.write_json(_state_path(), state)
    store.write_json(status["llm_request"], request)
    store.write_json(status["llm_response"], {"repairs": []})


def test_an_edited_loop_state_is_refused(tmp_path):
    from src.agents.routine_repair.handler import (
        StaleRepairRequest,
        finalize_routine_repair,
        run_routine_repair,
    )

    store = _job(tmp_path)
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    _forge(store, status, reseal=False)
    with pytest.raises(StaleRepairRequest):
        finalize_routine_repair(store, fx.DB, fx.JOB, proof="local")
    assert not store.exists(f"{fx.DB}/{fx.JOB}/routines/v1/routine_repair.json")


@pytest.mark.parametrize(
    "candidate", ["CREATE anything", fx.WAL_REPAIRED], ids=["any-text", "a-real-candidate"]
)
def test_a_forged_loop_state_never_carries_a_repair(tmp_path, candidate):
    """finalize re-verifies every settled repair it did not compute itself; with no proof
    in this invocation, nothing is carried, whatever the file says."""
    from src.agents.routine_repair.handler import finalize_routine_repair, run_routine_repair

    store = _job(tmp_path)
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    _forge(store, status, candidate=candidate)
    done = finalize_routine_repair(store, fx.DB, fx.JOB)
    assert done["counts"]["model_repaired"] == 0
    row = _row(_result(store, done))
    assert row.state != "model_repaired"
    assert all(a.outcome != "proved" for a in row.attempts)
    if candidate == fx.WAL_REPAIRED:
        # Rebuilt and checked again in this invocation: it waits for a proof.
        assert row.state == "repaired_pending_proof"
    else:
        assert row.state == "needs_attention" and row.attention == "not_verified"


def test_the_state_file_cannot_raise_the_proof_mode(tmp_path, monkeypatch):
    """The first run chose --proof none. A state edited to say local is refused; and a
    finalize that asks for local still proves nothing (the lower of the two holds)."""
    from src.agents.routine_repair.handler import (
        StaleRepairRequest,
        finalize_routine_repair,
        run_routine_repair,
    )

    store = _job(tmp_path)
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    saved = store.read_json(_state_path())
    assert saved["proof"] == "none"
    store.write_json(_state_path(), {**saved, "proof": "local"})
    store.write_json(status["llm_response"], _answer(fx.WAL_REPAIRED))
    with pytest.raises(StaleRepairRequest):
        finalize_routine_repair(store, fx.DB, fx.JOB, proof="local")
    store.write_json(_state_path(), saved)
    asked: list[str] = []
    import src.tools.routines.repair_proof as rp

    real = rp.RepairVerifier.__init__

    def spy(self, *a, **k):
        asked.append(k.get("proof", "none"))
        real(self, *a, **k)

    monkeypatch.setattr(rp.RepairVerifier, "__init__", spy)
    done = finalize_routine_repair(store, fx.DB, fx.JOB, proof="local")
    assert asked and set(asked) == {"none"}
    assert _row(_result(store, done)).state == "repaired_pending_proof"


def test_an_inverted_condition_with_no_proof_stays_pending_and_its_note_is_not_printed(
    tmp_path, model
):
    """The rebuild puts the literals back by position, so flipping a condition around two
    '?' passes every screen and check; only the proof would tell. With --proof none it is
    repaired_pending_proof, and the deliverables never print its note as what changed."""
    from src.agents.routine_repair.handler import run_routine_repair
    from src.shared.routine_facts import read_routine_facts

    store = _sign_job(tmp_path)
    flipped = _seen(SIGN_EXACT).replace("p > 0", "p <= 0")
    note = "Wrote the test the other way round."
    model([_answer(flipped, note, SIGN_ID)])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    row = _row(_result(store, status), SIGN_ID)
    assert row.state == "repaired_pending_proof"
    (item,) = read_routine_facts(store, fx.DB, fx.JOB, 1)["interventions"]
    assert note not in item["line"] and "not carried" in item["line"]


def test_a_candidate_that_turns_a_literal_into_dynamic_sql_is_never_built(tmp_path, model):
    """The review's repro: the exact text returns ';DROP TABLE public.orders', redacted to
    '?'. A candidate writing EXECUTE '?' would rebuild into that statement: the routine is
    not rebuildable, so it is never sent; and an added EXECUTE is refused by the screen."""
    from src.agents.routine_repair.handler import run_routine_repair

    exact = WAL_EXACT.replace(f"|| '{fx.LITERAL_MARK}'", "|| ';DROP TABLE public.orders'")
    store = _job(tmp_path, exact)
    fake = model([])
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="bedrock")
    assert fake.requests == []
    row = _row(_result(store, status))
    assert (row.state, row.attention) == ("needs_attention", "not_rebuildable")


# --------------------------------------------------------------------------- MySQL header
# #2444: a MySQL collector row may not list its parameters (the offline script's rows carry
# ``parameters: None``), so a header written from the row's fields drops them. The request
# takes the header from the routines file instead, its literals redacted, and step 1's
# rebuild reads the same text the model was given.

MYSQL_COMMENT_MARK = "HeaderComment2444"
MYSQL_HEADER_EXACT = (
    "CREATE DEFINER=`app`@`%` PROCEDURE `hashpw`(IN `p` varchar(64), OUT `r` varchar(64))\n"
    f"    READS SQL DATA\n    COMMENT '{MYSQL_COMMENT_MARK}'\n"
)


def _mysql_job(tmp_path, *, parameters: Any = None, sources: bool = True):
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.routine_sources import RoutineSource
    from src.storage.local_store import LocalArtifactStore
    from src.tools.routines.migration import run_routine_migration_deterministic

    col = fx.mysql_collector()
    col["database_schema"]["procedures"][0]["parameters"] = parameters
    source = RoutineSource(
        kind="procedure", routine_id=fx.MYSQL_ID, schema_name="shop", specific_name="hashpw",
        create_statement=MYSQL_HEADER_EXACT + fx.MYSQL_BODY, origin="show_create",
    )  # fmt: skip
    built = run_routine_migration_deterministic(
        col, None, [source], job_id=fx.JOB, database_name=fx.DB, assignment_version=1
    )
    assert built is not None
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{fx.DB}/{fx.JOB}/collector/output.json", col)
    write_routine_sources(store, fx.DB, fx.JOB, col, [source] if sources else [])
    store.write_json(
        f"{fx.DB}/{fx.JOB}/routines/v1/routine_migration.json", built.model_dump(mode="json")
    )
    return store, built


def test_a_mysql_request_has_the_routines_file_header_with_its_parameters(tmp_path):
    from src.agents.routine_repair.handler import repairable_routines, run_routine_repair

    store, _ = _mysql_job(tmp_path)
    assert repairable_routines(store, fx.DB, fx.JOB) == 1  # what /test-routines offers
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    assert status["status"] == "awaiting_llm", status
    request = store.read_json(status["llm_request"])
    (block,) = _blocks(request)
    create = block["create_statement"]
    assert create.startswith(
        "CREATE DEFINER=`app`@`%` PROCEDURE `hashpw`(IN `p` varchar(64), OUT `r` varchar(64))\n"
        "    READS SQL DATA\n    COMMENT '?'\nBEGIN"
    ), create
    assert MYSQL_COMMENT_MARK not in json.dumps(request) and "'salt'" not in create
    assert block["signature"] == "IN varchar(64), OUT varchar(64)"


def test_step_one_rebuilds_a_mysql_repair_on_the_text_the_model_saw(tmp_path):
    from src.agents.routine_repair.handler import run_routine_repair
    from src.tools.routines.repair_proof import seen_texts

    store, built = _mysql_job(tmp_path)
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode="external")
    (block,) = _blocks(store.read_json(status["llm_request"]))
    seen = seen_texts(store, fx.DB, fx.JOB, built)
    assert seen[("procedure", fx.MYSQL_ID)] == block["create_statement"]


def test_a_mysql_row_without_its_parameters_and_no_header_is_not_sent(tmp_path):
    """No header in the routines file and a row that lists no parameters: the header the
    row's fields give has none, so it is not the routine's, and no model sees it."""
    from src.agents.routine_repair.handler import run_routine_repair_deterministic
    from src.storage.collector_for_models import read_collector_for_models

    store, built = _mysql_job(tmp_path)
    collector = read_collector_for_models(store, fx.DB, fx.JOB)
    plan = run_routine_repair_deterministic(collector, built)
    assert plan.candidates == ()
    assert [(u.kind, u.routine_id) for u in plan.unsent] == [("procedure", fx.MYSQL_ID)]
    store, built = _mysql_job(tmp_path / "listed", parameters=fx.mysql_collector()[
        "database_schema"]["procedures"][0]["parameters"])  # fmt: skip
    collector = read_collector_for_models(store, fx.DB, fx.JOB)
    (c,) = run_routine_repair_deterministic(collector, built).candidates
    assert "(IN `p` varchar(64), OUT `r` varchar(64))" in c.create_text


def _without_signature(built):
    rows = [r.model_copy(update={"signature": None}) for r in built.routines]
    return built.model_copy(update={"routines": rows})


def test_a_routine_whose_signature_the_artifact_did_not_read_is_never_sent(tmp_path):
    """#2444 review: with no signature to compare, a header could have lost its parameters
    unseen, so the routine is not sent, with the header from the routines file too."""
    from src.agents.routine_repair.handler import run_routine_repair_deterministic
    from src.storage.collector_for_models import read_collector_for_models
    from src.tools.routines.repair_proof import routine_headers

    store, built = _mysql_job(tmp_path)
    collector = read_collector_for_models(store, fx.DB, fx.JOB)
    headers = routine_headers(store, fx.DB, fx.JOB, built, collector)
    assert len(run_routine_repair_deterministic(collector, built, headers=headers).candidates) == 1
    plan = run_routine_repair_deterministic(collector, _without_signature(built), headers=headers)
    assert plan.candidates == () and len(plan.unsent) == 1


@pytest.mark.parametrize("llm_mode", ["external", "none"])
def test_a_routine_not_sent_for_its_signature_has_a_row_with_its_reason(tmp_path, llm_mode):
    """#2444 review: the result says why the routine was not offered for repair, in fixed
    words that hold no routine text, and step 1 has no text of it to rebuild a repair on."""
    from src.agents.routine_repair.handler import SIGNATURE_MISMATCH_WORDS, run_routine_repair
    from src.tools.routines.repair_proof import seen_texts

    store, built = _mysql_job(tmp_path)
    blind = _without_signature(built)
    store.write_json(
        f"{fx.DB}/{fx.JOB}/routines/v1/routine_migration.json", blind.model_dump(mode="json")
    )
    status = run_routine_repair(store, fx.DB, fx.JOB, llm_mode=llm_mode)
    assert status["status"] in ("complete", "skipped"), status
    assert not store.exists(f"{fx.DB}/{fx.JOB}/routines/v1/repair/llm_input.json")
    result = _result(store, status)
    row = _row(result, fx.MYSQL_ID)
    assert (row.state, row.attention) == ("needs_attention", "signature_mismatch")
    assert row.attention_detail == SIGNATURE_MISMATCH_WORDS and row.attempts == []
    assert result.counts["needs_attention"] == 1
    assert seen_texts(store, fx.DB, fx.JOB, blind) == {}
    from src.shared.routine_facts import read_routine_facts

    facts = read_routine_facts(store, fx.DB, fx.JOB, 1)
    assert facts is not None
    (item,) = facts["interventions"]
    assert item["line"].startswith("It was not sent for a model repair: The text")
