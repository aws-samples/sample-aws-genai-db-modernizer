# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ElastiCache never owns a query and never holds the only copy of the data (#296, #1860).

``docs/understand-the-results.md`` says so of every result. The proof has two parts:

- the move ledger is the only writer of a query's engine and tie (ADR-031 Amendment 3,
  ``test_only_the_ledger_writes_engines.py``), and it refuses a Move onto a cache engine
  or a tie that names one, so no routing pass, customer edit or later version can make a
  cache a query's owner;
- with every owner a system of record, every table gets a system-of-record owner and
  source of truth, the cached ones included (a property over generated assignments).

``tests/e2e/test_elasticache_never_owns.py`` checks the same on every bundled sample's
saved assignments and report.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee import move_ledger
from src.agents.referee.move_ledger import MoveLedger
from src.agents.referee.table_assignments import BASELINE, derive_table_assignments
from src.contracts.assignment_models import QueryAssignment
from src.contracts.move_ledger import MovePass, ReasonCode
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.catalog import ACTIVE, engine_ids
from src.shared.migration_wave_engines import SEARCH_ENGINES
from tests.hypothesis_examples import scaled

(CACHE,) = sorted(CACHE_ENGINES)
OWNERS = sorted(engine_ids(status=ACTIVE) - CACHE_ENGINES)
DURABLE = sorted(engine_ids(status=ACTIVE, system_of_record=True))
_KW = {"pass_": MovePass.SCORE, "reason_code": ReasonCode.HIGHEST_SCORE, "score_after": 80.0}


def _qa(engine: str = "aurora_mysql", **kw) -> dict:
    return {"query_id": "q1", "assigned_engine": engine, "tied_engines": [], **kw}


@pytest.mark.parametrize("cache", sorted(CACHE_ENGINES))
def test_the_ledger_refuses_a_first_move_onto_a_cache(cache):
    ledger = MoveLedger()
    with pytest.raises(ValueError, match="a cache owns no query"):
        move_ledger.apply(ledger, "q1", cache, **_KW)
    assert ledger.moves == [] and ledger.engine_of("q1") is None


@pytest.mark.parametrize("cache", sorted(CACHE_ENGINES))
def test_the_ledger_refuses_moving_an_owned_query_onto_a_cache(cache):
    """Every later pass (the reality check, a model flag, a customer pick, the post-schema
    reroute) moves through ``apply_to_assignment``; the record keeps its owner."""
    ledger = MoveLedger({"q1": "aurora_mysql"})
    qa = _qa(confidence=70)
    with pytest.raises(ValueError, match="a cache owns no query"):
        move_ledger.apply_to_assignment(ledger, qa, cache, **_KW)
    assert qa["assigned_engine"] == "aurora_mysql" and ledger.engine_of("q1") == "aurora_mysql"
    assert ledger.moves == []


@pytest.mark.parametrize("cache", sorted(CACHE_ENGINES))
def test_the_ledger_refuses_a_tie_that_names_a_cache(cache):
    """An unresolved tie is resolved later onto one of its engines, so a cache in it would
    be a cache owner in waiting."""
    ledger = MoveLedger()
    with pytest.raises(ValueError, match=f"cache {cache}"):
        move_ledger.apply(ledger, "q1", "dynamodb", tied_engines=["dynamodb", cache], **_KW)
    assert ledger.tied_of("q1") == []


def test_the_ledger_refuses_a_cache_on_a_query_assignment_model_too():
    ledger = MoveLedger({"q1": "dynamodb"})
    qa = QueryAssignment(
        query_id="q1",
        assigned_engine="dynamodb",
        confidence=70,
        source_tables=["s.t"],
        assignment_reason="x",
    )
    with pytest.raises(ValueError, match="a cache owns no query"):
        move_ledger.apply_to_assignment(ledger, qa, CACHE, actor=move_ledger.MoveActor.CUSTOMER,
                                        **_KW)  # fmt: skip
    assert qa.assigned_engine == "dynamodb"


@pytest.mark.passes_on_main
@pytest.mark.parametrize("owner", OWNERS)
def test_a_move_off_a_cache_and_onto_any_owner_is_recorded(owner):
    """The negative case: an assignment saved before the cache overlay has cache owners,
    and the normalisation moves each off it; every engine that is not a cache still
    takes a Move, and a tie of owners still records."""
    ledger = MoveLedger({"q1": CACHE})
    qa = _qa(CACHE)
    move = move_ledger.apply_to_assignment(
        ledger, qa, owner, tied_engines=sorted({owner, "aurora_mysql"}), **{
            **_KW, "pass_": MovePass.CACHE_OWNER_NORMALIZE})  # fmt: skip
    assert (move.from_engine, move.to_engine, qa["assigned_engine"]) == (CACHE, owner, owner)
    assert qa["tied_engines"] == sorted({owner, "aurora_mysql"})


_tables = st.lists(st.sampled_from(["s.a", "s.b", "s.c", "s.d"]), min_size=1, max_size=3,
                   unique=True)  # fmt: skip


@st.composite
def _assignments(draw: st.DrawFn) -> list[QueryAssignment]:
    out = []
    for i in range(draw(st.integers(1, 8))):
        engine = draw(st.sampled_from(OWNERS))
        out.append(
            QueryAssignment(
                query_id=f"q{i}",
                assigned_engine=engine,
                confidence=70,
                source_tables=draw(_tables),
                assignment_reason="x",
                cache_engine=draw(st.sampled_from([None, CACHE])),
            )
        )
    return out


@pytest.mark.passes_on_main
@settings(max_examples=scaled(200), deadline=None)
@given(
    assignments=_assignments(),
    retained=st.sampled_from([None, "aurora_mysql", "aurora_postgresql"]),
    writes=st.booleans(),
)
def test_every_table_a_cache_fronts_has_a_system_of_record_owner(assignments, retained, writes):
    """With the ledger's owners (any engine but a cache), no table's primary engine is the
    cache or a search read model, and a written table's source of truth is a system of
    record: the cache is never the only copy."""
    groups = {}
    if writes:
        by_table: dict[str, list[str]] = {}
        for qa in assignments:
            if qa.assigned_engine not in SEARCH_ENGINES:  # a search engine owns no write
                by_table.setdefault(qa.source_tables[0], []).append(qa.query_id)
        groups = by_table
    rows, _ = derive_table_assignments(assignments, retained_engine=retained, write_groups=groups)
    cached = {t for qa in assignments if qa.cache_engine for t in qa.source_tables}
    assert cached <= {r.table_id for r in rows}
    for row in rows:
        # A table only OpenSearch reads, with no retained engine anywhere, gets the
        # generic Aurora placeholder: a system of record still to be chosen, never a
        # cache or a read model.
        assert row.primary_engine not in CACHE_ENGINES | SEARCH_ENGINES, row
        assert row.source_of_truth in (None, *DURABLE), row
        if row.table_id in cached:
            assert row.primary_engine in (*DURABLE, BASELINE), row
