"""``break_tie`` -- ADR-031 Decision 2's shared engine tie-break (#503).

Exercises the three rules directly, so the resolver's ``break_owner_tie``
and the reality check's ``_pick_winner``/absorber callers cannot drift from
each other or from ADR-031 without this test catching it first.
"""

from __future__ import annotations

import hashlib

from src.agents.referee.tie_break import break_tie, pick_unresolved_tie_placeholder, settled_counts


class TestSourceCompatibleWinsAnAuroraTie:
    """ADR-031 Decision 2: "On an exact tie, the source-compatible
    relational engine wins; no other engine preference exists."
    """

    def test_source_compatible_engine_wins_even_with_no_current_engine(self):
        winner, tie_kind = break_tie(["dynamodb", "aurora_mysql"], set(), "aurora_mysql")
        assert (winner, tie_kind) == ("aurora_mysql", "source_compatible")

    def test_source_compatible_engine_wins_over_the_current_engine_too(self):
        # Rule 1 (source-compatible) outranks rule 2 (stability): a query
        # already sitting on documentdb still moves to the source-compatible
        # engine on a tie, not the other way around.
        winner, tie_kind = break_tie(
            ["documentdb", "aurora_postgresql"], {"documentdb"}, "aurora_postgresql"
        )
        assert (winner, tie_kind) == ("aurora_postgresql", "source_compatible")

    def test_source_compatible_engine_wins_regardless_of_settled_counts(self):
        winner, tie_kind = break_tie(
            ["dynamodb", "aurora_mysql"],
            set(),
            "aurora_mysql",
            settled_counts={"dynamodb": 100, "aurora_mysql": 0},
            query_id="q1",
        )
        assert (winner, tie_kind) == ("aurora_mysql", "source_compatible")

    def test_order_of_the_tied_list_never_matters(self):
        forward, _ = break_tie(["dynamodb", "aurora_mysql"], set(), "aurora_mysql")
        backward, _ = break_tie(["aurora_mysql", "dynamodb"], set(), "aurora_mysql")
        assert forward == backward == "aurora_mysql"


class TestStabilityWinsANonAuroraTieWithOneCurrentEngine:
    """Rule 2: when no tied engine is source-compatible, an engine the
    query (or a co-dependency group member) is already on wins -- a tie is
    not "strictly higher," so it is never an opportunity for a different
    engine to pick up a query it did not already have.
    """

    def test_the_current_engine_wins_over_a_stranger(self):
        winner, tie_kind = break_tie(["dynamodb", "documentdb"], {"documentdb"}, None)
        assert (winner, tie_kind) == ("documentdb", "stability")

    def test_the_current_engine_wins_even_when_it_sorts_last_by_name(self):
        # If stability degenerated into "sort by name," this would pick
        # documentdb; it must pick dynamodb instead because that is where
        # the query already is.
        winner, tie_kind = break_tie(["documentdb", "dynamodb"], {"dynamodb"}, None)
        assert (winner, tie_kind) == ("dynamodb", "stability")

    def test_a_co_dependency_groups_single_current_engine_also_counts(self):
        # ``current_engines`` may hold more than one engine for a
        # co-dependency group (each member's own current engine), but only
        # ONE of them may be among the tied top-scorers for this to count
        # as stability -- see TestTwoTiedCurrentEnginesAreUnresolved below
        # for what happens when two different members sit on two different
        # tied engines.
        winner, tie_kind = break_tie(
            ["dynamodb", "documentdb", "opensearch"], {"dynamodb", "elasticache"}, None
        )
        assert (winner, tie_kind) == ("dynamodb", "stability")


class TestTwoTiedCurrentEnginesAreUnresolved:
    """A co-dependency group's members can sit on two different engines.

    If both of those engines are still tied for the top score, there is no
    single "the" current engine to be stable about -- #503 review item 3:
    this must not silently fall back to picking whichever of the two sorts
    first by name under the guise of "stability."
    """

    def test_two_tied_current_engines_is_unresolved_not_a_name_pick(self):
        winner, tie_kind = break_tie(
            ["documentdb", "dynamodb"], {"documentdb", "dynamodb"}, None, query_id="g1"
        )
        assert tie_kind == "tie_unresolved"
        assert winner in {"documentdb", "dynamodb"}

    def test_two_tied_current_engines_outranks_a_third_tied_stranger(self):
        # Even with a third tied engine that is NOT a current engine, two
        # different current engines among the tied set is still unresolved
        # -- it is not "pick the one current engine that happens to also be
        # tied," because there are two, not one.
        winner, tie_kind = break_tie(
            ["documentdb", "dynamodb", "opensearch"],
            {"documentdb", "dynamodb"},
            None,
            query_id="g1",
        )
        assert tie_kind == "tie_unresolved"
        assert winner in {"documentdb", "dynamodb", "opensearch"}


class TestNonAuroraTieWithNoCurrentEngineIsReportedUnresolved:
    """Rule 3: once source-compatible and stability are both ruled out,
    ADR-031 has no rule left for this tie. Table traffic and engine name
    are never consulted directly (#503) -- a deterministic engine is still
    returned (so repeated runs agree with themselves), but it is labelled
    ``tie_unresolved`` rather than passed off as a real rule, and the
    engine returned comes from the maintainer-decided placeholder in
    ``pick_unresolved_tie_placeholder``, not from sorting names.
    """

    def test_a_tie_with_no_current_engine_and_no_source_compatible_engine_is_unresolved(self):
        winner, tie_kind = break_tie(["dynamodb", "documentdb"], set(), None, query_id="q1")
        assert tie_kind == "tie_unresolved"
        assert winner in {"dynamodb", "documentdb"}

    def test_the_unresolved_pick_is_deterministic_regardless_of_input_order(self):
        forward, forward_kind = break_tie(["dynamodb", "documentdb"], set(), None, query_id="q1")
        backward, backward_kind = break_tie(["documentdb", "dynamodb"], set(), None, query_id="q1")
        assert forward == backward
        assert forward_kind == backward_kind == "tie_unresolved"

    def test_a_tie_with_a_current_engine_that_is_not_one_of_the_tied_engines_is_unresolved(self):
        # The current engine exists, but it already lost outright (it is
        # not among the tied top-scorers) -- stability has nothing to grab
        # onto, so this is still an unresolved tie, not a stability win.
        winner, tie_kind = break_tie(
            ["dynamodb", "documentdb"], {"opensearch"}, None, query_id="q1"
        )
        assert tie_kind == "tie_unresolved"
        assert winner in {"dynamodb", "documentdb"}


class TestPickUnresolvedTiePlaceholder:
    """The isolated placeholder function, tested directly (maintainer
    decision, 2026-10-12, ADR-031 Decision 12, #503)."""

    def test_the_engine_with_more_settled_workload_wins(self):
        winner, by_workload = pick_unresolved_tie_placeholder(
            ["documentdb", "dynamodb"],
            {"documentdb": 5, "dynamodb": 2},
            "q1",
        )
        assert winner == "documentdb"
        assert by_workload is True

    def test_settled_workload_counts_ignore_which_engine_sorts_first_by_name(self):
        # dynamodb has more settled workload despite sorting first by name;
        # the result must follow the count, not the name.
        winner, by_workload = pick_unresolved_tie_placeholder(
            ["documentdb", "dynamodb"],
            {"documentdb": 1, "dynamodb": 9},
            "q1",
        )
        assert winner == "dynamodb"
        assert by_workload is True

    def test_equal_settled_counts_fall_back_to_a_stable_hash_of_query_and_engine(self):
        tied = ["documentdb", "dynamodb"]
        counts = {"documentdb": 3, "dynamodb": 3}
        winner, by_workload = pick_unresolved_tie_placeholder(tied, counts, "q1")
        assert by_workload is False
        assert winner in tied

        expected = min(
            tied,
            key=lambda e: hashlib.sha256(f"q1:{e}".encode()).hexdigest(),
        )
        assert winner == expected

    def test_no_settled_workload_at_all_also_falls_back_to_the_hash(self):
        winner, by_workload = pick_unresolved_tie_placeholder(["documentdb", "dynamodb"], {}, "q1")
        assert by_workload is False
        assert winner in {"documentdb", "dynamodb"}

    def test_the_hash_fallback_depends_on_query_id_not_just_engine_names(self):
        # Different query ids with the exact same tied/equal-count engines
        # can land on different engines -- the hash is not a disguised,
        # fixed name preference.
        tied = ["documentdb", "dynamodb"]
        counts = {"documentdb": 0, "dynamodb": 0}
        winners = {pick_unresolved_tie_placeholder(tied, counts, f"q{i}")[0] for i in range(20)}
        assert winners == {"documentdb", "dynamodb"}

    def test_the_pick_is_independent_of_the_tied_lists_input_order(self):
        counts = {"documentdb": 3, "dynamodb": 3}
        forward, _ = pick_unresolved_tie_placeholder(["documentdb", "dynamodb"], counts, "q1")
        backward, _ = pick_unresolved_tie_placeholder(["dynamodb", "documentdb"], counts, "q1")
        assert forward == backward

    def test_the_pick_is_deterministic_across_repeated_calls(self):
        counts = {"documentdb": 0, "dynamodb": 0}
        first, _ = pick_unresolved_tie_placeholder(["documentdb", "dynamodb"], counts, "q1")
        second, _ = pick_unresolved_tie_placeholder(["documentdb", "dynamodb"], counts, "q1")
        assert first == second


class TestSettledCounts:
    """#550/#549: the settled-workload-share definition, from a finished list
    of query assignments rather than a live resolver run -- counts a query
    toward its ``assigned_engine`` only when its own ``tied_engines`` is
    empty.
    """

    def test_counts_only_untied_queries(self):
        qas = [
            {"query_id": "q1", "assigned_engine": "dynamodb", "tied_engines": []},
            {"query_id": "q2", "assigned_engine": "dynamodb", "tied_engines": []},
            {
                "query_id": "q3",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
        ]
        assert settled_counts(qas) == {"dynamodb": 2}

    def test_missing_tied_engines_key_counts_as_untied(self):
        # A legacy or hand-built dict with no ``tied_engines`` key at all --
        # never a tie, same as an explicit empty list.
        qas = [{"query_id": "q1", "assigned_engine": "dynamodb"}]
        assert settled_counts(qas) == {"dynamodb": 1}

    def test_no_assigned_engine_is_not_counted(self):
        qas = [{"query_id": "q1", "assigned_engine": "", "tied_engines": []}]
        assert settled_counts(qas) == {}

    def test_empty_list_returns_an_empty_dict(self):
        assert settled_counts([]) == {}

    def test_accepts_query_assignment_objects_not_only_dicts(self):
        from src.contracts.assignment_models import QueryAssignment

        qas = [
            QueryAssignment(
                query_id="q1",
                assigned_engine="aurora_mysql",
                confidence=80,
                source_tables=[],
                assignment_reason="test",
            ),
            QueryAssignment(
                query_id="q2",
                assigned_engine="aurora_postgresql",
                confidence=80,
                source_tables=[],
                assignment_reason="test",
                tied_engines=["aurora_mysql", "aurora_postgresql"],
            ),
        ]
        assert settled_counts(qas) == {"aurora_mysql": 1}
