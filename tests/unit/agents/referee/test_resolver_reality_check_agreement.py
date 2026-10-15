"""Regression test for #490: the reality check must never disagree with the
resolver's own per-query engine fit.

Before this fix, the reality check scored a query/engine pair with its own
``_engine_fit_score``: table/pattern confidence plus or minus a flat
``SIGNAL_MATCH_BONUS`` keyed off triage *signals* and its own, separate
``ENGINE_CAPABILITIES`` table. The resolver scores the same pair with
``query_scoring.compute_query_confidence`` minus a penalty from
analysis-level anti-patterns (``ANTI_PATTERN_PENALTIES``/
``CAPABILITY_SIGNAL_ENGINES``, via ``query_scoring.build_anti_pattern_map``)
-- a different mechanism, fed by different data (an engine's own analysis
output, not triage signals). The two could, and on the bundled wordpress/
discourse samples did, disagree on a query's best-fit engine (#490's own
measurement). This module fixes that by having the reality check call the
resolver's own ``query_scoring.score_query_for_engine`` (#549) directly, or
read it back from a resolver-built ``RoutingTrace`` (``_resolver_score``),
instead of keeping a second, separately drifting copy.

The fixtures below reproduce exactly the kind of case that used to disagree:
an engine's OWN analysis self-detects (or another engine's analysis
positively detects) an anti-pattern for a query that an old-style, purely
signal-driven fit score would never have seen at all, because the fixture
carries no triage signals for it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check import _resolver_score

_REPO_SRC = Path(__file__).resolve().parents[4] / "src"


def _triage(engines: list[str]) -> dict:
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": []}


def _collector(query_ids: list[str]) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {"query_id": qid, "tables_accessed": ["db.users"], "query_type": "SELECT"}
                for qid in query_ids
            ]
        }
    }


def _assert_resolver_and_reality_check_agree(
    analysis_outputs: dict[str, dict], engines: list[str], query_id: str
) -> str:
    """Resolve ``query_id`` and assert every engine's reality-check-side score
    (both ``_resolver_score`` against the resolver's own trace, and a direct
    ``score_query_for_engine`` call against the same ``ScoringContext``) equals
    the resolver's own score for that engine -- never a second, independently
    computed number. Returns the resolver's own top engine.
    """
    triage = _triage(engines)
    collector = _collector([query_id])
    trace = RoutingTrace()
    result = AssignmentResolver().resolve(triage, analysis_outputs, collector, routing_trace=trace)
    resolver_top = next(
        qa for qa in result.query_assignments if qa.query_id == query_id
    ).assigned_engine

    query_map = {
        query_id: {"query_id": query_id, "tables_accessed": ["db.users"], "query_type": "SELECT"}
    }
    ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector)

    reality_check_scores: dict[str, float] = {}
    for engine in engines:
        from_trace = _resolver_score(trace.by_query, query_id, engine)
        standalone = score_query_for_engine(query_map[query_id], engine, ctx)
        # The reality check's direct scoring call site (the mandatory-engine
        # floor's absorber, which calls ``score_query_for_engine``) must read
        # the identical number the resolver itself used -- a hard gate (``None`` from
        # ``_resolver_score``) has no standalone equivalent to compare
        # against, but both sides agree it is gated either way.
        if from_trace is not None:
            assert standalone.score is not None, (engine, from_trace, standalone)
            assert from_trace == standalone.score, (engine, from_trace, standalone.score)
        else:
            assert standalone.score is None, (engine, from_trace, standalone.score)
        reality_check_scores[engine] = standalone.score if standalone.score is not None else -1.0

    reality_check_top = max(reality_check_scores, key=lambda e: reality_check_scores[e])
    assert reality_check_top == resolver_top, (
        f"resolver picked {resolver_top!r} but the reality check's own scorer "
        f"picked {reality_check_top!r}: {reality_check_scores}"
    )
    return resolver_top


def test_a_self_detected_anti_pattern_no_longer_causes_disagreement():
    """DynamoDB's own analysis self-detects a text_search anti-pattern for q1:
    the resolver penalizes its table confidence (80) down to 40, below
    OpenSearch's own matched-pattern score (60), and assigns q1 to
    OpenSearch. The fixture carries no triage *signal* at all, so the old,
    now-deleted ``_engine_fit_score`` -- which only read triage signals, not
    an engine's own analysis anti-patterns -- would have scored DynamoDB's
    table average unpenalized (80) against OpenSearch's identical
    matched-pattern score (60) and picked DynamoDB instead: exactly the kind
    of disagreement #490 reports.
    """
    analysis = {
        "dynamodb": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 80}],
            "workload_analysis": {
                "patterns_detected": [],
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "text_search",
                        "query_ids": ["q1"],
                        "description": "Full text search not supported",
                    }
                ],
            },
        },
        "opensearch": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 60}],
            "workload_analysis": {
                "patterns_detected": [
                    {
                        "pattern_type": "full-text-search",
                        "query_ids": ["q1"],
                        "table_ids": ["db.users"],
                    }
                ],
                "anti_patterns_detected": [],
            },
        },
    }
    top = _assert_resolver_and_reality_check_agree(analysis, ["dynamodb", "opensearch"], "q1")
    assert top == "opensearch"


def test_a_cross_engine_detected_pattern_no_longer_causes_disagreement():
    """OpenSearch's analysis positively detects a text_search pattern for q1,
    which penalizes every OTHER engine (DynamoDB) for it -- 75 - 40 = 35,
    below OpenSearch's own 70. No triage signal names this query either, so
    the old ``_engine_fit_score`` would again have missed the penalty
    entirely and picked DynamoDB on its unpenalized table average.
    """
    analysis = {
        "dynamodb": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 75}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        },
        "opensearch": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 70}],
            "workload_analysis": {
                "patterns_detected": [
                    {
                        "pattern_type": "text_search",
                        "query_ids": ["q1"],
                        "table_ids": ["db.users"],
                    }
                ],
                "anti_patterns_detected": [],
            },
        },
    }
    top = _assert_resolver_and_reality_check_agree(analysis, ["dynamodb", "opensearch"], "q1")
    assert top == "opensearch"


def test_a_capability_exemption_no_longer_causes_disagreement():
    """DynamoDB's own analysis self-detects an ``acid-transactions``
    anti-pattern for q1 -- DynamoDB has no native capability for it
    (``CAPABILITY_SIGNAL_ENGINES``), only a reduced "needs a redesign"
    penalty (``CAPABILITY_REDESIGN_PENALTIES``, #477), so its table
    confidence (90) drops by only 10, to 80 -- just short of Aurora
    PostgreSQL's own, unpenalized 85. The old ``_engine_fit_score`` had no
    equivalent redesign-penalty mechanism and no analysis-anti-pattern
    penalty at all, so it would have scored DynamoDB's full, unpenalized
    table average (90) against Aurora's 85 and picked DynamoDB instead.
    """
    analysis = {
        "dynamodb": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 90}],
            "workload_analysis": {
                "patterns_detected": [],
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "acid-transactions",
                        "query_ids": ["q1"],
                        "description": "Needs multi-row transactions",
                    }
                ],
            },
        },
        "aurora_postgresql": {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 85}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        },
    }
    top = _assert_resolver_and_reality_check_agree(
        analysis, ["dynamodb", "aurora_postgresql"], "q1"
    )
    assert top == "aurora_postgresql"


class TestAbsorptionBehaviorChanges:
    """#490 review (Important 1): deleting the reality check's own
    ``ENGINE_CAPABILITIES`` also deleted every workload-fit capability that
    had no equivalent in ``capability_registry``'s hard-constraint table --
    ``session_store``, ``leaderboard_simple``, ``metadata_config``,
    ``time_series_simple``, ``write_heavy``, ``key_value_lookup``/
    ``range_query`` and ``referential_integrity``. None of these are
    structural impossibilities (ADR-031 Decision 2 Step 2: "an engine is
    excluded only if it is structurally unable to run the query at all...
    poor fit is a score difference, not an exclusion") -- every modeled
    engine can run a session lookup or a top-N query, just less efficiently
    than a purpose-built one, which is exactly the kind of thing Decision
    2 Step 4's score (not Step 2's gate) is supposed to express. So none of
    them is added back as a hard capability; the two tests below pin the
    resulting, intentional absorption changes through the real entry points
    (``run_reality_check`` -- the shared rule that replaced the Aurora
    absorption pass, #588 -- and ``_find_best_absorber_for_query``),
    not just a per-engine score comparison, so they fail for a behavioural
    reason, not a changed signature.

    ``transactions`` (the anti-pattern penalty an engine's own analysis can
    self-detect, scored by the shared ``score_query_for_engine``) is the one old
    capability that already has a strictly better equivalent:
    ``query_scoring.ANTI_PATTERN_PENALTIES["acid-transactions"]``/
    ``CAPABILITY_SIGNAL_ENGINES`` exempts Aurora and DocumentDB outright and
    gives DynamoDB a reduced "needs a redesign" penalty (#477) instead of
    the full one -- the OLD table never listed DocumentDB as
    transaction-capable at all (a pre-existing gap, PR #479 review round 1)
    and had no DynamoDB redesign nuance either. That is a bug fix, not a
    behaviour this suite needs to pin as "new and chosen".

    A round-2 review found a second, separate claim in this class wrong:
    the fit-0 drop in ``_find_best_absorber_for_query`` (the old, deleted
    ``_can_engine_serve_query``) was called unreachable dead code outside
    ElastiCache. That is true only of its *no-signal* branch (see
    ``test_a_fit_zero_candidate_is_now_a_valid_absorber_with_no_signal``
    below) -- its *signal-gated* branch returned ``False`` outright for
    DynamoDB/DocumentDB/OpenSearch on ``complex_joins``/``referential_
    integrity``/``transactions`` and for Aurora on ``text_search``,
    regardless of score, whenever the signal demanded a capability the old
    table didn't list for that engine. The four tests after it cover each
    of those, decided individually: ``complex_joins`` is still excluded
    (the real per-query capability now does the gating); the other three
    are accepted, each for its own ADR-031 reason given in its own
    docstring, not restored.
    """

    def test_a_session_store_signal_does_not_protect_a_small_engine(self):
        """A 2-query DynamoDB engine with a ``session_store`` (or
        ``leaderboard_pattern``) signal, next to a committed Aurora MySQL that
        scores its table 70 against DynamoDB's 60.

        Neither signal maps to a hard capability, so it protects nothing: the
        shared rule removes DynamoDB (no query scores
        ``UNIQUE_DELTA_THRESHOLD`` above Aurora) and its queries move to
        Aurora. #588: this goes through ``run_reality_check``, the
        shared rule that replaced the Aurora absorption pass this test used to
        call; the pass's tiny signal-pinned case is now the justification
        floors' job, so these queries are plain, not signal-pinned.
        """
        from src.agents.referee.reality_check import run_reality_check

        for signal in ("session_store", "leaderboard_pattern"):
            revised_assignments = [
                {"query_id": f"aq{i}", "assigned_engine": "aurora_mysql"} for i in range(30)
            ] + [
                {"query_id": "s1", "assigned_engine": "dynamodb"},
                {"query_id": "s2", "assigned_engine": "dynamodb"},
            ]
            tables = {f"aq{i}": "db.main" for i in range(30)} | {
                "s1": "db.sessions",
                "s2": "db.sessions",
            }
            collector_output = {
                "queries": {
                    "query_patterns": [
                        {"query_id": qid, "tables_accessed": [table]}
                        for qid, table in tables.items()
                    ]
                }
            }
            analysis_outputs = {
                "aurora_mysql": {
                    "table_recommendations": [
                        {"table_id": "db.main", "confidence_score": 90},
                        {"table_id": "db.sessions", "confidence_score": 70},
                    ]
                },
                "dynamodb": {
                    "table_recommendations": [
                        {"table_id": "db.main", "confidence_score": 30},
                        {"table_id": "db.sessions", "confidence_score": 60},
                    ]
                },
            }
            triage = {"signals": [{"signal": signal, "query_ids": ["s1", "s2"]}]}

            result = run_reality_check(
                {"query_assignments": revised_assignments},
                triage,
                analysis_outputs,
                collector_output,
            )
            revised, consolidations = result["revised_assignments"], result["consolidations"]

            engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in revised}
            assert engine_of["s1"] == "aurora_mysql", signal
            assert engine_of["s2"] == "aurora_mysql", signal
            assert {c["to_engine"] for c in consolidations} == {"aurora_mysql"}, signal

    def test_a_leaderboard_signal_no_longer_protects_a_small_specialist(self):
        """A 3-query DynamoDB specialist next to a committed Aurora
        PostgreSQL, with DynamoDB scoring 60 on its own table and Aurora 50.

        Before #490, the ``leaderboard_pattern`` triage signal gave DynamoDB a
        +20 bonus and Aurora a -20 penalty in the reality check's own scorer,
        so DynamoDB looked 50 points ahead and was protected. There is no such
        bonus any more (``leaderboard_pattern`` is not an anti-pattern or
        capability signal), so the real gap is 10 points, below
        ``UNIQUE_DELTA_THRESHOLD``: DynamoDB has no unique value and its
        queries move to Aurora. #588: through ``run_reality_check``,
        the shared rule that replaced the Aurora absorption pass.
        """
        from src.agents.referee.reality_check import run_reality_check

        revised_assignments = [
            {"query_id": f"aq{i}", "assigned_engine": "aurora_postgresql"} for i in range(30)
        ] + [
            {"query_id": "k1", "assigned_engine": "dynamodb"},
            {"query_id": "k2", "assigned_engine": "dynamodb"},
            {"query_id": "k3", "assigned_engine": "dynamodb"},
        ]
        tables = {f"aq{i}": "db.main" for i in range(30)} | {
            k: "db.sessions" for k in ("k1", "k2", "k3")
        }
        collector_output = {
            "queries": {
                "query_patterns": [
                    {"query_id": qid, "tables_accessed": [table]} for qid, table in tables.items()
                ]
            }
        }
        analysis_outputs = {
            "aurora_postgresql": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 90},
                    {"table_id": "db.sessions", "confidence_score": 50},
                ]
            },
            "dynamodb": {
                "table_recommendations": [
                    {"table_id": "db.main", "confidence_score": 30},
                    {"table_id": "db.sessions", "confidence_score": 60},
                ]
            },
        }
        triage = {"signals": [{"signal": "leaderboard_pattern", "query_ids": ["k1", "k2", "k3"]}]}

        result = run_reality_check(
            {"query_assignments": revised_assignments}, triage, analysis_outputs, collector_output
        )
        revised, consolidations = result["revised_assignments"], result["consolidations"]

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in revised}
        assert engine_of["k1"] == "aurora_postgresql"
        assert engine_of["k2"] == "aurora_postgresql"
        assert engine_of["k3"] == "aurora_postgresql"
        assert {c["to_engine"] for c in consolidations} == {"aurora_postgresql"}

    def test_a_fit_zero_candidate_is_now_a_valid_absorber_with_no_signal(self):
        """The no-signal branch of the old, now-deleted
        ``_can_engine_serve_query``: with no capability-mapped signal at
        all, it fell through to its own basic-CRUD fallback, which every
        engine but ElastiCache passed regardless of score -- so this
        specific branch genuinely was a no-op for every candidate that
        reaches this line (ElastiCache is excluded earlier by ``may_absorb``).
        DynamoDB here has no table recommendation at all for the query's own
        table, so its base confidence -- and so its fit score with no
        anti-pattern penalty either -- is exactly 0; it is still returned
        rather than ``None``, per ADR-031 Decision 2 Step 2 ("poor fit is a
        score difference, not an exclusion").

        The *signal-gated* branch of the same old function is a different
        story -- see the four tests below, added after a round-2 review
        found this class's first version wrongly called that branch
        unreachable too.
        """
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        qa = {"query_id": "q1", "assigned_engine": "documentdb"}
        query_map = {"q1": {"query_id": "q1", "tables_accessed": ["db.unmodeled_table"]}}
        ctx = ScoringContext(analysis_outputs={"dynamodb": {"table_recommendations": []}})

        result = _find_best_absorber_for_query(
            qa,
            committed_engines={"dynamodb"},
            source_engine="documentdb",
            query_signals={},
            query_map=query_map,
            engine_queries={"dynamodb": []},
            mandatory_committed_engines=set(),
            primary_engine="dynamodb",
            ctx=ctx,
        )

        assert result is not None
        assert result["target_engine"] == "dynamodb"
        assert result["fit_score"] == 0

    def test_complex_joins_signal_still_excludes_a_fit_zero_candidate(self):
        """The old ``_can_engine_serve_query``'s signal-gated branch WAS
        reachable here (#490 round-2 review correction): a ``complex_joins``
        triage signal maps to the ``complex_joins`` capability, which
        DynamoDB and DocumentDB never had in the old ``ENGINE_CAPABILITIES``
        either -- the signal-gated branch returned ``False`` outright,
        skipping the basic-CRUD fallback, so a fit-0 DynamoDB/DocumentDB was
        dropped as an absorber. The new path excludes them the same way,
        just through the real per-query capability (``capability_registry``,
        ``complex_joins``) rather than a private table: ``score_query_for_
        engine`` gates both to ``None``, so ``_find_best_absorber_for_query``
        still returns no absorber. Nothing to accept or restore here --
        this one was never actually going to change.
        """
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        qid = "q1"
        qa = {"query_id": qid, "assigned_engine": "opensearch"}
        query_map = {
            qid: {
                "query_id": qid,
                "query_text": "SELECT a.x FROM a JOIN b ON a.id = b.id JOIN c ON b.id = c.id",
                "tables_accessed": ["a", "b", "c"],
            }
        }
        ctx = ScoringContext(query_capabilities={qid: ["complex_joins"]})

        for target in ("dynamodb", "documentdb"):
            result = _find_best_absorber_for_query(
                qa,
                committed_engines={target},
                source_engine="opensearch",
                query_signals={qid: ["complex_joins"]},
                query_map=query_map,
                engine_queries={target: []},
                mandatory_committed_engines=set(),
                primary_engine=target,
                ctx=ctx,
            )
            assert result is None, (target, result)

    def test_referential_integrity_signal_no_longer_excludes_a_fit_zero_candidate(self):
        """#490 round-2 review: the old signal-gated branch also returned
        ``False`` for a ``referential_integrity`` signal against DynamoDB/
        DocumentDB (neither had that capability in the old table), dropping
        a fit-0 candidate regardless of score. There is no equivalent hard
        capability in ``capability_registry`` at all -- nothing in
        ``detect_required_capabilities`` ever emits a ``referential_
        integrity`` requirement -- so ``score_query_for_engine`` never gates
        on it, and this is now a valid (if weak) candidate.

        Accepted, not restored: whether an engine enforces foreign-key
        constraints is a data-modeling property, not something that blocks
        the query itself from running (ADR-031 Decision 2 Step 2's
        structural-impossibility test) -- the old signal-gated exclusion
        conflated the two.
        """
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        qid = "q1"
        qa = {"query_id": qid, "assigned_engine": "opensearch"}
        query_map = {qid: {"query_id": qid, "tables_accessed": ["a"]}}
        ctx = ScoringContext()

        for target in ("dynamodb", "documentdb"):
            result = _find_best_absorber_for_query(
                qa,
                committed_engines={target},
                source_engine="opensearch",
                query_signals={qid: ["referential_integrity"]},
                query_map=query_map,
                engine_queries={target: []},
                mandatory_committed_engines=set(),
                primary_engine=target,
                ctx=ctx,
            )
            assert result is not None and result["target_engine"] == target, (target, result)
            assert result["fit_score"] == 0

    def test_transactions_signal_no_longer_excludes_a_fit_zero_candidate(self):
        """#490 round-2 review: same mechanism as the ``referential_
        integrity`` case above, for a ``transactions`` signal against
        DynamoDB/DocumentDB on an ordinary (non-utility-statement) query.
        ``detect_required_capabilities`` never emits a hard capability for
        this signal alone either, so ``score_query_for_engine`` does not
        gate on it.

        Accepted, not restored -- and for DynamoDB specifically this is a
        correctness fix, not just a tolerated side effect: ADR-031 Decision
        3 says DynamoDB's native ``TransactWriteItems``/``TransactGetItems``
        support means a transaction is "not... a capability DynamoDB lacks"
        at all -- it is scored by shape (``query_scoring.
        CAPABILITY_REDESIGN_PENALTIES``), never hard-excluded. The old
        signal-gated branch hard-excluding DynamoDB for this signal was
        itself an ADR-031 violation.
        """
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        qid = "q1"
        qa = {"query_id": qid, "assigned_engine": "opensearch"}
        query_map = {
            qid: {
                "query_id": qid,
                "query_text": "UPDATE a SET x = 1 WHERE id = 1",
                "tables_accessed": ["a"],
            }
        }
        ctx = ScoringContext()

        for target in ("dynamodb", "documentdb"):
            result = _find_best_absorber_for_query(
                qa,
                committed_engines={target},
                source_engine="opensearch",
                query_signals={qid: ["transactions"]},
                query_map=query_map,
                engine_queries={target: []},
                mandatory_committed_engines=set(),
                primary_engine=target,
                ctx=ctx,
            )
            assert result is not None and result["target_engine"] == target, (target, result)
            assert result["fit_score"] == 0

    def test_text_search_signal_no_longer_excludes_aurora_at_fit_zero(self):
        """#490 round-2 review: the old signal-gated branch mapped
        ``text_search`` to the exact capability name ``"text_search"``,
        which the old table only ever gave OpenSearch -- Aurora's own entry
        was ``"text_search_basic"``, a different string, so an exact-string
        comparison hard-excluded Aurora for a fit-0 candidate even though
        Aurora genuinely has basic full-text support (tsvector/FULLTEXT).
        The new registry names that support ``inverted_index`` on both
        Aurora engines (and, since #485, DocumentDB), so ``score_query_for_
        engine`` no longer gates them out.

        Accepted, not restored: a structural capability Aurora actually has
        is not a reason to exclude it (ADR-031 Decision 2 Step 2) -- Aurora
        scoring worse than OpenSearch on a deep search query is the score
        dimension's job, not the capability gate's.
        """
        from src.agents.referee.reality_check import _find_best_absorber_for_query

        qid = "q1"
        qa = {"query_id": qid, "assigned_engine": "opensearch"}
        query_map = {
            qid: {
                "query_id": qid,
                "query_text": "SELECT * FROM a WHERE title LIKE '%x%'",
                "tables_accessed": ["a"],
            }
        }
        ctx = ScoringContext(query_capabilities={qid: ["inverted_index"]})

        for target in ("aurora_mysql", "aurora_postgresql"):
            result = _find_best_absorber_for_query(
                qa,
                committed_engines={target},
                source_engine="opensearch",
                query_signals={qid: ["text_search"]},
                query_map=query_map,
                engine_queries={target: []},
                mandatory_committed_engines=set(),
                primary_engine=target,
                ctx=ctx,
            )
            assert result is not None and result["target_engine"] == target, (target, result)
            assert result["fit_score"] == 0


# ---------------------------------------------------------------------------
# Structural guard: there is exactly one scorer, in query_scoring.py.
# ---------------------------------------------------------------------------

# The reality check's own, now-deleted fit-scoring names (#490). None of
# these may be *defined* anywhere under src/ except query_scoring.py --
# a definition elsewhere would mean a second, separately drifting fit
# function or capability table has come back. ``ENGINE_CAPABILITIES`` is
# scoped to the ``reality_check*.py`` modules rather than banned repo-wide:
# ``capability_registry.py`` legitimately owns a table of the same name
# (hard architectural constraints, a different fact from workload-fit
# scoring), and that one must stay.
_BANNED_DEFINITIONS = [
    re.compile(r"^def _engine_fit_score\(", re.MULTILINE),
    re.compile(r"^def _can_engine_serve_query\(", re.MULTILINE),
    re.compile(r"^BASIC_CRUD_SCORE\s*=", re.MULTILINE),
    re.compile(r"^SIGNAL_MATCH_BONUS\s*=", re.MULTILINE),
]
_REALITY_CHECK_ONLY_BANNED = re.compile(r"^ENGINE_CAPABILITIES\s*:", re.MULTILINE)


def test_nothing_outside_query_scoring_defines_a_fit_scorer():
    """Guards against reintroducing a second scorer (#490/#549): the old,
    private names this issue deleted must never be *defined* again outside
    ``query_scoring.py``, the one module every caller shares.
    """
    offenders: list[str] = []
    for path in sorted(_REPO_SRC.rglob("*.py")):
        if path.name == "query_scoring.py":
            continue
        text = path.read_text()
        for pattern in _BANNED_DEFINITIONS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(_REPO_SRC.parent)}: {pattern.pattern}")
        if path.name.startswith("reality_check") and _REALITY_CHECK_ONLY_BANNED.search(text):
            offenders.append(
                f"{path.relative_to(_REPO_SRC.parent)}: {_REALITY_CHECK_ONLY_BANNED.pattern}"
            )
    assert offenders == [], (
        "a deleted private fit-scoring name has reappeared outside "
        f"query_scoring.py: {offenders}"
    )


def test_query_scoring_is_the_only_module_with_the_fit_pipeline():
    """``score_query_for_engine``/``compute_query_confidence``/
    ``build_anti_pattern_map`` are each defined exactly once, in
    ``query_scoring.py`` -- a second ``def`` of any of them anywhere else
    under ``src/`` would mean a parallel implementation exists.
    """
    names = ("score_query_for_engine", "compute_query_confidence", "build_anti_pattern_map")
    for name in names:
        pattern = re.compile(rf"^def {re.escape(name)}\(", re.MULTILINE)
        defining_files = [
            str(path.relative_to(_REPO_SRC.parent))
            for path in sorted(_REPO_SRC.rglob("*.py"))
            if pattern.search(path.read_text())
        ]
        assert defining_files == ["src/agents/referee/query_scoring.py"], (
            name,
            defining_files,
        )


# #490 review (Important 3): a name-based guard only catches a scorer that
# reuses the exact old names -- a renamed replacement (a new module building
# its own anti-pattern map or capability-penalty table under different
# names) would sail through both tests above undetected. This guard instead
# follows imports, by AST, not text: ``query_scoring.py``'s own *internal*
# building blocks (the anti-pattern penalty table, the capability-signal
# scoping, the map builder) are not meant to be reused directly by another
# module to assemble a second scoring pipeline -- only the composed, public
# API (``score_query_for_engine``, ``ScoringContext``, ``EngineScore``,
# and the standalone ``compute_query_confidence`` the resolver's own
# ``resolve()`` still calls for its Step 4b/7c diagnostics, #549) is a
# legitimate import. If a future module imports one of the internals below,
# that is itself the first sign a parallel pipeline is being built, whatever
# it ends up being named.
_QUERY_SCORING_INTERNALS = frozenset(
    {
        "ANTI_PATTERN_PENALTIES",
        "CAPABILITY_SIGNAL_ENGINES",
        "CAPABILITY_REDESIGN_PENALTIES",
        "CAPABILITY_REDESIGN_NOTES",
        "CAPABILITY_EXEMPT_NOTES",
        "_capability_scoped_penalty",
        "build_anti_pattern_map",
    }
)
_QUERY_SCORING_MODULE = "src.agents.referee.query_scoring"


def test_only_query_scoring_itself_imports_its_anti_pattern_internals():
    """No module other than ``query_scoring.py`` imports its anti-pattern-map
    or capability-penalty building blocks -- see ``_QUERY_SCORING_INTERNALS``'s
    own comment for why this, not a name-based check, is the guard that
    survives a renamed second scorer.
    """
    offenders: list[str] = []
    for path in sorted(_REPO_SRC.rglob("*.py")):
        if path.relative_to(_REPO_SRC.parent) == Path("src/agents/referee/query_scoring.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != _QUERY_SCORING_MODULE:
                continue
            imported = {alias.name for alias in node.names}
            hit = imported & _QUERY_SCORING_INTERNALS
            if hit:
                offenders.append(f"{path.relative_to(_REPO_SRC.parent)}: imports {sorted(hit)}")
    assert offenders == [], (
        "a module outside query_scoring.py imports its internal anti-pattern/"
        f"capability building blocks directly: {offenders}"
    )
